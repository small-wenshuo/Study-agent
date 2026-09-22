from __future__ import annotations

import unittest

from app.tools import READ_PAGES_SCHEMA, SEARCH_WEB_SCHEMA, ReadPagesTool, SearchWebTool, ToolContext


async def ignore_status(kind: str, message: str) -> None:
    """在工具单元测试中丢弃进度事件。"""


class FakeProvider:
    """记录调用并返回可配置结果的外部搜索 adapter。"""

    def __init__(self) -> None:
        """初始化调用记录和默认供应商响应。"""
        self.search_calls = []
        self.extract_calls = []
        self.search_results = [
            {"title": "Docs", "url": "https://example.com/docs", "snippet": "summary"}
        ]
        self.extract_results = [
            {"url": "https://example.com/docs", "content": "verified content"}
        ]

    async def search(self, query: str, max_results: int = 5):
        """记录搜索参数并返回预设结果。"""
        self.search_calls.append((query, max_results))
        return self.search_results

    async def extract(self, urls: list[str]):
        """记录阅读 URL 并返回预设正文。"""
        self.extract_calls.append(urls)
        return self.extract_results


class ToolTests(unittest.IsolatedAsyncioTestCase):
    """通过统一工具 interface 验证搜索与阅读行为。"""

    def setUp(self) -> None:
        """为每个测试准备空的证据上下文和供应商 adapter。"""
        self.provider = FakeProvider()
        self.context = ToolContext(search_results={}, sources={}, search_rounds=0)

    async def test_search_schema_and_result_registration(self):
        """搜索工具应暴露正确 Schema 并登记去重后的 R 编号。"""
        tool = SearchWebTool(self.provider, max_rounds=2, timeout=1, emit_status=ignore_status)
        result = await tool.execute({"query": " docs "}, self.context)
        self.context.read_calls = 1
        duplicate = await tool.execute({"query": "docs again"}, self.context)

        self.assertEqual(SEARCH_WEB_SCHEMA["function"]["name"], "search_web")
        self.assertTrue(result["ok"])
        self.assertEqual(self.context.search_results["R1"]["url"], "https://example.com/docs")
        self.assertEqual(duplicate["results"], [])
        self.assertEqual(self.context.search_rounds, 2)

    async def test_search_validates_arguments_and_budget(self):
        """无效搜索词和耗尽预算都不应调用供应商。"""
        tool = SearchWebTool(self.provider, max_rounds=0, timeout=1, emit_status=ignore_status)
        invalid = await tool.execute({"query": ""}, self.context)
        exhausted = await tool.execute({"query": "docs"}, self.context)

        self.assertEqual(invalid["error_type"], "invalid_arguments")
        self.assertEqual(exhausted["error_type"], "budget_exhausted")
        self.assertEqual(self.provider.search_calls, [])

    async def test_search_requires_reading_existing_candidates_first(self):
        """已有候选结果但没有正文时，应先阅读而不是连续搜索。"""
        self.context.search_results["R1"] = {
            "result_id": "R1",
            "title": "Docs",
            "url": "https://example.com/docs",
            "snippet": "summary",
        }
        tool = SearchWebTool(self.provider, max_rounds=3, timeout=1, emit_status=ignore_status)
        result = await tool.execute({"query": "another query"}, self.context)

        self.assertEqual(result["error_type"], "read_required")
        self.assertEqual(self.provider.search_calls, [])

    async def test_reader_rejects_unknown_result(self):
        """阅读工具不能绕过搜索结果注册表访问任意 URL。"""
        tool = ReadPagesTool(self.provider, timeout=1, emit_status=ignore_status)
        result = await tool.execute({"result_ids": ["R99"]}, self.context)

        self.assertEqual(READ_PAGES_SCHEMA["function"]["name"], "read_pages")
        self.assertEqual(result["error_type"], "unknown_result")
        self.assertEqual(self.provider.extract_calls, [])

    async def test_reader_rejects_duplicate_result_ids(self):
        """运行时校验应拒绝绕过 Schema 传入的重复结果编号。"""
        self.context.search_results["R1"] = {
            "result_id": "R1",
            "title": "Docs",
            "url": "https://example.com/docs",
            "snippet": "summary",
        }
        tool = ReadPagesTool(self.provider, timeout=1, emit_status=ignore_status)
        result = await tool.execute({"result_ids": ["R1", "R1"]}, self.context)

        self.assertEqual(result["error_type"], "invalid_arguments")
        self.assertEqual(self.provider.extract_calls, [])

    async def test_reader_registers_and_reuses_source(self):
        """成功正文应登记为 S 编号，重复阅读应复用该来源。"""
        self.context.search_results["R1"] = {
            "result_id": "R1",
            "title": "Docs",
            "url": "https://example.com/docs",
            "snippet": "summary",
        }
        tool = ReadPagesTool(self.provider, timeout=1, emit_status=ignore_status)
        first = await tool.execute({"result_ids": ["R1"]}, self.context)
        second = await tool.execute({"result_ids": ["R1"]}, self.context)

        self.assertTrue(first["ok"])
        self.assertEqual(first["documents"][0]["source_id"], "S1")
        self.assertEqual(second["documents"][0]["source_id"], "S1")
        self.assertEqual(list(self.context.sources), ["S1"])


if __name__ == "__main__":
    unittest.main()
