"""论文模式、任务节点、证据及笔记应用服务。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from app.agent.paper import PaperModel, PaperGenerationError
from app.notes.service import NoteService, artifact_markdown
from app.papers.reader import read_pdf, make_batches, relevant_blocks, PARSER_VERSION
from app.papers.store import LearningStore, now
from app.presentations.render import validate_slides, render_pptx
from app.services.tasks import TaskManager, TaskConflict, TERMINAL, TaskBudgetError


class PaperService:
    """用服务公开接口协调资源，不把论文模式交给模型推断。"""

    def __init__(self, settings, data_dir: Path, sessions, knowledge, model=None, web=None):
        """依赖使用 getter，方便测试隔离并避免复制数据库业务。"""
        self.settings, self.data_dir = settings, data_dir
        self.sessions, self.knowledge = sessions, knowledge
        self.store = LearningStore(data_dir / 'paper-learning.db')
        self.model = PaperModel(model, settings)
        self.web = web
        self.tasks = TaskManager(self.store, settings, self.valid)
        self.notes = NoteService(self.store)

    def kb(self):
        """配置缺失时明确拒绝论文读取，不回退到模型自有知识。"""
        value = self.knowledge()
        if value is None:
            raise RuntimeError('知识库未启用，请配置 Embedding 并上传论文。')
        return value

    def create_study(self, kb_id: str, file_id: str) -> dict:
        """检查文件类型和归属；创建记录不会触发全文或模型调用。"""
        info = self.kb().file_info(kb_id, file_id)
        if info['file_type'] != 'pdf':
            raise ValueError('论文模式首期只支持文本型 PDF。')
        return self.store.create_study(kb_id, file_id, str(info['filename']))

    def study(self, study_id: str) -> dict:
        """提供导读、笔记、历史任务和覆盖情况，纯查询不创建任务。"""
        study = self.store.study(study_id)
        self.kb().file_info(study['knowledge_base_id'], study['file_id'])
        artifacts = self.store.many('SELECT id,kind,snapshot_id,created_at FROM artifacts WHERE study_id=? ORDER BY created_at DESC LIMIT 100', (study_id,))
        notes = self.store.many('SELECT * FROM notes WHERE study_id=? ORDER BY created_at DESC LIMIT 100', (study_id,))
        tasks = self.store.many('SELECT id,status,kind,result_id,message,session_id FROM tasks WHERE study_id=? ORDER BY created_at DESC LIMIT 100', (study_id,))
        snapshot = self.store.snapshot(study['active_snapshot_id'], study_id) if study['active_snapshot_id'] else None
        return {**study, 'artifacts': artifacts, 'notes': notes, 'tasks': tasks,
                'quality': snapshot['quality'] if snapshot else None}

    def valid(self, task: dict) -> bool:
        """每次调用和发布前核验模式版本、任务状态以及来源仍然存在。"""
        try:
            session = self.sessions().detail(task['session_id'], limit=1)
            study = self.store.study(task['study_id'])
            info = self.kb().file_info(study['knowledge_base_id'], study['file_id'])
            current = self.store.task(task['id'])
            return bool(session['paper_mode'] and session['paper_study_id'] == task['study_id']
                        and session['paper_mode_revision'] == task['mode_revision']
                        and info['status'] == 'ready' and current['status'] not in TERMINAL)
        except (KeyError, RuntimeError):
            return False

    def require_mode(self, session_id: str, study_id: str, revision: int):
        """学习生成接口必须获得当前会话显式模式，不接受客户端单独布尔授权。"""
        session = self.sessions().detail(session_id, limit=1)
        if not session['paper_mode'] or session['paper_study_id'] != study_id or session['paper_mode_revision'] != revision:
            raise TaskConflict('请开启论文模式并选择当前论文；设置版本可能已改变。')
        self.store.study(study_id)
        if self.model.model is None:
            raise RuntimeError('模型尚未配置。')

    def change_mode(self, session_id: str, enabled: bool, study_id: str | None, expected: int) -> dict:
        """同步协调模式和任务，无 await 间隙；关闭或切换先使旧版本失效。"""
        previous = self.sessions().detail(session_id, limit=1)
        if not enabled:
            # 关闭只撤销自动执行，保留导航指针以回看/导出已保存学习内容。
            study_id = previous['paper_study_id']
        if previous['paper_mode_revision'] != expected:
            raise TaskConflict('论文模式设置已变化，请刷新。')
        cached, inputs = None, {}
        if enabled and study_id:
            study = self.store.study(study_id)
            info, data = self.kb().read_file(study['knowledge_base_id'], study['file_id'])
            if info['status'] != 'ready':
                raise TaskConflict('论文尚未入库完成，请先重试文件处理。')
            digest = hashlib.sha256(data).hexdigest()
            sid = hashlib.sha256((digest + PARSER_VERSION).encode()).hexdigest()
            sid = hashlib.sha256((study_id + sid).encode()).hexdigest()
            cached = self.store.latest(study_id, 'overview', sid)
            inputs = {'content_hash': digest}
            same = previous['paper_mode'] and previous['paper_study_id'] == study_id
            if not cached and not same:
                if self.model.model is None:
                    raise RuntimeError('模型尚未配置。')
                pending = self.store.many("SELECT id FROM tasks WHERE study_id=? AND status IN ('queued','running') AND session_id!=?", (study_id, session_id))
                if pending:
                    raise TaskConflict('这篇论文在其他会话中已有任务。')
                self.tasks.available(excluding_session=session_id)
        if bool(previous['paper_mode']) == enabled and previous['paper_study_id'] == study_id:
            pending = self.store.many("SELECT id FROM tasks WHERE session_id=? AND status IN ('queued','running')", (session_id,))
            return {'enabled': enabled, 'study_id': study_id, 'revision': expected,
                    'task_id': pending[0]['id'] if pending else None, 'artifact_id': cached['id'] if cached else None}
        updated = self.sessions().set_paper_mode(session_id, enabled, study_id, expected,
                                                knowledge_base_id=study['knowledge_base_id'] if enabled and study_id else None)
        self.tasks.cancel_session(session_id)
        task = None
        if enabled and study_id and not cached:
            try:
                task = self.tasks.submit(study_id, session_id, updated['paper_mode_revision'], 'overview', inputs, self.worker)
            except Exception:
                # 同步异常时恢复普通模式，不能把未启动任务显示为成功。
                self.sessions().set_paper_mode(session_id, False, None, updated['paper_mode_revision'])
                raise
        return {'enabled': enabled, 'study_id': study_id, 'revision': updated['paper_mode_revision'],
                'task_id': task['id'] if task else None, 'artifact_id': cached['id'] if cached else None}

    def generate(self, study_id: str, session_id: str, revision: int, kind: str, inputs: dict) -> dict:
        """显式生成请求校验目标、笔记版本与联网许可后进入有界执行器。"""
        self.require_mode(session_id, study_id, revision)
        study = self.store.study(study_id)
        if kind == 'presentation_outline':
            note = self.store.note(inputs['note_id'], inputs['note_revision'])
            if note['study_id'] != study_id:
                raise ValueError('笔记不属于当前论文。')
        if kind == 'presentation':
            outline = self.store.artifact(inputs['outline_id'])
            if outline['study_id'] != study_id or outline['kind'] != 'presentation_outline' or outline['revision'] != inputs['outline_revision']:
                raise ValueError('大纲不属于当前论文或版本不匹配。')
        if kind in {'explanation', 'self_test'}:
            snapshot_id = inputs.get('snapshot_id') or study['active_snapshot_id']
            if not snapshot_id or snapshot_id != study['active_snapshot_id']:
                raise TaskConflict('请先完成当前论文导读，旧快照不能用于新追问。')
            snapshot = self.store.snapshot(snapshot_id, study_id)
            inputs = {**inputs, 'snapshot_id': snapshot_id}
            if inputs.get('target_block_id'):
                artifact_id, block_id = inputs['target_block_id'].split(':', 1) if ':' in inputs['target_block_id'] else ('', '')
                artifact = self.store.artifact(artifact_id)
                if artifact['study_id'] != study_id or artifact['snapshot_id'] != snapshot_id:
                    raise ValueError('追问目标不属于当前论文版本。')
                block = next((b for b in artifact['blocks'] if b['id'] == block_id), None)
                if not block:
                    raise ValueError('追问目标不存在。')
                inputs['target_ids'] = [e['block_id'] for e in artifact['evidence']
                                        if e['id'] in block['evidence_ids'] and e['source_type'] == 'paper']
            else:
                inputs['target_ids'] = []
            relevant_blocks(snapshot, inputs.get('question', ''), inputs['target_ids'], self.settings.paper_input_chars // 2)
            if kind == 'self_test':
                selected = inputs.get('artifact_ids', [])
                if not selected:
                    raise ValueError('请先选择已学习内容。')
                for artifact_id in selected:
                    artifact = self.store.artifact(artifact_id)
                    if artifact['study_id'] != study_id or artifact['snapshot_id'] != snapshot_id:
                        raise ValueError('自测内容属于其他论文版本。')
        return self.tasks.submit(study_id, session_id, revision, kind, inputs, self.worker)

    async def worker(self, task: dict, budget) -> dict:
        """独立节点路由，不在普通聊天中自动扩展论文工作流。"""
        if task['kind'] == 'overview':
            return await self.overview(task, budget)
        if task['kind'] in {'explanation', 'self_test'}:
            return await self.explain(task, budget)
        if task['kind'] == 'presentation_outline':
            return await self.outline(task, budget)
        if task['kind'] == 'presentation':
            return await self.presentation(task, budget)
        raise ValueError('未知论文任务。')

    @staticmethod
    def evidence(block: dict, quote: str | None = None, claim: str = '') -> dict:
        """证据始终指向原文文本块，展示引用编号由渲染层生成。"""
        quote = quote or block['text']
        key = hashlib.sha256((block['id'] + quote).encode()).hexdigest()[:24]
        return {'id': key, 'source_type': 'paper', 'block_id': block['id'], 'page': block['page'],
                'quote': quote, 'claim': claim}

    async def overview(self, task: dict, budget) -> dict:
        """阅读全文后分批抽取证据，超预算或失败明确记录未处理页。"""
        study = self.store.study(task['study_id'])
        await budget.emit('read', 'running', '正在提取论文全文与页码。')
        info, data = self.kb().read_file(study['knowledge_base_id'], study['file_id'])
        if task['input'].get('content_hash') and hashlib.sha256(data).hexdigest() != task['input']['content_hash']:
            raise ValueError('原论文内容已变化，请重新选择。')
        snapshot = await asyncio.wait_for(asyncio.to_thread(read_pdf, str(info['filename']), data),
                                           self.settings.knowledge_timeout_seconds)
        budget.check()
        snapshot = self.store.save_snapshot(study['id'], snapshot)
        cached = self.store.latest(study['id'], 'overview', snapshot['id']) if not task['input'].get('force') else None
        if cached:
            await budget.emit('overview', 'completed', '复用当前版本已有导读，没有新增模型调用。')
            return {k: cached[k] for k in ('blocks', 'questions', 'evidence', 'coverage', 'snapshot_id')}
        batches = make_batches(snapshot['blocks'], self.settings.paper_batch_chars)
        processed, cards, failures, timed_out = set(), [], [], False
        version = f'cards-v1-{self.settings.paper_batch_chars}'
        for index, blocks in enumerate(batches):
            if index >= self.settings.paper_max_batches or budget.calls >= self.settings.paper_max_model_calls - 6:
                failures.extend(range(index, len(batches)))
                break
            await budget.emit('read_batch', 'running', f'正在整理第 {index+1} 批，共 {len(batches)} 批。',
                              {'batch_index': index+1, 'total_batches': len(batches), 'pages': sorted({b['page'] for b in blocks})})
            batch_cards = self.store.batch(snapshot['id'], index, version)
            try:
                if batch_cards is None or task['input'].get('force'):
                    batch_cards = await self.model.cards(blocks, budget)
                    budget.check()
                    self.store.save_batch(snapshot['id'], index, version, batch_cards)
                cards += batch_cards
                processed.update(b['id'] for b in blocks)
            except (PaperGenerationError, TimeoutError) as exc:
                timed_out = timed_out or isinstance(exc, TimeoutError)
                failures.append(index)
                await budget.emit('read_batch', 'failed', f'第 {index+1} 批处理失败，覆盖报告将保留缺口。')
        if not cards:
            if timed_out:
                raise TimeoutError('模型未能在单次时限内完成批次。')
            raise ValueError('没有通过校验的论文证据。')
        block_map = {b['id']: b for b in snapshot['blocks']}
        evidence = []
        for card in cards:
            item = self.evidence(block_map[card['block_id']], card['quote'], card['claim'])
            item['filename'] = snapshot['filename']
            if item['id'] not in {e['id'] for e in evidence}:
                evidence.append(item)
        # 超长证据分组筛选，所有引用仍然回到已校验的原文，不把汇总当成原文。
        evidence = await self.reduce_evidence(evidence, budget)
        content = await self.model.compose('overview', evidence, '先整体理解这篇论文，再给出阅读路线。', budget)
        missing = [b for b in snapshot['blocks'] if b['id'] not in processed]
        quality = snapshot['quality']
        coverage = {'status': 'partial' if missing or quality['failed_pages'] or quality['empty_pages'] else 'complete',
                    'processed_pages': sorted({b['page'] for b in snapshot['blocks'] if b['id'] in processed}),
                    'missing_pages': sorted({b['page'] for b in missing} | set(quality['failed_pages']) | set(quality['empty_pages'])),
                    'processed_blocks': len(processed), 'total_blocks': len(snapshot['blocks']),
                    'failed_batches': failures, 'quality': quality, 'limitations': quality['limitations']}
        await budget.emit('overview', 'completed', '导读与来源校验完成。', {'coverage': coverage, 'model_calls': budget.calls})
        return {**content, 'evidence': evidence, 'coverage': coverage, 'snapshot_id': snapshot['id']}

    async def reduce_evidence(self, evidence: list[dict], budget) -> list[dict]:
        """超出输入预算时分组选择关键证据，最多两轮，仍保留原文来源。"""
        limit = self.settings.paper_input_chars - 1500
        for _ in range(2):
            if len(json.dumps(evidence, ensure_ascii=False)) <= limit:
                return evidence
            groups, group = [], []
            for item in evidence:
                if group and len(json.dumps([*group, item], ensure_ascii=False)) > limit:
                    groups.append(group)
                    group = []
                group.append(item)
            if group:
                groups.append(group)
            output = []
            for group in groups:
                known = {e['id']: e for e in group}
                def validate(value):
                    """选择只能使用已有原文卡片身份。"""
                    ids = value.get('ids')
                    if not isinstance(ids, list) or not 1 <= len(ids) <= min(8, len(group)) or any(eid not in known for eid in ids):
                        raise PaperGenerationError('关键证据选择无效。')
                    return [known[eid] for eid in dict.fromkeys(ids)]
                chosen = await self.model.call('选择最重要的问题、方法、实验条件/结果和局限证据，不丢失重要限定。只返回 {"ids":["已有证据ID"]}，最多8条。',
                                                {'evidence': group}, budget, validate)
                output += chosen
            evidence = output
        if len(json.dumps(evidence, ensure_ascii=False)) > limit:
            raise TaskBudgetError('关键证据仍超过输入预算。')
        return evidence

    async def explain(self, task: dict, budget) -> dict:
        """针对用户选定的内容展开，联网只有独立授权后才执行。"""
        inputs, study_id = task['input'], task['study_id']
        snapshot = self.store.snapshot(inputs['snapshot_id'], study_id)
        question = inputs.get('question', '根据选定的学习内容生成自测题。')
        if task['kind'] == 'self_test':
            parts = [self.store.artifact(aid) for aid in inputs['artifact_ids']]
            evidence = list({e['id']: e for a in parts for e in a['evidence']}.values())
            question += '\n已学习内容：' + '\n'.join(b['text'] for a in parts for b in a['blocks'])[:4000]
        else:
            target_ids = list(inputs['target_ids'])
            # 真实知识库使用文件过滤的向量召回，随后映射回当前快照原文。
            # 失败时明确降级到快照内词匹配，不能跨文件补充。
            retrieval_warning = None
            if hasattr(self.kb(), 'search'):
                try:
                    study = self.store.study(study_id)
                    hits = await asyncio.wait_for(self.kb().search(study['knowledge_base_id'], question,
                                                                  limit=4, file_id=study['file_id']),
                                                  self.settings.knowledge_timeout_seconds)
                    budget.check()
                    for hit in hits:
                        if hit.metadata.get('file_id') != study['file_id']:
                            continue
                        page = hit.metadata.get('page')
                        matching = [b for b in snapshot['blocks'] if b['page'] == page]
                        best = next((b for b in matching if hit.content[:100] in b['text']), matching[0] if matching else None)
                        if best and best['id'] not in target_ids:
                            target_ids.append(best['id'])
                except (TimeoutError, RuntimeError, ValueError):
                    retrieval_warning = '向量召回暂不可用，本轮降级为当前快照内的词匹配检索。'
            blocks = relevant_blocks(snapshot, question, target_ids, self.settings.paper_input_chars // 2)
            evidence = [self.evidence(b) | {'filename': snapshot['filename']} for b in blocks]
        await budget.emit('retrieve', 'completed', '已读取当前论文版本中的相关原文。',
                          {'pages': sorted({e['page'] for e in evidence if e['source_type'] == 'paper'})})
        limitations = list(snapshot['quality']['limitations'])
        if task['kind'] != 'self_test' and retrieval_warning:
            limitations.append(retrieval_warning)
        if inputs.get('allow_web'):
            if self.web is None:
                limitations.append('联网服务未配置，未获取外部补充。')
            else:
                try:
                    await budget.emit('search', 'running', '已获得本次联网许可，正在搜索补充资料。')
                    results = await asyncio.wait_for(self.web.search(question[:2000], 3), self.settings.tool_timeout_seconds)
                    budget.check()
                    urls = [r['url'] for r in results[:3]]
                    pages = await asyncio.wait_for(self.web.extract(urls), self.settings.tool_timeout_seconds) if urls else []
                    budget.check()
                    titles = {r['url']: r['title'] for r in results}
                    for page in pages:
                        if page.get('url') not in urls or not page.get('content'):
                            continue
                        text = page['content'][:2400]
                        evidence.append({'id': hashlib.sha256((page['url'] + text).encode()).hexdigest()[:24],
                                         'source_type': 'web', 'url': page['url'], 'title': titles[page['url']],
                                         'quote': text, 'retrieved_at': now()})
                    if not any(e['source_type'] == 'web' for e in evidence):
                        limitations.append('搜索未获得可读取正文，未将摘要作为证据。')
                except (TimeoutError, ValueError, RuntimeError):
                    limitations.append('联网搜索或正文读取失败，本轮仅使用已读取论文证据。')
        evidence = await self.reduce_evidence(evidence, budget)
        question += '\n展开方式：' + inputs.get('style', '自由追问')
        content = await self.model.compose(task['kind'], evidence, question, budget)
        return {**content, 'evidence': evidence, 'snapshot_id': snapshot['id'],
                'coverage': {'status': 'complete', 'processed_pages': sorted({e['page'] for e in evidence if e['source_type'] == 'paper'}),
                             'missing_pages': [], 'limitations': limitations}}

    async def outline(self, task: dict, budget) -> dict:
        """固定笔记版本生成大纲，来源取自该版本，编辑后不重新读论文。"""
        inputs = task['input']
        note = self.store.note(inputs['note_id'], inputs['note_revision'])
        evidence = list({e['id']: e for b in note['blocks'] for e in b.get('sources', [])}.values())
        desired = inputs.get('page_count', 10)
        payload = {'title': note['title'], 'page_count': desired,
                   'focus': inputs.get('focus', ''), 'note_blocks': note['blocks']}
        def validate(value):
            """大纲要点不能新增来源，也不能把用户修改声称为作者事实。"""
            slides = validate_slides(value.get('slides'), {e['id'] for e in evidence})
            return {'slides': slides}
        content = await self.model.call('根据用户笔记生成可编辑PPT大纲，不增加新论文事实。用户编辑内容必须注明。'
                                        'JSON：{"slides":[{"title":"标题","bullets":["要点，最多150字符"],'
                                        '"notes":"讲解备注，包含内容类型与局限","evidence_ids":["笔记已有证据ID"]}]}。每页最多6条要点，标题最多50字符。'
                                        '按用户指定页数组织，内容不足时可减少页数并在备注说明，不为凑页数编造。'
                                        '必要时额外提供 table:{"headers":["列名"],"rows":[["数据"]]}；最多6列6行，单元格最多35字符。',
                                        payload, budget, validate)
        return {**content, 'evidence': evidence, 'note_id': note['id'], 'note_revision': note['revision'],
                'revision': 1, 'snapshot_id': None}

    def edit_outline(self, artifact_id: str, expected: int, slides: list[dict]) -> dict:
        """大纲更新创建新版本产物，旧版本和正在渲染任务不被改写。"""
        old = self.store.artifact(artifact_id)
        if old['kind'] != 'presentation_outline' or old['revision'] != expected:
            raise TaskConflict('大纲类型或版本不匹配。')
        latest = self.store.many('SELECT id FROM artifacts WHERE study_id=? AND kind=? ORDER BY rowid DESC',
                                 (old['study_id'], 'presentation_outline'))
        for row in latest:
            current = self.store.artifact(row['id'])
            if current.get('root_id', current['id']) == old.get('root_id', old['id']):
                if current.get('revision', 1) > expected:
                    raise TaskConflict('大纲已有新版本，请重新读取。')
        validated = validate_slides(slides, {e['id'] for e in old['evidence']})
        payload = {k: old[k] for k in ('note_id', 'note_revision', 'evidence')}
        payload.update(slides=validated, revision=expected + 1, root_id=old.get('root_id', old['id']))
        return self.store.save_artifact(old['study_id'], None, old['kind'], None, payload)

    async def presentation(self, task: dict, budget) -> dict:
        """渲染固定大纲版本，取消后清理未发布导出，不把结构检查当视觉检查。"""
        outline = self.store.artifact(task['input']['outline_id'])
        filename = str(uuid4()) + '.pptx'
        path = self.data_dir / 'exports' / filename
        await budget.emit('render', 'running', '正在生成 PPTX，生成后检查文件结构与画布边界。')
        # 同步渲染上限为 60 页，不把可取消的线程留在删除后继续写文件。
        result = render_pptx(outline['slides'], outline['evidence'], path)
        try:
            await budget.emit('render', 'completed', 'PPTX 已生成；实际字体与溢出仍需渲染验收。')
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {**result, 'filename': filename, 'outline_id': outline['id'],
                'note_id': outline['note_id'], 'note_revision': outline['note_revision'], 'snapshot_id': None}

    def retry(self, task_id: str, session_id: str, revision: int) -> dict:
        """重试绑定原任务会话和当前显式模式，不在打开页面时自动执行。"""
        old = self.store.task(task_id)
        if old['session_id'] != session_id or old['status'] not in {'failed', 'partial', 'cancelled', 'interrupted'}:
            raise TaskConflict('仅原会话的失败、部分完成或中断任务可重试。')
        return self.generate(old['study_id'], session_id, revision, old['kind'], {**old['input'], 'retry_of': task_id})

    async def invalidate_file(self, kb_id: str, file_id: str, delete: bool):
        """文件删除/重试先取消学习任务，等待收尾后再清理来源。"""
        rows = self.store.many('SELECT id FROM paper_studies WHERE knowledge_base_id=? AND file_id=?', (kb_id, file_id))
        for row in rows:
            task_rows = self.store.many("SELECT id FROM tasks WHERE study_id=? AND status IN ('queued','running')", (row['id'],))
            handles = [self.tasks.running[r['id']] for r in task_rows if r['id'] in self.tasks.running]
            for task in task_rows:
                self.tasks.cancel(task['id'])
            await asyncio.gather(*handles, return_exceptions=True)
            self.sessions().disable_paper_study(row['id'])
            if delete:
                for artifact_row in self.store.many("SELECT id FROM artifacts WHERE study_id=? AND kind='presentation'", (row['id'],)):
                    artifact = self.store.artifact(artifact_row['id'])
                    (self.data_dir / 'exports' / artifact['filename']).unlink(missing_ok=True)
                self.store.delete_study(row['id'])

    def artifact(self, artifact_id: str) -> dict:
        """附带安全 Markdown，来源详情由结构化列表提供。"""
        value = self.store.artifact(artifact_id)
        value['markdown'] = artifact_markdown(value) if 'blocks' in value else ''
        return value

    def download(self, artifact_id: str) -> Path:
        """只下载服务生成的 PPTX 文件，拒绝目录穿越及非导出产物。"""
        artifact = self.store.artifact(artifact_id)
        if artifact['kind'] != 'presentation':
            raise ValueError('该产物不是 PPTX。')
        directory = (self.data_dir / 'exports').resolve()
        path = (directory / artifact['filename']).resolve()
        if path.parent != directory or not path.is_file():
            raise KeyError('导出文件不存在。')
        return path

    async def close(self):
        """退出先等待任务结束，再释放存储。"""
        await self.tasks.close()
        self.store.close()
