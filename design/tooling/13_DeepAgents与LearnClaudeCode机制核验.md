# Deep Agents 与 Learn Claude Code 机制核验

核验日期：2026-10-15。用途：为播前 Agent Harness 设计提供版本化技术依据；本文件是核验记录，不是业务实现完成证明。没有读取密钥，没有构造或调用真实模型，没有修改声播或播前 Agent 业务代码。

## 1. 核验边界与结论

固定技术基线：`deepagents==0.7.25`、`langchain==1.4.6`、`langgraph==1.2.15`、`langchain-deepseek==1.1.1`、`langchain-mcp-adapters==0.3.2`，位于 `D:/Codex project/shengbo-ops-agent/.venv`。版本取自既有离线核验报告，并核对本机源码及本轮无网络的导入/签名检查。

**建议：一个 `create_deep_agent` 主入口，在可观察状态、动作权限和依赖约束内自主选择工具、Skills、isolated 子 Agent 与少量预定义局部工作流。**不把整个首期业务固定为必须从第 1 节点走到第 8 节点的顶层图。依赖规则是动作执行的前置条件：用户只改活动时，主 Agent 应识别局部影响，而不重跑材料解析和顾客研究。

Deep Agents 自身就是 Harness。Learn Claude Code 用于核验机制意图；不再复制教程中的 `while` 循环、Hook 总线、队友运行时与另一套调度器叠在 SDK 外。

## 2. Learn Claude Code 实际版本范围

本机 `D:/Codex project/learn-claude-code` 当前包含新版 **s01–s17**，不是仅旧版 12 章。`README-zh.md:235` 起明确新旧版本差异，`:354` 起列出新版章节。目录无 `.git`，不能给出已验证的 commit；以下采用本机文件、行号和 SHA256 标定此次快照。远程主线可继续变化。

