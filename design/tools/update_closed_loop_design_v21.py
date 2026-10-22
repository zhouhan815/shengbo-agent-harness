"""Update design documents only. Does not import or execute either application."""
from pathlib import Path
import json
import re

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / 'engineering-v2'


def edit(name, replacements=(), append=''):
    path = DOC / name
    text = path.read_text(encoding='utf-8')
    for before, after in replacements:
        if before not in text:
            raise ValueError(f'expected passage absent: {name}: {before[:90]}')
        text = text.replace(before, after)
    text = text.replace('PRELIVE-2.0', 'PRELIVE-2.1')
    if append:
        text = text.rstrip() + '\n\n' + append.strip() + '\n'
    path.write_text(text, encoding='utf-8')


edit('00_后端工程设计.md', [
    ('| [06 前端对接合同](06_前端对接合同.md) | 人工操作/对话、选区、版本替换、事件和成果定位如何连接后端 |',
     '| [06 前端对接合同](06_前端对接合同.md) | 人工操作/对话、选区、版本替换、事件和成果定位如何连接后端 |\n'
     '| [08 架构与挂载图](08_架构图与中间件Skill挂载说明.md) | 前端、主从并行、具体Skill/Middleware、人工门槛和调试记录 |\n'
     '| [09 复盘输入与闭环](09_复盘输入与话术闭环设计.md) | 模拟/真实复盘接入、适用性、逐条处置与模块变化计划 |\n'
     '| [10 人工确认](10_人工确认与语音门槛设计.md) | 澄清、整包文案→语音批准、过期与可信人工API |\n'
     '| [11 原始运行记录](11_原始运行记录与调试台设计.md) | 主从树、时间线、原始输入输出、SDK与业务埋点、调试接口 |'),
    ('主 Deep Agent | 用户目标、冻结选区、业务观察、任务结果；判断本次缺什么、调用哪项能力、改哪些模块',
     '主 Deep Agent | 用户目标、冻结选区、业务观察、任务结果与上场复盘；判断本次缺什么、调用哪项能力、改哪些模块'),
    ('job_service.py / audio_service.py / publication_service.py',
     'job_service.py / audio_service.py / publication_service.py\n    recap_service.py / approval_service.py  # 复盘业务权威、文案→语音批准'),
], '''
## 10. 本次修订的闭环和可观察性

本版合同升为 **PRELIVE-2.1**。新增上场复盘的导入与显式绑定，作为4B的完整输入，不实现播后Agent。模拟文件见[复盘原文](fixtures/模拟上场复盘.md)和[结构化夹具](fixtures/模拟上场复盘.json)，均明确为合成测试资料。

新增整包文案到语音的人工门槛：在编排编译、得到精确AudioRequirements后展示 `AudioApprovalRequest`，可信人工命令生成 `AudioApprovalReceipt`；Agent只能申请/读取批准，不能批准自身。声播在TTS受理、领取、结果绑定及发布时验证批准仍有效。人工采用和开播继续独立。

一主三分析图不是四个共享大脑：主图选择能力、各角色图独立上下文并发分析，Supervisor只做普通程序调度。三路最长只需等待最慢的必要任务，而非把三段分析串行相加；实际耗时仍受模型配额和网络影响，不承诺固定速度提升。完整控制图见08。

M9扩展为安全原始事件采集，并由SDK callbacks覆盖摘要等旁路模型调用；声播JobRunner也记录自身文案/TTS过程。普通业务事件给前端展示状态，受限开发调试接口展示原始可观察消息、工具参数/结果与关联树。日志故障不能把已经提交的业务成功伪装成可重试失败。权限、载荷、安全过滤及降级规则见11。
''')

