"""Agent 可调用工具及其统一 interface。"""

from app.tools.base import AgentTool, ToolContext
from app.tools.reader import READ_PAGES_SCHEMA, ReadPagesTool
from app.tools.search import SEARCH_WEB_SCHEMA, SearchWebTool


TOOL_SCHEMAS = [SEARCH_WEB_SCHEMA, READ_PAGES_SCHEMA]

__all__ = [
    "AgentTool",
    "READ_PAGES_SCHEMA",
    "ReadPagesTool",
    "SEARCH_WEB_SCHEMA",
    "SearchWebTool",
    "TOOL_SCHEMAS",
    "ToolContext",
]
