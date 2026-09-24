# Study-agent

一款帮助学习的agent

面向计算机学习者的学习辅助 Agent，实现“知识库优先 → 证据判断 → 必要时联网 → 带来源回答”的学习流程。

## 当前状态

V2 已实现：用户可创建知识库，上传 PDF、DOCX、Markdown、TXT、PPTX、XLSX 或 CSV。后端统一解析、切分并写入本地 Chroma；提问时先检索和重排，再由模型判断证据为充分、部分充分或不足。知识库来源使用 `[K1]`，网页来源使用 `[S1]`，证据不足时只有用户明确允许才联网补充。

V1 的直接回答、Tavily 搜索/阅读、SSE 进度、引用校验和会话能力继续保留。会话在内存中，知识库、原文件和向量索引持久化到 `DATA_DIR`。

- [第一版技术设计与验收要求](.scratch/study-agent-v1/technical-design.md)
- [第二版技术设计](.scratch/study-agent-v2/technical-design.md)
- [项目开发约定](AGENTS.md)

## 快速开始

约定使用 Python 3.11，当前本机解释器为 `D:/codex/.env/new_agent/python.exe`。

```powershell
# 仅首次配置时创建，避免覆盖已经填写的本地密钥
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# 编辑 .env：聊天至少填写 MODEL_*；知识库还需填写 EMBEDDING_*
# 如需联网，再填写 TAVILY_API_KEY。Embedding 服务可以与聊天模型不同。

D:/codex/.env/new_agent/python.exe -m pip install -e .
D:/codex/.env/new_agent/python.exe -m uvicorn app.main:app --reload
```

浏览器打开 <http://127.0.0.1:8000>。`GET /api/health` 只返回模型/搜索是否就绪及缺失的模型字段，不返回配置值或密钥。修改 `.env` 后需要重启服务。

网页左侧可新建知识库、上传/删除/重试文件，并选择“知识库不足时允许联网补充”。未选择知识库时保持 V1 普通问答。扫描版 PDF 需要 OCR，当前会明确返回没有可提取文本。

同一兼容协议的模型服务通常只需换 `.env`；首版只正式支持具备 Tool Calling 的 OpenAI-compatible Chat Completions 接口。未配置 Tavily 时仍可基础问答，但要求来源的问题应明确说明联网不可用。

默认运行限制为：任务总时限 180 秒、单次模型调用 60 秒且超时重试 1 次、网页工具 30 秒、文件处理/Embedding 120 秒、单文件 20 MB。对应值及 Chunk 大小可在 `.env` 调整。

## 测试

```powershell
D:/codex/.env/new_agent/python.exe -m unittest discover -v
```

自动测试不访问外部服务，使用真实文件解析器、SQLite 和本地 Chroma，并通过 adapter 隔离模型、Embedding 和搜索供应商。配置真实凭据后还应人工验证：

1. 普通知识题不触发搜索。
2. “解释 Python 装饰器并提供官方资料”实际发生搜索和正文读取，回答含可打开的 `[S*]` 链接。
3. 搜索失败、正文失败和连续工具调用最终都会结束并说明局限。
4. 分别上传 PDF、DOCX、PPTX 和 XLSX，回答中的 K 引用能定位到页码、章节或 Sheet。
5. 库内可答、部分可答和完全超纲问题分别进入正确分支；未授权时不得静默联网。
6. 连续追问保留上下文，新建会话不继承旧上下文。

首批 20 道评测题在 `evals/questions.json`。2026-09-22 使用真实模型和 Tavily 回归通过 20/20；判定方法、修复前后结果和剩余性能问题见 [真实评测报告](.scratch/study-agent-v1/evaluation-2026-09-22.md)。该结果验证工具链，不替代独立的事实正确性与引用支持度评分。

## 结构

```text
app/agent/       V1 LangGraph 工具循环与 V2 知识库编排
app/parsers/     七类文件解析器与统一 Document
app/knowledge_base/ SQLite 元数据、原文件生命周期与 Chroma
app/retrieval/   向量结果与词覆盖率组合重排
app/tools/       工具 interface、Schema、search_web 与 read_pages 实现
app/providers/   模型、Embedding 与 Tavily 适配器
app/services/    引用和内存会话
app/static/      零构建网页 Demo
tests/           解析器、知识库、Agent、HTTP 与 V1 回归测试
evals/           人工/后续自动评测题
```

密钥通过环境变量配置；不提交 `.env`、虚拟环境或日志。

架构不变量、验证边界和下一步优先级见 [CONTEXT.md](CONTEXT.md)，方便后续开发者或 AI 直接续作。
