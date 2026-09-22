from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from app.agent.prompts import REPAIR_PROMPT, SYSTEM_PROMPT
from app.agent.state import AgentState
from app.providers.model import ModelClient
from app.providers.tavily import SearchProvider
from app.services.citations import append_source_links, validate_citations
from app.tools import AgentTool, ReadPagesTool, SearchWebTool, ToolContext


ProgressCallback = Callable[[str, str], Awaitable[None] | None]
logger = logging.getLogger(__name__)


def needs_sources(text: str) -> bool:
    """根据用户措辞判断本轮是否明确需要联网来源。"""
    markers = ("来源", "引用", "链接", "文档", "最新", "现在", "当前", "官网", "查一下", "搜索")
    return any(marker in text for marker in markers)


class StudyAgent:
    """编排模型决策、工具执行和引用校验的 LangGraph Agent。"""

    def __init__(
        self,
        model: ModelClient,
        search_provider: SearchProvider | None,
        *,
        max_search_rounds: int = 3,
        max_tool_calls: int = 12,
        task_timeout: int = 180,
        tool_timeout: int = 30,
    ) -> None:
        """注入外部服务并设置每次任务的执行预算。"""
        self.model = model
        self.search_provider = search_provider
        self.max_tool_calls = max_tool_calls
        self.task_timeout = task_timeout
        self.tool_timeout = tool_timeout
        # A single agent instance serves many sessions. ContextVar keeps progress
        # callbacks task-local when two sessions run concurrently.
        self._progress: ContextVar[ProgressCallback | None] = ContextVar("progress", default=None)
        tools: list[AgentTool] = [
            SearchWebTool(
                search_provider,
                max_rounds=max_search_rounds,
                timeout=tool_timeout,
                emit_status=self._emit,
            ),
            ReadPagesTool(search_provider, timeout=tool_timeout, emit_status=self._emit),
        ]
        self.tools = {tool.name: tool for tool in tools}
        self.graph = self._build_graph()

    def _build_graph(self):
        """构建“模型 → 工具 → 模型 → 引用校验”的状态图。"""
        workflow = StateGraph(AgentState)
        workflow.add_node("agent", self._agent_node)
        workflow.add_node("tools", self._tools_node)
        workflow.add_node("validate_answer", self._validate_node)
        workflow.add_edge(START, "agent")
        workflow.add_conditional_edges("agent", self._after_agent, {"tools": "tools", "validate": "validate_answer"})
        workflow.add_edge("tools", "agent")
        workflow.add_conditional_edges("validate_answer", self._after_validate, {"repair": "agent", "done": END})
        return workflow.compile()

    async def _emit(self, kind: str, message: str) -> None:
        """向当前请求发送进度事件，同时隔离并发会话。"""
        progress = self._progress.get()
        if progress:
            result = progress(kind, message)
            if inspect.isawaitable(result):
                await result

    async def _agent_node(self, state: AgentState) -> dict[str, Any]:
        """调用模型决定下一步工具动作或生成最终回答。"""
        if state["agent_steps"] >= self.max_tool_calls + 3:
            return {
                "messages": [AIMessage(content="执行步骤已达上限。我只能基于目前获得的信息作答；如证据不足，请缩小问题范围后重试。")],
                "agent_steps": state["agent_steps"] + 1,
                "disable_tools": True,
            }
        tools_enabled = (
            not state["disable_tools"]
            and state["tool_calls"] < self.max_tool_calls
            and self.search_provider is not None
        )
        system_prompt = SYSTEM_PROMPT
        if state["citation_retries"]:
            system_prompt += f"\n\n{REPAIR_PROMPT}\n校验错误：{state['citation_error']}"
        messages: list[BaseMessage] = [SystemMessage(content=system_prompt), *state["messages"]]
        await self._emit("status", "正在组织回答…")
        response = await asyncio.wait_for(
            self.model.ainvoke(messages, tools_enabled), timeout=self.tool_timeout
        )
        return {"messages": [response], "agent_steps": state["agent_steps"] + 1}

    @staticmethod
    def _after_agent(state: AgentState) -> str:
        """根据模型是否返回工具调用选择下一个图节点。"""
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "validate"

    async def _tools_node(self, state: AgentState) -> dict[str, Any]:
        """按顺序校验并执行模型请求的白名单工具。"""
        message = state["messages"][-1]
        assert isinstance(message, AIMessage)
        results = dict(state["search_results"])
        sources = dict(state["sources"])
        context = ToolContext(results, sources, state["search_rounds"])
        errors = list(state["errors"])
        calls = state["tool_calls"]
        tool_messages: list[ToolMessage] = []

        for call in message.tool_calls:
            calls += 1
            name = str(call.get("name", ""))
            args = call.get("args") or {}
            call_id = str(call.get("id", f"call-{calls}"))
            if calls > self.max_tool_calls:
                payload = {"ok": False, "error_type": "budget_exhausted", "message": "工具调用预算已耗尽"}
            else:
                tool = self.tools.get(name)
                payload = (
                    await tool.execute(args, context)
                    if tool
                    else {"ok": False, "error_type": "unknown_tool", "message": f"不允许的工具：{name}"}
                )
            if not payload.get("ok"):
                errors.append(str(payload.get("message", "工具执行失败")))
            tool_messages.append(ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id=call_id, name=name))
        return {
            "messages": tool_messages,
            "search_results": results,
            "sources": sources,
            "search_rounds": context.search_rounds,
            "tool_calls": calls,
            "errors": errors,
        }

    async def _validate_node(self, state: AgentState) -> dict[str, Any]:
        """校验回答中的来源编号，必要时触发一次模型修复。"""
        message = state["messages"][-1]
        answer = self._message_text(message)
        valid, reason = validate_citations(answer, state["sources"], state["requires_sources"])
        if valid:
            formatted = append_source_links(answer, state["sources"])
            if state["requires_sources"] and not state["sources"]:
                formatted += "\n\n> 本次回答未取得可验证的网页正文，因此不应将其视为带来源回答。"
            return {"answer": formatted}
        if state["citation_retries"] < 1:
            await self._emit("status", "正在修复来源引用…")
            return {
                "citation_retries": state["citation_retries"] + 1,
                "citation_error": reason,
                "disable_tools": True,
            }
        clean = re.sub(r"\[S\d+\]", "", answer).strip()
        return {"answer": f"{clean}\n\n> 来源校验未通过：{reason}"}

    @staticmethod
    def _after_validate(state: AgentState) -> str:
        """在引用修复和结束任务之间选择后续节点。"""
        return "done" if state["answer"] else "repair"

    @staticmethod
    def _message_text(message: BaseMessage) -> str:
        """兼容纯文本与内容块两种模型响应格式。"""
        if not isinstance(message, AIMessage):
            return ""
        if isinstance(message.content, str):
            return message.content
        return "".join(
            str(block.get("text", ""))
            for block in message.content
            if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
        )

    async def run(
        self,
        history: Sequence[BaseMessage],
        question: str,
        progress: ProgressCallback | None = None,
    ) -> str:
        """运行一次有总超时保护的 Agent 任务并返回最终文本。"""
        from langchain_core.messages import HumanMessage

        task_id = str(uuid4())
        initial: AgentState = {
            "task_id": task_id,
            "messages": [*history[-12:], HumanMessage(content=question)],
            "search_results": {},
            "sources": {},
            "search_rounds": 0,
            "tool_calls": 0,
            "agent_steps": 0,
            "errors": [],
            "answer": "",
            "requires_sources": needs_sources(question),
            "citation_retries": 0,
            "citation_error": "",
            "disable_tools": False,
        }
        token = self._progress.set(progress)
        started = time.monotonic()
        logger.info("agent_task_started task_id=%s", task_id)
        try:
            final = await asyncio.wait_for(
                self.graph.ainvoke(initial, config={"recursion_limit": self.max_tool_calls * 2 + 10}),
                timeout=self.task_timeout,
            )
            logger.info(
                "agent_task_finished task_id=%s elapsed_ms=%d tool_calls=%d search_rounds=%d errors=%d",
                task_id,
                int((time.monotonic() - started) * 1000),
                final["tool_calls"],
                final["search_rounds"],
                len(final["errors"]),
            )
            return str(final["answer"])
        except TimeoutError:
            logger.warning("agent_task_timeout task_id=%s elapsed_ms=%d", task_id, int((time.monotonic() - started) * 1000))
            return "本次任务超过总时限，已停止执行。请缩小问题范围后重试。"
        except Exception as exc:
            logger.error("agent_task_failed task_id=%s error_type=%s", task_id, type(exc).__name__)
            raise
        finally:
            self._progress.reset(token)