edit('01_类型与持久化.md', [
    ('selected_product_refs、draft_ref、analysis_refs、bundle_ref', 'selected_product_refs、draft_ref、analysis_refs、recap_refs、bundle_ref'),
    ('完整SessionDraft、旧稿全文/模块、用户决定、原资料/证据索引', '完整SessionDraft、旧稿全文/模块、用户决定、完整复盘原文/结构化finding/适用性、原资料/证据索引'),
    ('验收规则、触发原因；scope必须闭合', '验收规则、触发原因、recap_dispositions；scope必须闭合，每条复盘意见有处置'),
    ('| `AudioManifest` |', '| `AudioApprovalRequest` | 纯读构建的待审核整包，绑定精确话术/编排/voice/费用范围/事实readset及review_hash；不可由模型自定 |\n'
     '| `AudioApprovalReceipt` | 声播保存的可信人工批准，actor/批准对象hash/适用范围/状态/时间/operation；文字、编排、voice或被消费事实变化即失效 |\n'
     '| `RecapDocument` / `RecapFinding` | 复盘原文、结构化意见、来源/适用对象/证据性质；synthetic与真实记录明确区分；类型以09为准 |\n'
     '| `RecapDisposition` | finding_ref、apply/partial/reject/defer/already_covered、理由、目标模块、证据及限定范围；不把建议变成商品事实 |\n'
     '| `AudioManifest` |'),
    ('| `adoptions` |', '| `audio_approval_receipts` | approval_id PK、scope/preparation、review_hash、human_actor、bound refs/readset、status、operation；仅可信人工写 |\n'
     '| `adoptions` |'),
], '''
## 11. 复盘、人工语音批准和原始追踪的附加不变量

复盘及其适用/处置结果存声播 `business_artifacts`，原文存受控blob；`Preparation.recap_refs`只保留明确绑定的版本。主Agent/生成服务必须可回读全部已绑定原文，不从聊天摘要重建。替换或解绑复盘使受影响bundle/plan失效，不追改历史脚本；下次生成仍须显式计划。详细合同见09。

待审核请求纯读可重建，批准回执是声播业务真值。`AudioRequirements`与TTS job增加 `approval_ref/review_hash`；缓存文件不是批准记录。旧批准不能批准新的文字、顺序或音色。命令 `approve_text_for_audio/revoke_text_approval` 由human Principal提交，模型参数与自由聊天均不能充当批准回执。详细合同见10。

Agent运行库新增 `trace_events/trace_payloads/trace_exports`，仅存执行事实，不成为另一套商品或计划正本。每个事件同时带run、branch、invocation、span、parent_span、tool_call、operation、job等可用关联ID；事件按自身流单调seq追加。跨两个数据库不伪造全局原子时序，使用caused_by和声播outbox游标对齐。载荷hash/原始安全正文与前端摘要分别保存；不将实时运行中的模型原始上下文发给普通业务SSE。详细schema及存储失败策略见11。
''')

edit('02_运行时与中间件.md', [
    ('话术计划所需的商品/研究/本场/旧稿/用户决定均以声播 ContextBundle 完整引用汇合。',
     '话术计划所需的商品/研究/本场/旧稿/用户决定/已绑定复盘均以声播 ContextBundle 完整引用汇合。'),
    ('记录去敏 ID/hash、耗时、usage、结果状态；不记录凭据、认证头或把商家原始资料复制进普通日志。Audit 不承担重试、完成判断或恢复，不改变原始异常控制流。',
     '以上是基础Hook签名示意，原先只记hash/refs已不足本次要求；实际实现必须按11扩展：普通日志只放去敏索引；受限安全payload库保留实际可观察请求、响应、工具args/results原文和hash。SDK callback记录摘要等旁路；voice任务/outbox记录实际业务提交。不得采集凭据，不伪称有未公开内部推理。Audit不重试业务；提交后日志失败通过try_end/spool记录缺口，不能覆盖已取得的业务回执。'),
], '''
## 13. 本次新增的执行位置

不新增第十个Middleware。M3每轮观察提供 `recap_refs`、逐条处置完成率、当前 `audio_approval` 状态和可回读完整入口；S1解释复盘并制定模块计划，S2—S4不因挂复盘就取得额外写权限。复盘可在三路分析时并行导入，4B前汇合；重复导入同operation不新建副本。

M5在执行 `prepare_required_audio` 前读取当前审核绑定，缺批准返回 `TEXT_APPROVAL_REQUIRED`，引导主Agent调用 `read_text_review → request_user_decision`。M5只是早发现，声播事务必须再次强制检查；worker没有这些工具，不可能替主批准。QuestionService按问题类型保存目标/hash，interrupt只暂停当前主图；回答原问题可恢复原run，新目标扩权必须新run。详见10。

M9的实现以11为准覆盖完整安全载荷、调用关联、原始事件与独立摘要；开始记录应在动作发生前持久化，结束记录错误不得遮蔽原始业务结果。为了防止旧示例 `audit.end()` 的异常覆盖已提交动作，正式包装必须用11规定的无抛出结尾/独立补写策略，已知提交不能被重试。SDK持久checkpoint负责执行恢复；trace是调试证据，不能把重播日志当作重执行业务。

主/子并行控制仍是02§5的唯一typed队列实现。子任务输入彼此隔离，报告先PREPARED后主接受；主处于WAITING_JOBS时释放invoke租约而保留active-run槽，子租约不依赖父invoke。人工答复和后台结果经持久inbox唤醒，不能同一main thread并发执行两个ainvoke。
''')

