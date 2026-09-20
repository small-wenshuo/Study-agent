# Study Agent 项目上下文

## 产品边界

V1 是面向计算机学习者的本地网页 Agent，完成“提问 → 模型判断 → 搜索 → 阅读正文 → 整理证据 → 带引用回答”。不包含登录、多 Agent、长期记忆、向量库、文件上传和代码执行。详细规格在 `.scratch/study-agent-v1/technical-design.md`。

## 当前实现

- `app/agent/graph.py`：LangGraph 决策循环。模型决定是否调用工具；程序执行工具、限制搜索/调用次数并校验引用。
- `app/providers/model.py`：OpenAI-compatible Tool Calling 适配器。
- `app/providers/tavily.py`：Tavily Search/Extract、一次有限重试和公开 URL 校验。
- `app/services/sessions.py`：内存会话、最近 6 轮消息及单会话执行锁。
- `app/main.py`：FastAPI、配置状态、创建会话和 SSE 聊天接口。
- `app/static/index.html`：无构建步骤的同源聊天 Demo；所有模型文本先转义，链接仅允许 HTTP(S)。
- `tests/`：不依赖外部凭据的核心循环、引用、会话和 HTTP 冒烟测试。
- `evals/questions.json`：10 道基础题与 10 道必须联网题的首批人工评测集。

## 不变量

1. 搜索摘要不是证据；只有 `read_pages` 成功登记的 `S*` 正文能被引用。
2. `read_pages` 只接受本任务 `search_web` 登记的 `R*`，不能让模型传任意 URL。
3. 工具失败要以 `ToolMessage` 回填，给模型调整机会；不隐藏失败也不伪造资料。
4. 密钥只从环境变量/.env 读取，不进入健康接口、SSE、日志或 Git。
5. 每个外部调用与整项任务都有时限；搜索、工具调用、引用修复和图递归都有硬上限。

## 已验证与未验证

`python -m unittest discover -v` 可离线验证核心分支。当前尚未使用真实模型和 Tavily 凭据跑端到端，因此兼容服务的 Tool Calling 细节、网络连通性、搜索质量与引用支持度仍须实测。测试替身只验证程序控制流，不代表真实服务验收。

下一步应先复制 `.env.example` 配置真实凭据，运行 README 的真实验收清单并记录服务名称、模型名与结果；若供应商响应结构不同，只修改 provider 适配层，不把供应商数据结构泄漏进 Agent 图。

## 续作优先级

1. 真实端到端验收，修正具体兼容服务的 Tool Calling 差异。
2. 为 20 道题添加可重复执行的评测 runner，记录正确性、引用支持度、耗时和工具次数。
3. 增加会话过期/容量清理与可观测性；不要在日志保存问题全文或网页正文。
4. 根据真实失败案例补测试，再考虑流式模型输出、持久化或部署。

`docs/agents/issue-tracker.md`、`triage-labels.md`、`domain.md` 在本次实现时仍不存在；未臆造其规则。
