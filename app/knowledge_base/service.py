from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.knowledge_base.vector_store import VectorRecord, VectorStore
from app.parsers import Document, ParseError, ParserRegistry
from app.retrieval import HybridReranker


logger = logging.getLogger(__name__)


class EmbeddingProvider(Protocol):
    """知识库服务依赖的文本向量化接口。"""

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """向量化一批文档。"""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """向量化单个检索问题。"""
        ...


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """返回给 Agent 的知识库证据。"""

    source_id: str
    content: str
    metadata: dict[str, object]
    score: float


class KnowledgeBaseService:
    """管理知识库、原文件、解析状态和向量索引。"""

    _mime_types = {
        ".pdf": {"application/pdf"},
        ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        ".md": {"text/markdown", "text/plain"},
        ".txt": {"text/plain"},
        ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
        ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        ".csv": {"text/csv", "application/csv", "text/plain"},
    }

    def __init__(
        self,
        database_path: Path,
        storage_dir: Path,
        parsers: ParserRegistry,
        embeddings: EmbeddingProvider,
        vectors: VectorStore,
        *,
        max_file_bytes: int = 20 * 1024 * 1024,
        chunk_size: int = 1200,
        chunk_overlap: int = 150,
        reranker: HybridReranker | None = None,
        processing_timeout: int = 120,
    ) -> None:
        """初始化本地元数据、原文件目录及可替换向量组件。"""
        database_path.parent.mkdir(parents=True, exist_ok=True)
        storage_dir.mkdir(parents=True, exist_ok=True)
        self._storage_dir = storage_dir.resolve()
        self._parsers = parsers
        self._embeddings = embeddings
        self._vectors = vectors
        self._max_file_bytes = max_file_bytes
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._reranker = reranker or HybridReranker()
        self._processing_timeout = processing_timeout
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        """创建知识库和文件状态表。"""
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS files_knowledge_base_idx ON files(knowledge_base_id);
                """
            )

    @staticmethod
    def _now() -> str:
        """生成可排序的 UTC 时间。"""
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _public_file(row: sqlite3.Row) -> dict[str, object]:
        """移除内部存储路径后返回安全文件信息。"""
        return {
            "id": row["id"],
            "knowledge_base_id": row["knowledge_base_id"],
            "filename": row["filename"],
            "file_type": row["file_type"],
            "status": row["status"],
            "error": row["error"],
            "chunk_count": row["chunk_count"],
            "created_at": row["created_at"],
        }

    def create_knowledge_base(self, name: str) -> dict[str, object]:
        """创建命名知识库并返回公开字段。"""
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 100:
            raise ValueError("知识库名称长度必须为 1 到 100 个字符")
        record = {"id": str(uuid4()), "name": clean_name, "created_at": self._now()}
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO knowledge_bases(id, name, created_at) VALUES (?, ?, ?)",
                (record["id"], record["name"], record["created_at"]),
            )
        return record

    def list_knowledge_bases(self) -> list[dict[str, object]]:
        """按创建时间列出知识库及文件数量。"""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT kb.id, kb.name, kb.created_at, COUNT(f.id) AS file_count,
                       COALESCE(SUM(f.chunk_count), 0) AS chunk_count
                FROM knowledge_bases kb LEFT JOIN files f ON f.knowledge_base_id = kb.id
                GROUP BY kb.id ORDER BY kb.created_at
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _require_knowledge_base(self, knowledge_base_id: str) -> None:
        """确认知识库存在，防止文件写入孤立空间。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM knowledge_bases WHERE id = ?", (knowledge_base_id,)
            ).fetchone()
        if row is None:
            raise KeyError("知识库不存在")

    def list_files(self, knowledge_base_id: str) -> list[dict[str, object]]:
        """列出指定知识库的上传与解析状态。"""
        self._require_knowledge_base(knowledge_base_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM files WHERE knowledge_base_id = ? ORDER BY created_at", (knowledge_base_id,)
            ).fetchall()
        return [self._public_file(row) for row in rows]

    def _validate_upload(self, filename: str, content_type: str, data: bytes) -> str:
        """校验扩展名、MIME 和大小，拒绝不一致上传。"""
        extension = Path(filename).suffix.lower()
        if extension not in self._parsers.supported_extensions:
            raise ValueError(f"不支持的文件格式：{extension or '无扩展名'}")
        if not data:
            raise ValueError("文件为空")
        if len(data) > self._max_file_bytes:
            raise ValueError(f"文件不能超过 {self._max_file_bytes // (1024 * 1024)} MB")
        allowed = self._mime_types[extension]
        if content_type and content_type != "application/octet-stream" and content_type not in allowed:
            raise ValueError("文件扩展名与 MIME 类型不一致")
        return extension

    def _chunk_documents(self, documents: list[Document], file_id: str, knowledge_base_id: str) -> list[Document]:
        """按字符上限切分正文，并在每段复制完整来源信息。"""
        chunks: list[Document] = []
        step = max(1, self._chunk_size - self._chunk_overlap)
        for document in documents:
            content = document.content.strip()
            for start in range(0, len(content), step):
                text = content[start : start + self._chunk_size].strip()
                if not text:
                    continue
                metadata = {
                    **document.metadata,
                    "knowledge_base_id": knowledge_base_id,
                    "file_id": file_id,
                    "chunk_index": len(chunks),
                }
                chunks.append(Document(text, metadata))
                if start + self._chunk_size >= len(content):
                    break
        return chunks

    async def ingest_file(
        self, knowledge_base_id: str, filename: str, content_type: str, data: bytes
    ) -> dict[str, object]:
        """保存原文件、解析切分、向量化并写入 Chroma。"""
        self._require_knowledge_base(knowledge_base_id)
        safe_name = Path(filename).name
        extension = self._validate_upload(safe_name, content_type, data)
        file_id = str(uuid4())
        stored_path = self._storage_dir / f"{file_id}{extension}"
        stored_path.write_bytes(data)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO files(id, knowledge_base_id, filename, stored_path, file_type, content_type,
                                  status, error, chunk_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'processing', '', 0, ?)
                """,
                (file_id, knowledge_base_id, safe_name, str(stored_path), extension.lstrip("."), content_type, self._now()),
            )
        return await self._process_file(file_id, knowledge_base_id, safe_name, data)

    async def _process_file(
        self, file_id: str, knowledge_base_id: str, safe_name: str, data: bytes
    ) -> dict[str, object]:
        """执行可供首次上传和失败重试复用的解析、向量化与状态更新。"""
        try:
            documents = await asyncio.wait_for(
                asyncio.to_thread(self._parsers.parse, safe_name, data), timeout=self._processing_timeout
            )
            chunks = self._chunk_documents(documents, file_id, knowledge_base_id)
            try:
                embeddings = await asyncio.wait_for(
                    self._embeddings.embed_documents([chunk.content for chunk in chunks]),
                    timeout=self._processing_timeout,
                )
                if len(embeddings) != len(chunks):
                    raise RuntimeError("Embedding 返回数量与 Chunk 不一致")
            except Exception as exc:
                # 对用户返回稳定提示，同时在日志中保留第三方服务的原始错误，且不记录密钥。
                logger.exception(
                    "embedding_generation_failed file_id=%s error_type=%s error=%s",
                    file_id,
                    type(exc).__name__,
                    exc,
                )
                raise ValueError("向量生成失败，请检查 Embedding 服务配置") from exc
            records = [
                VectorRecord(str(uuid4()), chunk.content, embedding, chunk.metadata)
                for chunk, embedding in zip(chunks, embeddings, strict=True)
            ]
            await asyncio.wait_for(
                asyncio.to_thread(self._vectors.replace_file, file_id, records), timeout=self._processing_timeout
            )
            with self._lock, self._connection:
                self._connection.execute(
                    "UPDATE files SET status = 'ready', chunk_count = ?, error = '' WHERE id = ?",
                    (len(chunks), file_id),
                )
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ParseError, ValueError)) else "文件处理失败"
            if not isinstance(exc, (ParseError, ValueError)):
                logger.exception(
                    "knowledge_file_processing_failed file_id=%s error_type=%s error=%s",
                    file_id,
                    type(exc).__name__,
                    exc,
                )
            with self._lock, self._connection:
                self._connection.execute("UPDATE files SET status = 'failed', error = ? WHERE id = ?", (message, file_id))
            raise ValueError(message) from exc
        with self._lock:
            row = self._connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        assert row is not None
        return self._public_file(row)

    async def retry_file(self, knowledge_base_id: str, file_id: str) -> dict[str, object]:
        """使用已保存原文件重新执行失败或过期的解析与索引。"""
        self._require_knowledge_base(knowledge_base_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM files WHERE id = ? AND knowledge_base_id = ?", (file_id, knowledge_base_id)
            ).fetchone()
        if row is None:
            raise KeyError("文件不存在")
        path = Path(row["stored_path"]).resolve()
        if path.parent != self._storage_dir or not path.exists():
            raise ValueError("原文件不存在，无法重新解析")
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE files SET status = 'processing', error = '', chunk_count = 0 WHERE id = ?", (file_id,)
            )
        data = await asyncio.to_thread(path.read_bytes)
        return await self._process_file(file_id, knowledge_base_id, str(row["filename"]), data)

    async def search(self, knowledge_base_id: str, query: str, limit: int = 8) -> list[RetrievalHit]:
        """向量化问题并在单一知识库内检索证据。"""
        self._require_knowledge_base(knowledge_base_id)
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("检索问题不能为空")
        embedding = await asyncio.wait_for(
            self._embeddings.embed_query(clean_query), timeout=self._processing_timeout
        )
        candidates = await asyncio.wait_for(
            asyncio.to_thread(self._vectors.search, embedding, knowledge_base_id, max(limit * 2, limit)),
            timeout=self._processing_timeout,
        )
        matches = self._reranker.rerank(clean_query, candidates, limit)
        return [
            RetrievalHit(f"K{index}", match.content, match.metadata, match.score)
            for index, match in enumerate(matches, start=1)
        ]

    def delete_file(self, knowledge_base_id: str, file_id: str) -> None:
        """删除文件记录、原文件和全部向量。"""
        self._require_knowledge_base(knowledge_base_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT stored_path FROM files WHERE id = ? AND knowledge_base_id = ?", (file_id, knowledge_base_id)
            ).fetchone()
        if row is None:
            raise KeyError("文件不存在")
        self._vectors.delete_file(file_id)
        path = Path(row["stored_path"]).resolve()
        if path.parent == self._storage_dir and path.exists():
            path.unlink()
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM files WHERE id = ?", (file_id,))

    def delete_knowledge_base(self, knowledge_base_id: str) -> None:
        """删除知识库下所有原文件、元数据和向量。"""
        self._require_knowledge_base(knowledge_base_id)
        for file in list(self.list_files(knowledge_base_id)):
            self.delete_file(knowledge_base_id, str(file["id"]))
        self._vectors.delete_knowledge_base(knowledge_base_id)
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM knowledge_bases WHERE id = ?", (knowledge_base_id,))

    def close(self) -> None:
        """关闭 SQLite 与向量库连接，供测试和进程退出清理。"""
        with self._lock:
            self._connection.close()
        self._vectors.close()