edit('03_声播业务服务与事务.md', [
    ('→ 按 AudioRequirements 完成缺失音频', '→ 人工审核精确话术/编排/音色并批准 → 按 AudioRequirements 完成缺失音频'),
    ('还包含旧稿全文/模块、用户决定。', '还包含旧稿全文/模块、用户决定、已绑定复盘原文与结构化finding/适用性。'),
    ('adopt和start是两个独立动作；不额外增加逐份脚本approve的必经步骤。现有测试明确未单独approved的稿件可以在音频齐备后播放。',
     'adopt和start是两个独立动作；不逐句/逐稿重复批准，但本次新增TTS前的本场整包文案批准（见10）。现有旧播放测试不代表新批准门槛已实现，必须新增门槛回归。'),
    ('下节24项字典', '下节28项字典'),
    ("    'import_blob': blob_service.import_blob,", "    'import_blob': blob_service.import_blob,\n    'import_recap': recap_service.import_recap,\n    'link_recap_to_preparation': recap_service.link_recap_to_preparation,\n    'approve_text_for_audio': approval_service.approve_text_for_audio,\n    'revoke_text_approval': approval_service.revoke_text_approval,"),
    ('ImportBlobCommand | CreateRoomCommand', 'ImportBlobCommand | ImportRecapCommand | LinkRecapToPreparationCommand\n    | ApproveTextForAudioCommand | RevokeTextApprovalCommand | CreateRoomCommand'),
    ('24种命令的class.command', '28种命令的class.command'),
    ('issue/revoke仅控制器，adopt/start仅human。', 'issue/revoke仅控制器，adopt/start及approve_text_for_audio/revoke_text_approval仅human。'),
    ('| read_context | ContextBundle', '| read_recap | exact recap ref、scope、可选cursor | 完整复盘原文/结构化finding/定位；synthetic与证据性质明确 |\n'
     '| create_text_review | preparation/compiled plan/requirements精确refs | 纯读构造AudioApprovalRequest和review_hash；不写批准记录 |\n'
     '| read_text_approval | approval ref或当前审核目标 | 回读可信human回执与current/stale/revoked状态 |\n'
     '| read_context | ContextBundle'),
    ('本表19项', '本表22项'),
    ('| preview_audio | 精确spoken_text或sentence_ref、voice、normalized style、operation | submit短事务冻结试听需求',
     '| preview_audio | 精确spoken_text或sentence_ref、voice、normalized style、approval_ref、operation | 纯播放已有asset不创建新TTS；新合成须同10验证当前人工批准，submit短事务冻结试听需求'),
], '''
## 21. PRELIVE-2.1新增业务服务与强制门槛

本节和09—11补充前文函数，旧示意若省略了批准或复盘参数，均以本节为准；不是保留绕过门槛的旧handler。

| 公开函数 | 关键入参和事务 | 返回与拒绝条件 |
| --- | --- | --- |
| `import_recap(args, mutation, principal) -> RecapReceipt` | 已导入raw blob、结构化document/schema、synthetic/source；run_mutation中验证hash/意见ID/来源并保存不可变artifact | 不自动连prep，不写商品/优惠；同operation重放同回执；09定义细节 |
| `link_recap_to_preparation(args, mutation, principal) -> PreparationReceipt` | prep与expected_revision、exact recap refs、显式绑定/解绑范围 | CAS保存recap_refs和变化事件；废弃受影响bundle/plan；不把旧场活动带入新场 |
| `create_text_review(args, principal) -> AudioApprovalRequest` | 精确编译计划/requirements/话术refs/voice/费用范围/事实readset；一致只读构造 | hash派生request_id；无DB写，无批准副作用；缺文本/选择/条件返回blocker |
| `approve_text_for_audio(args, mutation, principal) -> AudioApprovalReceipt` | human-only；服务器重建审核对象，核验用户看到的review_hash及目标当前性，再run_mutation保存回执/outbox | Agent principal拒绝；过期409；重复operation不重复批准；不启动TTS/采用/开播 |
| `revoke_text_approval(args, mutation, principal) -> ApprovalRevocationReceipt` | human-only；批准ID/expected状态；事务撤销并停止该批准下未提交job | 撤销后不能领新项或绑定迟到音频；已发生外部费用留痕，不伪称退费 |
| `prepare_required_audio(args, mutation, principal) -> JobReceipt` | 新必填approval_ref/review_hash；受理与每次claim/commit重新验证精确绑定、授权、epoch/readset | 缺批准TEXT_APPROVAL_REQUIRED；不符TEXT_APPROVAL_STALE；仅缺音频项入队，缓存也不能替代批准 |
| `publish_preparation(args, mutation, principal) -> PublicationReceipt` | 新增approval_ref，检查当前整包批准、manifest/readset全部匹配 | 仍只发布unadopted candidate；旧批准下资产可作缓存但不能自动批准当前包 |

声播扩展 `recap_service.py/approval_service.py`，既有迁移增加审核回执表与业务artifact类型。批准命令权限来自可信human会话，不来自请求body写的actor_type。通过Agent聊天回答审核卡时，QuestionService验证真实user事件、明确question/version/hash，可信控制器再以human身份提交；模型只得到批准回执引用。批准不增加Agent原Goal/grant的对象或费用权限。

`save_script_plan`增加 `validate_recap_dispositions`：绑定复盘全部finding须处置一次，apply/partial必须指向本计划的具体模块或已有覆盖证据；不允许复盘“建议降价/免运/限量”绕过商品/活动事实与用户决定。文案任务冻结完整bundle，不能在生成途中读取刚上传的新复盘。11中的trace事件经job/outbox旁录，业务提交与事件同声播事务，原始provider载荷在事务外安全记录；不把跨库trace写入放进声播数据库事务。
''')

