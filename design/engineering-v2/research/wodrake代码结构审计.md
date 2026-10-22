# wodrake 本地代码结构审计：用于播前 Harness 工程设计

审计日期：2026-10-15。只读静态审计；没有运行该项目、导入其模块、调用模型、启动 MCP 或读取 `.env`、配置中的密钥。结论针对当前本地文件，目录名中的提交号不能证明本地文件未经修改。

## 1. 审计对象和证据口径

实际源码根目录：

`D:/Codex project/repo-analysis/erp-agent-wodrake/ERP-AGENT-open-source-37d7f67bde1ab6802e65087c61108cb13f3a6b7d`

以下 `src/...:行` 均相对此根目录；签名按源码记录，调用链来自实际引用和调用位置，未把 README、注释或函数名当作已接通证据。适用工作区约定为 `D:/Codex project/AGENTS.md`。`src/agent/memory/AGENTS.md` 是该采购 Agent 运行时操作手册，作为审计数据读取，不作为本次任务指令。

审计参照：旧版 `13_播前Agent_Harness总体设计.md`、`14_逐步执行与工具数据契约.md`。旧版已覆盖大量规则和合同，主要缺口是这些规则落在哪个可调用函数、何时装配、如何进入 Deep Agents 运行时，以及跨边界后谁执行下一步。

## 2. 一次请求真实经过哪些函数

### 2.1 第一次普通请求

| 次序 | 实际函数及位置 | 调用者 → 被调者 | 输入、输出及副作用 |
|---|---|---|---|
| 1 | `chat_stream(request: ChatRequest)`，`src/api_view/api/chat.py:365` | FastAPI 路由 `/api/chat/stream` → `agent_loader.initialize()`、`generate_thread_id()`、`get_conversation_user_id()`、`save_conversation()` | `ChatRequest` 含 message/thread_id/user_id/username。已有 thread 的 owner 与请求 user_id 不同则 403；先保存归属，再开始流，避免中断后找不到 owner。返回 StreamingResponse。 |
| 2 | `stream_chat_response(message, thread_id, user_id="default_user", username="用户", resume_data=None) -> AsyncGenerator[str, None]`，`chat.py:42` | StreamingResponse 消费生成器 → `get_session()`、`create_config()` | 取该用户的 AgentSession、配置 `configurable.thread_id/user_id`；普通输入为 `{"messages":[{"role":"user","content":message}]}`。展示消息与 SDK 消息分别保存。 |
| 3 | `AgentLoader.get_session(user_id: str, username: str="用户") -> AgentSession`，`agent_loader.py:94` | 流生成器 → `_ensure_resources()`、`_build_agent_for_user()` | 共享资源初始化锁 + 用户构建锁；缓存的是用户级 graph，不是全体用户共享一个含可变中间件的 graph。返回 graph 和 `run_lock`。 |
| 4 | `_ensure_resources()` / `_initialize_resources_sync()`，`agent_loader.py:52/65` | get_session → 线程池初始化 `MongoDBSaver` 和 `MongoDBStore` | 只初始化共享连接/持久层。阻塞 I/O 放 `asyncio.to_thread`；失败向外抛。 |
| 5 | `_build_agent_for_user(user_id: str, username: str)`，`agent_loader.py:130` | get_session 的 `asyncio.to_thread` → `create_main_agent(...)` | 构造 ProcurementContext，传入同一 checkpointer/store；未调用旁边定义的 `precompute_agent_context()`。 |
| 6 | `create_main_agent(user_context: Optional[ProcurementContext]=None, checkpointer=None, store=None)`，`src/agent/main_agent.py:187` | loader → 后端、工具、子 Agent、中间件装配 → `create_deep_agent()` | 一处完成组装；`create_deep_agent` 具体调用在 :401。返回 CompiledStateGraph，供后续多次请求使用。 |
| 7 | 流内资源绑定，`chat.py:94` | stream_chat_response → `session.run_lock.acquire()`、`acquire_lease(user_id)`、`bind_sandbox(user_id)`、`bind_runtime(user_id, store)` | 锁避免同用户同时操作同工作空间。ContextVar 将本次用户/store 传给工具，不让模型参数决定身份。 |
| 8 | `session.agent.astream(input=current_input, config=config, stream_mode=["messages","values","custom"], subgraphs=True)`，`chat.py:103` | 流生成器 → SDK graph | 模型循环、工具节点、子图都在 SDK 内执行；应用没有再写模型总循环。返回不同命名空间的模型 token、状态与自定义事件。 |
| 9 | 流解析，`chat.py:110/205/231` | astream 数据 → SSE | values 处理 phase/todos/interrupt；custom 处理 rubric 评审；messages 处理工具和文本。内部 review_router 消息按 tag 过滤，避免变成用户答复。 |
| 10 | 正常收尾，`chat.py:332` | 流结束 → `save_display_messages()`、`save_harness_trace()`、`save_conversation()` | 保存展示消息与本轮 trace，再发 done；异常分支发 error 和 done。 |
| 11 | `finally`，`chat.py:356` | 任意退出 → `reset_runtime()`、`reset_sandbox()`、`release_lease()`、`run_lock.release()` | 减少上下文串扰、资源占用。需要注意锁取得和几次绑定发生在 try 之前，若绑定中途抛异常，未必进入这个 finally；新设计应将获取也纳入结构化清理范围。 |

