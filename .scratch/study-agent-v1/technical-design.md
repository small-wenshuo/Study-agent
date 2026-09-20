# 学习辅助 Agent 第一版技术设计

日期：2026-09-20  
状态：待实现；本文记录设计与验收要求，不代表功能已经完成。  
分诊标签：ready-for-agent

## 1. 目标与范围

面向计算机学习者，提供可在浏览器使用的聊天 Demo，完成“提问 → 判断 → 搜索 → 阅读正文 → 整理证据 → 回答与引用”的闭环。支持同一会话追问，模型和搜索服务通过后端配置接入。

第一版覆盖编程语言、数据结构与算法、操作系统、计算机网络和数据库基础。默认中文解释，保留必要的英文术语。优先引用官方文档、标准及项目官方仓库，涉及具体 API 时注意版本。

第一版不包含：登录和多用户权限、跨会话长期记忆、向量知识库、多 Agent、文件上传、PDF 阅读、登录后网页、自动执行生成的代码。默认仅在本机运行，不作为未经加固的公网服务。

## 2. 用户体验

- 网页提供消息列表、输入框、发送按钮、示例问题和新建会话按钮。
- 支持连续追问；新建会话清空上下文。页面刷新可沿用当前标签页保存的会话 ID，后端重启后会话失效并提示重新开始。
- 展示真实执行状态：分析问题、搜索资料、阅读资料、整理回答、完成或失败；不展示模型内部思考过程。
- 回答支持 Markdown、代码块和可点击引用。回答内容按需采用“核心结论 → 分步解释 → 示例 → 来源”，短问题无需强制展开。
- 未联网的回答明确显示“本次未检索外部资料”。联网失败后的已有知识回答明确标注未完成来源核验。
- 搜索未配置、额度不足、网络失败等情况显示可理解的提示；正在处理时禁止同一会话重复提交。
- 第一版逐步推送执行状态，最终答案一次返回；逐 token 输出不是首版验收条件。

## 3. 技术方案

| 层次 | 方案 | 职责 |
| --- | --- | --- |
| 前端 | HTML / CSS / JavaScript | 聊天、状态、Markdown 与引用展示 |
| 后端 | FastAPI + Uvicorn | 同源静态页面、会话与聊天接口 |
| 流程控制 | LangGraph + LangChain 模型集成 | 状态转换、模型决策、工具调用 |
| 模型 | 可替换适配器，首版支持兼容聊天接口的服务 | 判断、证据整理与回答 |
| 搜索 / 阅读 | Tavily Search / Extract | 获取候选网页和正文 |
| 配置与校验 | python-dotenv + Pydantic | 加载配置、校验输入及模型输出 |
| 存储 | 进程内存 | 单进程会话与本轮任务状态 |

开发解释器使用 `D:/codex/.env/new_agent/python.exe`（已检查为 Python 3.11.16）。此前环境检查中核心包导入与 pip check 通过；这不代表服务连通性已验证。pytest 尚需安装。实施时记录经过测试的直接依赖版本，不直接复制课程的整份依赖清单。

后端以单 worker 运行，避免不同进程间内存会话不一致。未来需要持久化或多实例时再引入数据库。

## 4. 模块与目录

以下为计划目录，不表示这些文件已经创建：

```text
app/
  main.py                 # 应用、路由和静态文件入口
  config.py               # 唯一配置加载入口
  schemas.py              # HTTP 与事件数据结构
  agent/
    graph.py              # 判断、工具执行、整理与回答流程
    state.py              # 本轮任务状态
    prompts.py            # 计算机学习领域提示词
  providers/
    model.py              # 模型接口及首个适配器
    search.py             # 搜索和正文提取接口及 Tavily 实现
  services/
    sessions.py           # 会话生命周期与并发控制
    evidence.py           # 正文截取、去重、证据整理
    citations.py          # 引用校验、链接映射
  static/
    index.html
    app.js
    styles.css
tests/
evals/questions.json
.env.example
.gitignore
requirements.txt
requirements-dev.txt
README.md
```

模型、搜索与阅读的服务细节只在适配模块中出现；前端和流程控制不依赖供应商特有的返回结构。搜索与阅读分别暴露 `search(query)` 和 `read_pages(urls)`，允许以后单独替换。

## 5. 配置与密钥

用户复制 `.env.example` 为项目根目录 `.env` 后填写。示例中的占位值必须替换；不提供真实密钥。

```dotenv
MODEL_PROVIDER=openai_compatible
MODEL_NAME=replace-with-your-model
MODEL_BASE_URL=https://your-provider.example/v1
MODEL_API_KEY=replace-with-your-key

SEARCH_PROVIDER=tavily
TAVILY_API_KEY=

MAX_SEARCH_ROUNDS=3
SEARCH_RESULTS_PER_QUERY=5
MAX_READ_PAGES_PER_ROUND=3
MAX_TOOL_CALLS=12
REQUEST_TIMEOUT_SECONDS=30
MAX_RETRIES=1
TASK_TIMEOUT_SECONDS=180
```

