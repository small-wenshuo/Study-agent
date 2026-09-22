from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.providers.tavily import ProviderError, SearchProvider
from app.tools.base import ToolContext


SEARCH_WEB_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": "搜索公开网页。摘要仅用于选择要阅读的页面，不能直接作为回答证据。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 1000}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


class SearchWebTool:
    """隐藏搜索参数校验、预算、调用和 R 编号登记逻辑。"""

    name = "search_web"
    schema = SEARCH_WEB_SCHEMA

    def __init__(
        self,
        provider: SearchProvider | None,
        *,
        max_rounds: int,
        timeout: int,
        emit_status: Callable[[str, str], Awaitable[None]],
    ) -> None:
        """注入搜索 adapter、执行限制和进度回调。"""
        self._provider = provider
        self._max_rounds = max_rounds
        self._timeout = timeout
        self._emit_status = emit_status

    async def execute(self, args: Any, context: ToolContext) -> dict[str, Any]:
        """搜索公开网页，将去重结果登记为稳定的 R 编号。"""
        query = args.get("query") if isinstance(args, dict) else None
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            return {"ok": False, "error_type": "invalid_arguments", "message": "query 必须是 1～1000 字符的字符串"}
        if context.search_rounds >= self._max_rounds:
            return {"ok": False, "error_type": "budget_exhausted", "message": "搜索轮次已耗尽"}
        if self._provider is None:
            return {"ok": False, "error_type": "search_unavailable", "message": "搜索服务未配置"}
        if context.search_results and context.read_calls == 0:
            return {
                "ok": False,
                "error_type": "read_required",
                "message": "已有候选搜索结果，请先调用 read_pages 阅读正文，再决定是否补充搜索",
                "result_ids": list(context.search_results),
            }

        query = query.strip()
        await self._emit_status("status", f"正在搜索：{query}")
        context.search_rounds += 1
        try:
            found = await asyncio.wait_for(self._provider.search(query, 5), timeout=self._timeout)
        except (ProviderError, TimeoutError, ValueError) as exc:
            return {"ok": False, "error_type": type(exc).__name__, "message": str(exc)}

        registered = []
        known_urls = {item["url"] for item in context.search_results.values()}
        for item in found:
            if item["url"] in known_urls:
                continue
            result_id = f"R{len(context.search_results) + 1}"
            value = {"result_id": result_id, **item}
            context.search_results[result_id] = value
            registered.append(value)
            known_urls.add(item["url"])
        return {"ok": True, "results": registered, "message": f"找到 {len(registered)} 条新结果"}
