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

## V4 论文学习（2026-10-02）

V4 以“先整体理解，再针对不懂的部分展开”为默认学习方式。论文模式默认关闭，普通聊天、知识库问答和 V3 记忆继续使用原流程；上传 PDF 或问题包含“论文”不会自动启动学习任务。

使用步骤：

1. 按快速开始运行服务，选择知识库并上传文本型 PDF，等待文件状态为 `ready`。
2. 打开左侧“论文模式”，选择单篇 PDF。系统自动读取全文、整理证据并生成简明导读；已有同版本成功导读优先复用。
3. 针对导读段落选择解释概念、拆解公式、举例说明，或直接在聊天输入框追问。自测按需触发，提示和答案默认折叠。
4. 点击“将这份内容加入笔记”，编辑并保存。历史版本不覆盖，用户编辑不再声称是校验通过的论文结论；支持下载 Markdown。
5. 从已保存笔记生成逐页 PPT 大纲，修改标题、要点、讲解备注和表格，确认后导出 PPTX。长文字和表格自动拆页，实际页数可能多于大纲页数。

关闭模式会取消当前会话未完成的学习任务，保留已保存笔记与时间线；刷新仅恢复状态，不重新生成或重试。联网许可独立控制，打开论文模式不代表允许网页补充。第一次导读只读论文，授权后的深入追问才可补充网页正文。

论文学习数据保存在 `DATA_DIR/paper-learning.db`，导出文件在 `DATA_DIR/exports/`。删除论文或知识库会清理关联学习记录和内部证据；下载到电脑的副本不会删除。重试原文件会关闭关联学习会话，历史产物保留。

执行器只支持本地单用户、单 worker，任务不会跨服务重启自动续跑。默认总时限 600 秒、最多 24 阅读批次和 40 次模型尝试；超限可能返回部分覆盖或明确失败。`PAPER_*` 配置见 `.env.example`，字符预算不是精确 token 预算。PPT 复用 python-pptx；直接声明现有 OpenAI SDK 依赖用于异常分类。

完整测试与可选真实验收：

```powershell
D:/codex/.env/new_agent/python.exe -m unittest discover -v
# 需要已有模型、Embedding 凭据；使用独立的 .scratch 目录，产生真实外部调用。
D:/codex/.env/new_agent/python.exe evals/run_paper_acceptance.py
# 额外明确启用网页补充验证：添加 --with-web
```

首期不支持扫描件 OCR、PDF 框选、高保真公式/图表恢复或多篇比较。引用身份与摘录校验不等于事实正确性；真实模型和关键实验值仍需人工核验。实现与验证范围见 [V4 验收记录](docs/v4-validation.md)，详细规格见 [V4 技术设计](.scratch/study-agent-v4/technical-design.md)。

## 多用户访问与服务器升级（2026-10-13）

网站现在必须先登录。管理员在**运行服务的服务器**终端创建账号；网页不提供公开注册。首次创建管理员的内部 ID 固定为 `legacy-admin`，升级前的聊天记录、长期记忆、知识库及其论文资料只归这个账号。其他账号各自拥有独立数据，即使知道别人的会话、文件或笔记 ID 也无法读取。

升级前先暂停公开访问并备份完整 `DATA_DIR`（包括 SQLite、Chroma、上传文件和导出文件）；确保服务已经停止后更新代码。沿用原 `DATA_DIR`，不要删除或重新创建原数据目录。在项目根目录用**服务所用的同一个 Python 环境**执行：

```bash
python -m scripts.create_user admin --admin
python -m scripts.create_user alice
# 忘记密码：会同时吊销该账号的全部旧登录
python -m scripts.create_user alice --reset
```

命令会在终端无回显地询问两次密码（至少 12 字符），不要把密码放在命令行、配置文件或聊天里。本地开发若使用项目约定环境，把上述 `python` 换为 `D:/codex/.env/new_agent/python.exe`。账号信息保存在 `DATA_DIR/auth.db`，也须备份；丢失它不会自动恢复账号与归属映射。第一次启动会给旧业务表添加归属列，建议在备份后先于维护窗口验证，再开放访问。请保持**单 worker** 运行。

**公网必须先配置 HTTPS。** 明文公网 HTTP 登录会返回 403；反向代理需把可信的 HTTPS 请求协议传给 ASGI 服务，并禁止外部客户端伪造转发头。浏览器 Cookie 设置为 HttpOnly、SameSite=Strict，HTTPS 时加 Secure；服务器端登录令牌可吊销，七天过期，连续五次错误会锁定账号十五分钟。站点仍通过明文 HTTP 暴露时，先在防火墙或反向代理关闭公网入口，完成 HTTPS 和升级后再开放。升级后分别用管理员和普通账号验证：管理员看得到旧数据，普通账号只看得到自己的数据；退出后刷新页面应回到登录页。

本次代码与隔离验证见 [多用户隔离验收](docs/auth-isolation-validation.md)。