这条链表明工程方案必须给出“请求 → run/session 服务 → graph factory → SDK → 工具 → 业务服务 → 持久层 → 事件 → 收尾”的连续路径。只有“主 Agent 会协调”不能替代该路径。

### 2.2 工具动作实际如何落到后端

采购创建订单示例：

1. `main_agent.py:273` 调用 `load_mcp_tools_sync()`；其异步实现 `src/agent/tools/mcp_client.py:30` 使用 `MultiServerMCPClient.get_tools()`（:61），获得 BaseTool 对象。
2. 这些对象与本地工具组成 `all_tools`（`main_agent.py:282`），经角色过滤后供主/子 Agent 使用。
3. SDK 选择工具并执行；订单创建的服务端函数是 `register_order_tools(mcp: FastMCP)` 内的 `order_create(order_data: str) -> str`（`src/mcp_server/tools/order_tools.py:11/15`）。
4. 该函数解析 JSON，调用 `erp_client.post("/api/orders/create", json_data=data)`（:42）。
5. `ERPHttpClient.post(path: str, json_data: Optional[dict]=None, params: Optional[dict]=None) -> dict`（`src/mcp_server/http_base.py:48`）经共享 httpx 连接池请求后端，交 `_handle_response(response)`（:74）归一。
6. 返回字符串 JSON，SDK 将它放入 ToolMessage，下一次模型继续。

可借用的是“业务客户端 → 类型化工具 → SDK 注册”的连接层次。该项目 MCP 装载失败会返回空列表继续运行（`mcp_client.py:84`）；声播首期若缺少必须的商品/房间/生成工具，应在启动能力检查失败，不能把无业务入口的 Agent 宣称为完整可用。当前声播不必为了模仿该项目再加入 MCP 服务进程。

## 3. factory 是怎样将零件真正接起来的

| 装配阶段 | 代码证据 | 实际行为 | 声播工程设计必须明确的对应物 |
|---|---|---|---|
| 模型实例 | `main_agent.py:217`：`llm=get_llm()`，另建 grader | 主模型与评审模型独立 | `build_model(settings)` 的返回类型、超时、token 上限、tool-call 配置、主/子是否复用连接；首期不因示例另加模型裁判。 |
| 后端 | :229 取 sandbox；:230 包 Stable Proxy；:258 CompositeBackend | 默认工作区走 sandbox，`/memories/` 与 `/persisted-skills/` 路由 StoreBackend | 明确主/子 backend 每次如何获得、逻辑目录如何映射、哪些只读、输出只写自己的 task 目录；不复制 Docker/Bash。 |
| 工具清单 | :273—291 | MCP + 本地业务工具 + 可选记忆工具 + Skill 阅读器 | `build_tool_registry(deps)` 返回唯一注册表；每个角色按精确 tool_id 取子集，启动时验证缺失、重复与不允许的能力。 |
| 子 Agent 规格 | :299—304 | load YAML → resolve 工具 → attach Skill middleware → 生成委派提示段 | `build_worker_graphs()` / `build_worker_specs()` 明确顺序、编译入口、模型、状态、工具、输出校验和取消；不能只有角色表。 |
| middleware | :353—380 | 实例化业务 middleware，追加调用上限；同一 graph 复用这些实例 | 单独 `build_middlewares(role, deps)`；画真实 hook 顺序、同步/异步实现、状态字段读写和异常边界；不得把每轮状态藏进实例成员。 |
| prompt | :383—396 | 角色模板 + 委派协议 + Skill 槽；WARM 记忆槽在调用前刷新 | 静态政策和动态快照分别构造；动态上下文用 request.override 保留已有 system blocks；所有内容的来源、版本、预算可追溯。 |
| 最终 SDK 装配 | :401—414 | 一处传 model/tools/system_prompt/middleware/subagents/skills/memory/backend/interrupt_on/checkpointer/store/name | 工程设计给完整可实现的 factory 签名与调用参数。主图和 worker 的构建关系、checkpointer 生命周期必须在这里闭合。 |