`app/config.py` 使用 `Path(__file__).resolve().parents[1] / '.env'` 定位配置文件，并通过 `load_dotenv(..., override=False)` 加载，已有进程环境变量优先。配置只由后端读取，修改后重启服务生效。

- 模型名称、地址、密钥缺失或仍为占位值：网页可打开，但禁用提问并指出缺失项名称，不返回配置值。
- Tavily 密钥缺失：保留基础问答；需要联网时明确告知联网未配置，不伪造搜索结果。
- 不支持的 provider 或非法数值配置：提供明确配置错误，不静默切换供应商。
- 同协议模型通过配置切换；其他协议需增加适配器。启动配置检查不能代替真实工具调用能力测试。
- `.env` 加入 `.gitignore`；日志、错误响应和前端不得包含密钥。外部服务 SDK 的原始错误经脱敏后输出。

## 6. 执行流程与约束

1. 校验输入、会话和配置，获取会话执行锁，创建 task_id。
2. 根据当前问题和近期对话判断是否检索；用户明确要求来源、核实最新信息时必须检索。
3. 需要检索时调用搜索工具。默认每次一个查询、返回至多 5 个候选结果，可按计算机术语补充英文查询。
4. 模型选择候选结果中的至多 3 个链接阅读。阅读成功后才建立正式来源编号；搜索摘要不能冒充已读正文。
5. 清理、去重并截取正文，提取与问题有关的证据，每条证据关联来源编号。
6. 判断证据是否足够；不足时改写查询继续，最多 3 轮搜索。程序同时限制总工具调用次数和任务时长，模型不能解除限制。
7. 根据可用证据组织解释；达到限制仍不足时说明具体缺口，不编造答案或引用。
8. 校验引用和输出结构，发送最终答案，保存本次有效对话，释放锁。

每次 `search` 或批量 `read_pages` 调用计为一个工具调用；一次搜索计为一轮。对话、工具结果和答案均计入上下文预算。上述参数为初始设计值，实施时通过评测调整。

网络超时、429 和可重试的服务端错误最多重试 1 次，遵守合理的 Retry-After，并受 180 秒总任务期限约束；认证和参数错误不重试。取消、断连、异常和超时都必须释放会话锁。

## 7. 状态与来源结构

| 对象 | 主要字段 |
| --- | --- |
| Session | session_id、messages、updated_at、active_task_id |
| AgentState | task_id、question、history、queries、sources、evidence、search_rounds、tool_calls、errors、deadline |
| SearchResult | title、url、snippet |
| Source | source_id、title、url、retrieved_at、content_excerpt |
| Evidence | source_id、excerpt、relevant_point |
| Answer | markdown、used_source_ids、search_status、limitations |

来源编号由程序生成，仅在本轮回答内有效，跨轮引用以 task_id 和 source_id 共同定位。追问历史保留既有答案和来源元数据，但需要重新核实的事实要再次读取。

初始限制：问题最多 4,000 字符，保留最近 6 轮完整对话，每页正文最多 8,000 字符，每轮任务累计送入模型的来源文本最多 24,000 字符；截断时保留标记。适配器还应检查模型上下文预算，优先裁剪旧对话和低相关证据，不裁掉系统规则及当前问题。字符限制不等同于 token 限制。

会话空闲 60 分钟后清理；本地 Demo 最多保留 100 个会话，清理不活跃会话，不清理正在执行的任务。进程重启后不承诺恢复历史。

## 8. HTTP 接口与事件

| 接口 | 用途 |
| --- | --- |
| GET / | 返回聊天网页 |
| GET /api/config/status | 返回模型是否就绪、搜索是否配置及缺失项名称，不返回密钥或完整配置 |
| POST /api/sessions | 创建会话，返回 session_id |
| POST /api/chat | 接收 session_id、message，返回 text/event-stream |

前端通过 fetch POST 读取流式响应，不使用只能发 GET 的原生 EventSource。每个事件使用标准 SSE 分隔格式，data 为 JSON：

```text
event: status
data: {"task_id":"...","stage":"reading","message":"正在阅读资料"}

event: answer
data: {"task_id":"...","markdown":"解释内容 [S1]","sources":[{"source_id":"S1","title":"...","url":"https://..."}],"search_status":"completed","limitations":[]}

event: done
data: {"task_id":"...","status":"completed"}
```

`search_status` 为 not_needed、completed、partial、unavailable 或 failed。`completed` 表示检索读取流程完成，不代表所有结论已获真实性保证。

