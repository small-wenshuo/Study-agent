from __future__ import annotations

import asyncio
import unittest

from app.services.sessions import SessionStore


class BlockingAgent:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, history, question, progress=None):
        self.started.set()
        await self.release.wait()
        return f"回答：{question}"


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_history_and_concurrent_guard(self):
        store = SessionStore()
        session_id = store.create()
        agent = BlockingAgent()
        first = asyncio.create_task(store.chat(session_id, "问题", agent))
        await agent.started.wait()
        with self.assertRaisesRegex(RuntimeError, "已有任务"):
            await store.chat(session_id, "另一个问题", agent)
        agent.release.set()
        self.assertEqual(await first, "回答：问题")
        self.assertEqual(len(store.get(session_id).messages), 2)

    async def test_missing_session(self):
        store = SessionStore()
        with self.assertRaises(KeyError):
            await store.chat("missing", "问题", BlockingAgent())


if __name__ == "__main__":
    unittest.main()