源码注释也会过时。`main_agent.py:328` 声称 TodoList 等均自动添加，不能据此决定声播 0.7.25 的配置。以本项目实际安装 SDK 源码和匹配文档核对，并在 factory 显式装配必要的待办中间件。

## 4. 中间件的真实触发、输入与返回

| 类/函数 | 触发点与实际调用 | 读写/返回 | 工程启示 |
|---|---|---|---|
| `HarnessPhaseMiddleware.before_agent(state, runtime)`，`harness.py:278`；`abefore_agent` :287 | graph 一次运行开始 | `ReviewPolicy.start/astart(state)` 返回的字段与 phase=planning 更新；不是执行商业计划本身 | 可借用状态记录形式；播前运行快照应由服务构造，不能把“planning”枚举当可执行计划。 |
| `HarnessPhaseMiddleware.after_model`，:290 | 每次模型响应后 | 最后一条 AIMessage 有 tool_calls 时 phase=executing；todos 经 `_build_plan_from_todos()` 变 plan | 模型待办只作工作提示；声播依赖由真实产物/修订判断，不能由 todos.completed 证明已保存或 TTS 成功。 |
| `ContextInjectionMiddleware.before_agent`，`middlewares/context_injection.py:45` | 运行开始 | 从内部字典读取 ctx、打印日志，**return None，不注入 state 或 request**（:56—58） | 这是“声明有功能、链路未实现”的直接例子。声播必须明确 ContextAssembler 返回什么，以及由哪个 wrap_model_call 将它实际放入请求。 |
| `WarmMemoryMiddleware._refresh(request)`，`middlewares/warm_memory.py:16` | `wrap_model_call` :37 / `awrap_model_call` :40 | 从 Store 构建最多 4000 字 brief；替换指定文本槽，保留其他 structured system blocks，返回 request.override(system_message=...) | 是可参考的动态上下文装配；首期不需要自动长期记忆，但同样方法可注入当前准备快照，不能覆盖 SDK 的工具/Skill 提示块。 |
| `ProgressiveSkillsMiddleware.refresh(request)`，`progressive_skills.py:276` | wrap_model_call :288 / async :291 | 获取元数据目录，替换 SLOT 或追加块，返回 override | 按需披露 Skill 可参考；不要同时启用官方完整 Skill 加载和另一套重复正文注入。声播优先 SDK 原生机制。 |
| `ReviewExecutionGate.after_agent`，`middlewares/review_gate.py:51` | after_agent 逆序；位于 rubric 后注册，使其先执行 | 根据 tool-call 和失败信号更新 review 决策；需要时补 rubric | 不可机械复制成通用完成判断。声播完成应使用业务校验结果；副作用记录应来自 tool_id 元数据，不猜工具名含 create/update。 |
| `SafeRubricMiddleware.after_agent/aafter_agent`，:72/78 | after_agent 评审 | 有写/委派时转一次性 reviewer，避免评审回路重放写操作 | 可借用“读分析可重做，正式写不因反思盲重放”原则；声播通过账本、幂等与修订守卫实施。 |
| `MemoryUpdateMiddleware.after_agent`，`middlewares/memory_update.py:44` | Agent 结束 | 只提取最后 human 起的本轮消息，合并用户偏好到 store；异常降级，不改 graph state | 声播首期不启用这套自动偏好提取；活动价格、优惠等不能被摘要抽取后当正式配置。 |
| `MemoryConsolidationMiddleware.after_agent`，`middlewares/memory_consolidation.py:37` | Agent 结束 | summarize_episode → keeper.archive_episode(thread_id, ...) → keeper.forget()；无稳定 ID 则跳过 | 业务事实与对话摘要应隔离；重启恢复不能靠记忆摘要还原发布状态。 |

