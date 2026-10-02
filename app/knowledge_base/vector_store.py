from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import chromadb


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """写入向量库的一段正文、向量和来源信息。"""

    id: str
    content: str
    embedding: list[float]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VectorMatch:
    """向量库返回的正文、来源和归一化相关度。"""

    id: str
    content: str
    metadata: dict[str, Any]
    score: float


class VectorStore(Protocol):
    """知识库服务依赖的向量存储接口。"""

    def upsert(self, records: list[VectorRecord]) -> None:
        """新增或替换一批 Chunk。"""
        ...

    def replace_file(self, file_id: str, records: list[VectorRecord]) -> None:
        """新向量写入成功后再删除同一文件的旧 Chunk。"""
        ...

    def search(self, embedding: list[float], knowledge_base_id: str, limit: int,
               *, file_id: str | None = None) -> list[VectorMatch]:
        """在指定知识库内检索最相关 Chunk。"""
        ...

    def delete_file(self, file_id: str) -> None:
        """删除一个文件的全部 Chunk。"""
        ...

    def delete_knowledge_base(self, knowledge_base_id: str) -> None:
        """删除一个知识库的全部 Chunk。"""
        ...

    def close(self) -> None:
        """释放持久化向量库资源。"""
        ...


class ChromaVectorStore:
    """使用本地持久化 Chroma 实现向量数据库。"""

    def __init__(self, path: Path) -> None:
        """在指定目录创建余弦距离集合。"""
        path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(path))
        self._collection = self._client.get_or_create_collection(
            name="study_agent_chunks",
            metadata={"hnsw:space": "cosine"},
        )

    @staticmethod
    def _clean_metadata(metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
        """移除 Chroma 不接受的空值并限制字段类型。"""
        clean: dict[str, str | int | float | bool] = {}
        for key, value in metadata.items():
            if value is None:
                continue
            clean[key] = value if isinstance(value, (str, int, float, bool)) else str(value)
        return clean

    def upsert(self, records: list[VectorRecord]) -> None:
        """批量写入文本、向量和可过滤来源字段。"""
        if not records:
            return
        self._collection.upsert(
            ids=[record.id for record in records],
            documents=[record.content for record in records],
            embeddings=[record.embedding for record in records],
            metadatas=[self._clean_metadata(record.metadata) for record in records],
        )

    def replace_file(self, file_id: str, records: list[VectorRecord]) -> None:
        """先写新 Chunk，再按旧 ID 清理，避免失败时丢失原索引。"""
        existing = self._collection.get(where={"file_id": file_id}, include=[]).get("ids", [])
        self.upsert(records)
        if existing:
            self._collection.delete(ids=[str(item) for item in existing])

    def search(self, embedding: list[float], knowledge_base_id: str, limit: int,
               *, file_id: str | None = None) -> list[VectorMatch]:
        """用知识库过滤条件执行余弦向量检索。"""
        count = self._collection.count()
        if not count:
            return []
        result = self._collection.query(
            query_embeddings=[embedding],
            n_results=min(max(1, limit), count),
            where=({"$and": [{"knowledge_base_id": knowledge_base_id}, {"file_id": file_id}]}
                   if file_id is not None else {"knowledge_base_id": knowledge_base_id}),
            include=["documents", "metadatas", "distances"],
        )
        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        return [
            VectorMatch(
                id=str(record_id),
                content=str(content or ""),
                metadata=dict(metadata or {}),
                score=max(0.0, min(1.0, 1.0 - float(distance))),
            )
            for record_id, content, metadata, distance in zip(ids, documents, metadatas, distances, strict=True)
        ]

    def delete_file(self, file_id: str) -> None:
        """按来源文件过滤删除向量。"""
        self._collection.delete(where={"file_id": file_id})

    def delete_knowledge_base(self, knowledge_base_id: str) -> None:
        """按知识库过滤删除向量。"""
        self._collection.delete(where={"knowledge_base_id": knowledge_base_id})

    def close(self) -> None:
        """释放 Chroma 在 Windows 上持有的文件句柄。"""
        self._client.close()
