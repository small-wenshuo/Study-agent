from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.agent.graph import StudyAgent
from app.config import Settings
from app.providers.model import OpenAICompatibleModel
from app.providers.tavily import TavilyProvider
from app.services.sessions import SessionStore


STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger(__name__)
settings = Settings.from_env()
sessions = SessionStore()


def build_agent() -> StudyAgent | None:
    """在模型配置完整时组装生产用 Agent 及可选搜索服务。"""
    if not settings.model_ready:
        return None
    model = OpenAICompatibleModel(settings)
    search = TavilyProvider(settings.tavily_api_key, settings.tool_timeout_seconds) if settings.search_ready else None
    return StudyAgent(
        model,
        search,
        max_search_rounds=settings.max_search_rounds,
        max_tool_calls=settings.max_tool_calls,
        task_timeout=settings.task_timeout_seconds,
        tool_timeout=settings.tool_timeout_seconds,
    )


agent = build_agent()
app = FastAPI(title="Study Agent", version="0.1.0")


class ChatRequest(BaseModel):
    """定义聊天接口接受的会话 ID 和用户消息。"""

    session_id: str
    message: str = Field(min_length=1, max_length=12000)

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        """拒绝只包含空白字符的聊天消息。"""
        if not value.strip():
            raise ValueError("message 不能为空")
        return value


@app.get("/")
async def index() -> FileResponse:
    """返回无需构建步骤的聊天网页。"""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """报告服务就绪状态，但不暴露任何配置值或密钥。"""
    return {
        "model_ready": settings.model_ready,
        "search_ready": settings.search_ready,
        "missing_model_fields": settings.missing_model_fields,
    }


@app.post("/api/sessions", status_code=201)
async def create_session() -> dict[str, str]:
    """创建一个重启后即清空的内存会话。"""
    return {"session_id": sessions.create()}


def _sse(event: str, data: Any) -> str:
    """把事件名称和数据编码成一个 SSE 消息块。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    """启动聊天任务并以 SSE 返回进度、回答、错误和结束事件。"""
    if agent is None:
        raise HTTPException(503, detail=f"模型配置不完整：{', '.join(settings.missing_model_fields)}")
    if sessions.get(request.session_id) is None:
        raise HTTPException(404, detail="会话不存在，请新建会话")

    async def stream():
        """桥接后台 Agent 任务与 HTTP 流，并处理客户端断开。"""
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def progress(kind: str, message: str) -> None:
            """把 Agent 进度写入当前 SSE 队列。"""
            await queue.put((kind, {"message": message}))

        async def execute() -> None:
            """运行会话任务并保证无论成败都发送 done 事件。"""
            try:
                answer = await sessions.chat(request.session_id, request.message.strip(), agent, progress)
                await queue.put(("answer", {"answer": answer}))
            except (KeyError, RuntimeError) as exc:
                await queue.put(("error", {"message": str(exc).strip("'")}))
            except Exception as exc:  # boundary: never leak provider details or credentials
                logger.error("chat_request_failed error_type=%s", type(exc).__name__)
                await queue.put(("error", {"message": "任务执行失败，请检查后端日志和服务配置。"}))
            finally:
                await queue.put(("done", {}))

        task = asyncio.create_task(execute())
        try:
            while True:
                event, payload = await queue.get()
                yield _sse(event, payload)
                if event == "done":
                    break
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
