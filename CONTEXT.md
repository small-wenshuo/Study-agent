# Study Agent 项目上下文

## 产品边界

V1 是面向计算机学习者的本地网页 Agent，完成“提问 → 模型判断 → 搜索 → 阅读正文 → 整理证据 → 带引用回答”。不包含登录、多 Agent、长期记忆、向量库、文件上传和代码执行。详细规格在 `.scratch/study-agent-v1/technical-design.md`。

## 当前实现

- `app/agent/graph.py`：LangGraph 决策循环、工具调用总预算、回答校验和任务总超时；不包含具体工具实现。
- `app/tools/base.py`：两个工具共用的 `execute(args, context)` interface 和任务内证据上下文。
- `app/tools/search.py`、`reader.py`：各自拥有 Schema、参数校验、执行、结果/来源编号与结构化错误。
- `app/tools/__init__.py`：汇总模型可见的工具 Schema；`app/providers/model.py` 只负责绑定并调用 OpenAI-compatible 模型。
- `app/providers/tavily.py`：Tavily Search/Extract、一次有限重试和公开 URL 校验。
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
- 改会话或 HTTP/SSE 行为：分别修改 `app/services/sessions.py` 或 `app/main.py`。

## 不变量

1. 搜索摘要不是证据；只有 `read_pages` 成功登记的 `S*` 正文能被引用。
2. `read_pages` 只接受本任务 `search_web` 登记的 `R*`，不能让模型传任意 URL。
3. 工具失败要以 `ToolMessage` 回填，给模型调整机会；不隐藏失败也不伪造资料。
4. 密钥只从环境变量/.env 读取，不进入健康接口、SSE、日志或 Git。
5. 每个外部调用与整项任务都有时限；搜索、工具调用、引用修复和图递归都有硬上限。
6. 工具 Schema 的单一来源是 `app/tools/search.py` 和 `reader.py`，模型 adapter 只能导入使用。

## 已验证与未验证

README 中的测试命令可离线验证核心分支。当前覆盖工具 Schema 与运行时校验、R/S 编号、Agent 调度、循环/超时、引用、会话隔离和 HTTP 冒烟。当前尚未使用真实模型和 Tavily 凭据跑端到端，因此兼容服务的 Tool Calling 细节、网络连通性、搜索质量与引用支持度仍须实测。测试 adapter 只验证程序控制流，不代表真实服务验收。

下一步应先在本地 `.env` 配置真实凭据，运行 README 的真实验收清单并记录服务名称、模型名与结果；若供应商响应结构不同，只修改 provider adapter，不把供应商数据结构泄漏进 Agent 图。

已知验证缺口：公开 URL 会在交给 Tavily 前检查协议、主机名和 DNS 解析，但目标站点后续跳转由 Tavily 执行，当前程序无法独立核验跳转链。

## 续作优先级

1. 真实端到端验收，修正具体兼容服务的 Tool Calling 差异。
2. 为 20 道题添加可重复执行的评测 runner，记录正确性、引用支持度、耗时和工具次数。
3. 增加会话过期/容量清理与可观测性；日志只保留任务元数据和错误类型。
4. 增加基于总 token 预算的历史/正文裁剪；当前仅保留最近 6 轮并限制单页正文长度。
5. 根据真实失败案例补测试，再考虑流式模型输出、持久化或部署。

`docs/agents/issue-tracker.md`、`triage-labels.md`、`domain.md` 在本次实现时仍不存在；未臆造其规则。
