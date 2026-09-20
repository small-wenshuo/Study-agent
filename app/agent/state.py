from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    task_id: str
    messages: Annotated[list[AnyMessage], add_messages]
    search_results: dict[str, dict[str, str]]
    sources: dict[str, dict[str, Any]]
    search_rounds: int
    tool_calls: int
    agent_steps: int
    errors: list[str]
    answer: str
    requires_sources: bool
    citation_retries: int
    citation_error: str
    disable_tools: bool
