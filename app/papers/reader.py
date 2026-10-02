"""读取指定论文的完整文本，页码和证据身份不依赖检索排名。"""
from __future__ import annotations

import hashlib
import io
import re
from pypdf import PdfReader

PARSER_VERSION = 'paper-text-v1'


def read_pdf(filename: str, data: bytes, block_chars: int = 3000) -> dict:
    """逐页读取 PDF，记录空页/异常并为规范化文本块建立稳定身份。"""
    digest = hashlib.sha256(data).hexdigest()
    snapshot_id = hashlib.sha256((digest + PARSER_VERSION).encode()).hexdigest()
    try:
        pdf = PdfReader(io.BytesIO(data))
        if pdf.is_encrypted and not pdf.decrypt(''):
            raise ValueError('加密 PDF 无法读取，请先解密。')
        total = len(pdf.pages)
        if total > 1000:
            raise ValueError('PDF 超过 1000 页，请拆分文件。')
        blocks, empty, failed, warnings = [], [], [], []
        for page_no, page in enumerate(pdf.pages, 1):
            try:
                raw = page.extract_text() or ''
            except Exception:
                failed.append(page_no)
                continue
            normalized = re.sub(r'[ \t]+', ' ', raw.replace('\r\n', '\n')).strip()
            if not normalized:
                empty.append(page_no)
                continue
            if '\ufffd' in normalized or len(normalized) < 40:
                warnings.append(page_no)
            for start in range(0, len(normalized), block_chars):
                text = normalized[start:start + block_chars]
                block_id = f'{snapshot_id[:16]}-{page_no}-{start}'
                blocks.append({'id': block_id, 'page': page_no, 'ordinal': len(blocks),
                               'text': text, 'char_start': start, 'char_end': start + len(text),
                               'raw_text': raw if start == 0 else ''})
        if not blocks:
            raise ValueError('没有可提取正文；扫描 PDF 暂不支持 OCR。')
        return {'id': snapshot_id, 'content_hash': digest, 'parser_version': PARSER_VERSION,
                'filename': filename, 'page_count': total, 'blocks': blocks,
                'quality': {'empty_pages': empty, 'failed_pages': failed, 'warning_pages': warnings,
                            'limitations': ['仅提取文本；图片、复杂表格、公式符号和双栏顺序需对照原文核验。']}}
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('PDF 损坏或无法解析。') from exc


def make_batches(blocks: list[dict], max_chars: int) -> list[list[dict]]:
    """按稳定顺序分批，计入标识和 JSON 的保守开销，禁止截断块内容。"""
    batches, batch, size = [], [], 0
    for block in blocks:
        cost = len(block['text']) * 2 + 180
        if cost > max_chars:
            raise ValueError('文本块超过批次预算。')
        if batch and size + cost > max_chars:
            batches.append(batch)
            batch, size = [], 0
        batch.append(block)
        size += cost
    if batch:
        batches.append(batch)
    return batches


def relevant_blocks(snapshot: dict, question: str, target_ids: list[str], max_chars: int) -> list[dict]:
    """仅在当前不可变快照内选取目标、邻近及词匹配段落，不跨论文。"""
    blocks = snapshot['blocks']
    known = {b['id'] for b in blocks}
    if any(value not in known for value in target_ids):
        raise ValueError('目标段落不属于当前论文。')
    tokens = set(re.findall(r'[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]', question.lower()))
    target_positions = {i for i, b in enumerate(blocks) if b['id'] in target_ids}
    def rank(pair):
        """目标块优先，其后是邻近上下文和问题词覆盖。"""
        i, block = pair
        near = any(abs(i - pos) == 1 for pos in target_positions)
        score = sum(token in block['text'].lower() for token in tokens)
        priority = len(target_ids) - target_ids.index(block['id']) if block['id'] in target_ids else 0
        return (i in target_positions, priority, near, score, -i)
    selected, size = [], 0
    for _, block in sorted(enumerate(blocks), key=rank, reverse=True):
        cost = len(block['text']) * 2 + 180
        if size + cost <= max_chars:
            selected.append(block)
            size += cost
        if len(selected) >= 8:
            break
    return sorted(selected, key=lambda b: b['ordinal'])
