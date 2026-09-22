from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.providers.tavily import ProviderError, SearchProvider
from app.tools.base import ToolContext


READ_PAGES_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_pages",
        "description": "读取本轮搜索结果的网页正文，成功后返回可引用的 S 编号。",
        "parameters": {
            "type": "object",
            "properties": {
                "result_ids": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^R[0-9]+$"},
                    "minItems": 1,
                    "maxItems": 3,
                    "uniqueItems": True,
                }
            },
            "required": ["result_ids"],
            "additionalProperties": False,
        },
    },
}


class ReadPagesTool:
    """隐藏结果授权、正文提取、截断、去重和 S 编号登记逻辑。"""

    name = "read_pages"
    schema = READ_PAGES_SCHEMA

    def __init__(
        self,
        provider: SearchProvider | None,
        *,
        timeout: int,
        emit_status: Callable[[str, str], Awaitable[None]],
    ) -> None:
        """注入正文提取 adapter、超时和进度回调。"""
        self._provider = provider
        self._timeout = timeout
        self._emit_status = emit_status

    async def execute(self, args: Any, context: ToolContext) -> dict[str, Any]:
        """读取已登记结果，并将成功正文登记为可引用的 S 来源。"""
        result_ids = args.get("result_ids") if isinstance(args, dict) else None
        if (
            not isinstance(result_ids, list)
            or not 1 <= len(result_ids) <= 3
            or any(not isinstance(value, str) for value in result_ids)
            or len(set(result_ids)) != len(result_ids)
        ):
            return {"ok": False, "error_type": "invalid_arguments", "message": "result_ids 必须包含 1～3 个不重复的结果编号"}
        unknown = [value for value in result_ids if value not in context.search_results]
        if unknown:
            return {"ok": False, "error_type": "unknown_result", "message": f"未知结果编号：{', '.join(unknown)}"}
        if self._provider is None:
            return {"ok": False, "error_type": "search_unavailable", "message": "阅读服务未配置"}

        await self._emit_status("status", f"正在阅读 {len(result_ids)} 个页面…")
        urls = [context.search_results[value]["url"] for value in result_ids]
        try:
            extracted = await asyncio.wait_for(self._provider.extract(urls), timeout=self._timeout)
        except (ProviderError, TimeoutError, ValueError) as exc:
            return {"ok": False, "error_type": type(exc).__name__, "message": str(exc)}

        documents = []
        failures = []
        for result_id, item in zip(result_ids, extracted, strict=False):
            content = str(item.get("content") or "").strip()
            if not content:
                failures.append({"result_id": result_id, "reason": str(item.get("error") or "正文为空")})
                continue
            result = context.search_results[result_id]
            existing = next((value for value in context.sources.values() if value["url"] == result["url"]), None)
            if existing:
                documents.append(existing)
                continue
            source_id = f"S{len(context.sources) + 1}"
            source = {
                "source_id": source_id,
                "title": result["title"],
                "url": result["url"],
                "content": content[:12000],
                "truncated": len(content) > 12000,
            }
            context.sources[source_id] = source
            documents.append(source)
        return {"ok": bool(documents), "documents": documents, "failures": failures}
