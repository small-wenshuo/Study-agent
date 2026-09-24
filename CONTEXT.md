# Study Agent 项目上下文

## 产品边界

V2 是面向计算机学习者的本地网页 Agent，完成“多格式资料 → 个人知识库 → 检索/重排 → 三档证据判断 → 带引用回答 → 用户允许后联网补充”。不包含登录、多 Agent、长期记忆、OCR、音视频解析和代码执行。规格在 `.scratch/study-agent-v2/technical-design.md`。

## 当前实现

- `app/agent/graph.py`：LangGraph 决策循环、工具调用总预算、回答校验和任务总超时；不包含具体工具实现。
- `app/agent/v2.py`：知识库优先编排、证据充分/部分/不足判定、K 引用和授权后联网组合。
- `app/parsers/`：PDF、DOCX、Markdown、TXT、PPTX、XLSX、CSV 到统一 `Document` 的解析入口。
- `app/knowledge_base/`：SQLite 知识库/文件状态、原文件生命周期、Chunk 与 Chroma 向量存储。
- `app/retrieval/`：向量分数与词覆盖率组合的轻量 Rerank。
- `app/tools/base.py`：两个工具共用的 `execute(args, context)` interface 和任务内证据上下文。
- `app/tools/search.py`、`reader.py`：各自拥有 Schema、参数校验、执行、结果/来源编号与结构化错误。
- `app/tools/__init__.py`：汇总模型可见的工具 Schema；`app/providers/model.py` 只负责绑定并调用 OpenAI-compatible 模型。
- `app/providers/tavily.py`：Tavily Search/Extract、一次有限重试和公开 URL 校验。
- `app/providers/embeddings.py`：独立的 OpenAI-compatible Embedding 适配器。
- `app/services/sessions.py`：内存会话、最近 6 轮消息及单会话执行锁。
- `app/main.py`：FastAPI、配置状态、创建会话和 SSE 聊天接口。
- `app/static/index.html`：无构建步骤的同源聊天 Demo；所有模型文本先转义，链接仅允许 HTTP(S)。
- `tests/`：不依赖外部凭据的 Agent、工具 interface、引用、会话和 HTTP 冒烟测试。
- `evals/questions.json`：10 道基础题与 10 道必须联网题的首批人工评测集。

## 修改路由

- 改工具名称、参数或行为：修改 `app/tools/`，Schema 和运行时校验保持一致，并从工具 interface 增补测试。
- 改 Tavily HTTP 请求或响应转换：修改 `app/providers/tavily.py`，供应商字段在此归一化后再进入 Agent。
- 改模型协议或 Tool Calling 转换：修改 `app/providers/model.py`，工具 Schema 始终从 `app/tools/` 导入。
- 改循环、预算或节点路由：修改 `app/agent/graph.py`；工具参数与证据规则由 `app/tools/` 继续拥有。
- 改文件支持或结构提取：修改 `app/parsers/`，所有格式继续输出统一 `Document`。
- 改知识库持久化、切分或文件生命周期：修改 `app/knowledge_base/`；向量库只通过 `VectorStore` 访问。
- 改证据分级、知识库/联网组合：修改 `app/agent/v2.py`；不得绕过 K/S 引用校验。
- 改会话或 HTTP/SSE 行为：分别修改 `app/services/sessions.py` 或 `app/main.py`。

## 不变量

1. 搜索摘要不是证据；只有 `read_pages` 成功登记的 `S*` 正文能被引用。
2. `read_pages` 只接受本任务 `search_web` 登记的 `R*`，不能让模型传任意 URL。
3. 工具失败要以 `ToolMessage` 回填，给模型调整机会；不隐藏失败也不伪造资料。
4. 密钥只从环境变量/.env 读取，不进入健康接口、SSE、日志或 Git。
5. 每个外部调用与整项任务都有时限；搜索、工具调用、引用修复和图递归都有硬上限。
6. 工具 Schema 的单一来源是 `app/tools/search.py` 和 `reader.py`，模型 adapter 只能导入使用。
7. 不同文件只在解析阶段区别处理；入库后统一为 Chunk、Embedding 和来源 Metadata。
8. 知识库证据不足时不得使用模型自身知识补齐；非明确联网问题必须先取得用户授权。
9. K 引用只能指向本轮实际检索的 Chunk，S 引用只能指向成功读取的网页正文，两类来源分开展示。
10. 删除或重试文件时按 `file_id` 清理或替换向量，不能留下过期 Chunk。

## 已验证与未验证

README 中的测试命令可离线验证核心分支。当前覆盖七类解析入口、来源元数据、真实 SQLite/Chroma、多知识库隔离、文件删除与失败重试、三档证据结果、联网授权、K/S 引用、V1 工具循环、会话隔离和 HTTP 管理接口。模型、Embedding 与搜索 adapter 仍不代表真实供应商验收。

2026-09-22 已使用本地真实模型和 Tavily 对 `evals/questions.json` 完成端到端回归：修复前 13/20，修复超时分类、模型重试、来源保留和先读后搜后通过 20/20。报告见 `.scratch/study-agent-v1/evaluation-2026-09-22.md`。该验收确认真实 Tool Calling、搜索、正文读取和引用链路可运行；事实正确性与引用支持度目前仅人工抽查，尚无独立自动评分。

2026-09-24 本机模型和 Tavily 配置可用，但缺少 `EMBEDDING_MODEL`，因此 V2 尚未做真实 Embedding 入库和端到端知识库问答。已知的 V1 URL 重定向核验缺口仍然存在。

## 续作优先级

1. 配置真实 Embedding 模型，使用真实 PDF/DOCX/PPTX/XLSX 完成 V2 端到端验收。
2. 建立“可答、部分可答、超纲”评测集，校准召回数量、Rerank 和模型证据判定。
3. 把同步上传处理改为受控后台任务，补任务容量、过期清理和更细的可观测性。
4. 增加基于总 token 预算的历史/证据裁剪，并评估专用 Reranker。
5. 根据真实失败案例再考虑 OCR、网页导入、音视频字幕和部署。

`docs/agents/issue-tracker.md`、`triage-labels.md`、`domain.md` 在本次实现时仍不存在；未臆造其规则。