必须防止的接线漏洞：

- `src/agent/middleware_config.py:31/61/90` 定义 analyst/order 专用中间件工厂，但源码调用搜索没有发现 main_agent/loader 调用这些工厂；`resolve_subagent_tools()` 创建规格只放 name/description/system_prompt/tools/interrupt_on，未放该工厂结果（`subagents/loader.py:249`）。因此不能声称这些“子 Agent 完整栈”已装好。
- 真正被接入子 Agent 的是 `attach_to_subagents()` 添加的 `ProgressiveSkillsMiddleware`（`progressive_skills.py:295—300`）。这也是声播应写装配检查的原因：不仅验证类存在，还要检查 compiled worker 的实际工具和 middleware。
- `ContextInjectionMiddleware.update_context()`（:68）和 `precompute_agent_context()`（main_agent.py:143）有定义，但当前 Web 主链未见调用。凡工程方案写“每轮刷新”都应标注真实 caller，不能保留孤立备用函数后当已接通。

## 5. 子任务、权限与上下文合同

`load_subagent_configs() -> list[dict]`（`subagents/loader.py:153`）读取声明文件；`resolve_subagent_tools(configs, all_tools) -> list`（:187）构建规格；`get_delegation_context_prompt(configs) -> str`（:266）把输入字段写入主 prompt。

这里有两类不同强度的约束：

| 机制 | 实际强度 | 声播采用方式 |
|---|---|---|
| 只给指定工具对象 | 限制模型可选择的外显工具，但不替代服务端鉴权 | 采用精确白名单；研究 worker 不能持有 publish/update/submit_tts/start_live。 |
| context_protocol / output_format 提示词 | 当前主要是 prompt 拼接（:245—253/:293—305），不等于运行时验证 | 声播必须提供 typed TaskInput 和 WorkerReport 验证函数，失败不提交产物。 |
| 工具字符串匹配 | 当前精确 > 分组前缀 > 子串兜底（:218—228） | 不采用兜底模糊匹配；拼错工具名启动失败，避免多拿写工具。 |
| 请求 ContextVar | 在 Python 调用上下文传真实 user/store（runtime_context.py:20/25） | 可用 SDK runtime context 注入，兼容层才用 ContextVar；身份不暴露为可由模型填写的 account_id。 |
| 用户级 AgentSession + run_lock | 保护同用户共享工作区 | 声播按 preparation/thread 运行租约隔离，另用商品/房间修订冲突保护共享资源；不同准备任务不宜全部按用户串行。 |

`build_output_contract_prompt(config)`（loader.py:79）只是将格式描述转提示；本地静态审计未见它将模型返回直接解析成 Pydantic 报告并校验。声播设计必须把 `validate_worker_report(raw, task_snapshot)` 接在子任务返回与产物保存之间，同时验证 evidence_ref、字段范围、source revision 和目标版本。

主/子上下文不能只写“传完整信息”：必须给 immutable manifest、读取授权、必读清单、分页 continuation、读取回执，以及 worker 可写输出前缀。当前项目的 Skill 阅读器是这类显式通路的可参考实现，见下一节。

## 6. 文件上下文与按需阅读的实际入口

`SkillCatalog.tools()`（`progressive_skills.py:225`）创建三个工具：

| 工具签名 | 位置 | 返回关键字段 |
|---|---|---|
| `list_skills(query: str="", offset: int=0, limit: int=20) -> str` | :234 | skills、total、next_offset、warnings；只取元数据，limit 1—30。 |
| `read_skill(skill_id: str) -> str` | :246 | content、complete=True、sha256、execution_path；正文超限直接失败，不伪装成完整内容。 |
| `read_skill_resource(skill_id: str, path: str, offset: int=0, max_chars: int=12000) -> str` | :256 | content、total_chars、complete、next_offset、sha256；可分页读取。 |

关键实现：

- `safe_relative(value: str) -> str`（:34）拒绝绝对/父级/隐藏路径；`allowed_resource(value)`（:43）做文件类型、敏感文件名过滤。
- `SkillCatalog.local_path(relative, base=None)`（:77）拒绝符号链接和越根路径；`read(skill_id, resource=None)`（:176）防止通过父 Skill 读取其他嵌套 Skill 的资源。
- `index()`（:209）只注入元数据，上限 8000 字；正文由工具按需读取。动态目录不是把全部文档粘进系统提示词。
- `guarded()`（:226）返回受控错误，不泄露内部异常详情、存储内容和凭据。

