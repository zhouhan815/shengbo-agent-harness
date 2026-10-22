# 播中播后综合 Agent：SDK 与 Harness 实现核查

日期：2026-10-20。性质：只读源码核查、离线 SDK 机制验证与拟议工程计划；不是已经完成的业务实现。

本文件供总体工程计划编写者采用。部署决策以总体计划为准：在现有 `shengbo-ops-agent` 仓库新增 `src/shengbo_live_review`，合同放 `src/shengbo_contracts/live_review.py`；复用既有 Agent 8791 聚合服务，声播 8765 继续承担同源网页桥接。复盘拥有自己的 Runtime、`review.sqlite`、`checkpoint.sqlite`、scope 和事件命名空间；不复制播前巨大 RuntimeEngine，不增加独立端口。

## 1. 实际 SDK，而不是印象中的 API

| 项目 | 本机核验结果 | 对开发的约束 |
|---|---|---|
| Deep Agents | 0.7.25 | 使用此安装版本编译和测试，不随文档示例升级 |
| LangChain | 1.4.6 | 自定义中间件使用实际异步 hooks |
| langchain-core | 1.6.10 | BaseChatModel、ToolMessage、RunnableConfig 以此为准 |
| LangGraph | 1.2.15 | 主图持久检查点、同线程串行、显式恢复 |
| Python | 既有 `.venv/Scripts/python.exe` | 本轮没有修改依赖、运行模型、打开服务或读写生产数据库 |

核查入口：`D:/Codex project/shengbo-ops-agent/.venv/Lib/site-packages/deepagents/graph.py:278`。

实际 `create_deep_agent` 接受 `model, tools, system_prompt, middleware, subagents, skills, memory, permissions, backend, interrupt_on, response_format, state_schema, context_schema, checkpointer, store, debug, name, cache`。它没有顶层 `general_purpose_subagent`、`role`、`parallel`、`max_budget` 参数。不得把这些不存在的参数写进实施方案。

实际 SDK 自动构造：FilesystemMiddleware → SubAgentMiddleware（有同步子 Agent 时）→ SummarizationMiddleware → PatchToolCallsMiddleware → 自定义中间件 → SkillsMiddleware（配置 skills 时）→ 供应商缓存中间件 → MemoryMiddleware（配置 memory 时）→ HumanInTheLoopMiddleware（配置 interrupt_on 时）→ UnsupportedContentMiddleware。不同模型 HarnessProfile 可能增加或替换槽位，因此启动时必须记录实际中间件名称序列并断言没有重复名称。

## 2. 能复用的已有部分与不能直接复制的部分

| 已有位置 | 真实作用 | 新模块如何复用 | 不可以误称为什么 |
|---|---|---|---|
| `shengbo_prelive/providers/chatgpt.py:361,704` | ChatGPTPlanModel、工具调用转译、真实请求前后回调、公开 usage 及失败元数据 | 通过小型 `ModelFactory` 调用同一个安全工厂，传入复盘自己的 observer/before_send/on_request/on_response | 不能从网页拿内部令牌；不能复制凭据到新配置 |
| `providers/credentials.py:106–253` | 加密凭据读取、进程锁、到期续期、旋转结果未知保护、身份核对 | 复用 CredentialManager 的实现和既有凭据位置 | 不能用“刷新失败一律再试一次”替代原有防重放规则 |
| `observability.py:48` | `sanitize` 凭据与私有推理安全投影 | 直接依赖此纯函数或在小 shared 模块中原样提取并保留测试 | 它只负责脱敏，不能代替商品/场次权限与个人信息最小化 |
| `observability.py:122` 后 | durable 事件、payload 哈希、游标、失败 spool、回读 | 独立实例使用复盘数据根；若合同不同，通过适配器包装，不写旧播前运行的事件 | 不能把所有数据库都合并成同一个表，以为这样自动具有事务一致性 |
| `execution.py:18–239` | InstanceLock、GraphExecutionRegistry、实际 SDK 任务生命周期与 cancel/join | 复用小组件，独立复盘 namespace/root；启动与关闭由聚合 app lifespan 管理 | 不能把数据库 lease 到期当成 SDK 任务已经退出 |
| `client.py:53–141` | ShengboClient、精确 ref、只读 query、带 operation_id 的 command、结果未知异常 | 通过受限 `PreliveFeedbackClient` 只暴露精确读取和两个反馈命令；复盘 Agent 工具不得拿到通用 command | 不能直接把整个播前写工具集传给复盘模型 |
| `shengbo_contracts/common.py` | 严格 Pydantic 合同、canonical_hash、RecordRef/ArtifactRef | 对新 `ReviewBundle` 等组合使用，不新造第二种通用 ref 形态 | `dict[str,Any]` 不等于语义已校验 |
| 播前 document reader/parser 纯组件 | Office/PDF/图片资料读取和结构保真 | 经营报表导入只复用需要的只读表格读取器，并新增报表 schema 映射 | 不调用播前“资料→商品→话术”整套工作流 |
| `runtime.py:146–475` | 当前播前自定义摘要、上下文投影、多个恢复分支 | 借鉴经过验证的边界与测试，不整体复制 | 不应让复盘背负商品更新/生成/TTS/候选发布全部状态 |

代码整洁原则：先写复用清单、依赖方向和反例测试，再决定是否提取 shared 模块。首次实现不为“统一”而大规模移动播前代码。公共 provider、安全投影和锁组件的改动必须同时运行相关旧测试；复盘专属错误不能用播前业务条件分支补进去。

## 3. 主子 Agent 的实际调用机制

### 3.1 `task` 的参数只有两个

本机 `deepagents/middleware/subagents.py:430–464` 的 `TaskToolSchema`：

```python
class TaskToolSchema(BaseModel):
    description: str
    subagent_type: str
```

