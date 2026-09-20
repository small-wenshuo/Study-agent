from __future__ import annotations

import asyncio
import unittest

from langchain_core.messages import AIMessage

from app.agent.graph import StudyAgent


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.tools_enabled = []

    async def ainvoke(self, messages, tools_enabled):
        self.tools_enabled.append(tools_enabled)
        return self.responses.pop(0)


class FakeSearch:
    async def search(self, query, max_results=5):
        return [{"title": "Python Docs", "url": "https://docs.python.org/3/glossary.html", "snippet": "decorator"}]

    async def extract(self, urls):
        return [{"url": urls[0], "content": "A decorator is a function returning another function."}]


class RepeatingToolModel:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages, tools_enabled):
        self.calls += 1
        return tool_call("not_allowed", {}, f"c{self.calls}")


class SlowModel:
    async def ainvoke(self, messages, tools_enabled):
        await asyncio.sleep(1)
        return AIMessage(content="too late")


def tool_call(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_answer_does_not_need_tools(self):
        model = FakeModel([AIMessage(content="二分查找要求有序序列。")])
        agent = StudyAgent(model, None, task_timeout=5, tool_timeout=2)
        answer = await agent.run([], "什么是二分查找？")
        self.assertEqual(answer, "二分查找要求有序序列。")
        self.assertEqual(model.tools_enabled, [False])

    async def test_search_read_and_citation(self):
        model = FakeModel([
            tool_call("search_web", {"query": "Python decorator docs"}, "c1"),
            tool_call("read_pages", {"result_ids": ["R1"]}, "c2"),
            AIMessage(content="装饰器会包装可调用对象。[S1]"),
        ])
        events = []
        agent = StudyAgent(model, FakeSearch(), task_timeout=5, tool_timeout=2)
        answer = await agent.run([], "解释装饰器并给官方来源", lambda kind, text: events.append((kind, text)))
        self.assertIn("[S1]", answer)
        self.assertIn("https://docs.python.org", answer)
        self.assertTrue(any("搜索" in text for _, text in events))
        self.assertTrue(any("阅读" in text for _, text in events))

    async def test_unknown_result_is_returned_to_model(self):
        model = FakeModel([
            tool_call("read_pages", {"result_ids": ["R99"]}, "c1"),
            AIMessage(content="没有可验证来源，无法完成带来源的回答。"),
        ])
        agent = StudyAgent(model, FakeSearch(), task_timeout=5, tool_timeout=2)
        answer = await agent.run([], "请给来源")
        self.assertIn("无法", answer)

    async def test_invalid_citation_gets_one_repair(self):
        model = FakeModel([
            AIMessage(content="结论。[S9]"),
            AIMessage(content="当前没有已读来源，因此无法提供可验证引用。"),
        ])
        agent = StudyAgent(model, None, task_timeout=5, tool_timeout=2)
        answer = await agent.run([], "给我来源")
        self.assertNotIn("[S9]", answer)
        self.assertIn("未取得可验证的网页正文", answer)
        self.assertEqual(model.tools_enabled, [False, False])

    async def test_repeated_read_reuses_source_id(self):
        model = FakeModel([
            tool_call("search_web", {"query": "docs"}, "c1"),
            tool_call("read_pages", {"result_ids": ["R1"]}, "c2"),
            tool_call("read_pages", {"result_ids": ["R1"]}, "c3"),
            AIMessage(content="结论。[S1]"),
        ])
        agent = StudyAgent(model, FakeSearch(), task_timeout=5, tool_timeout=2)
        answer = await agent.run([], "查文档并给来源")
        self.assertIn("[S1]", answer)
        self.assertNotIn("[S2]", answer)

    async def test_repeating_tool_calls_are_stopped(self):
        model = RepeatingToolModel()
        agent = StudyAgent(model, FakeSearch(), max_tool_calls=2, task_timeout=5, tool_timeout=2)
        answer = await agent.run([], "解释一个基础概念")
        self.assertIn("步骤已达上限", answer)
        self.assertLessEqual(model.calls, 5)

    async def test_total_timeout_returns_clear_answer(self):
        agent = StudyAgent(SlowModel(), None, task_timeout=0.01, tool_timeout=2)
        answer = await agent.run([], "问题")
        self.assertIn("超过总时限", answer)


if __name__ == "__main__":
    unittest.main()
