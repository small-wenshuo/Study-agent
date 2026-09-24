from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import app.main as main_module
from app.knowledge_base import ChromaVectorStore, KnowledgeBaseService
from app.parsers import ParserRegistry


class ApiEmbeddings:
    """为 HTTP 集成测试提供无需外部服务的固定向量。"""

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """按文本长度生成稳定二维向量。"""
        return [[float(len(text)), 1.0] for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        """为查询生成与文档同维度向量。"""
        return [float(len(text)), 1.0]


class ContextCapturingAgent:
    """记录 HTTP 聊天传入的 V2 上下文。"""

    def __init__(self) -> None:
        """初始化空调用记录。"""
        self.context = None

    async def run(self, history, question, progress=None, **kwargs):
        """保存知识库和联网选择并返回固定回答。"""
        self.context = (question, kwargs.get("knowledge_base_id"), kwargs.get("allow_web"))
        return "回答"


class V2ApiTests(unittest.IsolatedAsyncioTestCase):
    """通过真实 ASGI 路由验证知识库管理与上传接口。"""

    async def asyncSetUp(self) -> None:
        """将生产全局知识库替换为隔离的真实本地实现。"""
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = KnowledgeBaseService(
            root / "study.db",
            root / "uploads",
            ParserRegistry.default(),
            ApiEmbeddings(),
            ChromaVectorStore(root / "vectors"),
        )
        self.patch = patch.object(main_module, "knowledge_bases", self.service)
        self.patch.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app), base_url="http://test"
        )

    async def asyncTearDown(self) -> None:
        """关闭客户端与持久化句柄。"""
        await self.client.aclose()
        self.patch.stop()
        self.service.close()
        self.temp.cleanup()

    async def test_create_upload_list_and_delete_file(self):
        """用户应能通过 HTTP 完成建库、上传、查看和删除文件。"""
        created = await self.client.post("/api/knowledge-bases", json={"name": "Python 课程"})
        knowledge_base_id = created.json()["id"]

        uploaded = await self.client.post(
            f"/api/knowledge-bases/{knowledge_base_id}/files",
            files={"file": ("notes.txt", b"python decorator", "text/plain")},
        )
        listed = await self.client.get(f"/api/knowledge-bases/{knowledge_base_id}/files")
        deleted = await self.client.delete(
            f"/api/knowledge-bases/{knowledge_base_id}/files/{uploaded.json()['id']}"
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(uploaded.status_code, 201)
        self.assertEqual(uploaded.json()["status"], "ready")
        self.assertEqual(listed.json()[0]["filename"], "notes.txt")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual((await self.client.get(f"/api/knowledge-bases/{knowledge_base_id}/files")).json(), [])

    async def test_upload_rejects_unsupported_format(self):
        """HTTP 边界应把不支持格式转换为可读的 422 响应。"""
        created = await self.client.post("/api/knowledge-bases", json={"name": "课程"})

        response = await self.client.post(
            f"/api/knowledge-bases/{created.json()['id']}/files",
            files={"file": ("archive.zip", b"data", "application/zip")},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("不支持", response.json()["detail"])

    async def test_chat_forwards_selected_knowledge_base_and_web_consent(self):
        """聊天接口应把当前知识库和联网授权交给 V2 Agent。"""
        agent = ContextCapturingAgent()
        session_id = (await self.client.post("/api/sessions")).json()["session_id"]

        with patch.object(main_module, "agent", agent):
            response = await self.client.post(
                "/api/chat",
                json={
                    "session_id": session_id,
                    "message": "解释资料",
                    "knowledge_base_id": "kb-selected",
                    "allow_web": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: answer", response.text)
        self.assertEqual(agent.context, ("解释资料", "kb-selected", True))


if __name__ == "__main__":
    unittest.main()
