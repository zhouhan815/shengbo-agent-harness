# Harness 工程计划复核

复核日期：2026-10-20。范围：`02_AgentHarness与上下文中间件.md`、`03_工具与可调用工作流.md`，连读01合同中与评分/完成身份相关部分。没有修改根计划，没有实施业务代码。

复核基线：

- 02 SHA-256：`91A7900427D46255F3E627F5A954730A160CADCA5AC286A5EB2968395166328B`。
- 03 SHA-256：`039440C24AA8B1A8FCD334B8D4322D59262110C39F36030D3C9E49CBBA1D633E`。

结论：主体方向和已验证SDK控制机制已正确整合。尚有几个“写了意图，但编码入口未闭合”的地方，应在开发前补齐。以下按会影响实现的具体问题列出；不要求增加Agent层数，也不建议再增加定制中间件数量。

## 1. 必须补齐的设计

| 编号/优先级 | 位置与具体缺口 | 可能实际出错的路径 | 最小修正方案 | 验收 |
|---|---|---|---|---|
| H01 / P1 | 02第9节等待、03 K12及最后一节：只说持久化WAITING_INPUT/WAITING_JOB，没有指定原生模型循环如何停止 | 普通工具返回waiting→native工具节点继续路由模型→模型再次查job/重复问问题，浪费调用；或者图结束后Runtime误标完成 | 同一个ReviewCompletionMiddleware的`abefore_model`读取可信wait receipt，存在有效等待便`jump_to=end`，但run保持WAITING_*；controller以Decision/job完成的持久inbox唤醒同Goal同thread新invocation，投递短ResumeObservation。或者完整采用interrupt/Command(resume)协议，二选一；不要只有“协调器接管” | 假模型首轮请求输入后不出现第二次模型；答复持久化后能同Goal继续；job未完成时重复通知不重启；完成唤醒只消费一次；等待不等于COMPLETED |
| H02 / P1 | 03 F03第5步：需要ContentAssessment，但K09仅评论分类、K13仅笼统findings，缺四项评级到正式assessment_ref的写入入口 | Agent读完正文给四项评分→没有工具保存该结构→F03一直缺assessment→重复派专题/读正文，或把自由文本当正式评分输入 | 在现有K10/K13增加强类型`content_assessments[]`并由同一服务保存，或增加单一K14 `submit_content_assessment`；主/内容角色皆可提交，经营角色不可。K09不得冒充内容审核器。校验依赖事实/稿件/顾客简报/本场/政策refs及每项证据后形成assessment_ref，F03再纯程序复用/评分 | 同一模块四项可完整落库并复算；不完整项明确null；事实撤回后依赖失效；再请求F03命中同一assessment，不再调用模型 |
| H03 / P1 | 02 SDK文件工具、03 registry/角色工具：SDK注入工具没逐个纳入明确授权/可见性 | 实现严格K/F registry时误拒`read_file`，Skill无法读；放宽所有SDK工具时默认`execute/delete`暴露给模型 | 用`SDK_TOOL_POLICY`或同一registry适配SDK工具。显式配置FilesystemMiddleware.tools白名单，隐藏`execute/delete`；read仅受控skills/history/scratch，write/edit仅scratch；`task`仅主且两子类型；`write_todos`按H04显式加入。工具内和middleware双验，模型schema用同一名单 | 每角色真实模型工具schema快照、Skill读取成功、execute/delete不出现且伪造调用拒绝、路径逃逸拒绝、scratch不能改skills/历史 |
| H04 / P2 | 02第2节“保留TodoListMiddleware/write_todos”与实际SDK不符 | 开发照文档不显式添加→没有write_todos，提示词/trace却要求使用该工具→调用未知或计划能力失效 | 0.7.25当前通用默认栈没有TodoList。若要此能力，显式添加SDK `TodoListMiddleware`；它属于配置SDK，不是第五个自定义业务中间件。否则删write_todos承诺 | 构建后真实工具集合包含一次write_todos；startup middleware名无重复；todo不能被当成交付正本 |
| H05 / P1 | 02 provider预算/摘要写了最终口径，但未明确整个图调用外层的可信ContextVar与每次provider reservation绑定 | Summary在TraceBudgetMiddleware外层自行调用模型→若scope只在TraceBudget里设，摘要无scope或沿用别的run；主/子并发可能账本错归属 | `runtime.invoke_run`在整个`graph.ainvoke(...,context=scope)`外层set/reset `CURRENT_REVIEW_SCOPE`；compiled wrapper在整个child ainvoke外层重绑；K09分类同理标purpose。provider before_send对当前scope查run lease/cancel代次、最终payload预算、原子预留；on_request将provider_attempt_id绑定该预留；公开usage先于内容校验落账。每个request独立task-local reservation token，finally复位 | 强制主/子/摘要/分类同时出现，所有真实attempt均正确归run/role/purpose，共享12次预算，不漏摘要、不串票；取消/lease丢失时新发送为0 |
| H06 / P2 | 02第6节find_valid_receipt只写goal_ref/archive_ref/generation；主/子/政策修订的身份尚未逐字段固定 | 同run不同子任务/同归档不同政策/变更as_of的报告，被宽泛“已有报告”查询误判完成；子回执使主图跳END | 由GoalDefinition形成唯一`completion_input_hash`，覆盖本任务所有实际依赖；find_valid_receipt至少按account/run/role/branch、goal hash、generation、archive、metric/score政策、目标as_of/baseline选择或其完整输入hash查。主只取DeliveryReceipt，子只取自身SubReviewReceipt；不读“最新结果” | 子报告不能终止主；同archive政策改变不复用旧完成；UI开关等无关输入不失效；恢复精确完成回执无需模型 |
| H07 / P2 | 03列五个可调用flow，但registry角色与模型schema只逐列K工具；F05成功适配器没定义 | 开发把F函数仅写为内部helper，模型看不见；或者F05已经提交却仍额外调用K11/模型写结尾 | 每个F也有同一ToolSpec（name/args/result/roles/effect/timeout/handler）。F05是普通非direct模型工具，内部draft+finish服务一次完成；工具adapter把DeliveryReceipt投影为ToolResult/Command，Completion.before_model读取同正式回执停止。K10→K11保留给确有草稿编辑需求的情况；正常新报告优先F05，不强制两次胶水轮次 | 假模型能直接选择F01–F05；F05成功只需该次模型，参数错误仍可纠正；重复调用同输入命中同正式receipt；K/F错误语义一致 |