`runtime` 是 SDK 注入参数，不由模型填写。附加 `prompt`、`role`、`archive_ref`、`parallel` 等字段会被此版本拒绝。需要的业务参数通过服务器生成的委派记录与可信 scope 传递，任务描述只表达调查问题、期望产出和模型选取的调查范围。

并行：主模型在同一条 AIMessage 中发出两条独立 `task` tool calls。工具节点异步运行二者，主图等待这一批返回。不是要求模型每次都发两条；简单任务直接由主 Agent 分析。

`RunnableConfig.max_concurrency=2` 可以控制部分 Runnable 并发，但不能把它视为所有嵌套工具节点的绝对安全闸门。另设本运行共享的 `asyncio.Semaphore(2)` 限制委派入口，同时令真实模型调用经过共享预算和并发闸门。不能仅靠提示词“最多两个”。

### 3.2 CompiledSubAgent 的配置范围

实际 `CompiledSubAgent` 只有 `name, description, runnable, mode`。不能在 compiled spec 顶层填写 `tools/skills/middleware/context_schema`。这些内容必须在编译该子图时配置好。

本计划的两个可选角色：

| 角色 | 何时值得委派 | 最小上下文 | 可以使用的动作 | 输出 |
|---|---|---|---|---|
| `content_reviewer` | 模块解释差、反复问题、意向与理解困难混杂，需要跨片段证据阅读 | 当前归档/指标修订、选中商品/模块、必要事实和原句 refs、样本批次 refs | 只读 panel/evidence/script/span、评论分类服务、提交子报告 | `SubReviewResult`，包含 findings、证据 refs、局限、建议和未决问题 |
| `commercial_reviewer` | 点击、订单、支付、退款之间出现值得调查的差异 | 相同归档修订、经营报表定义/粒度、商品映射、队列与截止日、可比历史 refs | 只读经营/历史/证据工具、提交子报告 | 同一合同，不能产生交易事实之外的数字 |

二者都是 `mode="isolated"`。不使用 fork 继承整段主会话，避免无关工具返回、旧指令和隐式角色混进上下文。子 Agent 不再委派子 Agent；没有 `task` 或播前写入工具。

### 3.3 默认 general-purpose 必须处理

SDK 默认 general-purpose 继承主工具与 skills，但不是无条件继承全部主自定义中间件。仅在主图配置了权限，并不能假设自动子图也具有相同应用级 scope 检查。

0.7.25 可以通过全局模型 HarnessProfile 关闭默认 general-purpose，但同一进程还运行播前图，给共享模型注册全局 profile 可能改变旧图。首期选用**按图替换同名 SDK SubAgentMiddleware**：

```python
delegation = SubAgentMiddleware(
    backend=backend,
    subagents=compiled_reviewers,  # 严格只有两个受限 compiled wrappers
    task_description=(
        "可选调查角色：\n{available_agents}\n"
        "仅对独立且有必要的复杂问题委派；直接读取一个指标时不要委派。"
    ),
    state_schema=ReviewState,
)
```

将该实例放到 `create_deep_agent(middleware=[...])`。`graph.py:210–245` 的 `_apply_custom_middleware` 按 `.name` 原位替换 core 的同名实例。它仍是 SDK 原生 SubAgentMiddleware，不是删除脚手架，也不是另外实现一个同名 `task` 工具。生成图后断言只有上述两个子类型，且 `task` 说明列出二者；不能只用字符串搜索默认模板里是否出现 general-purpose 来判断是否存在默认 Agent。

若不想采用按槽替换，另一条可行路径是在独立模型标识上注册 profile，并以离线测试证明不影响播前。但是这会多一套 provider 标识配置，首期没有必要。

### 3.4 可信子身份必须由应用重绑定

源码及离线验证确认：native task 调用 compiled runnable 时，`get_runtime().context` 默认沿用主图 context；它不会根据 `subagent_type` 自动把 `role` 改成子角色。

因此 `ReviewScopeMiddleware.awrap_tool_call` 识别 `task`：

1. 从 `request.runtime.context` 取已验证的主 scope，不读模型声称的 account/role。
2. 从受限 spec 表按 `subagent_type` 查固定角色与工具权限。
3. 以主 run ID、task tool_call_id、归档修订、角色建立幂等 `delegation_id`。
4. 创建或读取 `DelegationBinding`；若同委派已完成，直接返回引用和摘要；若不同内容复用了同 key，报冲突。
5. 用 task-local ContextVar 设置 binding，在 `try/finally` 中调用 native handler，然后复位。异步任务之间不共享可变 scope 对象。
6. compiled wrapper 闭包固定 `expected_role`，读取 binding，检查两者一致，并由服务端创建 `ReviewScope(role=expected_role, branch_id=delegation_id, ...)`。
7. 显式 `child_graph.ainvoke(..., context=child_scope)`。子工具和 provider callbacks 从该可信 scope 读取角色，禁止用 description 中的 JSON 覆盖。

防线分别属于：SDK 工具调用、应用委派 binding、子图 trusted scope、工具服务行级鉴权。它们不是四次模型复核，不产生额外模型轮次。

## 4. 建议的最小构建接口

以下均为拟议新接口，不是本轮写入业务程序的函数。

