from __future__ import annotations

from typing import Protocol, Sequence

from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI

from app.config import Settings


TOOL_SCHEMAS = [
    {
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
    },
    {
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
    },
]


class ModelClient(Protocol):
    async def ainvoke(self, messages: Sequence[BaseMessage], tools_enabled: bool) -> AIMessage: ...


class OpenAICompatibleModel:
    def __init__(self, settings: Settings) -> None:
        self._model = ChatOpenAI(
            model=settings.model_name,
            api_key=settings.model_api_key,
            base_url=settings.model_base_url,
            timeout=settings.tool_timeout_seconds,
            max_retries=0,
            temperature=0.2,
        )

    async def ainvoke(self, messages: Sequence[BaseMessage], tools_enabled: bool) -> AIMessage:
        model = self._model.bind_tools(TOOL_SCHEMAS) if tools_enabled else self._model
        response = await model.ainvoke(messages)
        if not isinstance(response, AIMessage):
            raise TypeError("模型返回了不支持的消息类型")
        return response
