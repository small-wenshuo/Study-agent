from __future__ import annotations

import json
import unittest

from langchain_core.messages import AIMessage

from app.agent.v2 import EvidenceAssessment, ModelKnowledgeModel, V2Assistant
from app.knowledge_base import RetrievalHit


class FakeKnowledgeBase:
    """返回可配置知识库证据的服务替身。"""

    def __init__(self, hits: list[RetrievalHit]) -> None:
        """保存固定检索结果并记录查询。"""
        self.hits = hits
        self.calls = []

    async def search(self, knowledge_base_id: str, query: str, limit: int = 8):
        """返回固定证据。"""
        self.calls.append((knowledge_base_id, query, limit))
        return self.hits


class FakeKnowledgeModel:
    """按指定证据等级生成带 K 引用的回答。"""

    def __init__(self, level: str) -> None:
        """保存证据等级并记录回答调用。"""
        self.level = level
        self.answer_calls = 0

    async def assess(self, question: str, hits: list[RetrievalHit]) -> EvidenceAssessment:
        """返回测试指定的结构化证据判定。"""
        return EvidenceAssessment(
            level=self.level,
            reason="资料覆盖不完整" if self.level != "sufficient" else "资料足够",
            supported_aspects=["定义"] if hits else [],
            missing_aspects=["示例"] if self.level != "sufficient" else [],
        )

    async def answer(self, question: str, hits: list[RetrievalHit], *, partial: bool) -> str:
        """生成引用首条知识库证据的固定回答。"""
        self.answer_calls += 1
        return "知识库中的结论。[K1]"


class FakeWebAgent:
    """记录联网分支并返回带网页来源的固定回答。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls = []

    async def run(self, history, question, progress=None, **kwargs):
        """模拟 V1 联网回答。"""
        self.calls.append(question)
        return "联网结论。[S1]\n\n来源：\n- [S1] [官方资料](https://example.com)"


class ScopeSensitiveModel:
    """只有提示词明确限制“缺口”范围时才判定证据充分。"""

    async def ainvoke(self, messages, tools_enabled):
        """模拟模型因追问无关信息而产生的过度保守判定。"""
        prompt = str(messages[-1].content)
        constrained = "不要把问题未要求的信息视为缺口" in prompt
        return AIMessage(
            content=json.dumps(
                {
                    "level": "sufficient" if constrained else "partial",
                    "reason": "问题要求已被证据覆盖" if constrained else "缺少未要求的格式信息",
                    "supported_aspects": ["问题要求"],
                    "missing_aspects": [] if constrained else ["格式信息"],
                },
                ensure_ascii=False,
            )
        )


def hit() -> RetrievalHit:
    """构造一条带文件定位的知识库证据。"""
    return RetrievalHit(
        source_id="K1",
        content="Python decorator wraps a function.",
        metadata={"filename": "course.pdf", "page": 7, "knowledge_base_id": "kb1"},
        score=0.91,
    )


class V2AssistantTests(unittest.IsolatedAsyncioTestCase):
    """验证知识库、超纲和联网组合的 Agent 公共行为。"""

    async def test_sufficient_evidence_returns_knowledge_answer_with_source(self):
        """资料充分时只回答知识库内容并附文件定位。"""
        web = FakeWebAgent()
        assistant = V2Assistant(web, FakeKnowledgeBase([hit()]), FakeKnowledgeModel("sufficient"))

        answer = await assistant.run([], "什么是装饰器？", knowledge_base_id="kb1")

        self.assertIn("[K1]", answer)
        self.assertIn("course.pdf，第 7 页", answer)
        self.assertEqual(web.calls, [])

    async def test_partial_evidence_explains_gap_before_web_search(self):
        """资料部分充分且未授权联网时，应回答已有部分并询问是否联网。"""
        web = FakeWebAgent()
        assistant = V2Assistant(web, FakeKnowledgeBase([hit()]), FakeKnowledgeModel("partial"))

        answer = await assistant.run([], "解释并举例", knowledge_base_id="kb1", allow_web=False)

        self.assertIn("知识库中的结论", answer)
        self.assertIn("缺少：示例", answer)
        self.assertIn("是否允许联网", answer)
        self.assertEqual(web.calls, [])

    async def test_insufficient_evidence_does_not_invent_answer(self):
        """资料不足且未授权联网时，不得调用回答模型编造知识。"""
        model = FakeKnowledgeModel("insufficient")
        assistant = V2Assistant(FakeWebAgent(), FakeKnowledgeBase([]), model)

        answer = await assistant.run([], "Rust 所有权是什么？", knowledge_base_id="kb1")

        self.assertIn("超出当前知识库范围", answer)
        self.assertIn("是否允许联网", answer)
        self.assertEqual(model.answer_calls, 0)

    async def test_user_consent_combines_knowledge_and_web_sources(self):
        """用户允许联网后，应明确分开知识库与联网补充。"""
        web = FakeWebAgent()
        assistant = V2Assistant(web, FakeKnowledgeBase([hit()]), FakeKnowledgeModel("partial"))

        answer = await assistant.run([], "解释并举例", knowledge_base_id="kb1", allow_web=True)

        self.assertIn("根据你的知识库", answer)
        self.assertIn("联网补充", answer)
        self.assertIn("[K1]", answer)
        self.assertIn("[S1]", answer)
        self.assertEqual(web.calls, ["解释并举例"])

    async def test_without_selected_knowledge_base_delegates_to_v1_agent(self):
        """未选择知识库时应保持 V1 直接回答和按需联网行为。"""
        web = FakeWebAgent()
        assistant = V2Assistant(web, FakeKnowledgeBase([hit()]), FakeKnowledgeModel("sufficient"))

        answer = await assistant.run([], "什么是二分查找？")

        self.assertIn("联网结论", answer)
        self.assertEqual(web.calls, ["什么是二分查找？"])

    async def test_evidence_assessment_only_considers_information_the_question_requests(self):
        """证据已覆盖问题时，不得因未提问的格式或元数据降级为部分充分。"""
        model = ModelKnowledgeModel(ScopeSensitiveModel())

        assessment = await model.assess("检索证据中出现了什么文本？", [hit()])

        self.assertEqual(assessment.level, "sufficient")
        self.assertEqual(assessment.missing_aspects, [])


if __name__ == "__main__":
    unittest.main()
