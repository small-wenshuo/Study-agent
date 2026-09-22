from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """描述一次 Agent 任务在 LangGraph 节点间传递的完整状态。"""

    task_id: str
    messages: Annotated[list[AnyMessage], add_messages]
    search_results: dict[str, dict[str, str]]
    sources: dict[str, dict[str, Any]]
    search_rounds: int
    read_calls: int
    tool_calls: int
    agent_steps: int
    errors: list[str]
    answer: str
    requires_sources: bool
    citation_retries: int
    citation_error: str
    disable_tools: bool
