"""显式执行 V4 真实验收；需要模型、Embedding 凭据，不使用模拟数据。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.knowledge_base import KnowledgeBaseService, ChromaVectorStore
from app.parsers import ParserRegistry
from app.providers.embeddings import OpenAICompatibleEmbeddings
from app.providers.model import OpenAICompatibleModel
from app.providers.tavily import TavilyProvider
from app.papers.service import PaperService
from app.services.sessions import SessionStore
from app.services.tasks import TERMINAL
import httpx

PAPERS = {
    '1706.03762': ('Attention Is All You Need', ['Explain scaled dot-product attention and the square root scaling factor.',
                                              'What do 28.4 and 41.8 BLEU refer to in the experiments?',
                                              'How does positional encoding represent sequence order?']),
    '2106.09685': ('LoRA', ['How do the low-rank matrices change the frozen weight matrix?',
                            'What are the trainable parameter and memory comparisons to full fine tuning?',
                            'What does the paper conclude about rank deficiency?']),
    '2005.11401': ('Retrieval-Augmented Generation', ['Explain the difference between RAG-Sequence and RAG-Token.',
                                                    'How are the retriever and generator trained?',
                                                    'Which datasets and metrics are used in the main experiments?']),
}


async def run(args):
    """在独立目录使用真实适配器执行导读、追问、笔记和 PPT 全链路。"""
    logging.disable(logging.CRITICAL)  # 不在验收控制台输出供应商请求或密钥细节。
    settings = Settings.from_env()
    out = ROOT / '.scratch' / 'study-agent-v4' / ('real-' + str(int(time.time())))
    out.mkdir(parents=True, exist_ok=False)
    report = {'runtime_directory': str(out), 'model': settings.model_name, 'cases': [], 'web_requested': args.with_web,
              'prompt_version': f'cards-v1-{settings.paper_batch_chars}',
              'budgets': {'task_seconds': settings.paper_task_timeout_seconds,
                          'model_seconds': settings.model_timeout_seconds,
                          'model_attempts': settings.paper_max_model_calls}}
    if not settings.model_ready or not settings.knowledge_base_ready:
        report['blocked'] = '模型或 Embedding 配置缺失。'
        (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('BLOCKED: missing credentials', flush=True)
        return
    kb = KnowledgeBaseService(out/'knowledge.db', out/'uploads', ParserRegistry.default(),
                              OpenAICompatibleEmbeddings(settings), ChromaVectorStore(out/'chroma'),
                              processing_timeout=settings.knowledge_timeout_seconds)
    sessions = SessionStore(out/'sessions.db')
    web = TavilyProvider(settings.tavily_api_key, settings.tool_timeout_seconds) if settings.search_ready else None
    service = PaperService(settings, out, lambda: sessions, lambda: kb, OpenAICompatibleModel(settings), web)
    kb_id = str(kb.create_knowledge_base('V4 real acceptance')['id'])

    async def wait(task_id):
        """有界等待并输出阶段变化，控制台不输出完整模型内容。"""
        after = 0
        async with asyncio.timeout(settings.paper_task_timeout_seconds + 20):
            while True:
                for event in service.store.events(task_id, after):
                    after = event['seq']
                    print(f"TASK {task_id[:8]} {event['stage']} {event['state']}", flush=True)
                task = service.store.task(task_id)
                if task['status'] in TERMINAL:
                    return task
                await asyncio.sleep(1)

    try:
        for paper_id in args.papers:
            case = {'paper_id': paper_id, 'source': f'https://arxiv.org/pdf/{paper_id}', 'title': PAPERS[paper_id][0]}
            report['cases'].append(case)
            print(f'PAPER {paper_id} downloading', flush=True)
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    for attempt in range(2):
                        try:
                            response = await client.get(case['source'])
                            response.raise_for_status()
                            data = response.content
                            if not data.startswith(b'%PDF') or len(data) > settings.max_upload_bytes:
                                raise ValueError('下载不是有效且大小合规的 PDF。')
                            break
                        except (httpx.HTTPError, ValueError):
                            if attempt:
                                raise
                case['sha256'] = hashlib.sha256(data).hexdigest()
                (out/(paper_id+'.pdf')).write_bytes(data)
                print(f'PAPER {paper_id} ingesting', flush=True)
                file = await kb.ingest_file(kb_id, paper_id+'.pdf', 'application/pdf', data)
                case['ingestion'] = {'status': file['status'], 'chunk_count': file['chunk_count']}
                if file['status'] != 'ready':
                    raise RuntimeError('文件入库失败。')
                study = service.create_study(kb_id, str(file['id']))
                session_id = sessions.create()
                state = service.change_mode(session_id, True, study['id'], 0)
                task = await wait(state['task_id'])
                case['overview_status'] = task['status']
                case['overview_task'] = task['id']
                current = service.store.study(study['id'])
                if current['active_snapshot_id']:
                    snapshot = service.store.snapshot(current['active_snapshot_id'], study['id'])
                    case['quality'] = snapshot['quality']
                    case['total_blocks'] = len(snapshot['blocks'])
                calls = service.store.many('SELECT payload FROM task_events WHERE task_id=?', (task['id'],))
                case['model_attempts'] = max((json.loads(e['payload']).get('data', {}).get('attempt', 0) for e in calls), default=0)
                if not task['result_id']:
                    case['blocked'] = task['error_code']
                    continue
                artifact = service.artifact(task['result_id'])
                case['coverage'] = artifact['coverage']
                (out/(paper_id+'-overview.json')).write_text(json.dumps(artifact, ensure_ascii=False, indent=2),encoding='utf-8')
                (out/(paper_id+'-overview.md')).write_text(artifact['markdown'],encoding='utf-8')
                case['questions'] = []
                for index, question in enumerate(PAPERS[paper_id][1]):
                    t=service.generate(study['id'],session_id,state['revision'],'explanation',
                                       {'question':question,'allow_web':args.with_web and index==2})
                    t=await wait(t['id'])
                    entry={'question':question,'status':t['status']}
                    case['questions'].append(entry)
                    if t['result_id']:
                        a=service.artifact(t['result_id'])
                        (out/f'{paper_id}-question-{index+1}.json').write_text(json.dumps(a,ensure_ascii=False,indent=2),encoding='utf-8')
                        (out/f'{paper_id}-question-{index+1}.md').write_text(a['markdown'],encoding='utf-8')
                        entry['web_sources']=sum(e['source_type']=='web' for e in a['evidence'])
                note=service.notes.create(study['id'],case['title'],[artifact['id']])
                (out/(paper_id+'-note.md')).write_text(service.notes.export(note['id']),encoding='utf-8')
                t=service.generate(study['id'],session_id,state['revision'],'presentation_outline',
                                   {'note_id':note['id'],'note_revision':1,'page_count':8})
                t=await wait(t['id'])
                case['outline_status']=t['status']
                if t['result_id']:
                    outline=service.store.artifact(t['result_id'])
                    t=service.generate(study['id'],session_id,state['revision'],'presentation',
                                       {'outline_id':outline['id'],'outline_revision':outline['revision']})
                    t=await wait(t['id'])
                    case['ppt_status']=t['status']
                    if t['result_id']:
                        case['ppt_path']=str(service.download(t['result_id']))
            except Exception as exc:
                case['blocked'] = type(exc).__name__
                print(f'PAPER {paper_id} blocked: {type(exc).__name__}',flush=True)
            finally:
                case['elapsed_seconds']=round(time.monotonic()-started,2)
                (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        await service.close()
        sessions.close()
        kb.close()
    print('REPORT '+str(out/'report.json'),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--papers',nargs='+',choices=list(PAPERS),default=list(PAPERS))
    parser.add_argument('--with-web',action='store_true',help='明确启用一次真实网页补充验证。')
    asyncio.run(run(parser.parse_args()))