教程入口：[Learn Claude Code](https://github.com/shareAI-lab/learn-claude-code)。当前本机关键依据：

| 机制 | 本机证据 | 播前首期建议 | Deep Agents 对应及边界 |
|---|---|---|---|
| s01 Agent Loop | `s01_agent_loop/code.py`；`README-zh.md:168` | 采用模型—工具—观察循环 | 复用 `create_deep_agent` 返回的编译图；不手写第二个模型主循环 |
| s02 Tool Use | `s02_tool_use/code.py`；`README-zh.md:170` | 采用明确 schema、描述、结果与错误的动作工具 | `tools=` 注册声播服务适配工具；确定性校验与计算放服务 |
| s03 Permission | `s03_permission/code.py`；`README-zh.md:172` | 采用业务作用域校验；仅已确认需要的事项请求人工确认 | SDK 文件权限只管内置文件工具；商品写入还需服务校验、版本与授权范围 |
| s04 Hooks | `s04_hooks/code.py:137,224–254` | 采用调用前后检查、追踪、失效校验 | 工具拦截用 `wrap_tool_call/awrap_tool_call`；输入与完成判断按合适扩展点实施；不照搬事件名 |
| s05 TodoWrite | `s05_todo_write/`；`README-zh.md:176` | 主 Agent 显式启用，反映当前动态计划 | `middleware=[TodoListMiddleware()]`；只跟踪规划，不承担任务认领、依赖执行与后台队列 |
| s06 Subagent | `s06_subagent/code.py:276–278` | 采用一次任务、隔离消息的专职子 Agent | 默认 `mode="isolated"`；提供完整任务包和只需工具，结果返回可验证报告与产物引用 |
| s07 Skill Loading | `s07_skill_loading/code.py:59,134` | 采用业务 Skill 渐进加载 | 主与自定义子 Agent 分别声明 `skills`；开发指导 Skill 不整包装进业务运行时 |
| s08 Context Compact | `s08_context_compact/code.py:254,534` | 采用结果转存、按需读取、自动摘要 | 使用 SDK summarization；摘要不是完整事实库，商品/场次/计划原文以版本引用保留 |
| s09 Memory | `s09_memory/code.py:114–206` | 首期保存已确认业务数据、线程进度、分析产物；暂不做自主跨场记忆学习 | checkpoint、StateBackend 与声播业务库各司其职；`MemoryMiddleware` 不自动实现教程全部筛选/整合算法 |
| s10 Task System | `s10_task_system/code.py:75–84,225–254` | 借鉴依赖、状态和完成证据；只做准备任务/作业所需最小记录 | Todo 不是任务系统；应用 Task/Job 记录保存 attempt、输入版本、产物与错误，不复制整个通用团队任务平台 |
| s11 Background Tasks | `s11_background_tasks/code.py:348–447` | TTS、OCR 等慢任务使用后端异步作业，返回 job_id | 普通 `task` 会等待结果；本地作业取消、重启恢复需应用服务；不用线程 daemon 假称持久任务 |
| s12 Cron Scheduler | `s12_cron_scheduler/`；`README-zh.md:190` | 首期不采用定时自动准备或自动开播 | 不因教程有本章增加定时器、云自动化 |
| s13 Agent Teams | `s13_agent_teams/code.py:1384–1504` | 采用职责分工思想，首期不做持久队友、收件箱、Worktree 协作 | SDK isolated 子 Agent 不等于教程常驻队友；当前需求用按次委派和持久产物足够 |
| s14 MCP Plugin | `s14_mcp_plugin/`；`README-zh.md:194` | 需要外部能力时使用；声播首选窄接口直连适配 | MCP 可作为工具来源，非必选传输协议。业务工具契约应独立于 HTTP/CLI/MCP |
| s15 Integrated Harness | `s15_integrated_harness/`；`README-zh.md:196` | 采用统一入口、统一规则、统一证据的整合思想 | 集成到 Deep Agents 现有中间件和工具，不运行两套 Harness |
| s16 Workflow Runtime | `s16_workflow_runtime/code.py:329,752–802` | 采用可被主 Agent 选用的稳定子流程 | 教程把预存 workflow 安装成 `Workflow` 工具；本项目可把折扣计算、开场映射、所需 TTS 清单等封成确定性服务/局部 workflow，非全局必走流水线 |
| s17 Goal Loop | `s17_goal_loop/code.py:275,364–428,542` | 采用完成条件与有界补做；无需首期强制第二个 LLM 裁判 | 应用证据/ready 检查为基础；若自动补做，使用支持模型跳转的 middleware 或外层有界运行控制。`after_agent` 不能直接等同 Stop 拒绝退出 |

教程的新版 s16/s17 正好区分固定子编排与目标收口，但不是“模型可以忽略业务前置条件”。可选的是怎样达成目标，价格冲突未解决不可输出 ready 等条件由服务端强制执行。

## 3. 本机 SDK 已核实语义

源码基目录：`D:/Codex project/shengbo-ops-agent/.venv/Lib/site-packages/`。以下相对路径均相对此目录。

| 核验事项 | 本机源码证据 | 对设计的约束 |
|---|---|---|
| 工厂/运行时 | `deepagents/graph.py:277` 定义工厂；`:989–1004` 调用 `create_agent` 并配置图 | 入口确实是 Deep Agents；框架已提供模型循环和 LangGraph 执行基础 |
| Todo 显式启用 | `deepagents/graph.py:889–952` 组装栈无 Todo；`profiles/harness/_openai_codex.py:69–77` 明确 SDK 不再默认启用 | 本项目显式添加 `from langchain.agents.middleware import TodoListMiddleware`；静态 core/orchestration Skill 的“默认自动包含”已过时。[v0.7 发布说明](https://www.langchain.com/blog/deep-agents-v0-7) |
| 默认 general-purpose | `deepagents/graph.py:817–886` 自动创建通用子 Agent | 必须审查其权限与工具。显式命名同名子 Agent 可替换；不要遗漏一个拥有父工具的通用写入通道 |
| 自定义工具继承 | `deepagents/graph.py:785–794` 缺省继承父工具 | 每个业务子 Agent 显式写最小 `tools`，不要依赖默认继承 |
| isolated / fork | `middleware/subagents.py:84–90,197`；`graph.py:707–710` | 首期选 isolated。0.7.25 支持实验 fork，不能声称 SDK 所有子 Agent 永远只收一条任务消息。fork 会继承父会话及 prompt，有单独边界 |
| 消息隔离不等于全部状态隔离 | `middleware/subagents.py:382–414,782–813` | isolated 重建 messages，但其余非排除/非 private state 会传下去；`files` 为共享状态。主权威商品对象不放共享 state |
| 子结果回并 | `middleware/subagents.py:725–765` | 非排除且变化的 state 会回并；`structured_response` 序列化为 ToolMessage，否则抽取最后非空 AI 文本。避免多个子 Agent 各自写主状态；用 scoped 产物工具与主 Agent 显式合并 |
| 私有状态 | `langchain/agents/middleware/types.py:345`；`deepagents/graph.py:971–976` | `from langchain.agents.middleware.types import PrivateStateAttr`；若必须扩展主状态，用 `Annotated[..., PrivateStateAttr]` 标记不下传/回并的字段。它不是密钥保险箱，也不能替代工具授权 |
| 自定义 state 类型 | `deepagents/graph.py:535–555` | 自定义 `state_schema` 应继承 `DeepAgentState`，避免丢掉 messages 的 SDK reducer；首期尽量仅存引用与运行标识 |
| 子 Agent Skills | `deepagents/graph.py:707–738` | isolated 子 Agent 必须显式声明所需 Skills；不自动继承主 Agent 的全部 Skills 或 memory middleware |
| HITL 继承 | `deepagents/graph.py:511–528,779–783` | 声明式子 Agent 默认继承顶层 `interrupt_on`，自己声明时覆盖；已编译和远程子 Agent 不自动继承。避免方案声称统一配置天然覆盖所有类型 |
| 多 task 并行 | `langgraph/prebuilt/tool_node.py:829–831,836–866`；`middleware/subagents.py:846–875` | 同一模型响应内多个工具调用可并发。需模型确实产出多调用、异步调用链和无冲突写入；仅在 prompt 写“并行”不能作为并行验收证据 |
| 后台子 Agent | `deepagents/graph.py:916–920`；`middleware/async_subagents.py:39–86,329–385,659–735` | `AsyncSubAgent` 是 Agent Protocol server/graph 运行，不是普通本地 `task` 加 async 就自动后台化；首期本地 TTS 作业不依赖该部署能力 |
| 工具 hooks | `langchain/agents/middleware/types.py:674–820` | 同步/异步拦截方法对应运行方式；API 服务用 async 必须实现 `awrap_tool_call` 或兼容装饰器 |
| Stop 差异 | `middleware/types.py:479–501,650–673,881–927` | `after_agent` 位于完成后；需要继续模型循环的逻辑应在可跳转 `after_model` 等位置返回合法 state/jump，或应用外层限次续跑；不能从结束回调随意 return 字符串期待继续 |
| 中间件组装顺序 | `deepagents/graph.py:369–424,923–959` | 新 middleware 放 core 后、Skills/memory tail 前；同名替换。业务 guard 放工具包装或入口，不假设 before_agent 已看到本轮 freshly loaded skills_metadata |
| 自动摘要与转存 | `middleware/summarization.py:269–300,524`；`graph.py:910–915` | 默认阈值依赖模型 profile；无 profile 时回退阈值可能不适合实际 DeepSeek 端点，开发时应明确 token 预算并测试。原始信息用工件保存，不承诺摘要无损 |
| MemoryMiddleware | `middleware/memory.py:193–241,279–340` | 读取配置文件并注入，已装载 state 可被复用；商品/活动变更不能期待启动 memory 自动实时刷新，必须通过业务引用重新取数 |
| StoreBackend 构造 | `backends/store.py:100–120` | 0.7.25 为 `StoreBackend(*, namespace=..., store=None)`；旧 `StoreBackend(runtime)` 示例不适用。首期无需跨线程 Store，可不启用 |

重要：SDK 默认 `recursion_limit=9999`（`graph.py:1004`）不是项目可接受的成本预算。应用需给模型轮数、子任务数、工具超时、总时限、token/cost、修复次数各自设界，不能只依赖 recursion_limit。

## 4. Web 应用的 Skills、文件和检查点建议

建议使用 `StateBackend()` 保存线程内虚拟工作文件，在首次创建准备任务时，由可信服务加载**审核过的业务 Skill 包**到初始 `files`。使用 `/skills/.../SKILL.md` 等 POSIX 虚拟路径；不把商家任意上传内容当作 Skill，不让模型读宿主 `.env` 或项目根目录。

已核实的 API 形态（配置示意，不代表已实施）：

```python
from deepagents import create_deep_agent, FilesystemPermission
from deepagents.backends import StateBackend
from deepagents.backends.utils import create_file_data
from langchain.agents.middleware import TodoListMiddleware

# checked_skill_text 来自服务审核包，不来自商品附件或远程临时抓取。
initial_files = {
    "/skills/catalog-preparation/SKILL.md": create_file_data(checked_skill_text),
}

agent = create_deep_agent(
    model=verified_model_instance,
    backend=StateBackend(),
    skills=["/skills/"],
    middleware=[TodoListMiddleware(), business_guard_middleware],
    permissions=[
        FilesystemPermission(operations=["write"], paths=["/skills/**"], mode="deny"),
    ],
    checkpointer=persistent_checkpointer,
    subagents=scoped_isolated_subagents,
    tools=domain_tools,
)
# 每个新线程预置一次；恢复既有线程不无条件覆盖历史工件。
agent_input = {"messages": user_messages, "files": initial_files}
config = {"configurable": {"thread_id": preparation_thread_id}}
```

证据：`backends/state.py:50–76` 明确在 `invoke` 输入中预置 files；`graph.py:467–473` 明确 Skills 的 backend 相对路径；`backends/utils.py:513–535` 为 `create_file_data`；`middleware/filesystem.py:367–405` 为权限规则。官方也给出同形示例：[Skills](https://docs.langchain.com/oss/python/deepagents/skills)。

这里的 StateBackend “ephemeral”是**线程作用域**，不等于“有持久检查点仍然重启就丢”：同一线程 files 随 checkpoint 持久保存；它不跨不同线程自动共享。业务库仍是正式数据的单一来源。官方对 Web 服务建议 StateBackend/StoreBackend 等而非直接暴露本机 FilesystemBackend：[后端选择](https://docs.langchain.com/oss/python/deepagents/backends)。

| 对象 | 首期保存位置 | 说明 |
|---|---|---|
| 正式商品、知识库、场次配置、脚本版本、音频作业 | 声播后端业务服务与数据库 | 主 Agent 通过窄接口读取/更新，不直接写数据库 |
| 准备任务、确认项、产物引用、输入版本、任务 attempt | Agent 应用任务服务 | 支持重新进入、取消、旧结果拒绝合并与 UI 展示 |
| 模型 messages、todos、虚拟工作文件 | LangGraph checkpoint | 同 thread_id 恢复；不替代业务动作日志 |
| 子任务证据和草案 | 按 preparation_id/task_id/attempt_id 分区的 artifact 工具 | 互不覆盖；主 Agent 读回验证后合并。即使底层 files 共享，也不以共享可变字典承担业务合并 |
| 稳定业务 Skills | 服务打包内容 → 各新线程 files | 固定包版本与内容 hash；写权限 deny；升级时新线程或显式迁移，不暗改执行中的规则 |
| 跨会话自主学习记忆 | 首期不启用 | 不影响正式商品复用，正式商品本来就从业务服务跨场复用 |

**持久检查点依赖尚未安装。**本机 `langgraph/checkpoint` 只有 `base`、`memory`、`serde`，无 sqlite；distribution 检查确认 `langgraph-checkpoint-sqlite` 不存在。开发阶段应安装与当前 checkpoint 包兼容的版本并锁定；不要把 `MemorySaver()` 称为跨进程恢复。

- 异步 FastAPI 路径候选：`from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver`，`async with AsyncSqliteSaver.from_conn_string(db_path) as saver:`；在应用 lifespan 内持有连接，搭配 `ainvoke/astream`。
- 同步路径候选：`from langgraph.checkpoint.sqlite import SqliteSaver`，`with SqliteSaver.from_conn_string(db_path) as saver:`；搭配同步调用，不能混用为异步 saver。
- API 名称由[官方 SQLite checkpoint reference](https://reference.langchain.com/python/langgraph.checkpoint.sqlite)核实，当前本机未安装，因此未做 SQLite 实际恢复测试。单进程开发先采用 SQLite；未来多进程部署再评估专门持久后端，不在首期强加云服务。
- 恢复 checkpoint 不能保证外部 API 只执行一次。商品保存、生成脚本、TTS 作业仍需业务层 idempotency_key、expected_revision、持久 operation_id 和读回验证。

## 5. 动态主循环、确定性约束与局部 workflow 的分工

| 行为 | 谁决策 | 谁强制执行 |
|---|---|---|
| 本轮用户需要全部准备，还是仅改活动/增补商品 | 主 Deep Agent 判断意图与已完成产物 | 准备记录、修订号、范围检查 |
| 是否需要研究顾客/场景、是否复用已有分析 | 主 Agent 结合证据、版本和用户决策 | 任务前置条件、产物新鲜度检查 |
| 2A/2B/3 哪些可以同时处理 | 主 Agent 委派合适工作，局部执行器承载批次 | 不同输出作用域；明确 join 条件；结果输入 revision 匹配 |
| 是否缺少商家事实，需要何时询问 | Agent 提出缺项，主 Agent 合并询问 | 缺少必要价格/条件不得编造，不把假设作为商品事实 |
| 1、6、11 项长开场以及所需音频清单 | 规则已由用户确定，不交模型自由决定 | 确定性纯函数/服务；主 Agent 可按需调用 |
| 是否达成可人工确认的准备结果 | Agent 描述状态；验收器根据证据判断 | ready gate 输出证据清单，禁止越过缺项/冲突/旧产物 |

只有明确的局部编排需要才额外写 `StateGraph`；可以作为工具或 `CompiledSubAgent` 接入。`CompiledSubAgent` 需兼容 `messages`，自己的 HITL/state_schema 单独配置。首期没有理由为了展示 LangGraph，再复制一个与主 Agent 竞争决定下一步的顶层业务图。

## 6. DeepSeek 接入：可用适配器与待实测项

本机 `langchain_deepseek/chat_models.py:110` 提供 `ChatDeepSeek`；`:511–552` 为 `bind_tools`，支持 `tool_choice/strict/parallel_tool_calls` 参数；`:555` 起为 structured output。**这些是适配器能力，不证明指定账号、模型和端点已全部通过。**先前离线测试只用 fake model 完成模型—工具闭环。

DeepSeek 自家当前[Thinking Mode 文档](https://api-docs.deepseek.com/guides/thinking_mode/)说明支持思考模式工具调用，且携带 tools 的多轮请求需要正确回传 `reasoning_content`；LangChain 某集成页面仍写旧 R1 限制。不得沿用旧结论“所有 reasoner 都不支持工具”，也不凭 SDK 签名保证新端点兼容。最终模型标识、思考开关和服务端能力在实现小探针时核定。

| 待验证项 | 最小通过证据 |
|---|---|
| 真实单工具循环 | 一次有效调用 → schema 合法 → 工具执行 → 模型据结果完成 |
| 多工具并发 | 同轮至少两个独立工具调用；事件时间重叠；无写入冲突；若模型不支持则并发由受控批次工具实现 |
| schema/结构化子报告 | Pydantic 解析通过；不合法返回有界重试/明确失败；选择 ToolStrategy 或已验证 provider strategy |
| 思考模式与续轮/恢复 | 工具前后及检查点恢复不丢必须字段、不产生 400；不把内部 reasoning 原文当用户所需轨迹 |
| OCR/截图解析 | 实际模型能否看图单独核验；不假设文本 DeepSeek 自动理解上传截图。OCR 结果和原图引用分开保存 |
| 文本上下文上限/摘要 | 根据实际模型 profile/官方限制设置阈值，超限前摘要，关键事实/确认项仍可从正式引用读回 |
| HITL 暂停和继续 | 同 thread_id 恢复；不存在重复副作用；用户编辑价格后旧计划/旧生成结果拒绝提交 |
| 取消与后台完成 | 应用取消标记立即使后续提交失效；外部已提交任务晚到只归档，不重新激活旧计划 |

## 7. 开发验收时必须排除的语义错误

1. 把 SDK 内置 Todo 当成依赖队列、持久团队任务与完整恢复系统。
2. 把 isolated 的消息隔离理解为所有 state/文件天然隔离。
3. 主与子 Agent 同时改正式商品/主计划，靠最后写入覆盖解决并行冲突。
4. 把静态 Skills 默认行为当成 0.7.25 的已核实契约。
5. 把 `after_agent` 当成可拒绝停止的 Stop Hook。
6. 在 Web 服务给 Agent 一个可见密钥的宿主 FilesystemBackend，或把商品上传文件当新技能自动执行。
7. 把 MemorySaver、StateBackend 的线程语义和持久业务库混为一谈。
8. 把普通子 Agent 的 `ainvoke` 误称为 SDK 后台部署任务。
9. 把流程依赖顺序变成每次都强制从头执行的顶层流水线。
10. 以“已导入 SDK、已编译图”宣称真实 DeepSeek、OCR、SQLite 恢复和声播接入已验收。

## 8. 本轮核验状态与证据指纹

已完成：读取核心及差异 Skill；检查本机 s01–s17 目录/README 与关键源码；检查 SDK 工厂、子 Agent、文件/Skills、memory、middleware 和模型适配器；核对官方 live 文档；无模型调用导入/签名检查成功。未执行任何真实业务验收。

| 文件 | SHA256 |
|---|---|
| `D:/Codex project/learn-claude-code/README-zh.md` | `F8604EE2D70FB98A0138182E39418E47801960C5519E0252301A595272D5566C` |
| `deepagents/graph.py`（本机0.7.25） | `B0A5E4BD56C07A35DB2F4EE62666F28DB99D1D78E60F3C5F8C52553C642CE2FC` |
| `deepagents/middleware/subagents.py`（本机0.7.25） | `0EB0B232E0707F1F679CD20D8CD9357B6E91BFEF4DD89B09789F13B2D62E9156` |

其他官方核对入口：[Deep Agents 定制](https://docs.langchain.com/oss/python/deepagents/customization)、[子 Agent](https://docs.langchain.com/oss/python/deepagents/subagents)、[上下文管理](https://docs.langchain.com/oss/python/deepagents/context-engineering)、[HITL](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop)、[自定义 middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom)。框架升级时以锁文件、源码和对应文档重新核验上述差异。
