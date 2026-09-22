from __future__ import annotations

from typing import Protocol, Sequence

from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI

from app.config import Settings
from app.tools import TOOL_SCHEMAS


class ModelClient(Protocol):
    """Agent 所依赖的最小模型调用接口。"""

    async def ainvoke(self, messages: Sequence[BaseMessage], tools_enabled: bool) -> AIMessage:
        """根据消息和工具开关返回一条模型消息。"""
        ...


class OpenAICompatibleModel:
    """把 OpenAI-compatible 聊天模型适配为 Agent 的模型接口。"""

    def __init__(self, settings: Settings) -> None:
        """根据项目配置初始化 LangChain ChatOpenAI 客户端。"""
        self._model = ChatOpenAI(
            model=settings.model_name,
            api_key=settings.model_api_key,
            base_url=settings.model_base_url,
            # 重试和超时分类由 Agent 图统一管理；HTTP 客户端稍宽一些，
            # 避免底层异常与图的单次模型超时竞争。
            timeout=settings.model_timeout_seconds + 5,
            max_retries=0,
            temperature=0.2,
        )

    async def ainvoke(self, messages: Sequence[BaseMessage], tools_enabled: bool) -> AIMessage:
        """调用模型，并按本轮预算决定是否暴露工具定义。"""
        model = self._model.bind_tools(TOOL_SCHEMAS) if tools_enabled else self._model
        response = await model.ainvoke(messages)
        if not isinstance(response, AIMessage):
            raise TypeError("模型返回了不支持的消息类型")
        return response