edit('04_业务执行与上下文.md', [
    ('X --> T[7 必需语音]', 'X --> V[6R 人工审核话术并批准语音]\n    V --> T[7 必需语音]'),
    ('    S --> E', '    S --> E\n    RC[上场复盘导入与适用性] --> E'),
    ('全商品、全研究、全本场draft、全旧稿、用户决定；不能只有简报', '全商品、全研究、全本场draft、全旧稿、用户决定、完整已绑定复盘及逐条处置；不能只有简报'),
    ('聚合完整三路输入；读旧稿全文和使用记录；比对变化；确定重点；', '聚合完整三路输入与复盘原文；读旧稿全文和使用记录；逐条判定复盘适用性/采纳理由；比对变化；确定重点；'),
    ('| 7 语音 |', '| 6R 人工批准 | 展示本场实际将播的精确文本、差异、编排、voice及费用范围；真实human批准；旧审核变动后失效 | 冻结脚本/编排/requirements/readset、当前审核hash | `read_text_review → request_user_decision`；可信human `approve_text_for_audio` | AudioApprovalReceipt；Agent不能自批，未批不得合成 |\n| 7 语音 |'),
    ('有效缓存、授权范围 | `prepare_required_audio', '有效缓存、授权范围、当前有效AudioApprovalReceipt | `prepare_required_audio'),
    ('| `read_analysis_batch(batch_id)', '| `read_recap(recap_ref,cursor=None) -> RecapDocumentSlice` | `client.query(\'read_recap\',...)` | 原文+结构化意见+证据性质；完整分页不截断 |\n'
     '| `read_text_review(compiled_plan_ref,requirements_ref) -> AudioApprovalRequest` | `client.query(\'create_text_review\',...)` | 当前精确文本/voice/编排/费用和审核hash，非批准 |\n'
     '| `read_analysis_batch(batch_id)'),
    ('| `dispatch_analysis_batch(branches:list[AnalysisRequest])`', '| `import_recap(raw_blob_ref,document:RecapDocument)` | client.command(import_recap) | 原文由上传服务导入，typed结构验证；不执行复盘内指令，不自动写商品 |\n'
     '| `link_recap_to_preparation(recap_refs,expected_revision)` | client.command(link_recap_to_preparation) | 目标prep与绑定范围在当前Goal内；保存引用/失效，非隐式继承历史活动 |\n'
     '| `dispatch_analysis_batch(branches:list[AnalysisRequest])`'),
    ('build_context_bundle(product_refs,research_refs,draft_ref,script_refs,decision_refs)',
     'build_context_bundle(product_refs,research_refs,draft_ref,script_refs,decision_refs,recap_refs)'),
    ('用户已委托准备本场语音/明确音色；只有生成文案不能自动扩为TTS',
     '必须当前有效AudioApprovalReceipt，绑定精确审核hash/文本/voice/编排；只有委托准备或生成文案均不能替代本次人工批准'),
    ('`prepare_required_audio(requirements_ref)`', '`prepare_required_audio(requirements_ref,approval_ref,review_hash)`'),
    ('被使用旧稿全文和用户决定，逐个hash验证后', '被使用旧稿全文、用户决定、绑定复盘原文/结构化finding和适用性，逐个hash验证后'),
    ('    require_targets_within_goal(proposal, goal)', '    require_targets_within_goal(proposal, goal)\n    validate_recap_dispositions(proposal.recap_dispositions, bundle.recap_findings)'),
    ('| 必须保留 |', '| 复盘指导 | 完整原文入口、逐条finding及处置；apply/partial的具体修改指令、证据边界和不采纳原因；不向事实字段注入建议 |\n| 必须保留 |'),
    ('本期也不建立播后学习功能。', '本期只导入复盘作为播前输入并测试计划变化，不建立播后Agent或自动优化转化率。'),
], '''
## 11. 主从控制、复盘汇合及确认的确定顺序

主Agent先判断依赖，再 `dispatch_analysis_batch`；Supervisor最多同时运行3个独立角色任务，主不持有子任务消息历史。主等待完成事件后逐份验证/接受当前报告，缺一个必要事实只阻塞依赖该事实的产物，不重做另两份有效报告。并行能否通过由input manifest与readset决定，不是模型随意说“可以并行”。

复盘导入、事实/本场/研究分析可并行；**话术计划不能在必要输入未汇合时定稿**。4B读取所有绑定复盘，对每条finding记录apply/partial/reject/defer/already_covered；新建无复盘时显式 `recap_refs=[]`，不得假装分析过上场。计划依然保存所有三路完整输入及旧稿，不因加入复盘丢失以前上下文。09给出拖鞋示例。

6完成用户编排并计算实际长/短开场以后进入6R，human批准当前整包才到7。只改明确商品不重复问是否执行；同名身份不确定则在4A保存前澄清；确切活动变化可直接改相关模块，但改后TTS仍须新的整包批准。10给出完整决策表。

每一个模型/工具、branch派发完成、报告接受、问题中断/恢复、业务job、复盘处置与审核都产生可关联trace；用户从运行树点到原文/结果/diff，普通进度与开发调试视图分开，11给出接口和原始记录合同。
''')

