from __future__ import annotations

import asyncio
import unittest

from app.services.sessions import SessionStore


class BlockingAgent:
    """暂停执行以便测试并发锁的 Agent 替身。"""

    def __init__(self):
        """创建开始与释放两个同步事件。"""
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, history, question, progress=None):
        """标记任务开始并等待测试主动释放。"""
        self.started.set()
        await self.release.wait()
        return f"回答：{question}"


class SessionTests(unittest.IsolatedAsyncioTestCase):
    """验证会话历史和单会话并发保护。"""

    async def test_session_history_and_concurrent_guard(self):
        """同一会话不能并发运行，成功后应保存问答。"""
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
        """未知会话 ID 应立即抛出 KeyError。"""
        store = SessionStore()
        with self.assertRaises(KeyError):
            await store.chat("missing", "问题", BlockingAgent())


if __name__ == "__main__":
    unittest.main()