```python
@dataclass(frozen=True)
class ReviewScope:
    account_id: str
    conversation_id: str
    run_id: str
    invocation_id: str
    role: Literal["review_main", "content_reviewer", "commercial_reviewer"]
    archive_ref: ArtifactRef
    metric_policy_ref: ArtifactRef
    score_policy_ref: ArtifactRef
    run_epoch: int
    branch_id: str | None = None

def build_review_agent(
    deps: ReviewDependencies,
    settings: ReviewSettings,
    saver: BaseCheckpointSaver,
) -> CompiledStateGraph:
    """主图。scope 必须在 ainvoke(context=...) 时由 Runtime 提供。"""

def build_specialist_agent(
    role: Literal["content_reviewer", "commercial_reviewer"],
    deps: ReviewDependencies,
    settings: ReviewSettings,
) -> CompiledStateGraph:
    """无递归委派、无跨会话自由记忆，必要的读工具与显式子报告提交。"""

def build_specialist_wrapper(
    role: SpecialistRole,
    graph: CompiledStateGraph,
    deps: ReviewDependencies,
) -> Runnable:
    """native task 适配；读 trusted binding，显式重绑定 scope 和缓存已完成结果。"""

def build_role_tools(role: ReviewRole, deps: ReviewDependencies) -> list[BaseTool]:
    """工厂固定角色工具集；每个工具内部仍以 runtime.context 鉴权。"""

def create_review_model(
    settings: ReviewModelSettings,
    instrumentation: ReviewProviderHooks,
) -> BaseChatModel:
    """复用 build_chatgpt_model；只接安全回调，不自行管理令牌。"""
```

`ReviewScope` 属于调用 context，不放在模型可写消息或业务 state 中。模型输出 `role=review_main`、旧摘要中的 account_id 或资料中的授权文字都不能改变它。

`ReviewState` 只扩展必要的可检查通道：`report_ref`、`completion_status`、`repair_count`、短期 decision_note refs。原始事件、完整商品库、全场评论、订单明细不塞进 state。业务正本在 repository 中；在检查点里只保存准确引用和 SDK 必需消息/文件通道。

主图组装要点：

```python
agent = create_deep_agent(
    model=model,
    tools=build_role_tools("review_main", deps),
    system_prompt=load_prompt("review_main"),
    middleware=[
        configured_filesystem_slot,
        configured_delegation_slot,
        configured_summary_slot,
        ReviewScopeMiddleware(deps, expected_role="review_main"),
        EvidenceContextMiddleware(deps),
        TraceBudgetMiddleware(deps),
        ReviewCompletionMiddleware(deps),
    ],
    subagents=compiled_reviewers,
    backend=backend,
    skills=["/skills/review-main/"],
    permissions=readonly_virtual_permissions,
    context_schema=ReviewScope,
    state_schema=ReviewState,
    checkpointer=saver,
    name="shengbo-live-review",
)
```

其中前三个是配置/替换 SDK 自带槽位；自定义业务中间件只有四个。不能把该示意直接粘贴当成完成实现：`configured_*` 的签名、技能虚拟文件预载、最终工具名单和 schema 必须由后续开发及假模型测试落实。

## 5. 四个自定义中间件的真实 hook 与职责

| 中间件 | hook | 实际执行细节 | 返回/短路行为 | 不承担的事 |
|---|---|---|---|---|
| ReviewScopeMiddleware | `abefore_agent(state,runtime)` | 验证 scope 类型、账户、run epoch、归档归属、run 状态；子角色检查 delegation binding | 正常返回 None；无权/已取消为结构化错误，不能调用模型 | 不用 LLM 再判断一次任务类型 |
| 同上 | `abefore_model(state,runtime)` | 在每次模型前验证本轮未取消/未被新 epoch 取代，当前归档仍为固定修订 | None 或终止错误 | 不把新报表静默换成原任务归档 |
| 同上 | `awrap_tool_call(request,handler)` | 检查角色工具白名单；对于 task 创建 trusted binding；对 refs 做 scope 检查并传受限服务 | 未授权调用不执行 handler；可纠正的参数错误以明确 ToolMessage 返回；系统错误抛出 | 不替代工具服务的行级权限检查 |
| EvidenceContextMiddleware | `awrap_model_call(request,handler)` | `assemble_review_context(scope)`，把当前精确归档/指标/政策 refs、覆盖缺口、成果/待决点、剩余预算拼成短可信 context；限制到本角色所需字段 | `await handler(request.override(system_message=...))`；保留原 system message 结构/SDK Skill 信息 | 不重读整个数据库、不用生成式摘要代替价格/版本/未知结果 |
| TraceBudgetMiddleware | `awrap_model_call(request,handler)` | 创建可观察逻辑 model span、设 task-local scope、限制每次 deadline；记录逻辑调用完成/失败，不把它冒充供应商实际请求 | handler 一次；不内置无条件通用重试 | 不在这里独自计算真实工具 schema/最终 payload 费用 |
| 同上 | `awrap_tool_call(request,handler)` | 记录父子 span、安全参数、参数哈希、缓存命中、权限结果、耗时和 result ref；共享调用计数原子预留 | 成功结果安全投影；取消及失败也有尾记录，尾记录失败不能推翻已提交成功 | 不保存私有推理；不输出客户订单隐私 |
| ReviewCompletionMiddleware | `aafter_model(state,runtime)` | 只在模型准备最终答复且没有工具 calls 时，检查当前 run 是否已有同输入版本的正式 report_ref；新建报告任务没报告则一次结构化提醒 | 必要时用 `@hook_config(can_jump_to=["model"])` 返回 correction message + jump_to=model；repair_count最多1；仍缺则 PARTIAL/BLOCKED | 不要求纯问答任务强行生成整场报告；不反复调用模型“复核” |
| 同上 | `abefore_model(state,runtime)` | 若当前精确 report 已完成，且任务只要求该报告生成，则不让模型继续找状态 | 使用 `@hook_config(can_jump_to=["end"])` 返回明确完成消息与 jump_to=end；不能伪造审批 | 不把“已生成草稿”说成“已采纳到下一场” |

异步方法名必须是上述 `a*` 形式。`before_model` 是同步 hook，不能在它内部漏 await 异步 DB/网络。`wrap_*` 是围绕 handler；`before/after_*` 则返回 state patch 或 None，不写 generator/yield。

自定义 hook 顺序是声明顺序进入、包装返回时逆序退出；SDKSummary 在这四个之前。若 summary 自行调用模型，它不会天然经过后面的 TraceBudget，因此**所有模型（含摘要）的真实准入、计数和用量都在 provider hooks 统一兜底**。

