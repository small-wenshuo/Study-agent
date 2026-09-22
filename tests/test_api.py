from __future__ import annotations

import unittest

import httpx

from app.main import app


class ApiTests(unittest.IsolatedAsyncioTestCase):
    """对无需外部凭据的 HTTP 接口执行冒烟测试。"""

    async def asyncSetUp(self):
        """为每个测试创建进程内 ASGI 客户端。"""
        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")

    async def asyncTearDown(self):
        """测试结束后关闭 HTTP 客户端。"""
        await self.client.aclose()

    async def test_health_never_exposes_secrets(self):
        """健康接口只能暴露就绪状态和缺失字段名。"""
        response = await self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"model_ready", "search_ready", "missing_model_fields"})

    async def test_create_session(self):
        """创建会话接口应返回非空会话 ID。"""
        response = await self.client.post("/api/sessions")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["session_id"])

    async def test_homepage_loads(self):
        """根路径应返回聊天网页。"""
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Study Agent", response.text)

    async def test_blank_message_is_rejected(self):
        """聊天接口应在执行 Agent 前拒绝空白消息。"""
        response = await self.client.post("/api/chat", json={"session_id": "anything", "message": "   "})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