edit('05_实施顺序与验收.md', [
    ('视频生成、播后循环、自动优惠决策', '视频生成、完整播后Agent、自动优惠决策'),
    ('playback compiler → required audio job', 'playback compiler → 人工文案批准服务 → required audio job'),
    ('1 → 2A/2B/3 → 4A → 4B → 5 → 6 → 7 → 8', '1 → 2A/2B/3（同时可导入复盘）→ 4A → 4B → 5 → 6 → 6R人工文案批准 → 7 → 8'),
], '''
## 12. 新增闭环/批准/原始追踪验收包

以下均为待实现的运行验收，当前只验证文档/夹具结构。详细故障和断言分别在09/10/11。

| 编号 | 场景/注入 | 必须观察到的结果与指标 |
| --- | --- | --- |
| CL-01 | 三角色fake model分别设置受控阻塞点，全部入队 | 观测start/end区间存在交叠，峰值≤3；主图同thread invoke并发=1；每报告只接受一次 |
| CL-02 | 商品worker失败，本场/顾客报告成功 | 独立报告不重做；只重试失败且依赖当前的任务；无编造ready |
| CL-03 | 导入模拟复盘后新建/局改话术计划 | finding处置覆盖率=100%；被采纳条目有具体模块/证据；拒绝制造库存/售后/优惠；商品178 TWD与本场107 TWD未擅改 |
| CL-04 | 换不相干商品/过期活动/对照不足的复盘 | 不错误套用；advisory建议不伪称因果结论；原文和排除原因可回读 |
| HC-01 | Agent直接发起TTS，缺人工批准 | 业务拒绝TEXT_APPROVAL_REQUIRED，新provider调用数=0；缓存不能自行批准 |
| HC-02 | 批准后改文字/voice/编排/被消费事实，再重放旧请求 | 拒绝TEXT_APPROVAL_STALE；无旧job激活；未改变的音频可继续缓存 |
| HC-03 | 同名上传 vs 明确指向product_id更新 | 前者必须身份澄清；后者授权范围内不重复执行确认；两者TTS均经过整包批准 |
| HC-04 | 并发撤销批准/领取TTS/迟到结果 | 事务先后清晰，撤销后无新claim/active绑定；外部已发生费用有记录，unknown不重付 |
| TR-01 | 主/子/文案/TTS成功与失败、interrupt/resume、SDK摘要 | 有关联树和安全原文；模型/工具调用配对率=100%（崩溃未结束项明确unknown）；丢失不假填 |
| TR-02 | 用长载荷、分页、导出及凭据canary | UI预览可折叠但安全原文导出hash一致；可见凭据残留=0；过滤位置/版本有标记 |
| TR-03 | 业务提交后trace结束写入失败 | 原业务仍只提交一次，回执可对账；不得因日志错误触发新operation/provider重复调用 |
| TR-04 | 两服务时钟偏移、重复outbox、恢复 | 以causal IDs与各流seq复建过程；不伪造全局时序；重复事件不重复呈现/执行 |

P0冻结RecapDocument/Approval/Trace schema和本目录fixtures；P1实现声播批准/复盘存储和trace安全载荷仓；P2挂M9与SDK callback；P3验证并行和中断；P4/5接复盘计划、6R和全部旧API门槛；P6注入上述故障；P7才接前端开发调试查看页与真实供应商小样本。不得等所有业务完成后再补日志。
''')