## 6. 工具、Skill 和机械工作流如何配合

### 6.1 普通工具合同

所有返回采用 `ToolResult[T]`：`schema_version, ok, data, ref, input_hash, source_revisions, limitations, cache, timings`；失败含 `code/retryable/known_effect`。`null` 不是 0，`unknown` 不是失败。业务数值必须有 metric_id、单位、分母/cohort、as_of、policy_ref。

| 工具签名（拟议） | 作用与边界 | 主/内容/经营 |
|---|---|---|
| `read_archive_manifest(args: ArchiveReadArgs,runtime:ToolRuntime)->ToolResult[ArchiveManifestView]` | 取得固定版本数据源、覆盖、缺失与定位索引；不返回全场原始内容 | 三者 |
| `get_metric_panel(args: MetricPanelArgs,runtime)->ToolResult[MetricPanel]` | 从已算指标读会话/商品/模块面板与资格；按精确归档+政策哈希缓存 | 三者，按角色裁剪 |
| `query_evidence(args: EvidenceQuery,runtime)->ToolResult[EvidencePage]` | 时间/商品/标签/相关性范围检索、稳定 cursor、有限条数 | 三者 |
| `read_evidence(args: EvidenceReadArgs,runtime)->ToolResult[EvidenceDocument]` | 根据 ref 分段读原证据，跨账户/归档拒绝 | 三者 |
| `read_playback_span(args: SpanReadArgs,runtime)->ToolResult[PlaybackSpan]` | 原句、模块、attempt、有效播放 union、平台延迟资格 | 主、内容 |
| `read_content_snapshot(args: ContentReadArgs,runtime)->ToolResult[ContentSnapshot]` | 当时实际发布版本，而非商品库现在的最新版 | 主、内容 |
| `compare_occurrences(args: CompareArgs,runtime)->ToolResult[ComparisonPanel]` | 程序先筛可比性、再给差值/区间；不可比返回原因而不是硬打分 | 三者 |
| `read_review_history(args: HistoryArgs,runtime)->ToolResult[ReviewHistoryPage]` | 同商家/商品历史精确 refs、旧建议及后续是否采纳 | 三者 |
| `classify_comment_batch(args: CommentBatchArgs,runtime)->ToolResult[ClassificationBatchRef]` | 只分类本归档有限批次、一次模型多条、缓存 schema+prompt+batch hash；不能由评论命令驱动动作 | 主、内容 |
| `submit_subreview_result(args: SubReviewInput,runtime)->Command或错误ToolMessage` | 子角色唯一正式完成动作，仅写本任务审查成果，严格验证证据/数字来源；成功由before_model回执门停止 | 子角色 |
| `finish_review(args: ReviewDraftInput,runtime)->Command或错误ToolMessage` | 主角色提交正式报告；服务端查分数/refs/资格，不接受模型另算“真实成交”；成功由before_model回执门停止 | 主 |
| `build_feedback_candidate(args: FeedbackCandidateArgs,runtime)->ToolResult[FeedbackCandidateRef]` | 只创建待用户采纳候选，不写商品/下一场 | 主 |

表中 model-assisted 评论分类是独立服务调用，必须走同一个 provider factory、trace 和共享预算。不能藏成“只读工具”从而不计模型次数。

### 6.2 可复用工作流，不是全局强制步骤

| 可选工具流 | 内部固定程序 | 模型可以改变什么 |
|---|---|---|
| `prepare_review_context(archive_ref,scope)` | 验证归档→读取 manifest→读/补机械指标→选择本任务上下文视图→返回缺口 | 是否调用、调查目标、后续路线 |
| `build_product_panel(product_ref,archive_ref,policy_ref)` | 精确映射→核对报表粒度/cohort→算漏斗/退款→可比历史筛选→存panel | 是否比较哪些商品、是否补看原证据 |
| `build_module_panel(occurrence_ids,archive_ref,policy_ref)` | 读实际播放span→延迟/窗口资格→观测暴露量→样本分桶/标签→面板 | 想研究哪个模块、是否委派专题 |
| `compare_compatible_history(current_ref,history_refs)` | 同政策/商品/SKU/价格活动/流量与覆盖筛选→计算差异/区间→不可比原因 | 选哪些候选历史、接受怎样的局限 |
| `package_review_delivery(report_draft_ref)` | 引用完整性→数值/等级引用一致→保存不可变成果→输出人类报告和机器候选 | 是否提交、修改何种建议、是否仅回答局部问题 |

每个流内部不再让 Agent 做“读A→读B→再读A确认→等待状态→重复读取”的机械往返；但是它可以不用某流、并行用两个独立流、改查别的证据。必需的数据/权限/发布门禁是前置条件，不是强制全局路线。

### 6.3 四个拟议 Skill

| Skill | 内容 | 阅读时机 | 必需保留的产出约束 |
|---|---|---|---|
| `review_scope_and_data_eligibility` | 归档、source粒度、null/0、当前/历史、延迟、观测覆盖资格 | 新复盘或发现缺失/异常口径时 | 可计算/不可计算原因，不编造留存和订单 |
| `commercial_funnel_diagnosis` | cohort、支付/退款截止日、漏斗、可比基线和分数解释 | 有经营数据且要判断经营效果 | 数字只引用程序指标，候选因果写为假设 |
| `content_and_customer_feedback` | 模块角色、评论标签、场景/顾虑、内容rubric、句子证据 | 要回答哪个模块/卖点需要改 | 同时列支持与反例、样本覆盖、区分意向与交易 |
| `next_session_experiment_and_feedback` | 建议优先级、改变什么/保留什么、下一次怎么验证、用户采纳合同 | 形成下一场建议时 | 不自动修改商品事实、顺序或实时播放 |

