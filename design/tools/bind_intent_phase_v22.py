from pathlib import Path
R=Path(__file__).resolve().parents[1]/'engineering-v2'

def edit(name,pairs,append=''):
    p=R/name;t=p.read_text(encoding='utf-8')
    for a,b in pairs:
        if a not in t: raise ValueError((name,a[:80]))
        t=t.replace(a,b)
    if append:t=t.rstrip()+'\n\n'+append.strip()+'\n'
    p.write_text(t,encoding='utf-8')

edit('00_后端工程设计.md',[
 ('  → client.command(issue_grant)；记录声播授权回执\n  → RunExecutor.invoke_once',
  '  → ASSESSING_INTENT；RunExecutor.invoke_once(phase=intent)，同一主图只读识别\n'
  '  → save_task_assessment保存IntentAssessment候选；确定性许可校验／必要澄清\n'
  '  → Coordinator.freeze_goal（仅首次）；需要写入才AUTHORIZING→issue_grant\n'
  '  → 同run/thread的RUNNING；RunExecutor.invoke_once(phase=execute)'),
 ('协调器不解析模型最终文字决定成功；',
  '意图阶段不输出业务完成：同主模型先识别目标，Coordinator以13的真实许可证据首次冻结Goal后才授权；已冻结Goal不能通过计划修改。协调器不解析模型最终文字决定成功；'),
 ('只保存了商品的任务可 `COMPLETED`',
  'catalog_update已保存商品、完成相关研究维护并给出影响清单后可 `COMPLETED`'),
], '''
## 13. 首次任务识别的调用边界

执行链以第7节和13分册为准，不再先授予宽权限再让模型判断。新run首先记录不可变用户事件、scope、selection，phase=intent；Goal/grant尚不存在。M1只验读权限和租约；M4/M5仅开放受限读、本地候选保存和问题。主图返回已保存的resolvable候选回执后，Coordinator验证许可并首次freeze_goal，再以稳定operation申请需要的写权限，安排execute invocation；不另起router模型。

结构化动作或覆盖明确完整指令形式的文本政策可免重复确认；对象/范围歧义才集中澄清。程序不宣称能普遍证明任意自然语言的全部授权含义。phase切换、Goal冻结、grant回执由服务控制，模型没有freeze_goal/issue_grant工具。
''')
edit('01_类型与持久化.md',[
 ('run_id PK、conversation、scope_key、目标合同、冻结输入',
  'run_id PK、conversation、scope_key、phase、intent_event_ref、intent_assessment_ref、intent_evidence_ref、goal_ref可空、goal_frozen_at、acceptance_spec_ref/result_ref、冻结输入'),
 ('QUEUED → AUTHORIZING → RUNNING',
  'QUEUED → ASSESSING_INTENT（phase=intent，Goal/grant为空）\nASSESSING_INTENT → WAITING_USER（resume_phase=intent） / AUTHORIZING / RUNNING\nAUTHORIZING → RUNNING（phase=execute，Goal已首次冻结）'),
 ('WAITING_* → RUNNING                 # 同run/thread，新的invocation',
  'WAITING_USER → ASSESSING_INTENT或RUNNING  # 按可信resume_phase恢复，同run/thread新invocation\nWAITING_JOBS → RUNNING                 # 已授权execute阶段，新的invocation'),
 ('`submit_intent`先在Agent库记录AUTHORIZING和稳定grant operation；声播 `issue_grant` 回执落盘后才RUNNING。',
  '`submit_intent`先记录QUEUED/不可变用户事件，激活后ASSESSING_INTENT；同一主图只读识别、必要澄清后，Coordinator在Agent事务首次freeze_goal并登记AUTHORIZING及稳定grant operation（只读Goal跳过grant）。声播 `issue_grant` 回执落盘后才phase=execute/RUNNING。'),
], '''
## 13. 意图候选与Goal冻结

新增ASSESSING_INTENT只承担同主图只读任务识别。runs.phase=intent时goal_ref/grant_ref为空，intent_event_ref必须真实不可变；WAITING_USER保存resume_phase。IntentAssessmentProposal/Receipt/AuthorizationEvidence按13唯一schema。`save_task_assessment`根据可信phase仅接受对应discriminator，模型不提交phase/actor/授权。

`freeze_goal`要求未冻结、候选及真实许可依据当前、scope slot/invoke lease有效，按固定政策编译Goal并保存一次；幂等重放返回同Goal，异候选不能覆盖旧Goal。该事务安排授权/执行事件，不在SQL写锁里调用声播。已冻Goal的范围改变仍是新run/replace。未派发授权前取消可本地原子禁止未来派发；AUTHORIZING在途未知继续使用声播tombstone。

State、TaskAssessment和runs不形成循环回填：Goal先冻结，执行Assessment引用Goal，AcceptanceSpec单向引用Assessment，runs再分别关联Spec/Result；不把后生成Spec写回不可变Assessment内容。
''')
edit('02_运行时与中间件.md',[
 ('    objective_ref: str             # 用户意图与允许范围的不可变引用',
  "    phase: Literal['intent', 'execute']\n    intent_event_ref: str         # 原始可信用户事件\n    objective_ref: str | None     # intent为空，execute为首次冻结Goal引用"),
 ('    terminal_receipt_ref: NotRequired[str]',
  '    terminal_receipt_ref: NotRequired[str]\n    intent_resolution_ref: NotRequired[str]'),
 ("        return {'guard_retry_count': 0, 'terminal_receipt_ref': None}",
  "        return {'guard_retry_count': 0, 'terminal_receipt_ref': None, 'intent_resolution_ref': None}"),
 ('    receipt = await turns.read_valid_receipt(runtime.context)',
  "    if runtime.context.phase == 'intent':\n        return await deps.intent.guard_candidate_or_request_clarification(state, runtime)\n    receipt = await turns.read_valid_receipt(runtime.context)"),
 ('        receipt = await turns.require_and_revalidate(scope)\n        return await finish_invocation(scope, receipt)',
  "        if scope.phase == 'intent':\n            resolution = await deps.intent.require_current_resolvable(scope, snapshot)\n            goal = await coordinator.freeze_goal(run_id, resolution.ref)\n            # 同Agent短事务安排authorize_frozen_goal/execute事件；后台在invoke释放后继续\n            return await coordinator.enqueue_after_goal_frozen(scope, goal)\n        receipt = await turns.require_and_revalidate(scope)\n        return await finish_invocation(scope, receipt)"),
 ('只读解释run可以不申请声播写grant，但仍必须有可信read principal和WorkScope。',
  'intent阶段未有Goal，assert_main_invoke只验可信read principal、WorkScope、slot/run状态和invoke lease；execute阶段必须存在已冻结Goal。只读解释run可以不申请声播写grant，但仍必须有可信read principal和WorkScope。'),
 ('主工具 `finish_turn` 返回的disposition', '主工具 `finish_turn` 返回的disposition') if False else
 ('M4在派发和业务写前要求有效TaskAssessment', 'execute阶段M4在派发和业务写前要求有效TaskAssessment'),
], '''
## 16. 两个phase的唯一装配与调度

| Hook／部件 | phase=intent | phase=execute |
| --- | --- | --- |
| scope构造器/M1 | 从runs注入phase/intent_event_ref，objective/grant空；检验读权限、slot、invoke lease | 首次冻结Goal必有；按原授权/租约检查 |
| M3投影 | 原始真实指令、精确选区、当前对象、有限许可政策、候选和歧义 | 原Goal、执行Assessment、历史/当前依据及批准状态 |
| M4/M5 | 受限读取、request_user_decision、本地save_task_assessment；拒绝dispatch/写业务/网络付费/TTS/finish_turn | 原Goal∩角色工具∩计划效果；写入仍需声播grant |
| M7 | 无tool_calls后验证当前IntentResolutionReceipt；resolvable只交协调器，不输出用户任务完成 | 按GoalAcceptanceResult及实际回执守卫 |
| invoke_once/Coordinator | 读取run.phase调用同main图；候选就绪后freeze_goal，安排授权/execute，释放本invoke租约 | 同run/thread续跑，按原真实结果结束/等待 |
| M6/M9/callbacks | 模型预算、原始记录同样计入当前run；记录首次Goal冻结/阶段变化 | 继续累计，不因phase改变清零父预算 |

`guard_candidate_or_request_clarification`：检查候选回执的run/invocation/原事件/对象版本/hash；resolvable返回intent_resolution_ref，needs_decision且无真实interrupt时有限纠正要求主调用问题工具，最多2次后阻塞；不伪造回答或自动批准。前置阶段也用同一持久QuestionService，问题kind=task_scope，回答后resume_phase=intent，不新建业务授权。

`select_invoke_payload`增加phase切换事件：前一intent图已正常结束且无pending节点/interrupt后，才在同thread注入稳定goal_frozen事件进入execute；若intent图未收敛先恢复旧调用，不能插入新消息覆盖pending工具。phase切换发生在协调器，不由模型state赋值；child_from_claim只接受execute父run，因此没有未授权worker。

背景授权服务 `authorize_frozen_goal`仅消费协调器持久outbox中的GoalReceipt；同run同Goal派发一次，取消在派发前原子阻止；已派发未知必须远端撤销。初始自由文本不能靠一个模糊“可信控制器”直接获得广泛写权，也不能每个确定性子步骤都重复问用户。
''')
edit('10_人工确认与语音门槛设计.md',[
 ("    'identity', 'missing_fields', 'fact_conflict', 'scope_conflict',", "    'task_scope', 'identity', 'missing_fields', 'fact_conflict', 'scope_conflict',"),
])
edit('11_原始运行记录与调试台设计.md',[
 ('`QUEUED/AUTHORIZING/RUNNING/', '`QUEUED/ASSESSING_INTENT/AUTHORIZING/RUNNING/'),
], '''
## 13. 同主模型首次意图阶段的记录

本轮phase=intent/execute属于同run/thread不同invocation；保留候选原文、真实用户事件、有限政策命中／范围答复、确定性验证、Goal首次冻结及grant回执。ASSESSING_INTENT不是用户任务完成；主图只读识别后的自然语言结束不发业务COMPLETED。摘要与模型调用费用跨phase累计；子任务只有execute时派发。
''')
edit('15_可复用工具流与Agent协调边界.md',[
 ('CO[可信入口：保存用户目标和原始选区]', 'CO[可信入口：保存原始指令和选区]'),
 ('MAIN[主Deep Agent：S1＋TaskAssessment＋按需上下文]', 'MAIN[同主Deep Agent：只读意图→首次冻结Goal→执行计划]'),
], '''
首次自由文本识别也在同一个主图中完成：phase=intent仅允许受限读取、候选保存与必要澄清；Coordinator首次冻结Goal并授权后进入execute。`save_task_assessment`兼容两种类型但不发grant；F流程在相应正式权限具备时才执行。详13§1.2—1.5及02§16。
''')
print('Bound first-intent phase to existing runtime; design only.')
