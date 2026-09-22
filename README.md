# Study-agent

一款帮助学习的agent

面向计算机学习者的学习辅助 Agent，实现“提问 → 判断 → 搜索 → 阅读正文 → 整理证据 → 回答并引用”的流程。

## 当前状态

V1 Demo 已实现：模型可直接回答或自主调用 `search_web` / `read_pages`，后端会限制执行预算、校验 `[S1]` 引用，并通过 SSE 向网页报告搜索和阅读状态。会话保存在内存中，服务重启后清空。

- [第一版技术设计与验收要求](.scratch/study-agent-v1/technical-design.md)
- [项目开发约定](AGENTS.md)

## 快速开始

约定使用 Python 3.11，当前本机解释器为 `D:/codex/.env/new_agent/python.exe`。

```powershell
# 仅首次配置时创建，避免覆盖已经填写的本地密钥
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# 编辑 .env，至少填写 MODEL_NAME、MODEL_BASE_URL、MODEL_API_KEY
# 如需联网，再填写 TAVILY_API_KEY

D:/codex/.env/new_agent/python.exe -m pip install -e .
D:/codex/.env/new_agent/python.exe -m uvicorn app.main:app --reload
```

浏览器打开 <http://127.0.0.1:8000>。`GET /api/health` 只返回模型/搜索是否就绪及缺失的模型字段，不返回配置值或密钥。修改 `.env` 后需要重启服务。

同一兼容协议的模型服务通常只需换 `.env`；首版只正式支持具备 Tool Calling 的 OpenAI-compatible Chat Completions 接口。未配置 Tavily 时仍可基础问答，但要求来源的问题应明确说明联网不可用。

默认运行限制为：任务总时限 180 秒、单次模型调用 60 秒且超时重试 1 次、单次网页工具调用 30 秒。可通过 `.env` 中的 `TASK_TIMEOUT_SECONDS`、`MODEL_TIMEOUT_SECONDS`、`MODEL_MAX_RETRIES` 和 `TOOL_TIMEOUT_SECONDS` 调整。

## 测试

```powershell
D:/codex/.env/new_agent/python.exe -m unittest discover -v
```

自动测试使用明确的模型与搜索 adapter，不访问外部服务，并覆盖两个工具的公开 interface。配置真实凭据后还应人工验证：

1. 普通知识题不触发搜索。
2. “解释 Python 装饰器并提供官方资料”实际发生搜索和正文读取，回答含可打开的 `[S*]` 链接。
3. 搜索失败、正文失败和连续工具调用最终都会结束并说明局限。
4. 连续追问保留上下文，新建会话不继承旧上下文。

首批 20 道评测题在 `evals/questions.json`。2026-09-22 使用真实模型和 Tavily 回归通过 20/20；判定方法、修复前后结果和剩余性能问题见 [真实评测报告](.scratch/study-agent-v1/evaluation-2026-09-22.md)。该结果验证工具链，不替代独立的事实正确性与引用支持度评分。

## 结构

```text
app/agent/       LangGraph 状态、提示词与工具循环
app/tools/       工具 interface、Schema、search_web 与 read_pages 实现
app/providers/   模型与 Tavily 适配器
app/services/    引用和内存会话
app/static/      零构建网页 Demo
tests/           离线自动测试
evals/           人工/后续自动评测题
```

密钥通过环境变量配置；不提交 `.env`、虚拟环境或日志。

架构不变量、验证边界和下一步优先级见 [CONTEXT.md](CONTEXT.md)，方便后续开发者或 AI 直接续作。
