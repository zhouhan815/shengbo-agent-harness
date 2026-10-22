# 播前 Harness 主稿 SDK 复审

日期：2026-10-15。审阅范围：`13_播前Agent_Harness总体设计.md`、`14_逐步执行与工具数据契约.md` 的 SDK/API、状态隔离与执行控制语义；不复审业务取舍，不修改主稿。当前结论为**设计主线可实施，以下实施合同需补齐**。本次未调用真实模型，未创建业务代码。

## 1. 已正确表达的部分

| 项目 | 主稿位置 | 复审结果 |
|---|---|---|
| 动态总循环与局部工作流 | 13 第3、5节；14总表 | 保留模型选择能力，程序保护业务约束，符合用户要求 |
| SDK 主入口 | 13 第10节 | `create_deep_agent` 与现有版本一致，没有额外复制总循环 |
| Todo | 13:327 | 已写显式添加，纠正静态 Skill 旧默认值 |
| 隔离消息不等于全部隔离 | 13:224、268 | 已识别非private state/files共享和自定义子Agent配置边界 |
| 文件预置 | 13:264–266 | StateBackend + 首次输入files + create_file_data形态受本机源码支持 |
| 持久恢复 | 13:331、357–371 | 明确SQLite额外依赖未装、checkpoint不足以保证外部副作用恰好一次 |
| 异步任务 | 13:396；14第10、13节 | 正确区分普通后台job与SDK远程AsyncSubAgent |
| Stop转换 | 13:348–353 | 正确选择支持跳转的after_model；没有把after_agent当成Stop阻止器 |
| 并发fallback | 13:4.2 | 明确模型并发需实测，批量适配器只能替代派发路径，不能同批重复派发 |
| 使用事实与待测边界 | 13首部、410；14首部 | 没有把设计和离线技术探针当成业务已完成 |

## 2. 必须补齐的实施合同

### M1：给 general-purpose 一个可执行的封口方案

**位置：13第4、10节，14第12节末尾。**现有“必须替换/限制”方向正确，但没有说明使用哪个实际支持的API；若实施者只传三业务子Agent，SDK仍会自动添加可继承父工具的general-purpose。

本机 `deepagents/graph.py:817–823` 明确：调用者已声明同名`general-purpose`时，不再自动添加默认项。`graph.py:785–794`表明显式`tools`覆盖父工具。`FilesystemPermission`公开导出在`deepagents/__init__.py:9`，写权限枚举为`write`，不是`write_file/edit_file/delete`。

**首期最稳妥方案：显式注册同名受限子Agent＋主派发白名单，双层限制。**

```python
from deepagents import FilesystemPermission

readonly_general_purpose = {
    "name": "general-purpose",
    "description": "只读信息核对，不提交业务变更。",
    "mode": "isolated",
    "system_prompt": "仅核对可读取资料；不得执行业务写入或继续委派。",
    "tools": [],  # 或项目审核过的只读域工具；显式空列表不会继承父工具。
    "permissions": [
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ],
    "middleware": [shared_budget_guard_for_this_role, role_guard],
}
# 与三类业务SubAgent一起放到create_deep_agent(subagents=[...])。
```

上述只是配置合同：实例由后续实现提供。本轮未执行此示意。SDK文件permissions只约束内置文件工具，不能替代业务API校验；这个同名子Agent应完全没有正式写入工具。

主Agent的业务派发工具/`task`包装守卫仅允许`catalog-analyst`、`session-analyst`、`audience-researcher`，由运行时注入role。即使遗漏派发拦截，同名general-purpose也只能只读；SDK自动注入的文件工具不得成为修改业务权威对象的途径。

**不要写成**`create_deep_agent(general_purpose_subagent=...)`：本机工厂没有这个参数，该设置属于`HarnessProfile`。首期无需为关闭通用角色引入按模型注册的全局profile副作用。

验收：正常三类委派成功；主动调用general-purpose被派发守卫拒绝；测试绕过派发守卫时仍无`save_product/execute_script_plan/submit_required_audio`，`write_file/edit_file/delete`写入被拒绝；不靠system_prompt自律。