Skill 是按需知识文件，不是中间件；它不自行执行代码、不给工具授权，也不意味着读完四份后才可工作。初始展示 name/description；模型需要时 `read_file` 完整读取。子 Agent 显式配置自己的子 Skill 路径，不假设继承主 Skill。

首期工具总量不大，建议静态配置受限小工具集；不要再建第二套复杂 tool_catalog/activate_tools 状态。如果 schema 后续变大，0.7.25 已有原生 `SkillsMiddleware(...,tools=resolver)` 与 `metadata.include_tools` 动态披露：读 Skill 后披露工具，调用前再解析。此机制可降低上下文，不替代服务器鉴权。`allowed-tools` frontmatter 只作元数据，不能当安全白名单。

## 7. 上下文、持久化和压缩的工程细节

| 数据层 | 存什么 | 是否给模型 | 恢复时如何处理 |
|---|---|---|---|
| 业务数据库/不可变artifact | Archive、MetricPanel、Scorecard、ReviewBundle、采纳决定、外部operation回执 | 通过role view和精确ref读取 | 业务事实正本，不用摘要覆盖 |
| 主图 checkpoint.sqlite | SDK messages/todos/virtual files/private摘要通道、必要ref/完成状态 | 下一轮会话 | 同 thread_id 恢复；不从前端随意传checkpoint_id |
| 同线程 StateBackend | Skill只读文件、SDK自动offload历史、必要工作笔记 | 按工具有界读 | 有持久checkpointer时会随线程状态保存，不能笼统说“进程重启就丢失” |
| ContextProjection | 当前目标、授权边界、run epoch、精确archive/policy refs、成果index、待决/未知事项、预算摘要 | 每轮短小固定注入 | 从业务正本重装，不从旧摘要反推 |
| 非权威工作摘要 | 尚未完成的判断、为什么需补证据、下一步备选、用户纠正 | 必要时替换旧消息前缀 | summary只是备忘；完整原历史offload可回读 |
| 原始评论/订单行 | 私有受控证据库 | 原始身份不进模型；HMAC/脱敏后按少量字段读 | 数据导入/采集修订独立，不因模型压缩改变 |

初始上下文只放 manifest摘要、任务范围、指标目录和当前结果索引，不把全场数据做一个超长 JSON。一条工具结果默认小于约4,000–8,000估算token（拟议可调阈值，真实接入时以token监测校准），超出返回分页ref。已有MetricPanel命中缓存应直接返回；读取不变数据不再重复模型转换。

摘要必须分两层预算：会话估算阈值触发SDKSummary；最终 `provider.before_send` 计算 system、实际工具schema、消息、重放项目的最终payload，执行总输入上限。静态工具与短context已控制住后，摘要应是后备机制，不应每次单场复盘都发生。

SDK默认Summary名为 `SummarizationMiddleware`。若使用子类做审计，显式保持同名槽位，否则名字改变会造成两份摘要中间件叠加。启动断言 `count(name=="SummarizationMiddleware")==1`，并监测每次摘要触发前后tokens、耗时、model_attempt_id、offload_ref。不复制播前所有恢复分支；先用bounded投影、分页、SDKSummary和最终wire fence通过反例测试。

## 8. 主运行、取消、恢复与并发写保护

建议 Runtime 不接管采集线程；复盘run是归档后的分析任务，播中普通服务持续采集。Runtime函数：

```python
async def start(self) -> ReviewRuntimeHealth: ...
async def stop(self) -> ReviewRuntimeHealth: ...
async def submit(self, request: ReviewRunRequest, principal: TrustedPrincipal) -> ReviewRunView: ...
async def drive(self, run_id: str) -> None: ...
async def cancel(self, run_id: str, expected_version: int, request_id: str) -> ReviewRunView: ...
async def resume(self, run_id: str, expected_version: int, request_id: str) -> ReviewRunView: ...
def get_run(self, run_id: str, principal: TrustedPrincipal) -> ReviewRunView: ...
def events(self, run_id: str, after: int, limit: int=200) -> EventPage: ...
```

运行状态：`QUEUED → RUNNING → SUCCEEDED/PARTIAL/BLOCKED/FAILED`；取消经 `CANCEL_REQUESTED → CANCELLED`，只有实际SDK任务退出后才完成取消。没有自然语言“已经停止”就提前解锁。

1. HTTP受理按 client_request_id去重；冻结archive/metric/score版本和本次目标；创建run epoch。
2. 同一个review会话主线程使用 `review:{run_id}:main`。同线程互斥；不以新HTTP请求并发 ainvoke 同一thread。
3. `GraphExecutionRegistry.execution(...)`包住整个SDK异步任务。`ReviewScope`与provider当前scope只在task-local ContextVar内设置，用finally复位。
4. 调用前 `aget_state`：新图输入用户消息+Skill虚拟文件；未完成checkpoint用`ainvoke(None,...)`继续；普通用户追加消息是新明确任务，不在循环中伪造“请继续”。
5. 进程重启只标记运行待恢复，检查旧lease/进程所有者并确认没有liveowner；恢复前对账未知外部操作。不能一开机立即把所有sent请求重试。
6. 取消更新epoch/取消标识，所有工具写入口再次核对；取消主任务并join native并行子任务。等待时间阈值只是提示，不能当作取消完成证明。
7. 归档有迟到数据时生成新archive_revision。进行中的报告继续针对旧快照，明确显示旧/新版本差异；用户要求重算时新run，不偷偷把旧分数分母换掉。

子 Agent 第一版采用短生命周期图，不引入独立持久会话；成功 `SubReviewResult` 按delegation+输入hash正式保存后返回，主图恢复同task先复用成功结果。已成功的评论分类批次和程序panel也复用。中途未完成子任务可由模型决定补做。若以后需要逐模型轮精确恢复，再新增独立子checkpointer和namespace，并专门验证，不能首期含糊写“SDK自动全恢复”。