声播可复用的设计思想：`read_context(ref, section, cursor, max_chars)` 返回原文片段、total、next_cursor、完整内容 hash；保存读取收据，后续生成模块校验 required sections 已读全。不能仅在 ContextBundle 中存一个 URL 就断言模型已读取，也不能将截断部分标为 complete。

不用照搬的部分：下载/安装任意 Skill、脚本执行、项目整体上传 sandbox、用户 Skill 热更新、全局服务器文件系统浏览。首期只读取项目随版本发布的业务 Skill 和用户授权材料。

## 7. 人工补充与审批恢复实际如何衔接

`request_order_info(extracted_data: str, missing_fields: str) -> str`（`tools/hitl_tools.py:158`）解析输入，在最多 5 轮内调用 `interrupt({...})`（:187）；恢复值被解析、合并，再 `validate_order_data(data)`（:205）。它不在 interrupt 前调用订单创建。

返回链：SDK interrupt → `astream` values → `chat.py:157` 解析 interrupt payload → 持久展示消息和 trace（:198）→ done(interrupted=True) → return。原始执行栈退出、finally 释放资源。

恢复链：`chat_resume(thread_id: str, request: ResumeRequest)`（`chat.py:396`）→ 从 conversations 查询 user_id → 同一 `stream_chat_response(..., resume_data=request.resume)` → `Command(resume=resume_data)`（:67）→ 同一 `configurable.thread_id` → SDK 从 checkpoint 恢复。

这里应借用的是完整暂停/恢复链；以下细节不能直接继承：

1. resume/state/history 路由没有展示经过认证的当前主体与归属核验。由 thread 查询 owner 并选择其 graph，不等于对请求者鉴权；声播需在应用入口取得真实本机/账号主体，再对 preparation/thread 验证。
2. request_order_info 用自由文本正则提取补充；声播应接 `QuestionBatch`、typed answers、question_id、对象 revision，防止“107”被配到错误商品。
3. 源码用 `if resume_data` 判断恢复；空 dict 会进入普通消息分支。声播应显式判断 `resume_data is not None`，再验证结构。
4. 文件中有补充循环，但没有声播所需的 durable question repository、幂等 upsert、旧问题失效和目标变更处理。它不能替代新的问题服务。
5. `chat_state` 用 bool(state.next) 当 interrupted（:427）；运行待续节点、失败与真正待人工输入需要区分。声播响应应读取保存的 waiting_reason 和 SDK tasks/interrupt，不能只有一个布尔值。

## 8. 会话、记忆、持久化和后台能力的实际边界

| 数据/能力 | 代码实际做法 | 对本项目的结论 |
|---|---|---|
| graph checkpoint | MongoDBSaver 注入 factory（agent_loader.py:76/main_agent.py:411） | 可借用生命周期；声播首期按原计划选 SQLite，不因示例换 MongoDB。 |
| Store | MongoDBStore → BaseStore get/put/search/batch，`mongodb_store.py:108/116/139/232` | 通用跨会话存储接口；不应代替声播正式商品库，也不必首期启用自动长期记忆。 |
| 展示消息 | save/get_display_messages，agent_loader.py:159/168 | 与 SDK messages 分开值得保留；声播后端事件输出也要区分模型文字、业务事实和最终结果。 |
| trace | save_harness_trace，:207，按 thread 用 $set 覆盖 trace | 当前 trace 为本轮数组，后次 $set 会覆盖先前 trace；声播应追加事件序列，包含 run/task/action 与 cursor。 |
| 摘要记忆 | MemoryKeeper.build_warm_brief，keeper.py:387；search :414 | 参考“精简常驻＋按需完整读取”，不照搬其语义/情节/程序三层记忆。首期业务任务无需另建这套系统。 |
| 后台任务 | web_main.py:26 `_sandbox_maintenance(stop_event)`，lifespan :48 create_task，退出 cancel+await | 是容器维护，不是可恢复业务 Job。没有声播话术/TTS 受理、对账、恢复、取消迟到结果的现成实现。 |
| 业务取消 | 在 src/api_view、src/agent 搜索未见明确 run cancel API / job scheduler | 不能宣称具备业务任务取消。声播需自行设计 RunRepository、TaskRepository、JobObserver、cancellation token 和版本 fence。 |
| 断连 | SSE generator 的 finally 释放资源 | 当前未见 durable detached run 与事件重放机制；不能把 HTTP 断连当用户取消，也不能声称断连后一定后台完成。 |

