from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.agent.graph import ProgressCallback, StudyAgent


@dataclass(slots=True)
class Session:
    messages: list[BaseMessage] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self) -> str:
        session_id = str(uuid4())
        self._sessions[session_id] = Session()
        return session_id

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def chat(self, session_id: str, question: str, agent: StudyAgent, progress: ProgressCallback | None = None) -> str:
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
