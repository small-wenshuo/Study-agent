from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.agent.graph import StudyAgent
from app.agent.v2 import ModelKnowledgeModel, V2Assistant
from app.config import Settings
from app.knowledge_base import ChromaVectorStore, KnowledgeBaseService
from app.parsers import ParserRegistry
from app.providers.embeddings import OpenAICompatibleEmbeddings
from app.providers.model import OpenAICompatibleModel
from app.providers.tavily import TavilyProvider
from app.services.sessions import SessionStore
from app.services.memory import MemoryIndex, SummaryModel


STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger(__name__)
settings = Settings.from_env()
data_dir = Path(settings.data_dir)
if not data_dir.is_absolute():
    data_dir = (Path(__file__).resolve().parents[1] / data_dir).resolve()
memory_index = MemoryIndex(
    data_dir / "memory-chroma", OpenAICompatibleEmbeddings(settings), settings.memory_timeout_seconds,
) if not settings.missing_embedding_fields else None
sessions = SessionStore(
    data_dir / "sessions.db",
    summarizer=SummaryModel(OpenAICompatibleModel(settings)) if settings.model_ready else None,
    memory_index=memory_index, window_turns=settings.memory_window_turns,
    context_chars=settings.memory_context_chars, timeout=settings.memory_timeout_seconds,
    task_timeout=settings.task_timeout_seconds,
)


def build_knowledge_base() -> KnowledgeBaseService | None:
    """在 Embedding 配置完整时组装本地知识库。"""
    if not settings.knowledge_base_ready:
        return None
    data_dir = Path(settings.data_dir)
    if not data_dir.is_absolute():
        data_dir = (Path(__file__).resolve().parents[1] / data_dir).resolve()
    return KnowledgeBaseService(
        database_path=data_dir / "study-agent.db",
        storage_dir=data_dir / "uploads",
        parsers=ParserRegistry.default(),
        embeddings=OpenAICompatibleEmbeddings(settings),
        vectors=ChromaVectorStore(data_dir / "chroma"),
        max_file_bytes=settings.max_upload_bytes,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        processing_timeout=settings.knowledge_timeout_seconds,
    )


knowledge_bases = build_knowledge_base()


def build_agent():
    """组装 V1 Agent，并在知识库可用时包装为 V2 Assistant。"""
    if not settings.model_ready:
        return None
    model = OpenAICompatibleModel(settings)
    search = TavilyProvider(settings.tavily_api_key, settings.tool_timeout_seconds) if settings.search_ready else None
    base_agent = StudyAgent(
        model,
        search,
        max_search_rounds=settings.max_search_rounds,
        max_tool_calls=settings.max_tool_calls,
        task_timeout=settings.task_timeout_seconds,
        model_timeout=settings.model_timeout_seconds,
        model_max_retries=settings.model_max_retries,
        tool_timeout=settings.tool_timeout_seconds,
    )
    if knowledge_bases is None:
        return base_agent
    return V2Assistant(base_agent, knowledge_bases, ModelKnowledgeModel(model))


agent = build_agent()
app = FastAPI(title="Study Agent", version="0.3.0")


class ChatRequest(BaseModel):
    """定义聊天接口接受的会话 ID 和用户消息。"""

    session_id: str
    message: str = Field(min_length=1, max_length=12000)
    knowledge_base_id: str | None = None
    allow_web: bool = False

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        """拒绝只包含空白字符的聊天消息。"""
        if not value.strip():
            raise ValueError("message 不能为空")
        return value


class KnowledgeBaseCreate(BaseModel):
    """定义创建知识库所需名称。"""

    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        """拒绝空白知识库名称。"""
        if not value.strip():
            raise ValueError("name 不能为空")
        return value.strip()


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
        "knowledge_base_ready": knowledge_bases is not None,
        "missing_model_fields": settings.missing_model_fields,
        "missing_embedding_fields": settings.missing_embedding_fields,
    }


@app.post("/api/sessions", status_code=201)
async def create_session() -> dict[str, str]:
    """创建重启后仍可恢复的持久化会话。"""
    return {"session_id": sessions.create()}


@app.get("/api/sessions")
async def list_sessions(limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)) -> list[dict]:
    """分页列出历史会话。"""
    return sessions.list_sessions(limit, offset)


@app.get("/api/sessions/{session_id}")
async def session_detail(session_id: str, before: int | None = Query(None, ge=1),
                         limit: int = Query(100, ge=1, le=100)) -> dict:
    """返回会话配置、摘要和可分页的完整聊天记录。"""
    try:
        return sessions.detail(session_id, before=before, limit=limit)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc).strip("'")) from exc