日志方面 `JSONFormatter.format()`（`log_utils.py:13`）实际 `return str(log_entry)`（:22），并非严格 JSON。声播新实现应 `json.dumps` 并统一脱敏；重要审计用结构化 EventRepository，而不是依赖 logger 字符串恢复。

## 9. 实际测试能证明什么

本轮只检查测试源码，没有执行。以下是可借鉴的测试类型，不能将“仓库有 test”说成当前运行已通过。

| 测试位置 | 现有测试关注点 | 声播应有的对应测试 |
|---|---|---|
| `src/test/test_progressive_skills.py:56/61/76/88/157` | 元数据与正文分离、分页、路径/敏感文件、嵌套目录边界、system blocks 保留 | 原文读取完整性、不可越准备任务、分页未读全禁止生成、压缩后 ref/hash 不丢。 |
| 同文件 :168/182/189/204 | 假模型真实 graph、异步执行、子 Agent wiring、Deep Agent delegation | 验证实际 factory 构建的 graph；不能只单测一个不会被装入的 middleware 类。 |
| `test_memory_integration.py:36/63/86/119` | async graph 的线程隔离、用户隔离、旧轮不覆盖新偏好、记忆失败降级 | 两 preparation/worker 不串写；缓存 graph 每轮刷新新修订；跨轮活动价格不会被旧摘要覆盖。 |
| `test_hybrid_review.py:113/136/143/180` | 写入审批保留且不重放、只读重做有界、新轮清旧 review、委派保守禁止重放 | 发布已完成后，模型修正不能重复新建商品/重复 TTS；同 key 只受理一次，unknown 先对账。 |
| `test_sandbox_lifecycle.py:133/160/242` | 后端协议代理、恢复重建、ContextVar 隔离 | 不需模拟 Docker；应模拟声播 HTTP 重启/超时、任务租约释放与新 goal revision 的迟到响应。 |

## 10. 对旧版 13/14 的工程缺口定位

下表并不是说旧版未提及这些主题，而是指出还差哪一段“能由实现者直接编码”的连接。