开始流之前：非法输入返回 422，会话不存在返回 404，同一会话忙碌返回 409，模型未配置返回 503。流开始后的致命错误发送 `error`（code、message、retryable），随后发送 `done`，status 为 failed；可恢复的网页读取失败使用 status 通知，继续工作。

每次任务最终只发送一个 done 事件。断连时后端尽力取消未完成任务，不保存半截助手答案；无法取消的在途供应商请求仍可能产生费用。

## 9. 引用与安全边界

- 来源列表由程序从实际读取成功的 Source 生成，只列回答引用的来源。模型不得自由生成引用 URL。
- 检查正文引用编号与 used_source_ids 一致且都存在；非法引用允许一次修复，仍失败则移除无效标记并标明来源校验未通过。
- 编号校验只保证来源存在；结论是否被正文支持，通过提示约束和评测人工核验，第一版不承诺自动证明事实正确。
- 网页内容视为不可信资料，不能覆盖系统指令、读取密钥或改变工具权限。只允许已注册的搜索与阅读工具。
- 只读取本轮搜索候选中的公开 HTTP(S) URL；校验并拒绝本机、私有地址与非网页协议，重定向目标同样检查。
- 前端 Markdown 禁用原始 HTML，净化渲染结果，来源链接只允许 HTTP(S)；新窗口打开时设置安全的 rel 属性。
- 不执行模型生成的代码或命令。日志仅记录 task_id、阶段、耗时、调用次数、错误类型及供应商返回的可用用量指标，默认不记录密钥、全文对话或网页正文。

## 10. 异常与降级

| 情况 | 行为 |
| --- | --- |
| 搜索未配置 | 明示无法联网；允许基础解释，不宣称完成来源核验 |
| 搜索无结果 | 在预算内改写查询；仍无结果则说明缺少依据 |
| 单页提取失败 | 尝试其他候选链接，保留已取得的证据 |
| 来源相互矛盾 | 说明差异，优先核对版本和官方资料，不强行合并结论 |
| 模型鉴权失败 | 终止任务，提示检查后端配置，不回显密钥 |
| 限流或服务超时 | 有限重试，失败后给出可重试提示 |
| 工具预算耗尽 | 基于已有证据给出有边界的回答 |
| 总任务超时 | 终止执行，返回超时错误，恢复输入可用状态 |
| 会话过期或服务重启 | 提示新建会话，不假装保留旧上下文 |

## 11. 验证与完成标准

自动测试使用可控的模型和工具替身覆盖：直接回答分支、检索阅读分支、追问上下文、搜索未配置、网页失败、次数限制、超时、非法引用、会话隔离与配置优先级。接口测试覆盖 SSE 结束事件和失败后释放执行锁。

准备 20 道计算机评测题：基础概念 5 道、代码解释 5 道、需要官方资料或版本核验的问题 5 道、追问与异常场景 5 道。记录回答正确性、引用支持度、耗时和工具调用次数；首轮评测后记录实际基线，不虚构准确率。

提供真实凭据后执行至少一次模型调用、一次搜索与正文提取，以及一次网页端完整问答。自动模拟测试通过与真实服务验证通过分别记录；缺少凭据时明确真实链路尚未验收。

验收要求：

- 浏览器中可完成提问、追问、新建会话和错误重试，代码与引用正常显示。
- 需要联网的问题有真实搜索和正文读取记录，回答引用可追溯。
- 空结果、页面失败、错误密钥和调用限制均有确定的结束路径。
- 修改 .env 并重启后配置生效；兼容协议的模型切换不改业务代码。
- README 提供可复现的安装、配置、启动和测试步骤。
- 测试通过后检查 diff 并创建本地 commit；仅在用户要求时 push。

## 12. 实施顺序

1. 创建项目骨架、配置模块、网页聊天布局和会话接口。
2. 接入首个模型适配器，打通无检索问答和追问。
3. 接入 Tavily 搜索 / 阅读、Agent 循环与预算限制。
4. 完成证据引用、进度事件、错误处理与前端渲染。
5. 补齐关键测试、评测题与真实服务冒烟测试，更新运行文档。

开发前无需再次选型。用户后续需在本地填写模型配置与 Tavily 密钥；真实网络连通性、模型工具调用能力和检索效果由测试确认。

## 13. 参考与仓库现状

- Tavily 搜索：https://help.tavily.com/articles/4840311948-tavily-search-api
- Tavily 正文提取：https://help.tavily.com/articles/8721959612-what-is-the-tavily-extract-api
- Tavily 价格与额度：https://www.tavily.com/pricing （以服务商当时公布值为准，本文不锁定套餐额度。）

本文件按 AGENTS.md 要求放在 `.scratch/study-agent-v1/`。当前仓库内尚无 `docs/agents/issue-tracker.md`、`docs/agents/triage-labels.md` 和 `docs/agents/domain.md`；本文依据已确认需求编写，没有假设这些文档的额外规则。
