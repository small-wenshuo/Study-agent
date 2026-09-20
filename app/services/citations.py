from __future__ import annotations

import re
from typing import Any


CITATION_RE = re.compile(r"\[S\d+\]")


def validate_citations(answer: str, sources: dict[str, dict[str, Any]], requires_sources: bool) -> tuple[bool, str]:
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
    cited_ids = {token[1:-1] for token in CITATION_RE.findall(answer)}
    entries = []
    for source_id in sorted(cited_ids, key=lambda value: int(value[1:])):
        source = sources.get(source_id)
        if source:
            entries.append(f"- [{source_id}] [{source['title']}]({source['url']})")
    if not entries:
        return answer.strip()
    return f"{answer.strip()}\n\n来源：\n" + "\n".join(entries)
