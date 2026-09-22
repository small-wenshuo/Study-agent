from __future__ import annotations

import asyncio
import unittest

from langchain_core.messages import AIMessage

from app.agent.graph import StudyAgent


class FakeModel:
    """按预设顺序返回响应的模型测试替身。"""

    def __init__(self, responses):
        """保存待返回响应并记录每次工具开关。"""
        self.responses = list(responses)
        self.tools_enabled = []

    async def ainvoke(self, messages, tools_enabled):
        """返回下一个预设模型响应。"""
        self.tools_enabled.append(tools_enabled)
        return self.responses.pop(0)


class FakeSearch:
    """返回固定搜索结果和正文的搜索测试替身。"""

    async def search(self, query, max_results=5):
        """返回一条固定的 Python 官方文档结果。"""
        return [{"title": "Python Docs", "url": "https://docs.python.org/3/glossary.html", "snippet": "decorator"}]

    async def extract(self, urls):
        """为请求 URL 返回固定正文。"""
        return [{"url": urls[0], "content": "A decorator is a function returning another function."}]


class RepeatingToolModel:
    """持续请求未知工具，用来验证循环上限。"""

    def __init__(self):
        """初始化模型调用计数。"""
        self.calls = 0

    async def ainvoke(self, messages, tools_enabled):
        """每次都返回一个新的未知工具调用。"""
        self.calls += 1
        return tool_call("not_allowed", {}, f"c{self.calls}")


class SlowModel:
    """故意延迟响应，用来验证任务总超时。"""

    async def ainvoke(self, messages, tools_enabled):
        """等待一秒后返回一条不会及时到达的响应。"""
        await asyncio.sleep(1)
        return AIMessage(content="too late")


class SlowThenFastModel:
    """第一次超时、第二次成功，用来验证有限重试。"""

    def __init__(self):
        """初始化调用计数。"""
        self.calls = 0

    async def ainvoke(self, messages, tools_enabled):
        """首次延迟，重试时立即返回回答。"""
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(1)
        return AIMessage(content="重试后成功回答。")


class SearchReadThenSlowModel:
    """先完成搜索和阅读调用，再持续超时。"""

    def __init__(self):
        """准备搜索与阅读两条工具消息。"""
        self.responses = [
            tool_call("search_web", {"query": "official docs"}, "c1"),
            tool_call("read_pages", {"result_ids": ["R1"]}, "c2"),
        ]

    async def ainvoke(self, messages, tools_enabled):
        """先返回预设工具调用，之后模拟模型持续无响应。"""
        if self.responses:
            return self.responses.pop(0)
        await asyncio.sleep(1)
        return AIMessage(content="too late")


def tool_call(name, args, call_id):
    """构造 LangChain 使用的标准工具调用消息。"""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


class AgentTests(unittest.IsolatedAsyncioTestCase):
    """验证 Agent 的主要分支、限制和引用行为。"""

    async def test_direct_answer_does_not_need_tools(self):
        """未配置搜索时仍能完成稳定知识问答。"""
        model = FakeModel([AIMessage(content="二分查找要求有序序列。")])
        agent = StudyAgent(model, None, task_timeout=5, tool_timeout=2)
        answer = await agent.run([], "什么是二分查找？")
        self.assertEqual(answer, "二分查找要求有序序列。")
        self.assertEqual(model.tools_enabled, [False])

    async def test_search_read_and_citation(self):
        """来源型问题应依次搜索、阅读并输出来源链接。"""
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
        """未知 R 编号应作为工具错误回填给模型。"""
        model = FakeModel([
            tool_call("read_pages", {"result_ids": ["R99"]}, "c1"),
            AIMessage(content="没有可验证来源，无法完成带来源的回答。"),
        ])
        agent = StudyAgent(model, FakeSearch(), task_timeout=5, tool_timeout=2)
        answer = await agent.run([], "请给来源")
        self.assertIn("无法", answer)

    async def test_invalid_citation_gets_one_repair(self):
        """不存在的 S 编号应触发一次无工具引用修复。"""
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
        """重复阅读同一 URL 不应创建新的来源编号。"""
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
        """持续工具调用必须被 Agent 步骤上限终止。"""
        model = RepeatingToolModel()
        agent = StudyAgent(model, FakeSearch(), max_tool_calls=2, task_timeout=5, tool_timeout=2)
        answer = await agent.run([], "解释一个基础概念")
        self.assertIn("步骤已达上限", answer)
        self.assertLessEqual(model.calls, 5)

    async def test_total_timeout_returns_clear_answer(self):
        """超过任务总时限时应返回明确的停止说明。"""
        agent = StudyAgent(SlowModel(), None, task_timeout=0.01, model_timeout=2)
        answer = await agent.run([], "问题")
        self.assertIn("超过总时限", answer)

    async def test_model_timeout_is_retried_once(self):
        """单次模型超时应重试一次，而不是终止整个任务。"""
        model = SlowThenFastModel()
        agent = StudyAgent(model, None, task_timeout=1, model_timeout=0.01, model_max_retries=1)
        answer = await agent.run([], "问题")
        self.assertEqual(answer, "重试后成功回答。")
        self.assertEqual(model.calls, 2)

    async def test_model_timeout_is_not_reported_as_total_timeout(self):
        """模型重试耗尽应报告单次调用超时，而非任务总超时。"""
        agent = StudyAgent(SlowModel(), None, task_timeout=1, model_timeout=0.01, model_max_retries=0)
        answer = await agent.run([], "问题")
        self.assertIn("模型响应超时", answer)
        self.assertNotIn("超过总时限", answer)

    async def test_model_timeout_preserves_read_sources(self):
        """最终生成超时时应返回已读来源，而不是丢弃证据。"""
        agent = StudyAgent(
            SearchReadThenSlowModel(),
            FakeSearch(),
            task_timeout=1,
            model_timeout=0.01,
            model_max_retries=0,
            tool_timeout=1,
        )
        answer = await agent.run([], "查文档并给来源")
        self.assertIn("模型响应超时", answer)
        self.assertIn("[S1]", answer)
        self.assertIn("https://docs.python.org", answer)


if __name__ == "__main__":
    unittest.main()