class MemoryRequest(BaseModel):
    """用户主动保存或更新的长期记忆；默认属于本地用户全局范围。"""

    content: str = Field(min_length=1, max_length=2000)
    scope: str = Field(default="global", max_length=200)


@app.get("/api/memories")
async def list_memories() -> dict:
    """返回记忆原文及索引可用性，配置缺失时仍可管理原文。"""
    return {"index_ready": sessions.memory_index is not None, "items": sessions.list_memories()}


async def _save_memory(request: MemoryRequest, memory_id: str | None = None) -> dict:
    """把记忆业务错误转换成 HTTP 错误，并校验知识库范围。"""
    try:
        if request.scope.startswith("kb:"):
            _require_knowledge_bases().list_files(request.scope[3:])
        return await sessions.save_memory(request.content, request.scope, memory_id=memory_id)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@app.post("/api/memories", status_code=201)
async def create_memory(request: MemoryRequest) -> dict:
    """保存用户确认的长期记忆，索引失败时保留 pending 状态供重试。"""
    return await _save_memory(request)


@app.put("/api/memories/{memory_id}")
async def update_memory(memory_id: str, request: MemoryRequest) -> dict:
    """修改原记忆或重试索引，不创建重复条目。"""
    return await _save_memory(request, memory_id)


@app.delete("/api/memories/{memory_id}", status_code=204)
async def delete_memory(memory_id: str) -> Response:
    """使长期记忆失效并清理向量，不删除原始聊天记录。"""
    try:
        await sessions.delete_memory(memory_id)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc).strip("'")) from exc
    return Response(status_code=204)


def _require_knowledge_bases() -> KnowledgeBaseService:
    """返回已配置知识库，缺失 Embedding 配置时给出安全错误。"""
    if knowledge_bases is None:
        raise HTTPException(
            503,
            detail=f"知识库配置不完整：{', '.join(settings.missing_embedding_fields)}",
        )
    return knowledge_bases


@app.post("/api/knowledge-bases", status_code=201)
async def create_knowledge_base(request: KnowledgeBaseCreate) -> dict[str, object]:
    """创建一个可容纳多文件的个人知识库。"""
    return _require_knowledge_bases().create_knowledge_base(request.name)


@app.get("/api/knowledge-bases")
async def list_knowledge_bases() -> list[dict[str, object]]:
    """列出知识库及文件、Chunk 数量。"""
    return _require_knowledge_bases().list_knowledge_bases()


@app.delete("/api/knowledge-bases/{knowledge_base_id}", status_code=204)
async def delete_knowledge_base(knowledge_base_id: str) -> Response:
    """删除知识库、原文件和全部索引。"""
    try:
        _require_knowledge_bases().delete_knowledge_base(knowledge_base_id)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc).strip("'")) from exc
    return Response(status_code=204)


@app.get("/api/knowledge-bases/{knowledge_base_id}/files")
async def list_knowledge_base_files(knowledge_base_id: str) -> list[dict[str, object]]:
    """返回一个知识库内的文件处理状态。"""
    try:
        return _require_knowledge_bases().list_files(knowledge_base_id)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc).strip("'")) from exc


@app.post("/api/knowledge-bases/{knowledge_base_id}/files", status_code=201)
async def upload_knowledge_base_file(
    knowledge_base_id: str, file: UploadFile = File(...)
) -> dict[str, object]:
    """有限读取上传文件并同步完成首期解析和入库。"""
    service = _require_knowledge_bases()
    data = await file.read(settings.max_upload_bytes + 1)
    try:
        return await service.ingest_file(
            knowledge_base_id,
            file.filename or "upload",
            file.content_type or "application/octet-stream",
            data,
        )
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@app.delete("/api/knowledge-bases/{knowledge_base_id}/files/{file_id}", status_code=204)
async def delete_knowledge_base_file(knowledge_base_id: str, file_id: str) -> Response:
    """删除一个文件及其全部向量。"""
    try:
        _require_knowledge_bases().delete_file(knowledge_base_id, file_id)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc).strip("'")) from exc
    return Response(status_code=204)


@app.post("/api/knowledge-bases/{knowledge_base_id}/files/{file_id}/retry")
async def retry_knowledge_base_file(knowledge_base_id: str, file_id: str) -> dict[str, object]:
    """使用原文件重试失败的解析或重新构建索引。"""
    try:
        return await _require_knowledge_bases().retry_file(knowledge_base_id, file_id)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


def _sse(event: str, data: Any) -> str:
    """把事件名称和数据编码成一个 SSE 消息块。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    """启动聊天任务并以 SSE 返回进度、回答、错误和结束事件。"""
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
                answer = await sessions.chat(
                    request.session_id,
                    request.message.strip(),
                    agent,
                    progress,
                    knowledge_base_id=request.knowledge_base_id,
                    allow_web=request.allow_web,
                )
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