edit('06_前端对接合同.md', append='''
## 9. 复盘输入、文案批准与开发调试的新增合同

原前端文档只作交互参考；以下后端合同是新增工程依据，不在本轮编写前端。

| 交互 | 后端入口/回执 | 展示与限制 |
| --- | --- | --- |
| 上传上场复盘 | 现有materials入口→import_recap；显式link_recap_to_preparation | 标明模拟/真实、原文和finding、适用商品/场次；上传不自动覆盖商品 |
| 检查复盘如何影响本稿 | read_context(plan/bundle) | 每条意见→采纳状态/理由→目标模块→实际diff；保留不采纳意见 |
| 文字完成后申请语音 | read_text_review→create_text_review | 展示本场实际文本/长短开场/顺序/音色/费用范围及review_hash |
| 人工点击批准或回答对应审核卡 | 可信human approve_text_for_audio | 精确question/version/hash；回AudioApprovalReceipt；修改hash后旧卡不可批准，不把普通“好”解释成未知包批准 |
| 暂不批准/继续编辑 | QuestionService决定事件，必要时revoke_text_approval | 无新TTS；可保存草案，不误判为取消全部资料 |
| 采用/开播 | 既有human adopt_snapshot、start_prepared_room | 与文案→语音批准分别展示，批准生成音频不代表批准开播 |
| 查看运行过程 | 本机开发调试API，详11 | 运行树/时间线/原始事件/调用详情/变更对比/导出；普通SSE不塞原始prompt与资料 |

身份重复等澄清卡沿原QuestionService/interrupt协议，答案后只恢复对应run；scope_conflict扩权仍新建replace run。审核批准由human端点形成声播权威记录，模型不能通过question答案拼一个approval。开发调试只读，点击事件定位原产物，回放日志不重放命令，重试操作另走受控业务入口。
''')

