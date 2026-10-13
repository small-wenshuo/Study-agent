"""V4 离线验收：模拟供应商，真实 PDF/SQLite/PPTX 与 HTTP 路由。"""
from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from pptx import Presentation

from app.config import Settings
from app.papers.reader import read_pdf
from app.papers.service import PaperService
from app.services.sessions import SessionStore
from app.services.tasks import TaskConflict, TaskCapacityError, TERMINAL
from tests.auth_support import install_auth, login_admin


def pdf_bytes(texts=('Problem: efficient learning. Method: staged reading. Result: accuracy is 91 percent.',)):
    """生成真实可提取文本的测试 PDF；数据明确为测试占位。"""
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(612, 792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        stream.set_data(f'BT /F1 12 Tf 50 700 Td ({escaped}) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class TestKnowledge:
    """文件目录模拟，只用于测试；解析流程使用真实 PDF。"""

    def __init__(self):
        """测试文件隔离。"""
        self.files = {'file': pdf_bytes(), 'other': pdf_bytes(('Other paper with unrelated claims.',))}

    def file_info(self, kb_id, file_id):
        """校验测试资源归属。"""
        if kb_id != 'kb' or file_id not in self.files:
            raise KeyError()
        return {'id': file_id, 'knowledge_base_id': kb_id, 'file_type': 'pdf', 'filename': file_id + '.pdf', 'status': 'ready'}

    def read_file(self, kb_id, file_id):
        """返回测试 PDF 字节。"""
        return self.file_info(kb_id, file_id), self.files[file_id]


class TestModel:
    """按论文 JSON 协议返回固定测试输出，不能代表真实模型正确性。"""

    def __init__(self):
        """记录调用次数及可控故障。"""
        self.calls = 0
        self.delay = 0
        self.bad_quote = False
        self.bad_citation = False
        self.entered = asyncio.Event()

    async def ainvoke(self, messages, tools_enabled):
        """验证工具禁用，模拟不同结构化学习节点。"""
        self.calls += 1
        self.entered.set()
        await asyncio.sleep(self.delay)
        payload = json.loads(messages[1].content)
        purpose = messages[0].content
        if '抽取本批次' in purpose:
            b = payload['blocks'][0]
            quote = 'invented quote not in original' if self.bad_quote else b['text'][:80]
            value = {'cards': [{'topic': '方法', 'claim': '测试论文使用分阶段阅读。', 'block_id': b['id'], 'quote': quote}]}
        elif 'note_blocks' in payload:
            ids = list({e['id'] for b in payload['note_blocks'] for e in b.get('sources', [])})
            value = {'slides': [{'title': '测试学习大纲', 'bullets': ['先整体理解，再按需展开。'], 'notes': '辅助解释，仅用于测试。', 'evidence_ids': ids[:1]}]}
        elif '选择最重要' in purpose:
            value = {'ids': [e['id'] for e in payload['evidence'][:4]]}
        else:
            eid = 'unknown' if self.bad_citation else payload['evidence'][0]['id']
            headings = ['研究问题', '核心方法', '主要结果', '局限', '阅读路线'] if '五个标题' in purpose else ['当前解释']
            value = {'blocks': [{'type': 'paper_fact', 'heading': h, 'text': '这是模拟模型的测试结论。', 'evidence_ids': [eid]} for h in headings]}
            if 'questions 数组' in purpose:
                value['questions'] = [{'question': '方法是什么？', 'hint': '回看方法段落。', 'answer': '分阶段阅读。', 'evidence_ids': [eid]}]
        return AIMessage(content=json.dumps(value, ensure_ascii=False))


class TestWeb:
    """模拟搜索与正文读取，不访问网络。"""

    def __init__(self):
        """记录搜索调用以验证独立授权。"""
        self.calls = 0

    async def search(self, query, max_results):
        """返回搜索结果测试占位。"""
        self.calls += 1
        return [{'url': 'https://example.org/paper', 'title': 'Test source', 'snippet': 'not evidence'}]

    async def extract(self, urls):
        """仅对本次搜索登记的 URL 返回正文。"""
        return [{'url': url, 'content': 'Actual extracted test page content.'} for url in urls]


class PaperTests(unittest.IsolatedAsyncioTestCase):
    """覆盖重要状态转换与来源身份，使用真实本地存储。"""

    async def asyncSetUp(self):
        """每个测试独立文件和数据库。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = SessionStore(self.root / 'sessions.db')
        self.session = self.sessions.create()
        self.kb, self.model, self.web = TestKnowledge(), TestModel(), TestWeb()
        self.settings = replace(Settings.from_env(), paper_task_timeout_seconds=10, model_timeout_seconds=2)
        self.service = PaperService(self.settings, self.root, lambda: self.sessions, lambda: self.kb, self.model, self.web)
        self.study = self.service.create_study('kb', 'file')

    async def asyncTearDown(self):
        """收尾任务与句柄，避免后台任务跨测试污染。"""
        await self.service.close()
        self.sessions.close()
        self.temp.cleanup()

    async def done(self, key):
        """有界等待测试任务终止。"""
        async with asyncio.timeout(5):
            while self.service.store.task(key)['status'] not in TERMINAL:
                await asyncio.sleep(.01)
        return self.service.store.task(key)

    async def enable(self):
        """显式开启并等待导读。"""
        state = self.service.change_mode(self.session, True, self.study['id'], 0)
        task = await self.done(state['task_id'])
        self.assertEqual(task['status'], 'complete', task)
        return state, self.service.store.artifact(task['result_id'])

    async def test_default_off_and_enable_idempotence(self):
        """默认关闭和元数据创建零调用，开启后只自动生成一次导读。"""
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.sessions.detail(self.session)['paper_mode'], 0)
        with self.assertRaises(TaskConflict):
            self.service.generate(self.study['id'], self.session, 0, 'overview', {})
        state, artifact = await self.enable()
        self.assertEqual(len(artifact['blocks']), 5)
        self.assertEqual(artifact['coverage']['processed_pages'], [1])
        calls = self.model.calls
        repeated = self.service.change_mode(self.session, True, self.study['id'], state['revision'])
        self.assertIsNone(repeated['task_id'])
        self.assertEqual(repeated['artifact_id'], artifact['id'])
        self.assertEqual(calls, self.model.calls)
        self.service.change_mode(self.session, False, None, state['revision'])
        state = self.service.change_mode(self.session, True, self.study['id'], state['revision'] + 1)
        self.assertEqual(state['artifact_id'], artifact['id'])
        self.assertIsNone(state['task_id'])
        self.assertEqual(calls, self.model.calls)

    async def test_close_cancels_model_and_no_artifact_write(self):
        """关闭模式取消已经进入模型的任务，不再发布成功结果。"""
        self.model.delay = .5
        state = self.service.change_mode(self.session, True, self.study['id'], 0)
        await asyncio.wait_for(self.model.entered.wait(), 2)
        self.service.change_mode(self.session, False, None, state['revision'])
        task = await self.done(state['task_id'])
        await asyncio.sleep(.02)
        self.assertEqual(task['status'], 'cancelled')
        self.assertIsNone(task['result_id'])
        self.assertEqual(self.service.study(self.study['id'])['artifacts'], [])
        self.assertEqual(self.model.calls, 1)

    async def test_explanation_web_permission_and_target_ownership(self):
        """论文追问只读当前快照，联网许可独立校验。"""
        state, artifact = await self.enable()
        task = self.service.generate(self.study['id'], self.session, state['revision'], 'explanation',
                                     {'question': '解释方法', 'target_block_id': artifact['id'] + ':b2', 'allow_web': False})
        result = await self.done(task['id'])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.web.calls, 0)
        task = self.service.generate(self.study['id'], self.session, state['revision'], 'explanation',
                                     {'question': '补充学习资料', 'allow_web': True})
        result = await self.done(task['id'])
        sources = self.service.store.artifact(result['result_id'])['evidence']
        self.assertEqual(self.web.calls, 1)
        self.assertTrue(any(s['source_type'] == 'web' for s in sources))
        other = self.service.create_study('kb', 'other')
        other_artifact = self.service.store.save_artifact(other['id'], 'other-snapshot', 'overview', None,
                                                        {'blocks': [{'id':'b1','evidence_ids':[]}], 'evidence': []})
        with self.assertRaises(ValueError):
            self.service.generate(self.study['id'], self.session, state['revision'], 'explanation',
                                  {'question': '解释', 'target_block_id': other_artifact['id'] + ':b1'})

    async def test_bad_quotes_and_bad_citations_are_not_published(self):
        """虚构摘录、未知引用经过有限修复仍失败，不能标为成功。"""
        self.model.bad_quote = True
        state = self.service.change_mode(self.session, True, self.study['id'], 0)
        task = await self.done(state['task_id'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(self.model.calls, 2)
        self.model.bad_quote = False
        self.model.bad_citation = True
        task = self.service.retry(task['id'], self.session, state['revision'])
        result = await self.done(task['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['result_id'])

    async def test_notes_versions_selftest_and_ppt_export(self):
        """学习产物可保存、编辑、自测，并从固定版本生成真实 PPTX。"""
        state, artifact = await self.enable()
        note = self.service.notes.create(self.study['id'], '测试笔记', [artifact['id']])
        original = self.service.notes.export(note['id'], 1)
        self.assertIn('PDF 第 1 页', original)
        texts = [b['text'] for b in note['blocks']]
        texts[0] = '我的理解，仅为测试。'
        edited = self.service.notes.update(note['id'], 1, '修改后的标题', texts)
        self.assertEqual(edited['blocks'][0]['type'], 'user_note')
        self.assertEqual(edited['title'], '修改后的标题')
        self.assertEqual(self.service.store.note(note['id'], 1)['title'], '测试笔记')
        self.assertEqual(self.service.notes.export(note['id'], 1), original)
        with self.assertRaises(RuntimeError):
            self.service.notes.update(note['id'], 1, '覆盖旧版本', texts)
        task = self.service.generate(self.study['id'], self.session, state['revision'], 'self_test', {'artifact_ids': [artifact['id']]})
        result = await self.done(task['id'])
        self.assertEqual(len(self.service.store.artifact(result['result_id'])['questions']), 1)
        quiz_note = self.service.notes.create(self.study['id'], '自测笔记', [result['result_id']])
        quiz_text = self.service.notes.export(quiz_note['id'])
        self.assertIn('方法是什么？', quiz_text)
        self.assertIn('答案：分阶段阅读。', quiz_text)
        self.assertTrue(quiz_note['blocks'][-1]['sources'])
        task = self.service.generate(self.study['id'], self.session, state['revision'], 'presentation_outline',
                                     {'note_id': note['id'], 'note_revision': 1, 'page_count': 8})
        result = await self.done(task['id'])
        outline = self.service.store.artifact(result['result_id'])
        updated = self.service.edit_outline(outline['id'], 1, outline['slides'])
        with self.assertRaises(TaskConflict):
            self.service.edit_outline(outline['id'], 1, outline['slides'])
        task = self.service.generate(self.study['id'], self.session, state['revision'], 'presentation',
                                     {'outline_id': updated['id'], 'outline_revision': updated['revision']})
        result = await self.done(task['id'])
        path = self.service.download(result['result_id'])
        self.assertEqual(len(Presentation(path).slides), 1)
        self.assertEqual(self.service.store.artifact(result['result_id'])['note_revision'], 1)

    async def test_delete_source_cancels_and_cleans_snapshots(self):
        """删除来源先失效任务和模式，清理后旧引用不能访问。"""
        state, artifact = await self.enable()
        self.model.delay = .5
        task = self.service.generate(self.study['id'], self.session, state['revision'], 'explanation', {'question': '解释'})
        await asyncio.sleep(.02)
        await self.service.invalidate_file('kb', 'file', True)
        self.assertEqual(self.sessions.detail(self.session)['paper_mode'], 0)
        with self.assertRaises(KeyError):
            self.service.store.artifact(artifact['id'])
        with self.assertRaises(KeyError):
            self.service.store.task(task['id'])

    async def test_partial_coverage_on_empty_page_and_batch_limit(self):
        """空文本页和未处理批次都显式列在覆盖报告中。"""
        self.kb.files['file'] = pdf_bytes(('Reading paper one. '*200, 'Reading paper two. '*200, ''))
        self.service.settings = replace(self.settings, paper_max_batches=1)
        self.service.model.settings = self.service.settings
        state = self.service.change_mode(self.session, True, self.study['id'], 0)
        task = await self.done(state['task_id'])
        self.assertEqual(task['status'], 'partial')
        report = self.service.store.artifact(task['result_id'])['coverage']
        self.assertIn(3, report['missing_pages'])
        self.assertLess(report['processed_blocks'], report['total_blocks'])

    async def test_capacity_stale_mode_and_other_sessions(self):
        """容量检查不遗留假任务，模式修改不会影响其他会话。"""
        self.service.tasks.settings = replace(self.settings, paper_queue_capacity=0)
        self.model.delay = .5
        state = self.service.change_mode(self.session, True, self.study['id'], 0)
        other_session = self.sessions.create()
        other_study = self.service.create_study('kb', 'other')
        with self.assertRaises(TaskCapacityError):
            self.service.change_mode(other_session, True, other_study['id'], 0)
        self.assertEqual(self.sessions.detail(other_session)['paper_mode'], 0)
        with self.assertRaises(TaskConflict):
            self.service.change_mode(self.session, False, None, 0)
        self.assertEqual(self.sessions.detail(self.session)['paper_mode'], 1)
        self.service.change_mode(self.session, False, None, state['revision'])
        await self.done(state['task_id'])

    async def test_switch_paper_replaces_cancelled_task_at_capacity(self):
        """队列容量为零时切换论文仍可替换本会话被取消的任务。"""
        self.service.tasks.settings = replace(self.settings, paper_queue_capacity=0)
        self.model.delay = .2
        state = self.service.change_mode(self.session, True, self.study['id'], 0)
        await asyncio.wait_for(self.model.entered.wait(), 2)
        other = self.service.create_study('kb', 'other')
        switched = self.service.change_mode(self.session, True, other['id'], state['revision'])
        self.assertEqual((await self.done(state['task_id']))['status'], 'cancelled')
        self.assertEqual((await self.done(switched['task_id']))['status'], 'complete')

    async def test_model_timeout_fails_bounded_without_fake_answer(self):
        """每批超时有限重试后停止，不能返回模型自有知识。"""
        self.model.delay = .2
        self.service.model.settings = replace(self.settings, model_timeout_seconds=.01, model_max_retries=0)
        state = self.service.change_mode(self.session, True, self.study['id'], 0)
        result = await self.done(state['task_id'])
        self.assertEqual(result['error_code'], 'model_timeout')
        self.assertIsNone(result['result_id'])
        self.assertEqual(self.model.calls, 1)

    async def test_http_mode_tasks_replay_notes_and_default_chat(self):
        """通过真实 ASGI 完成模式开启、SSE 回放、笔记导出并验证旧路由。"""
        import app.main as main
        class OrdinaryAgent:
            """仅用于普通聊天路由的测试对象。"""
            async def run(self, history, question, progress=None, **kwargs):
                """返回固定文本，不调用论文模型。"""
                return '普通问答'
        auth_store, auth_patch = install_auth(main, self.root)
        try:
          with patch.object(main, 'papers', self.service), patch.object(main, 'sessions', self.sessions), patch.object(main, 'agent', OrdinaryAgent()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://test') as client:
                await login_admin(client)
                response = await client.post('/api/chat', json={'session_id': self.session, 'message': '聊聊论文'})
                self.assertIn('普通问答', response.text)
                self.assertEqual(self.model.calls, 0)
                denied = await client.post(f"/api/paper-studies/{self.study['id']}/overview", json={'session_id': self.session, 'paper_mode_revision': 0})
                self.assertEqual(denied.status_code, 409)
                enabled = await client.patch(f'/api/sessions/{self.session}/paper-mode', json={'enabled': True, 'study_id': self.study['id'], 'expected_revision': 0})
                state = enabled.json()
                task = await self.done(state['task_id'])
                events = await client.get(f"/api/tasks/{task['id']}/events?limit=2")
                after = events.json()['next_after']
                replay = await client.get(f"/api/tasks/{task['id']}/stream", headers={'Last-Event-ID': str(after)})
                self.assertIn('event: done', replay.text)
                self.assertNotIn('id: 1\n', replay.text)
                artifact = self.service.store.artifact(task['result_id'])
                source = await client.get(f"/api/artifacts/{artifact['id']}/sources/{artifact['evidence'][0]['id']}")
                self.assertEqual(source.json()['page'], 1)
                saved = await client.post(f"/api/paper-studies/{self.study['id']}/notes", json={'title':'HTTP笔记', 'artifact_ids':[artifact['id']]})
                self.assertEqual(saved.status_code, 201)
                exported = await client.get(f"/api/notes/{saved.json()['id']}/export")
                self.assertIn('PDF 第 1 页', exported.text)
                explained = await client.post('/api/chat', json={'session_id':self.session, 'message':'解释方法'})
                self.assertIn('event: paper_task', explained.text)
        finally:
            auth_patch.stop()
            auth_store.close()


class ReaderAndMigrationTests(unittest.TestCase):
    """解析器与旧会话迁移不依赖任何外部服务。"""

    def test_stable_page_blocks_and_scan_rejection(self):
        """页码保持一基，空页记录可见，纯扫描占位 PDF 明确拒绝。"""
        data = pdf_bytes(('Readable original paragraph on first page.', '', 'Third page original text.'))
        first, second = read_pdf('paper.pdf', data), read_pdf('paper.pdf', data)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual([b['page'] for b in first['blocks']], [1, 3])
        self.assertEqual(first['quality']['empty_pages'], [2])
        with self.assertRaises(ValueError):
            read_pdf('scan.pdf', pdf_bytes(('',)))

    def test_old_sessions_migrate_default_off_and_restart(self):
        """旧数据库增量迁移，模式版本与保存设置可跨重启恢复。"""
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'session.db'
            db=sqlite3.connect(path)
            db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,created_at TEXT,updated_at TEXT,summary TEXT DEFAULT '',summary_cursor INTEGER DEFAULT 0,knowledge_base_id TEXT,allow_web INTEGER DEFAULT 0)")
            db.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES('old','old','now','now')")
            db.commit(); db.close()
            store=SessionStore(path)
            self.assertEqual(store.detail('old')['paper_mode'],0)
            store.set_paper_mode('old',True,None,0)
            store.close()
            store=SessionStore(path)
            self.assertEqual(store.detail('old')['paper_mode'],1)
            self.assertEqual(store.detail('old')['paper_mode_revision'],1)
            store.close()


class KnowledgeReadTests(unittest.IsolatedAsyncioTestCase):
    """真实 Chroma 检索过滤与知识库公开文件读取接口。"""

    async def test_file_scoped_retrieval_and_read_ownership(self):
        """相同内容的两个文件仍独立过滤，跨知识库读取必须拒绝。"""
        from app.knowledge_base import KnowledgeBaseService, ChromaVectorStore
        from app.parsers import ParserRegistry
        from tests.test_v2_api import ApiEmbeddings
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kb = KnowledgeBaseService(root/'kb.db',root/'uploads',ParserRegistry.default(),ApiEmbeddings(),ChromaVectorStore(root/'vectors'))
            try:
                first=kb.create_knowledge_base('first')['id']
                second=kb.create_knowledge_base('second')['id']
                data=pdf_bytes()
                a=await kb.ingest_file(first,'a.pdf','application/pdf',data)
                b=await kb.ingest_file(first,'b.pdf','application/pdf',data)
                info,original=kb.read_file(first,a['id'])
                self.assertEqual(original,data)
                self.assertNotIn('stored_path',info)
                with self.assertRaises(KeyError):
                    kb.read_file(second,a['id'])
                hits=await kb.search(first,'learning',file_id=b['id'])
                self.assertTrue(hits)
                self.assertTrue(all(hit.metadata['file_id']==b['id'] for hit in hits))
            finally:
                kb.close()


if __name__ == '__main__':
    unittest.main()
