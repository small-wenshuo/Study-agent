from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(slots=True)
class ToolContext:
    """保存两个网页工具共享且允许更新的任务内证据状态。"""

    search_results: dict[str, dict[str, str]]
    sources: dict[str, dict[str, Any]]
    search_rounds: int
    read_calls: int = 0


class AgentTool(Protocol):
    """工具调度节点所依赖的统一 interface。"""

    name: str
    schema: dict[str, Any]

    async def execute(self, args: Any, context: ToolContext) -> dict[str, Any]:
        """校验参数、执行工具并返回可序列化结果。"""
        ...