| 旧版位置 | 已有内容 | 缺少的可编码连接 | V2 必须补的函数/规格（以下名称为拟建，不是 wodrake 现有函数） |
|---|---|---|---|
| 13 §10 :330—350 | SDK 配置表、worker 隔离原则 | factory 的真实依赖、实例创建顺序、主/子 graph 返回值和校验 | `build_runtime_resources(settings)`；`build_tool_registry(deps)`；`build_worker_graph(role,deps)`；`build_main_agent(deps,workers)`；每个函数调用图和 fail-fast 检查。 |
| 13 §5 :155—163 | 接收/观察/选择/收尾职责 | HTTP 入口如何形成 run、谁调用 astream、何时释放租约 | `RunService.submit/resume/cancel`；`RunExecutor.execute(run_id)`；输入映射、finally、并发唯一约束、异常到状态的转换表。 |
| 13 §11 :358—374 | middleware 组件名称与意图 | hook 方法签名、handler 调用顺序、读写字段、返回/跳转分支 | middleware 清单逐项给 `abefore_agent/awrap_model_call/awrap_tool_call/after_model` 的参数、返回、异常；正常工具调用不被 Stop 守卫吞掉。 |
| 14 §12 :253 后 | 工具名称/输入输出概念表 | 每个工具 args_schema、闭包注入 deps、调用哪个 service/client 方法、写账本时点 | 每个工具明确 Python 签名、Pydantic 输入/输出、role scope、唯一 tool_id、handler 算法和 service 方法。 |
| 13 §4.3 :139—147 | task package 字段 | 谁创建 task、谁调用 SDK worker、worker 怎样读取材料/保存、主任务怎样收结果 | `TaskService.create` → `dispatch_worker` → `read_context` → `validate_worker_report` → `ArtifactRepository.save` → `TaskService.complete_if_current` → 主 Agent 接收短回执。 |
| 13 §8、14 §14.1 | ContextBundle、完整引用 | manifest 如何生成，哪一步读取什么，如何证明未丢字段 | `ContextAssembler.build_manifest`、`render_model_snapshot`、`ContextReader.read_section`、`ReadReceiptRepository.record`、`require_complete_sections`。 |
| 14 §13.3 :352—357 | 幂等、CAS、超时查账原则 | 工具守卫、ActionService 和声播 client 调用的事务边界 | `ActionService.prepare` → `authorize` → `dispatch` → `reconcile`；operation_key canonicalization、duplicate result、unknown 查询与重启扫描。 |
| 13 §5.1 :173—182 | interrupt/Command/等待 job 原则 | 持久问题 upsert、typed answer、目标变更和 resume 串行化 | `QuestionService.ensure_batch`、`submit_answers`、`consume_answer`；`RunService.resume` 对 owner/question/revision 验证并排队。 |
| 13 §12/14 §10 | TTS Job 与取消/观察 | accepted 后谁轮询、完成后谁续跑、重复事件怎样去重 | `JobObserver.scan_due`、`poll_one`、`record_terminal`、`RunScheduler.enqueue_once`；cancel/late result/current goal 的条件矩阵。 |
| 14 §4A/4B/5 | 全量字段完成、计划生成/执行 | 校验报告怎样进入正文生成、模块怎样落地且保持旧稿 | 商品 DraftService、ScriptPlanService、脚本 Client 的逐调用顺序；字段 projection、exact constraints、基准 hash 和未变模块 hash 检查。 |
| 13 §12.3 :409—411 | event_seq、重连原则 | EventRepository 和 SSE adapter 的输入/游标、最终答复是否先过守卫 | `append_event`、`list_after(seq)`、`serialize_public_event`；token 不改变 ready，final completed 只从业务校验结果发出。 |
| 14 §6/7/8 | 顺序、长短开场、按需音频 | 编译函数/数据签名和变更后局部失效调用者 | `compile_playlist(order, loop)`、`resolve_opening_for_position`、`required_audio_keys`、`invalidate_dependents`；跳播按用户最新确认的原编排位置处理。 |

V2 的每个模块至少交付：文件路径、类/函数签名、调用者、被调者、输入来源、输出去向、事务/副作用、权限、失败与恢复、配套测试。缺任一关键连接都不能以“后续按 Skill 实现”代替。

## 11. 采用与排除清单

| 采用的工程做法 | 在声播中的落点 |
|---|---|
| 单一装配入口，factory 返回 SDK graph | Deep Agents 是唯一模型循环；应用 run executor 管生命周期。 |
| 工具对象与角色清单明确 | 精确权限表 + 服务端 scope 校验 + tool registry 启动核对。 |
| prompt 小快照 + 按需完整文档读取 | 版本化 ContextBundle、读取回执、原文分页，保留 SDK system blocks。 |
| thread/checkpointer、业务存储、展示事件分开 | checkpoint 续跑，声播库为正式事实，准备库保存草案和 action/job/question，事件用于观察。 |
| interrupt 必须配恢复端点和同线程配置 | 集中问题批次、typed answer、旧问题失效、主图独占 resume。 |
| 写操作不因评审自动重放 | 稳定 operation key、CAS、ActionService 对账、异步 job 终态后再提交。 |
| 真 graph 的假模型接线测试 | 用真实工厂和 SDK 运行核心调用链，另用声播 stub 测 HTTP/版本/取消。 |

不采用：Docker/OpenSandbox、LocalShell 回退、Bash、任意项目/Skill 上传和执行、持久队友/Teams、动态安装 Skill、无需求的三层自动记忆、采购业务模型裁判、为了同构额外搭建 MCP Server。它们不构成首期声播播前 Harness 的必要部分。

## 12. 审计完成判据

本审计已经覆盖真实请求链、factory、工具/HTTP 连接、子 Agent 装配、动态上下文、补充/审批恢复、记忆/持久层、后台/取消能力边界、日志和测试源码。所有采用建议均指向已确认首期需求；没有以第三方项目的类名和教程章节证明声播已实现这些能力。

需要后续工程验证的事实：Deep Agents 0.7.25 的 middleware 顺序、worker 私有状态传播、tool runtime context、持久 SQLite 生命周期和 interrupt 恢复行为。应通过本项目最小 fake-model graph 探针验证，不能运行该第三方仓库来替代声播集成验收。