## 9. 真实调用预算、未知结果和耗时

建议默认12次真实模型请求、40次Agent工具入口、最多两个并行子任务作为首轮可调保护参数；不是平台性能承诺，也不能为了凑数隐去必要分析。主/子/摘要/评论分类共同占用预算。实际模型调用与模型前预留分别记账。

Provider接口：

```python
def before_send(payload: dict) -> None: ...  # 全wire输入上限、运行未取消、原子预算预留
def on_request(event: SafeRequestEvent) -> None: ...  # 实际attempt_id、已发送状态和脱敏payload ref
def on_response(event: SafeResponseEvent) -> None: ...  # 收到公开usage即保存，再做业务输出校验
def observe_provider_event(event: SafeProviderEvent) -> None: ...  # 失败阶段、未知状态、catalog等
```

复用当前 ChatGPTPlanModel 的 `_ResponseAccumulator.accept` 已在内容/协议校验之前观察公开usage，避免“输出校验失败所以用量消失”。请求超时或断网如果响应未知，只记录UNKNOWN；不能记为0token或无费用。取消同样不能逆转已付费调用。

重试权责：provider处理明确安全的认证续期；业务Runtime处理run恢复；工具处理确定只读网络的有限重试。不要SDK retry + provider retry + workflow retry三层叠加。任何有副作用的反馈命令先查operation回执；不知道结果就阻断对账，不生成新operation_id重发。

最小可测耗时用 `perf_counter_ns()`，存`duration_ns`并以毫秒显示；UTC wall time到毫秒/微秒用于跨服务定位，不能用wallclock差作为唯一耗时。跨进程没有时钟同步时保留source_clock/boot_id，不相减两个不一致monotonic值。任务总耗时分开：排队、采集持续时长、导入解析、机械计算、模型等待、工具等待、人工等待；并行阶段报告critical path及各span，不把子任务耗时直接相加冒充总时长。

## 10. 执行记录的最小字段与界面投影

必备公共字段：`schema_version,event_id,sequence,account_id,conversation_id,run_id,invocation_id,branch_id,parent_span_id,span_id,type,source,role,archive_ref,metric_policy_ref,score_policy_ref,run_epoch,occurred_at_utc,source_instance_id,status`。

按事件类型增加：

| 事件 | 最少增加字段 |
|---|---|
| `review.model.reserved/requested/responded/failed` | reservation_id、provider_attempt_id、model、request_hash、safe_request_ref、公开输入/输出usage或null、usage_status、发送/响应阶段、outcome_known、error_code、duration_ns |
| `review.tool.started/completed/failed` | tool_call_id、tool_name、sanitized_args或args_ref、args_hash、result_ref/hash、result_preview、cache_hit、cache_key、source_revisions、permission_result、duration_ns |
| `review.delegation.started/completed` | delegation_id、固定角色、parent_tool_call_id、输入refs/hash、子result_ref/hash、summary、共享budget剩余、duration_ns |
| `review.context.projected/compacted` | projected_field_names、估算tokens、真实wiretokens、摘要trigger、before/after、history_ref、summary_ref、policy版本 |
| `review.metric/score.computed` | metric/score_id、程序函数版本、输入refs、cohort、grain、单位、as_of、eligibility、missing理由、公式/政策ref、duration_ns |
| `review.feedback.requested/applied/conflicted/unknown` | decision_id、report_ref、finding_ids、candidate_hash、target_preparation_ref、expected_revision、operation_id、receipt_ref、CAS结果 |

后台安全记录一直保存。前端执行过程开关只控制展示：默认关闭；打开显示主任务卡、子任务卡、工具参数/结果预览、缓存/耗时/进度；复杂原文折叠并可按ref有界回读。不得显示provider凭据、用户身份敏感字段或模型隐含思维链。“怎么判断”呈现显式结论依据、证据ref和下一动作理由，不冒充完整内部推理。

## 11. 人工采纳与播前 CAS 的精确交接

现有 `ImportRecapArgs.document` 仍是dict；本地 `_cmd_import_recap` 校验finding ID、synthetic、blob哈希和部分quote，不等于新 `ReviewBundle` 语义天然被校验。新建 `review_to_recap.py` 类型适配器，先验证再调用旧接口。

```python
def build_feedback_candidate(bundle: ReviewBundle, selected_findings: list[str]) -> FeedbackCandidate: ...
def validate_feedback_decision(candidate: FeedbackCandidate, decision: FeedbackDecision, current: PreparationSnapshot) -> ValidatedFeedback: ...
def to_recap_document(validated: ValidatedFeedback) -> RecapDocument: ...
async def apply_feedback(validated: ValidatedFeedback, client: PreliveFeedbackClient) -> FeedbackApplyReceipt: ...
async def reconcile_feedback(operation_id: str, client: PreliveFeedbackClient) -> FeedbackApplyReceipt: ...
```

推荐顺序：

1. 模型只创建候选，写明“商品2可以先播”等建议、证据、预期改善、需要验证什么；不自动重排。
2. 用户在确认面板选择finding与目标本场。服务端保存 `FeedbackDecision`，绑定候选hash、report_ref、finding IDs、原商品/话术refs、target_preparation_ref与expected_revision。
3. 服务端对照当前商品/场次差异；旧事实明确撤回/停售拦截，其他变更展示差异，不以摘要代替用户看见的内容。
4. 将机器合同+证据出处写为raw blob（可包含原文quote以通过既有quote来源检验）；再 `import_recap` 得到recap_ref，所有命令都有稳定operation_id。
5. 调用 `link_recap_to_preparation` 时必须合并当前已有recap_refs，而不是覆盖为这一次一个ref；用目标当前expected_revision做CAS。该命令会清空bundle_ref/plan_ref，必须在用户确认预览中明确此影响。
6. CAS冲突不自动拿新revision再执行；返回差异，重新确认。本地反馈回执与远端状态双查后才标APPLIED；结果未知先对账。
7. 播前Agent正常读取本场已绑定recap refs，生成明确每条建议的采纳/不采纳/待验证disposition，再结合本场、商品和顾客研究制定话术计划。复盘不直接写话术；文本到TTS仍沿用既有整包人工确认。

