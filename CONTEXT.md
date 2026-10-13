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

2026-09-25 已使用 W3C、RFC Editor、Microsoft 和上游解析库的七类公开文件完成 V2 真实验收。初测 3/7，修复 DashScope 每批 20 条限制、CSV 说明行/空表头丢列和证据判定过度保守后，PDF、DOCX、Markdown、TXT、PPTX、CSV 均通过严格端到端问答；XLSX 曾成功完成 213 Chunk 入库与问答，但收紧问题后的最终复验被 DashScope 429 `insufficient_quota` 阻断。报告与复测脚本见 `.scratch/study-agent-v2/file-acceptance-report.md` 和 `run_file_acceptance.py`。已知的 V1 URL 重定向核验缺口仍然存在。

## 续作优先级

1. DashScope 额度恢复后，补跑 XLSX 唯一记录问题，闭环严格 7/7 真实验收。
2. 建立“可答、部分可答、超纲”评测集，校准召回数量、Rerank 和模型证据判定。
3. 把同步上传处理改为受控后台任务，补任务容量、过期清理和更细的可观测性。
4. 增加基于总 token 预算的历史/证据裁剪，并评估专用 Reranker。
5. 根据真实失败案例再考虑 OCR、网页导入、音视频字幕和部署。

`docs/agents/issue-tracker.md`、`triage-labels.md`、`domain.md` 在本次实现时仍不存在；未臆造其规则。

## V4 论文学习迭代（2026-10-02，保留上文历史）

- 会话新增默认关闭的 `paper_mode`、当前论文指针和设置版本。普通问答不创建论文任务；恢复页面仅查询历史，显式开启且选定论文才自动导读。关闭取消任务，保留论文导航和笔记。
- `app/papers/reader.py` 读取完整 PDF，保留页码、稳定块、原文与规范化文本、空页/失败页和质量提示。`KnowledgeBaseService.file_info/read_file` 拥有文件归属和路径核验；学习服务不查询它的私有表。
- `app/papers/store.py` 使用独立 `paper-learning.db` 保存快照、批次、任务、事件、产物及笔记版本。首期文本块在不可变快照 JSON 内、证据在产物 JSON 内，未拆成额外的块表和证据表；服务仍按快照和产物校验来源。
- `app/agent/paper.py` 生成批次证据卡、简明导读、解释与自测。论文摘录必须实际存在，允许仅空白差异后恢复实际原文；事实、教学解释与外部补充类型分开。引用身份校验不能证明语义支持度。
- `app/services/tasks.py` 负责容量、有限重试预算、取消、模式版本复核、错误分类和重启中断。SSE 从数据库按序号回放，无无限订阅队列；浏览器断开不取消任务。
- 追问先按当前文件限定向量召回，再映射到当前全文快照；检索失败明确降级到本篇快照词匹配。用户选定的目标块优先，历史引用不直接继承。模式和联网许可相互独立。
- `app/notes/service.py` 保存用户选定产物，编辑使用乐观锁，不改变旧版本，不自动写入知识库或长期记忆。`app/presentations/render.py` 生成可编辑文字及表格，按容量拆页，导出固定大纲与笔记版本。
- `app/papers/api.py` 为显式任务、产物、来源、笔记和导出提供接口；`app/static/paper.js` 负责开关、导读、深入入口、历史任务时间线、笔记和大纲编辑。
- 当前仍限定本地单用户、单 worker。未完成 OCR、PDF 坐标定位、复杂图表和公式恢复。PPT 文件每次生成只自动校验结构和边界，字体与实际视觉效果需打开核验；本次模板样例另做 PowerPoint 渲染验收。
- 测试、真实外部调用的失败与待验收范围记录在 `docs/v4-validation.md`；真实验收脚本为 `evals/run_paper_acceptance.py`。不能把离线或模拟浏览器测试当成真实模型事实正确性验收。

## 多用户访问修复（2026-10-13）

此前“单用户”约束不适合公网部署：匿名访问可直接读取全部历史。当前 `/api/*` 除健康状态和登录外须经过服务端 Cookie 身份校验；服务层的会话、记忆、知识库和论文资源均按账号归属校验，已知资源 ID 也不能越权读取。旧记录迁移为 `legacy-admin`，由第一个管理员账号继承。账号只允许服务器命令创建；没有公开注册。登录数据库独立存于 `DATA_DIR/auth.db`，不保存明文密码或令牌。生产环境登录必须走 HTTPS，继续保持单 worker。部署与测试边界见 README 和 `docs/auth-isolation-validation.md`。