edit('07_设计审查与修订记录.md', append='''
## 4. 本次用户新增需求的设计修订（PRELIVE-2.1）

| 旧缺口 | 本次解决 | 规范 |
| --- | --- | --- |
| 主/分析角色和并行的控制方式展示不足 | 显示唯一typed派发、角色隔离、并发上限、inbox唤醒与选择性汇合 | 00/02/04/08 |
| 首期排除全部播后信息会阻断测试闭环 | 只加入复盘输入接口/模拟夹具/逐条处置，继续不开发播后Agent | 09与fixtures |
| “不逐稿approve”容易误解为TTS前无审核 | 增加精确本场整包AudioApprovalReceipt，后端执行门槛，不增加逐句弹窗 | 03/04/10 |
| 同名新上传可能误覆盖 | 身份澄清与明确精确更新分流，事实冲突/扩权另问，不一律每次确认 | 10 |
| Audit只记ID/hash不足以查过程 | 安全原始payload仓、SDK callback/业务job埋点、运行树/时间线/导出 | 11 |
| Audit.end可能覆盖已成功的业务结果 | 结束写入失败spool/缺口标记，保留原业务回执，禁止日志错误驱动重复副作用 | 02/11 |

本次修订仍为设计；原始运行数据需要后续真实调用才会产生，模拟复盘不是实测效果。静态验证不替代05新增CL/HC/TR集成验收。
''')

index_path = DOC / '设计索引.json'
index = json.loads(index_path.read_text(encoding='utf-8'))
index['contract_version'] = 'PRELIVE-2.1'
index['documents'].extend(['09_复盘输入与话术闭环设计.md', '10_人工确认与语音门槛设计.md', '11_原始运行记录与调试台设计.md'])
index['main_business_tools'].extend(['import_recap', 'link_recap_to_preparation', 'read_recap', 'read_text_review'])
index['architecture']['audio_gate'] = ['compile_requirements', 'human_approve_text_for_audio', 'verify_current_approval', 'required_only_TTS']
index['architecture']['recap_authority'] = 'shengbo_business_artifacts'
index['architecture']['trace'] = 'append_only_sanitized_original_payloads_and_causal_events'
index['business_steps'].insert(index['business_steps'].index('7'), '6R')
index['pending_implementation_probes'].extend(['recap_disposition_coverage', 'human_text_audio_approval', 'full_payload_trace_and_export', 'trace_failure_after_business_commit'])
index['registry_counts'] = {'commands': 28, 'queries': 22, 'main_business_tools': 32}
index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

baseline = ROOT / '10_播前Agent首期流程基线.md'
text = baseline.read_text(encoding='utf-8')
text = text.replace('4A完整商品库＋3研究结果＋2B本场配置进入4B，另读取已有话术和用户决定',
                    '4A完整商品库＋3研究结果＋2B本场配置进入4B，另读取已有话术、用户决定和已绑定复盘全文/意见')
text = text.replace('再由6确定播放安排，7准备语音', '再由6确定播放安排，6R人工审核精确话术/编排/声音并批准，7准备语音')
text += '''

## 本次补充：复盘输入与文字到语音的确认

最新用户要求追加到首期：可导入一份明确标注模拟的上场复盘（未来同接口接受真实复盘），4B将其与全部原有输入共同用于逐模块计划，每条意见记录采纳/排除及原因；不开发完整播后Agent。以[09设计](engineering-v2/09_复盘输入与话术闭环设计.md)和[模拟复盘](engineering-v2/fixtures/模拟上场复盘.md)为准。

第6和7步之间增加6R整包文案→语音人工批准。只有当前精确文本、编排、voice、消费事实和费用范围的批准有效时才能合成；后续改动使旧批准失效。最终采用/开播仍分开。详[10确认设计](engineering-v2/10_人工确认与语音门槛设计.md)。

历史主表若未写复盘或6R是较早范围，以上为本次明确追加，不再以“首期不接复盘”拒绝当前需求；其他尚未授权的播中/播后自动优化仍不实施。原始执行追踪设计见[11](engineering-v2/11_原始运行记录与调试台设计.md)。
'''
baseline.write_text(text, encoding='utf-8')

print('Updated design-only contracts to PRELIVE-2.1; worker-owned 09/10/11 not modified.')