### M2：把父子共享预算/取消守卫明确装进每个worker

**位置：13:323、346、381。**“父子共享预算”作为目标正确，但当前容易被实现者写成只给主Agent传一份`middleware=`。本机`graph.py:753–760`显示isolated声明式子Agent只取自己的`spec.middleware`；通用子Agent只继承可替换默认槽位的部分middleware（`:847–850`）。普通自定义全局预算类不会天然全链路继承。

应补：每个业务SubAgent、只读general-purpose、fallback worker均显式注册同一预算/取消服务的**角色化middleware实例**；原生路径和fallback路径统一使用同一预算身份。预算扣减/预留使用服务端原子操作，覆盖模型、摘要模型、重试与外部计费生成作业。不要把API key、可变预算字典、可伪造role放到模型控制的state输入。

必填限制至少包括`max_model_calls`、token/费用或合理替代硬界、总时限、工具超时、子任务上限；没配置硬界时启动检查失败。SDK的`recursion_limit=9999`（`graph.py:1004`）不是本项目预算。调用服务的模型封装与业务middleware共同限制，不能仅数主Agent的messages数。

验收：主Agent剩余预算不足时无法再委派；三个并行worker不超卖共享额度；取消后worker后续工具提交与fallback派发都被阻止；自动摘要/模型重试计量不漏计。

### M3：隔离报告与files的作用域必须有程序实现

**位置：13:224–225、264–268；14的save_branch_artifact与ContextBundle。**方向正确，需补三项明确不变量：

1. `files`是SDK共享通道，不可以用`PrivateStateAttr`简单整体隐藏，否则Skills与文件工具不能按原有语义运行。正式Product、Session、Plan、预算与租约不放共享state里。
2. 业务worker用`save_branch_artifact`保存产物；scope、task、attempt由可信工具闭包/服务绑定，模型传的路径或role不生效。必须验证所有写入均在该任务草稿范围，报告结果回主Agent后经typed解析、输入版本校验再合并。
3. worker通用文件工具的读取/写入策略明确：`/skills/**`、`/policies/**`禁止写；其他worker草稿不能写。仅写提示“各自目录”不算隔离。直接backend调用绕过内置文件permissions（`graph.py:502–503`），自定义工具也必须执行同等检查。

本机`middleware/subagents.py:725–765,782–813`表明非排除/非private变化state会回并。因此主运行必须只允许预期类型/引用返回；不可让worker把`goal_revision`、`current_plan`等写成自己的值再依靠last-writer-wins。

验收：三分支同时返回不同draft无冲突；构造worker返回父版本/预算字段不会覆盖主对象；越界草稿写、政策改写被实际拒绝；一个worker失败不删除其他分支files。

### M4：补精确的interrupt重放与线程锁合同

**位置：13:5.1、329；14的request_user_decision。**在工具中“先持久化问题、再interrupt”可以实现，但恢复会重执行包含interrupt的节点/工具；此前写入必须可重放。

应补：问题batch和问题ID由准备/目标/对象/缺口指纹稳定分配，重复执行只读回同一条；用`Command(resume=...)`继续同一thread，不把回答作为另一个独立invoke强行跳过interrupt。回答带question ID和对应对象/目标revision；旧问题收到回答时返回过期/重评估，不硬套当前商品。

进入`waiting_input`后结束本次活跃运行、释放执行租约，保留线程检查点与待答记录；恢复获取新租约，同一thread不允许两个并发invoke。不能锁着等待用户而使恢复请求无法拿到锁。`waiting_job`也应明确是模型本轮正常结束，由作业观察器在有效修订下请求一次续跑，而非滥用HITL或持续付费轮询。

验收：问后重启→回答→恢复只产生一条QuestionBatch/Decision；重复回答不重复保存；取消或改商品后的旧答案不恢复旧计划；同时两条回复只有一个有效resume。

## 3. 建议进一步明确的点