此采纳操作是独立controller服务动作，不需要把整个复盘图interrupt住等待用户数天；报告完成就是完成。若用户在对话中明确要求采纳，先创建相同typed候选/确认，不用模型措辞替代精确批准。真正外部写入口始终核对批准快照。

## 12. 需要前置验证的反例，而不是上线后补丁

| 场景 | 验证方式 | 通过条件 |
|---|---|---|
| 模型伪造role/account/archive | 假模型在工具args、task描述、历史摘要分别注入 | 固定scope不改变；跨范围refs拒绝；0外部写 |
| 自动general-purpose逃逸 | 假模型请求general-purpose、子任务请求task | 不存在/明确拒绝；仅两个允许子角色；主子角色工具名单快照正确 |
| 两个独立任务并行 | 假runnable记录start/end+主toolcall IDs | 两区间重叠；不同delegation_id；同run共享预算，无contextvar串线 |
| 已完成task重放 | 主图在task结果落库后、checkpoint提交前中断 | 相同delegation input命中结果，不重复子模型调用 |
| 取消时清理慢 | 假SDK任务延迟退出/取消子任务中 | 仍持有线程owner；不会新开同thread；确认退出后解锁 |
| summary错配/重复 | 极长安全假工具输出触发阈值 | 一份summary槽；压缩前后refs/未知状态不丢；provider实际次数含summary |
| 内容拒绝但有usage | 假provider返回usage后非法输出 | 保留公开usage，记validation_rejected；不记成功业务成果 |
| 无经营数据 | 只给公开事件+播放日志 | 可出内容/互动报告，经营分为null+原因，不能0分或伪造成交 |
| 修改政策/迟到退款 | 新archive/score_policy修订后复用旧panel | 缓存不误命中，旧报告保留原版本，新报告显式变更 |
| 采纳CAS冲突/网络结果未知 | 假远端先提交再断线、另线程改本场 | 不重复提交、不静默覆盖、不丢旧recap_refs、可按operation对账 |
| 尾部日志失败 | 工具已提交后让tail sink不可写 | 成功成果不改成失败；spool/降级告警；新付费/写入在审计不可用时阻断 |

这些是后续开发的验收用例。本轮只做了下面的纯SDK离线机制验证，没有宣称上述全部业务用例已通过。

## 13. 本轮离线 SDK 验证结果

以 `FakeMessagesListChatModel` 的纯离线子类替换真实模型，禁止LangSmith上传；StateBackend只在内存；没有导入项目runtime、没有真实API/TTS、没有读写生产DB。

- native `task` schema：`["description","subagent_type"]`。
- 两个compiled runnable分别开始于同一monotonic时基的约355729.193461秒与355729.193504秒，均在约355729.2518秒结束；时间区间重叠，证明同条消息多个task可并行。
- 两个runnable的`get_runtime().context.role`均为父级`review_main`，run_id也继承父级；证明**子身份不会自动改写**。
- 同名SubAgentMiddleware替换后，使用精简task_description输出的允许类型严格为content_reviewer/commercial_reviewer。
- 第一次试验使用SDK原始FakeMessagesListChatModel时，因为它没有实现`bind_tools`而在假模型层报NotImplementedError；改用仅覆盖bind_tools的纯离线子类后机制验证通过。这个错误不来自业务Agent，也没有发出网络请求；保留此说明防止把第一次失败掩盖成“所有步骤一次成功”。

验证只支持并行/继承/替换这三个具体结论，不支持声称真实模型会正确规划、真实网络一定恢复、所有报告都在10分钟内完成。

## 14. 一手依据

优先级：本机锁定源码和离线机制验证 > 同版本可对应的官方文档 > Skill概括。Skill部分例子是框架通用说明，可能缺少本版本role/默认子图/summary细节，不能按例子照抄。

