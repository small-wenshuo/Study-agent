"""笔记只保存用户明确选定的产物，编辑后不继承事实校验状态。"""
from __future__ import annotations

LABELS = {'paper_fact': '论文原文结论', 'teaching_explanation': '辅助解释',
          'external_evidence': '外部补充', 'user_note': '用户编辑'}


def artifact_markdown(artifact: dict) -> str:
    """展示引用由登记的证据生成，下载后仍可按页码与摘录定位。"""
    sources = artifact.get('evidence', [])
    mapping = {s['id']: f'P{i}' for i, s in enumerate(sources, 1)}
    lines = []
    coverage = artifact.get('coverage', {})
    if coverage:
        lines += [f"覆盖状态：{coverage.get('status', 'unknown')}",
                  f"已处理页：{coverage.get('processed_pages', [])}；未处理页：{coverage.get('missing_pages', [])}",
                  *coverage.get('limitations', []), '']
    for block in artifact.get('blocks', []):
        citations = ' '.join(f'[{mapping[eid]}]' for eid in block.get('evidence_ids', []) if eid in mapping)
        lines += [f"## {block.get('heading', '')}", f"【{LABELS.get(block['type'], block['type'])}】",
                  block['text'] + (' ' + citations if citations else ''), '']
    for index, question in enumerate(artifact.get('questions', []), 1):
        citations = ' '.join(f'[{mapping[eid]}]' for eid in question['evidence_ids'] if eid in mapping)
        lines += [f"### 自测 {index}：{question['question']}", f"提示：{question['hint']}",
                  f"答案：{question['answer']} {citations}", '']
    if sources:
        lines += ['## 来源索引']
    for source in sources:
        locator = f"PDF 第 {source['page']} 页" if source['source_type'] == 'paper' else source['url']
        lines += [f"[{mapping[source['id']]}] {source.get('filename', source.get('title', ''))} · {locator}",
                  f"摘录：{source['quote']}", f"证据 ID：{source['id']}", '']
    return '\n'.join(lines)


class NoteService:
    """业务校验与渲染独立于 HTTP 和模型。"""

    def __init__(self, store):
        """注入学习存储。"""
        self.store = store

    def create(self, study_id: str, title: str, artifact_ids: list[str], user_text: str = '') -> dict:
        """只收录同一论文的产物，保存时携带不可变来源快照。"""
        self.store.study(study_id)
        blocks = []
        for artifact_id in dict.fromkeys(artifact_ids):
            artifact = self.store.artifact(artifact_id)
            if artifact['study_id'] != study_id or artifact['kind'] not in {'overview', 'explanation', 'self_test'}:
                raise ValueError('不能加入其他论文或非学习文本产物。')
            sources = {s['id']: s for s in artifact.get('evidence', [])}
            for block in artifact['blocks']:
                blocks.append({**block, 'artifact_id': artifact_id, 'snapshot_id': artifact['snapshot_id'],
                               'sources': [sources[eid] for eid in block['evidence_ids']]})
            # 自测题的提示、答案与来源一并保存，不能只保存自测的概述段落。
            for index, question in enumerate(artifact.get('questions', []), 1):
                blocks.append({'id': f'q{index}', 'type': 'teaching_explanation',
                               'heading': f"自测：{question['question']}",
                               'text': f"问题：{question['question']}\n提示：{question['hint']}\n答案：{question['answer']}",
                               'evidence_ids': question['evidence_ids'], 'artifact_id': artifact_id,
                               'snapshot_id': artifact['snapshot_id'],
                               'sources': [sources[eid] for eid in question['evidence_ids']]})
        if user_text.strip():
            blocks.append({'id': 'user', 'type': 'user_note', 'heading': '我的笔记', 'text': user_text.strip(),
                           'evidence_ids': [], 'sources': []})
        if not blocks:
            raise ValueError('请选择产物或填写笔记内容。')
        self.validate_blocks(blocks)
        return self.store.create_note(study_id, title, blocks)

    @staticmethod
    def validate_blocks(blocks: list[dict]):
        """限制笔记容量，不接受任意外部来源对象或过大文本。"""
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= 100:
            raise ValueError('笔记需要 1～100 段。')
        if sum(len(str(b.get('text', ''))) for b in blocks) > 100000:
            raise ValueError('笔记文本超过 100000 字符。')

    def update(self, note_id: str, revision: int, title: str, texts: list[str]) -> dict:
        """仅修改既有段落文本，来源由服务端保留，用户无法伪造已验证来源。"""
        note = self.store.note(note_id)
        if note['revision'] != revision:
            raise RuntimeError('笔记版本冲突，请刷新。')
        if len(texts) != len(note['blocks']):
            raise ValueError('编辑文本数量必须与原笔记段落一致。')
        blocks = []
        for old, text in zip(note['blocks'], texts, strict=True):
            if not isinstance(text, str) or not text.strip() or len(text) > 12000:
                raise ValueError('每段文本需要 1～12000 字符。')
            blocks.append({**old, 'text': text.strip(),
                           'type': old['type'] if text.strip() == old['text'] else 'user_note'})
        self.validate_blocks(blocks)
        return self.store.update_note(note_id, revision, title, blocks)

    def export(self, note_id: str, revision: int | None = None) -> str:
        """导出固定版本，用户编辑内容与原文摘录同时可见。"""
        note = self.store.note(note_id, revision)
        study = self.store.study(note['study_id'])
        lines = [f"# {note['title']}", f"论文：{study['title']}；笔记版本：{note['revision']}", '']
        for block in note['blocks']:
            lines += [artifact_markdown({'blocks': [block], 'evidence': block.get('sources', [])}),
                      f"原文快照：{block.get('snapshot_id', '用户内容')}", '']
        return '\n'.join(lines)
