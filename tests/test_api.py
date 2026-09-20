from __future__ import annotations

import unittest

import httpx

from app.main import app


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_health_never_exposes_secrets(self):
        response = await self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"model_ready", "search_ready", "missing_model_fields"})

    async def test_create_session(self):
        response = await self.client.post("/api/sessions")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["session_id"])

    async def test_homepage_loads(self):
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Study Agent", response.text)

    async def test_blank_message_is_rejected(self):
        response = await self.client.post("/api/chat", json={"session_id": "anything", "message": "   "})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