- [Deep Agents 子 Agent 官方文档](https://docs.langchain.com/oss/python/deepagents/subagents)：隔离子任务、compiled子图、默认general-purpose、最小工具集。
- [LangChain 自定义中间件官方文档](https://docs.langchain.com/oss/python/langchain/middleware/custom)：wrap hooks与state hooks的调用方式。
- [LangGraph 持久化官方文档](https://docs.langchain.com/oss/python/langgraph/persistence)：thread/checkpoint职责；外部操作幂等仍由应用实现。
- 本机 `deepagents/graph.py:210–245,278,718–883,892–1010`：自定义槽位替换、子工具继承、默认子图、实际顺序。
- 本机 `deepagents/middleware/subagents.py:430–464,782–873`：task精确参数、隔离模式state处理、runtime/config继承。
- 本机 `deepagents/middleware/skills.py:970–1042`：metadata.include_tools与SkillToolResolver；不应自造未测试的动态工具协议。
- 本机 `langgraph/pregel/main.py:2958–2983,4479–4493`：parent Runtime与context合并。
- 本机 `langchain/agents/middleware/types.py:431–805`：实际同步/异步hook签名。

本核查没有访问模型凭据内容；全文没有令牌、cookie、账户标识或客户订单原文。

## 15. 补核：完成即停止、错误可纠正与子图检查点（最终实施规范）

### 15.1 普通 Command(goto=END) 在本栈不足以停止

应总体计划编写者的具体问题，又做三种纯SDK假模型对比。三组均在第一轮模型调用 `finish_review`；假模型第二条响应固定为 `UNWANTED_EXTRA_MODEL`，用于发现是否多走一轮：

| 完成工具写法 | 实测模型轮次 | 结果 |
|---|---:|---|
| 普通 `@tool` 返回 `Command(update={report_ref,messages:[ToolMessage]},goto=END)`，没有before_model完成门 | 2 | 出现不需要的第二轮；不能采用 |
| `@tool(return_direct=True)` 返回 `Command(update={report_ref,messages:[ToolMessage]})`，不填goto | 成功时1 | 成功路径能停，但错误路径也可能停；最终不采用 |
| `@tool(return_direct=True)` 只返回字符串 | 成功时1 | 无state成果ref，且相同错误退出隐患；不采用 |
| 普通 `@tool` 返回Command更新正式ref，`ReviewCompletionMiddleware.abefore_model`按持久回执跳END | 成功时1；首错修正后2 | 正常完成不多模型；错误仍可纠正；最终采用 |

原因是当前 `create_agent` 有其工具节点→模型条件路由；在普通工具Command中附加END，并不代表其它预设路由被取消。不要按一般LangGraph节点经验猜测。

此外核验 `factory.py:2072–2104`：return_direct路由不判断ToolMessage.status。若直返完成工具参数非法或报告缺项，即使返回`status="error"`，仍可能结束整个图。不能为了节省一次模型调用而破坏错误纠正。

实施采用**普通完成工具 + 模型之前的持久回执门**。业务保存和语义验证在 `finalize_review` 服务内部完成；成功后工具更新graph ref，随后before_model先核对正本并跳END，不进入下一次模型：

```python
@tool
async def finish_review(payload: FinishReviewArgs, runtime: ToolRuntime) -> Command | ToolMessage:
    scope = require_review_scope(runtime, expected_role="review_main")
    # 参数校验由ToolNode处理；业务可纠正错误显式转成匹配当前调用的error ToolMessage。
    # REPORT_INCOMPLETE/REF_MISMATCH等没有保存正式成果，下一轮可以修正。
    receipt = await service.finalize_review(scope, payload)
    # finalize_review 幂等；已保存相同输入时返回相同正式 ref。
    return Command(update={
        "report_ref": receipt.report_ref,
        "completion_status": receipt.status,
        "messages": [ToolMessage(
            content=canonical_json(receipt.safe_view),
            name="finish_review",
            tool_call_id=runtime.tool_call_id,
        )],
    })

class ReviewCompletionMiddleware(AgentMiddleware):
    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        scope = require_review_scope(runtime)
        receipt = repository.read_matching_completion(
            scope.run_id, scope.role, scope.branch_id,
            archive_ref=scope.archive_ref,
            metric_policy_ref=scope.metric_policy_ref,
            score_policy_ref=scope.score_policy_ref,
            expected_goal_hash=repository.goal_hash(scope.run_id),
        )
        if receipt is not None:
            # 由正本回执恢复ref，不能只相信模型可写state里一个report_ref字符串。
            return {"report_ref": receipt.report_ref,
                    "completion_status": receipt.status,
                    "jump_to": "end"}
        return None
```

子 Agent 的 `submit_subreview_result` 同样采用**非direct工具 + before_model正式子回执门**。子图停止后compiled wrapper读取成果、进行安全投影，再交给主图。建议总体计划统一这两个完成入口名称，避免再出现 `submit_review_draft` 保存后额外 `finish_review`、然后额外一轮主模型凑总结的三个动作。如果允许保存中间草稿，`save_review_draft`应明确只是可选中间保存；最终由finish一次完成正式提交。

必须限制完成工具独占当前AIMessage的工具批次：如果同一条消息还包含别的tool calls，返回结构化可纠正错误，要求先收齐结果后单独完成，避免报告使用尚未返回的工具结果。

ReviewCompletionMiddleware同时保留模型没有正式成果却自然结束时最多一次提醒。正常完成和恢复后已存在本次精确成果都由`abefore_model`跳END；不是after_model，因为后者已经发生了一次模型请求。这个门检查已提交回执，不是额外LLM质量复核。

追加三组纯fake验证：正常完成1次模型；首次REPORT_INCOMPLETE后模型修正并完成2次；首次bool参数非法、收到ToolNode schema错误后模型修正并完成2次。三组均保留正式report_ref，均没有`UNWANTED_EXTRA_MODEL`。本轮没有隐藏失败：return_direct先被证明能停成功路径，随后发现并验证其错误退出边界，最终明确弃用。

### 15.2 子图 checkpointer=False 与主持久 saver 兼容

第二个纯SDK离线实验使用自动清理的临时SQLite测试文件（不是生产数据库）：

- 主图：AsyncSqliteSaver，固定测试thread_id。
- 子图：`create_deep_agent(...,checkpointer=False)`，通过compiled wrapper调用。
- 主模型两轮：第一轮委派，第二轮finish；子模型一轮：submit_subreview_result。
- 主图最终 `report_ref=offline:main`、`state.next=[]`。
- 临时checkpoint表只有主命名空间空字符串 `""`；子图没有继承写入子checkpoint。
- 真实模型/业务API/TTS/外网调用均为0。

这支持首期“主图耐久，短子图以正式成果幂等复用”的方案。它不宣称未完成的子Agent消息历史可以精确恢复；未完成子任务仍按正式已完成成果/批次缓存补做。

### 15.3 不要把子state泄回父state

本机native `_return_command_with_state_update` 会把未排除的子状态变化带回父state。若子图和主图都用了 `report_ref`，wrapper直接返回子图完整结果，可能让子报告覆盖主报告通道。

wrapper必须白名单投影：只取 `SubReviewResult`（已存ref、摘要、findings index、限制），返回 `{"messages":[AIMessage(content=canonical_json(safe_subresult))]}`，不回传child `report_ref/completion_status/files/messages原历史`。主图需要的branch状态在review repository有明确owner字段，不能靠同名state自动合流。若采用structured_response返回也须保持同样白名单与独立contract，明确SDK不会把它当作主ReviewBundle。