H06中的as_of、baseline并不是要求将所有历史对象塞进scope；可以由冻结Goal/输入hash间接绑定。关键是完成身份不能只靠“同场有个报告”。

H05并不是再写一个大预算中间件：四业务中间件维持不变，最终发送层调用统一预算服务；外层hook只提供逻辑span与deadline。未收到精确tokenizer时记录final_payload的估算token，不能因叫“最终wire”就把估算写成供应商实测token。

## 2. 已正确闭合、应保留的设计

| 部分 | 判断 | 具体理由 |
|---|---|---|
| Native task参数与并行 | 正确 | description/subagent_type、同AI消息多task、共享Semaphore、深度1，与源码和离线验证一致 |
| 默认泛用子Agent | 正确 | 同名SDK SubAgentMiddleware按槽替换，仅两个compiled规格；没有对共享provider全局注册profile |
| 子身份与状态 | 正确 | 显式child_scope、task-local ContextVar、wrapper安全投影、不回传子report_ref污染主，已采纳最关键实际SDK边界 |
| 子checkpoint范围 | 正确 | 子False+主持久saver已做机制核验；文档没有虚称逐轮子状态恢复 |
| 非direct完成工具+before_model门 | 正确 | 能兼顾成功不多模型与错误可纠正；正常1轮、首错修正2轮的假模型验证只用于SDK机制，未误称业务完成 |
| 机械flow可选 | 正确 | F内部程序化减少胶水往返、F之间由模型规划；保留权限和统计资格不等于把整个Agent固定成大流程 |
| 上下文与摘要 | 正确 | 正式refs/unknown/单位不摘要；工作记忆可压缩；超大最新恢复事件先改索引；同指纹防付费摘要循环 |
| 用量与失败 | 正确 | reserved与sent分开，usage早于内容校验保存，unknown费用不填0，外部采纳先对账 |
| 日志与权限 | 大方向正确 | 脱敏、私有推理不存；trace不是授权；需要H03/H05将SDK工具和嵌套模型的最后接口补齐 |
| 恢复与产品清晰度 | 正确 | run分析和播中采集生命周期分离；取消不关声播；已收回结果可补校验，不每次从头生成 |

## 3. Middleware调用顺序的两个实现提醒

1. 实际异步方法名 `abefore_agent/abefore_model/awrap_model_call/awrap_tool_call` 正确。`hook_config(can_jump_to=['end'])`必须声明在实现的`abefore_model`上。不要用同步before_model定义却返回coroutine。
2. 如后续在Scope加`abefore_model`，不能在finish刚把run写COMPLETED后又因“run不是RUNNING”抛错，抢在Completion门前把成功改成失败。可只允许可信完成/等待回执无副作用短路，或明确先执行Completion门再做继续调用的scope检查。provider发送层仍检查取消/lease；只有确实准备新模型请求才需要发送权限。不要为同一个问题加两个互相阻挡的前置门。

另一个非阻断表述：02说“自定义backend工厂”。当前create_deep_agent签名接受BackendProtocol实例。实施时先执行项目工厂得到backend实例，再传backend，不照旧Skill示例直接把lambda当BackendProtocol传入而不验证。

## 4. 本轮新增只读机制证据

SDK纯假模型构建（不执行模型、不触网、不导入项目Runtime）取得0.7.25默认ToolNode真实工具名单：

```json
["delete", "edit_file", "execute", "glob", "grep", "ls", "read_file", "task", "write_file"]
```

这证明：

- 通用默认栈没有write_todos，不能沿用旧Skill里的“必然默认提供”概括。
- StateBackend不等于schema中没有execute/delete。即使某后端不能执行shell，暴露不适用工具也会诱发无用调用；应明确隐藏与拒绝。

默认栈源码位置：`deepagents/graph.py:887–918`。角色/工具注册与完成控制其它机制证据在同目录`SDK与Harness实现核查.md`§13、§15。

## 5. 复核结论与完成门槛

02/03已经足以说明总体Harness思路，但H01、H02、H03、H05是应在进入编码前明确的接口/控制缺口；H04、H06、H07也建议本次文档收口，避免由实施者各自猜一套。

这些修正都能落在现有Runtime、四业务中间件、typed服务、SDK配置和同一ToolSpec注册器中，不需要新增层级或新增Agent。修正后应以同一批假模型契约测试联验：等待/续办、内容评级保存与评分、SDK工具允许/拒绝、主子摘要预算、完成/错误/恢复、可选F05一次交付。无需每改一句文档就真实请求模型反复测试。

本轮未发现需要推翻Deep Agents主Agent自主规划、两个可选子Agent或四业务中间件结构的证据。
