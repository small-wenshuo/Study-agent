from __future__ import annotations

import re

from app.knowledge_base.vector_store import VectorMatch


def _terms(text: str) -> set[str]:
    """提取英文词、中文单字和中文双字组用于轻量重排。"""
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    words.update(chinese)
    words.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    return {term for term in words if term}


class HybridReranker:
    """结合向量相似度和查询词覆盖率的本地轻量重排器。"""

    def rerank(self, query: str, matches: list[VectorMatch], limit: int) -> list[VectorMatch]:
        """重新计算候选分数并截取最相关结果。"""
        query_terms = _terms(query)
        reranked: list[VectorMatch] = []
        for match in matches:
            content_terms = _terms(match.content)
            lexical = len(query_terms & content_terms) / max(1, len(query_terms))
            score = 0.75 * match.score + 0.25 * lexical
            reranked.append(VectorMatch(match.id, match.content, match.metadata, score))
        return sorted(reranked, key=lambda item: item.score, reverse=True)[:limit]