| 编号 | 位置 | 建议 | 原因/验证 |
|---|---|---|---|
| S1 完成守卫的候选检测 | 13:348–353 | 仅当模型本轮拟结束、没有尚待执行的真实工具调用时核验；结构化输出路径单独识别。必要的blocked/waiting/预算退出不强行跳回模型 | after_model在每次模型调用后执行；不能把正常“我要查商品”的工具调用拦成过早完成，也不能破坏AI tool call/ToolMessage配对 |
| S2 完成守卫返回合同 | 同上 | 使用`@hook_config(can_jump_to=["model"])`等当前SDK支持声明；返回合法state update，追加有来源的缺口反馈，清理/避免复用旧structured_response；after_agent只记账 | 本机`middleware/types.py:881–927`支持before/after_model跳转；需正常final、工具final、interrupt、异常四类探针 |
| S3 并发fallback装配 | 13第4.2节 | 设统一worker配置构建器，原生和fallback共享prompt、Skills、工具、guard、输出schema、预算/任务身份。fallback只用公开API构建worker，不引用SDK私有内部编译缓存 | 三个声明式SubAgent内部由SDK编译；应用不能默认拿到可独立执行的worker对象。若fallback用create_deep_agent重建worker，也要限制其默认task/general-purpose，否则“只一层委派”失效 |
| S4 并发fallback失败处理 | 同上 | bounded gather收集每项结果；单项错误转typed报告；批次同一dispatch_key，不能原生超时后立即fallback导致旧批次仍运行 | 验收原生部分完成＋错误/超时，不重复启动已有效worker；取消传播与迟到结果fence |
| S5 子Agent结构化结果 | 13第4.3节 | 明确实际声明`response_format`或由save_branch_artifact schema校验；最终ToolMessage保存产物引用，不能仅在prompt列字段名 | SDK支持结构化子结果JSON序列化（subagents.py:739起），但DeepSeek策略需探针；不得宣称仅加Pydantic类型即端点支持 |
| S6 虚拟Skills升级 | 13第8.4节 | 每线程固定skill_bundle_version/hash，恢复不重新用新包覆盖files；升级走新任务或显式迁移 | before_agent可复用缓存skills_metadata；运行中暗换包可能导致目录与正文/任务规则不一致 |
| S7 SQLite生命周期 | 13:331 | 明确在FastAPI lifespan中`async with AsyncSqliteSaver.from_conn_string(...)`持有连接，不能with退出后仍保存agent对象 | import为`langgraph.checkpoint.sqlite.aio`；包未装，须先兼容安装再做中断/重启/并发探针 |
| S8 任务取消 | 13第12节、14 cancel_job | 补逻辑取消与物理取消分别记录；future取消只说明本机等待停止，厂商已受理任务需保留unknown/late结果，不回报未发生的refund/撤回 | 当前主稿已有fence原则，实施时要让UI显示取消是否已确认 |

## 4. 最小 SDK 验收矩阵

| 场景 | 必须观察到的事实 |
|---|---|
| 主入口编译与工具循环 | DeepAgents factory产物；真实模型完成工具往返；工具scope由服务注入 |
| 三业务角色与旁路 | role白名单有效；general-purpose无写业务工具；worker无再次委派通道 |
| state/files隔离 | 三分支各自草稿；父state/正式对象不被回并覆盖；政策不可改 |
| 预算与取消 | 所有模型路径/重试/摘要均计量；并行额度原子；取消阻止后续提交 |
| 真并发与fallback | 时间记录显示重叠；dispatch路线唯一；输出同schema；单项失败不重复收费/执行 |
| 完成守卫 | 正常工具调用不误拦；无证据final被有限纠正；needs_input/waiting_job/预算能退出 |
| HITL恢复 | 同线程问题重放幂等；进程重启可resume；旧答案无效；同线程并发被拒绝 |
| 后台恢复 | 不需要持续模型轮询；重启对账已受理job；迟到结果不激活旧计划 |

未达到以上行为验收之前，13/14中的组件仍是设计项。可先冻结合同，后续通过最小技术切片再决定SDK原生task和批次fallback的默认路线。
