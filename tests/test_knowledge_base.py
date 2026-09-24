from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.knowledge_base import ChromaVectorStore, KnowledgeBaseService
from app.parsers import ParserRegistry


class KeywordEmbeddings:
    """为知识库公共接口测试提供稳定、可解释的向量。"""

    _terms = ("python", "decorator", "database", "index")

    def __init__(self) -> None:
        """允许测试模拟一次暂时性向量化失败。"""
        self.fail_documents_once = False

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """将已知关键词计数映射到固定维度。"""
        if self.fail_documents_once:
            self.fail_documents_once = False
            raise RuntimeError("temporary embedding failure")
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        """使用与文档相同的可重复映射处理查询。"""
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        lowered = text.lower()
        values = [float(lowered.count(term)) for term in self._terms]
        return values if any(values) else [0.01, 0.01, 0.01, 0.01]


class KnowledgeBaseServiceTests(unittest.IsolatedAsyncioTestCase):
    """通过知识库服务公共接口验证持久化、检索与清理。"""

    async def asyncSetUp(self) -> None:
        """为每个测试创建隔离的 SQLite、原文件目录和 Chroma。"""
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.embeddings = KeywordEmbeddings()
        self.service = KnowledgeBaseService(
            database_path=root / "study.db",
            storage_dir=root / "uploads",
            parsers=ParserRegistry.default(),
            embeddings=self.embeddings,
            vectors=ChromaVectorStore(root / "vectors"),
            max_file_bytes=1024 * 1024,
        )

    async def asyncTearDown(self) -> None:
        """关闭数据库句柄后删除测试目录。"""
        self.service.close()
        self.temp.cleanup()

    async def test_user_can_ingest_and_search_multiple_files(self):
        """同一知识库中的多个文件应统一进入向量检索并保留来源。"""
        knowledge_base = self.service.create_knowledge_base("课程")
        first = await self.service.ingest_file(
            knowledge_base["id"], "python.txt", "text/plain", b"Python decorator wraps a function."
        )
        second = await self.service.ingest_file(
            knowledge_base["id"], "database.md", "text/markdown", b"# Index\nDatabase index speeds up queries."
        )

        python_hits = await self.service.search(knowledge_base["id"], "python decorator", limit=3)
        database_hits = await self.service.search(knowledge_base["id"], "database index", limit=3)

        self.assertEqual(first["status"], "ready")
        self.assertEqual(second["status"], "ready")
        self.assertEqual(python_hits[0].metadata["filename"], "python.txt")
        self.assertEqual(database_hits[0].metadata["filename"], "database.md")
        self.assertEqual(python_hits[0].source_id, "K1")
        self.assertGreater(python_hits[0].score, 0.5)

    async def test_search_is_isolated_by_knowledge_base(self):
        """检索不得返回其他知识库的 Chunk。"""
        python_kb = self.service.create_knowledge_base("Python")
        database_kb = self.service.create_knowledge_base("Database")
        await self.service.ingest_file(python_kb["id"], "python.txt", "text/plain", b"python decorator")
        await self.service.ingest_file(database_kb["id"], "db.txt", "text/plain", b"database index")

        hits = await self.service.search(python_kb["id"], "database index", limit=5)

        self.assertTrue(hits)
        self.assertTrue(all(hit.metadata["knowledge_base_id"] == python_kb["id"] for hit in hits))
        self.assertNotIn("database", " ".join(hit.content.lower() for hit in hits))

    async def test_delete_file_removes_metadata_source_and_vectors(self):
        """删除文件后列表、原文件和向量检索都不应留下残余。"""
        knowledge_base = self.service.create_knowledge_base("课程")
        file_record = await self.service.ingest_file(
            knowledge_base["id"], "python.txt", "text/plain", b"python decorator"
        )

        self.service.delete_file(knowledge_base["id"], file_record["id"])

        self.assertEqual(self.service.list_files(knowledge_base["id"]), [])
        self.assertEqual(await self.service.search(knowledge_base["id"], "python decorator"), [])

    async def test_invalid_upload_is_recorded_as_failed(self):
        """解析失败应留下可重试的失败记录，而不是伪装成成功。"""
        knowledge_base = self.service.create_knowledge_base("课程")

        with self.assertRaisesRegex(ValueError, "没有可提取"):
            await self.service.ingest_file(knowledge_base["id"], "empty.txt", "text/plain", b"   ")

        files = self.service.list_files(knowledge_base["id"])
        self.assertEqual(files[0]["status"], "failed")
        self.assertIn("没有可提取", files[0]["error"])

    async def test_failed_file_can_be_retried_from_saved_original(self):
        """暂时性处理失败后应能复用原文件重建索引。"""
        knowledge_base = self.service.create_knowledge_base("课程")
        self.embeddings.fail_documents_once = True

        with self.assertRaisesRegex(ValueError, "文件处理失败"):
            await self.service.ingest_file(
                knowledge_base["id"], "python.txt", "text/plain", b"python decorator"
            )
        failed = self.service.list_files(knowledge_base["id"])[0]

        retried = await self.service.retry_file(knowledge_base["id"], str(failed["id"]))

        self.assertEqual(retried["status"], "ready")
        self.assertTrue(await self.service.search(knowledge_base["id"], "python decorator"))


if __name__ == "__main__":
    unittest.main()
