from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.agent.graph import ProgressCallback, StudyAgent


@dataclass(slots=True)
class Session:
    """保存单个会话的短期消息历史和执行锁。"""

    messages: list[BaseMessage] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore:
    """管理进程内会话，并保证同一会话任务串行执行。"""

    def __init__(self) -> None:
        """初始化空的内存会话表。"""
        self._sessions: dict[str, Session] = {}

    def create(self) -> str:
        """创建新会话并返回随机会话 ID。"""
        session_id = str(uuid4())
        self._sessions[session_id] = Session()
        return session_id

    def get(self, session_id: str) -> Session | None:
        """按 ID 查询会话，不存在时返回 None。"""
        return self._sessions.get(session_id)

    async def chat(self, session_id: str, question: str, agent: StudyAgent, progress: ProgressCallback | None = None) -> str:
        """在会话锁内运行 Agent，并保存最近六轮对话。"""
        session = self.get(session_id)
        if session is None:
            raise KeyError("会话不存在，请新建会话")
        if session.lock.locked():
            raise RuntimeError("该会话已有任务正在执行")
        async with session.lock:
            answer = await agent.run(session.messages, question, progress)
            session.messages.extend([HumanMessage(content=question), AIMessage(content=answer)])
            session.messages = session.messages[-12:]
            return answer
