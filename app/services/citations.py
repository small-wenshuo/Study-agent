from __future__ import annotations

import re
from typing import Any


CITATION_RE = re.compile(r"\[S\d+\]")
KNOWLEDGE_CITATION_RE = re.compile(r"\[K\d+\]")


def validate_citations(answer: str, sources: dict[str, dict[str, Any]], requires_sources: bool) -> tuple[bool, str]:
    """确认回答只引用已读取来源，并在有来源时至少引用一次。"""
    cited = set(CITATION_RE.findall(answer))
    known = {f"[{source_id}]" for source_id in sources}
    unknown = cited - known
    if unknown:
        return False, f"未知来源编号：{', '.join(sorted(unknown))}"
    if sources and not cited:
        return False, "回答使用了已读资料但没有引用"
    if requires_sources and not sources and cited:
        return False, "没有已读正文，不能生成引用"
    return True, ""


def append_source_links(answer: str, sources: dict[str, dict[str, Any]]) -> str:
    """在回答末尾附加实际被引用来源的标题和链接。"""
    cited_ids = {token[1:-1] for token in CITATION_RE.findall(answer)}
    entries = []
    for source_id in sorted(cited_ids, key=lambda value: int(value[1:])):
        source = sources.get(source_id)
        if source:
            entries.append(f"- [{source_id}] [{source['title']}]({source['url']})")
    if not entries:
        return answer.strip()
    return f"{answer.strip()}\n\n来源：\n" + "\n".join(entries)


def validate_knowledge_citations(answer: str, sources: dict[str, dict[str, Any]]) -> tuple[bool, str]:
    """确认知识库回答只引用本轮真实取得的 K 编号。"""
    cited = set(KNOWLEDGE_CITATION_RE.findall(answer))
    known = {f"[{source_id}]" for source_id in sources}
    unknown = cited - known
    if unknown:
        return False, f"未知知识库来源编号：{', '.join(sorted(unknown))}"
    if sources and not cited:
        return False, "知识库回答没有引用证据"
    return True, ""


def append_knowledge_sources(answer: str, sources: dict[str, dict[str, Any]]) -> str:
    """在回答后附加实际引用的文件名和页码、章节或 Sheet。"""
    cited_ids = {token[1:-1] for token in KNOWLEDGE_CITATION_RE.findall(answer)}
    entries: list[str] = []
    for source_id in sorted(cited_ids, key=lambda value: int(value[1:])):
        metadata = sources.get(source_id)
        if not metadata:
            continue
        location = ""
        if metadata.get("page"):
            location = f"，第 {metadata['page']} 页"
        elif metadata.get("sheet"):
            location = f"，Sheet：{metadata['sheet']}"
        elif metadata.get("section"):
            location = f"，章节：{metadata['section']}"
        entries.append(f"- [{source_id}] {metadata.get('filename', '未知文件')}{location}")
    if not entries:
        return answer.strip()
    return f"{answer.strip()}\n\n知识库来源：\n" + "\n".join(entries)
