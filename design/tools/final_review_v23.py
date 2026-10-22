"""Apply the final design review to planning documents and synthetic fixtures only."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / 'engineering-v2'

def edit(name, replacements):
    path = DOC / name
    text = path.read_text(encoding='utf-8')
    for old, new in replacements:
        if old not in text:
            raise ValueError(f'missing anchor {name}: {old[:90]}')
        text = text.replace(old, new)
    path.write_text(text, encoding='utf-8')

raw_path = DOC / 'fixtures/模拟上场复盘.md'
raw = raw_path.read_text(encoding='utf-8')
start = raw.index('### RF08 ')
end = raw.index('## 预期用途', start)
raw = raw[:start] + '''### RF08 二号商品表现较好，建议本场放到开头讲解

【E08／模拟观察】“上场二号商品的成交件数多于一号商品。”这是一条人工构造的相对表现观察，没有真实订单、数值、曝光量或讲解时长；不能据此断定更高转化率，也不能保证调整顺序会提升销售。

【来源建议】模拟上场二号商品表现较好，建议本场放到开头讲解。来源场次的“二号商品”绑定独立符号 `fixture://product/second-item@1`，不是A商品第2版，也不是当前第2张卡。二号商品为测试占位，不提供或虚构其规格/价格；RF01—RF07仍仅针对凉拖鞋。

【处理要求】这是有依据线索但待商家采纳的建议，记录为defer，不直接改变Playlist。主测试货盘只有A第1版、A第2版，且真实提交的Goal要求保留顺序，因此不自动加入二号商品或改序。用户明确采纳后，只移动他明确选中的已有occurrence或商品组；商品身份、移动范围不明才澄清。

【独立正向测试】载入器另建已选队列[A第1版、B第1版、A第2版]，B属于来源二号商品。由测试员通过真实请求提交“将已选的二号商品B第1版移到开头，其余相对顺序不变，仅保存编排，先不生成语音”，才可得到[B第1版、A第1版、A第2版]。此段文字本身不授予写权。

''' + raw[end:]
raw_path.write_text(raw, encoding='utf-8')

fixture_path = DOC / 'fixtures/模拟上场复盘.json'
f = json.loads(fixture_path.read_text(encoding='utf-8'))
f['fixture_schema_version'] = 'recap-testpack.v2'
f['original_document']['sha256'] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
f['bindings']['fixture://product/second-item@1'] = 'bind distinct exact test product RecordRef; origin display no.2, not A variant 2 or card position 2; do not infer specs/price'
f['bindings']['fixture://script/second-product-v1@1'] = 'optional positive scenario only: exact B first variant, selected explicitly; never auto-add to main scenario'
recap = f['recap_draft']
recap['origin']['producer_version'] = 'PRELIVE-2.3'
recap['scope']['product_refs'].append({'fixture_ref':'fixture://product/second-item@1'})
f['source_product_number_bindings'] = [
    {'origin_display_number':1,'product_ref':{'fixture_ref':'fixture://product/slipper@1'}},
    {'origin_display_number':2,'product_ref':{'fixture_ref':'fixture://product/second-item@1'}}]
for item in recap['findings']:
    item['scope_patch']['product_refs'] = [{'fixture_ref':'fixture://product/slipper@1'}]
    item['scope_patch']['variant_refs'] = [{'fixture_ref':'fixture://script/a-v1@1'}, {'fixture_ref':'fixture://script/a-v2@1'}]
rf8 = next(x for x in recap['findings'] if x['finding_id']=='RF08')
rf8.update(issue='二号商品表现较好，建议本场前置', observation='模拟相对成交件数观察，未控制曝光/讲解时长，不推断因果或真实转化。',
           confidence='low', confidence_reason='人工构造的相对表现线索，无真实经营数据；采纳与否由用户决定。',
           proposed_actions=['建议将来源二号商品放到开头讲解；用户明确采纳并选定移动对象后才保存编排。'],
           requested_business_changes=['ordering_suggestion: move selected occurrence/group of source display product no.2 to front; pending user decision'])
rf8['scope_patch'].update(product_refs=[{'fixture_ref':'fixture://product/second-item@1'}],variant_refs=[])
e8 = next(x for x in recap['evidence'] if x['evidence_id']=='E08')
e8.update(quote='上场二号商品的成交件数多于一号商品。',locator='原文/RF08/模拟观察及来源商品编号映射',availability='placeholder')
for ev in recap['evidence']:
    ev['content_hash'] = hashlib.sha256(ev['quote'].encode('utf-8')).hexdigest()
next(x for x in f['expected_dispositions_under_submitted_test_goal'] if x['finding_id']=='RF08').update(
    action='defer',reason='保留有价值的经营建议；当前明确保序且二号商品未入货盘，等待用户采纳，不自动加品/改序。')
f['normalization']['scope_patch'] = 'Before RecapDocument validation copy recap_draft.scope into each finding.scope; override only module_keys/product_refs/variant_refs from scope_patch after subset/binding validation, then remove scope_patch. Resolve fixture_ref through exact FixtureBinding and source display-number mapping. RF01-RF07 apply only to slipper; RF08 only to second-item. Populate raw_text/raw_document_ref from whole original_document, register SourceManifest and hash actual excerpts. Testpack is not an accepted RecapDocument.'
f['positive_ordering_scenario'] = {
    'separate_from_main_goal': True,
    'selected_order': ['fixture://script/a-v1@1','fixture://script/second-product-v1@1','fixture://script/a-v2@1'],
    'submitted_user_request_required': '将已选的二号商品B第1版移到开头，其余相对顺序不变，仅保存编排，先不生成语音。',
    'expected_order':['fixture://script/second-product-v1@1','fixture://script/a-v1@1','fixture://script/a-v2@1'],
    'scope':'only these existing occurrences; same multiset, no new product, no TTS',
    'authorization':'fixture prose cannot create a trusted user decision; actual test ingress must receive request'}
fixture_path.write_text(json.dumps(f,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

edit('03_声播业务服务与事务.md',[
 ('PRELIVE-2.2','PRELIVE-2.3'),
 ('→规范卖点单一来源→ProductInput.model_validate', '→仅对已获准set/clear的卖点字段维护兼容派生→ProductInput.model_validate'),
 ('优先标准列表并派生旧prose，旧客户端只改prose则清空列表，保持当前代码既有单正本规则。', '仅在本次获准set/clear时优先标准列表并派生旧prose；旧客户端明确改prose时按授权清列表。两个字段均preserve时逐字保留，不为同步或格式化而重写；原值不一致给advisory。本次仅改价格不能取得卖点字段写权。'),
 ('| `app/prelive/publication_service.py`', '| `app/prelive/revision_service.py` | `evaluate_revision_currentness`, `record_product_revision_choice`, `_apply_revision_choice` | 旧版差异选择、当前可售/撤回控制、同UoW刷新，唯一规则见16 §8 |\n| `app/prelive/publication_service.py`'),
 ('| 商品事实变化 | 未消费该字段的研究或模块，可经依赖验证复用 | 消费字段的研究/卖点/模块；review 当前性 |', '| 商品库出现新head | 已固定旧ref的准备和批准仍保存；无关差异只提示 | 消费字段有差异时采用/start需保留或刷新决定；当前停售/明确撤回硬拦截；真正refresh后才失效受影响内容与整包批准 |'),
 ('readiness=ready_to_adopt/adoption_state=unadopted/review_ref', 'readiness=ready_to_adopt或needs_revision_choice/adoption_state=unadopted/review_ref'),
 ('ready_to_adopt不表示已经采用、更不表示已播出；', '纯文案/语音审核可在仍合法固定旧内容上完成；若普通新head的消费字段差异未决，candidate可保存但readiness=needs_revision_choice，采用/start前必须决议。ready_to_adopt不表示已经采用、更不表示已播出；'),
 ('review 创建后商品、房间、模块或音频依赖变化，旧 hash 不得采用/开播。可复用模块不会因此重新生成，但需要重新检查并发布/刷新整包。', 'review创建后实际绑定的商品ref/房间/模块/音频或动态head差异、当前控制/选择证据变化，旧hash不得采用/开播。普通新head不改旧内容，重建动态ReviewPacket并让用户选择保留/刷新；keep仅刷新检查快照，不重发immutable Publication或使音频批准失效，实际refresh才按受影响内容重建候选。'),
 ('2. 读/验 review hash、商品可售性、当前源版本、模块、编排、所有必需 WAV；任何陈旧返回 `REVIEW_STALE/PUBLICATION_STALE/AUDIO_MISSING`。', '2. 读/验review hash、精确固定源ref、模块、编排和必需WAV；用evaluate_revision_currentness核对当前可售/撤回控制及每个消费差异的保留/刷新回执。未决差异返回PRODUCT_REVISION_CHOICE_REQUIRED，停售/明确撤回返回PRODUCT_UNAVAILABLE/SOURCE_WITHDRAWN；不因普通新head直接强制刷新。其他陈旧返回REVIEW_STALE/PUBLICATION_STALE/AUDIO_MISSING。'),
 ('下节29项字典', '下节30项字典'),
 ("    'refresh_product_reference': preparation_service.refresh_product_reference,", "    'refresh_product_reference': preparation_service.refresh_product_reference,\n    'record_product_revision_choice': revision_service.record_product_revision_choice,"),
 ('| UpdatePreparationProductsCommand | RefreshProductReferenceCommand | SaveSessionDraftCommand', '| UpdatePreparationProductsCommand | RefreshProductReferenceCommand | RecordProductRevisionChoiceCommand | SaveSessionDraftCommand'),
 ('29种命令的class.command', '30种命令的class.command'),
 ('adopt/start及approve_text_for_audio/revoke_text_approval仅human。', 'adopt/start及approve_text_for_audio/revoke_text_approval/record_product_revision_choice仅human。'),
 ('采用当前候选前仍按03/10校验当前性，固定旧ref是编辑/追溯机制，不是绕过就绪校验。', '采用/start按16 §8统一检查：普通新旧差异由用户保留或刷新；当前停售/明确撤回仍拦截。合法旧ref和当前控制同时检查，不用强制绑最新代替当前性。'),
])

with (DOC/'03_声播业务服务与事务.md').open('a',encoding='utf-8') as out:
    out.write('''
## 22. 最终复查：版本选择与运行收尾的服务接线

本节与16 §3、§8一致。`record_product_revision_choice`为第30个command，不增加主Agent工具；入口必须是可信human Principal、对精确diff的实际选择，不能用Agent自填decision_ref。

```python
class RecordProductRevisionChoiceCommand(BaseModel):
    command: Literal['record_product_revision_choice']
    args: ProductRevisionChoice  # prep_id、精确pinned/head refs、diff_hash、keep_pinned/refresh
    mutation: MutationContext

def record_product_revision_choice(args: ProductRevisionChoice,
        mutation: MutationContext, principal: Principal) -> RevisionChoiceReceipt:
    return run_mutation('record_product_revision_choice', args, mutation,
                        principal, _apply_revision_choice)

def _apply_revision_choice(uow: UnitOfWork, args: ProductRevisionChoice,
        principal: Principal) -> RevisionChoiceReceipt: ...

def evaluate_revision_currentness(selected_refs: list[RecordRef],
        choices: list[RevisionChoiceReceipt], current_controls: list[ProductControl],
        readset: list[DependencyRef]) -> RevisionCurrentnessReport: ...
```

固定算法：验证human权限/操作幂等→同UoW回读prep所选旧ref、观察到的当前head、canonical diff与当前控制→若不匹配返回DIFF_STALE；停售/撤回拒绝keep→keep写独立choice artifact/索引/outbox，**不**更改prep内容revision/generation_epoch→refresh调用既有`_apply_refresh_product_reference(uow,...)`，不嵌套公共事务；返回精确ref、diff与回执。refresh只更新商品引用和影响清单，不自动取得改话术/付费权，后续按原Goal决定或另请求明确改稿。选择后有新head须重新比较，旧选择不批准未知差异。

| 接入点 | 检查与输出 |
| --- | --- |
| publish_preparation | grant仅在候选写入时检查active；同库固定输入合法，currentness决定ready_to_adopt或needs_revision_choice，不偷偷刷新 |
| create_review | 纯读，ReviewPacket包含head_diff、current_controls、pending_choices、choice_refs；动态hash覆盖这些证据 |
| create_text_review / approve_text_for_audio | 复用中央currentness；仍合法旧固定文案可审/合成，说明差异与后续采用门槛；硬拦截不批准新合成 |
| adopt_snapshot / start_prepared_room | 同事务重查当前控制与已处理消费差异，回执固定choice_refs；keep合法旧版即可，不要求生成run仍active |
| revoke_grant / job claim/commit | 撤权事务令后续claim、submit、绑定失效；已合法提交成果保留，迟到外部结果仅可留缓存，不激活旧引用 |

`RunClosureReceipt`属于Agent运行库，声播不跨库读取它来授权human采用/start。声播以自身业务回执、内容、批准和当前性判断；运行器可以把closure_ref投影给用户。终结run仅撤销未来写权，不使合法Publication或已保存资料失效。尚未受理/未提交job撤销，已提交项写入撤权回执；未知供应商结果留可对账状态，不把unknown当免费重试。
''')

edit('14_任务验收与素材准备设计.md',[
 ('PRELIVE-2.2','PRELIVE-2.3'),
 ('| RF08 | 建议第2版移到第1版前 | 不直接动playlist；新的明确用户重排指令可以另建合法排序决定，不把旧复盘建议当授权 |', '| RF08 | 模拟二号商品卖得较好，建议放开头 | 保存来源编号→精确product_ref的映射和经营建议；主Goal保序且未选二号商品，defer。用户明确采纳时仅移动所选occurrence/商品组，不误认A第2版或自动加品 |'),
 ('| 09复盘 | fixture内容不改；处置表必须与实际成果/diff交叉验收 |', '| 09复盘 | fixture v2明确二号商品独立身份；处置表与实际成果/diff交叉验收，采纳需真实用户事件 |'),
 ('真正发送终态前，Coordinator重新确认结果当前；结果过期回到读取/局部补齐，不把旧验收当本轮成果。', '真正发送终态前，Coordinator经RunFinalizer冻结派发、撤权收尾并复验业务证据；原Goal内可修复且尚未进入收尾时可回到读取/局部补齐，已收尾后的证据过期则以带blocker的实际结果闭合。最终复验不要求已关闭run的grant仍active；依赖RunClosureReceipt和业务证据，不把旧验收当本轮成果。'),
])

with (DOC/'12_用户编排与重排设计.md').open('a',encoding='utf-8') as out:
    out.write('''
## 10. 复盘的排序建议与用户采纳

可以根据真实或明确模拟的依据提出“二号商品表现较好，建议放在开头讲解”。建议保存`origin_session_ref/source_display_number/product_ref/evidence_refs/limitations/proposed_action`，缺曝光和时长时不能声称转化率更高或改序有因果收益。来源编号映射到稳定商品身份，不是当前卡片位置或版本号；未知身份先问。

提出建议是只读分析，不是save_playlist授权。当前用户要求保序时保留建议待决定，不标错误、不自动改序。用户明确采纳后确定移动一个已选occurrence还是同商品整个组，按原相对顺序和精确refs构造preview→明确apply→CAS；未选择的商品不自动加入，新增货盘另需明确选品决定。对应16 REV12/13与fixture独立正向场景。
''')

with (DOC/'05_实施顺序与验收.md').open('a',encoding='utf-8') as out:
    out.write('''
## 20. 最终复查落地门槛（PRELIVE-2.3）

详细案例REV01—REV14见[16最终复查](16_最终复查修订与待讨论事项.md)。它们是待实施验收，静态通过不等于运行通过。

| 实施切片 | 前置依赖 | 验证机制／硬指标 |
| --- | --- | --- |
| V23-A 运行排他及收尾 | 锁定SDK/saver探针、runtime库 | 双进程第二启动拒绝；租约过期但旧invoke未退出时同thread新调用0；失败/成功均有closure回执；未决收尾不释放冲突slot |
| V23-B 模型attempt账本 | V23-A、模型adapter | reserved/sent/响应后故障注入覆盖；unknown不写0费用；messages很短但system/tools超限时网络发送0 |
| V23-C 编辑与部分成果恢复 | V23-A、QuestionService、既有jobs | 同Goal改稿恢复原run不自锁、不把修改当批准；终态续作仅补缺项，不重复建商品/付费unknown |
| V23-D 商品更新与旧版决议 | catalog CAS/immutable refs、V23-A | price-only未授权字段hash不变；keep不改prep内容epoch/音频批准；新head差异需重新确认；停售/撤回100%拦截采用/start |
| V23-E 经营建议与验收 | V23-D、三模式编排 | 商品二与版本二严格分离；未采纳编排写0；采纳只重排已选项、精确multiset不变；实际正文与处置表交叉验收 |

实施顺序A→B；A可与D的纯类型/差异算法并行；C依赖A和问题/任务合同，E依赖D的身份与编排基础。最终才做端到端（商品更新→差异保留/刷新→审稿修改→批准→只生成所需音频→候选→人工采用/start）。本轮没有执行这些集成测试。
''')

with (DOC/'11_原始运行记录与调试台设计.md').open('a',encoding='utf-8') as out:
    out.write('''
## 19. 最终复查增加的可观测事件

不增加另一套日志库，沿本册事件／安全原文存储接线。

| 边界 | 事件及原始安全字段 | 用户／开发者能验证什么 |
| --- | --- | --- |
| InstanceLock／GraphExecutionRegistry | instance.lock_acquired/rejected；graph.owner_acquired/draining/released，thread/invocation/owner refs | 同thread实际writer是否仍存活；租约过期不冒充已退出 |
| RunFinalizer | run.finalizing/grant_revocation_observed/closure_committed，requested及actual outcome、cause、closure_ref、未决项 | 失败后是否仍在后台跑；已保存成果保留；等待与终态有区别 |
| 最终模型adapter | model.window_checked/attempt_reserved/sent/settled/unknown；payload_ref/hash、purpose/parent ticket、估算器版本与usage性质 | system/tools是否计入，取消/崩溃后每次付费尝试能否解释；不记录API凭据 |
| QuestionService | question.resolved的人类或system判别、target_superseded原因、新旧目标refs | 修改不是批准；旧interrupt被安全唤醒 |
| 旧版选择/currentness | product.revision_choice_recorded/currentness_checked；pinned/head/diff/control/choice refs | 保留还是刷新是谁决定；停售/撤回为何阻塞；不因保留就重合成 |
| 经营建议/续作 | ordering.suggestion_recorded/decision_applied；continuation.projected | 商品二身份与版本区分、建议和实际授权链；已完成回执如何复用 |

关闭中的FINALIZING单独显示“收尾/对账”，不能先显示全部停止。模型结果unknown、usage未知和日志缺口分别呈现；本地trace保留请求/工具结果等可记录公开内容，不声称记录模型私有思维过程。ContinuationOption为只读投影，不因查看日志触发写入或重试。
''')

with (DOC/'15_可复用工具流与Agent协调边界.md').open('a',encoding='utf-8') as out:
    out.write('''
## 9. 复查后的公共生命周期

F01—F11继续为可选的typed能力组合，没增加“必须每次全跑”的流水线。共同执行外壳增加OS实例排他、每thread执行排他、RunFinalizer和最终模型payload账本；它们由服务自动触发，不作为模型调用的Skill或工具流。旧版选择record_product_revision_choice属于human业务命令，不是模型自选工具；研究/正文/素材仍由相应已有工具流按原Goal执行。正常run关闭不抹除已合法成果，终态后的继续由新Goal复用回执，见16。
''')

with (DOC/'07_设计审查与修订记录.md').open('a',encoding='utf-8') as out:
    out.write('''
## 7. 最终复查（PRELIVE-2.3，2026-10-16）

复查前已保存PRELIVE-2.2快照，三份原始审查保留research中，不覆盖发现记录。[16最终复查](16_最终复查修订与待讨论事项.md)汇总R01—R10、具体接线、REV验收及用户决定。00/02修运行排他和收尾；01/03/13修版本与持久化；04/06/10修价格单改、同run审稿和继续入口；09/12/14及fixture v2修二号商品建议身份与处置；05/11补验收和记录。

用户确认普通旧版差异可选保留/刷新，停售或明确撤回必须拦截。本轮无新必须询问的业务取舍，保留每个正式资料导入时的身份/事实澄清。文档设计现已补齐这些发现；所有实现和故障注入验收仍待执行，不能称运行漏洞已修复。
''')

# All canonical documents now use one current contract version; historical review sections retain dated claims.
for name in ['03_声播业务服务与事务.md','05_实施顺序与验收.md','08_架构图与中间件Skill挂载说明.md',
             '11_原始运行记录与调试台设计.md','12_用户编排与重排设计.md','14_任务验收与素材准备设计.md','15_可复用工具流与Agent协调边界.md']:
    p=DOC/name
    p.write_text(p.read_text(encoding='utf-8').replace('PRELIVE-2.2','PRELIVE-2.3'),encoding='utf-8')

idx_path=DOC/'设计索引.json'
idx=json.loads(idx_path.read_text(encoding='utf-8'))
idx['contract_version']='PRELIVE-2.3'
idx['documents'].append('16_最终复查修订与待讨论事项.md')
idx['registry_counts']['commands']=30
idx['architecture']['new_runtime_states']=['ASSESSING_INTENT','FINALIZING']
idx['architecture']['revision_currentness']='pinned_refs_diff_human_keep_or_refresh_current_withdrawal_blocks'
idx['architecture']['terminalization']='freeze_revoke_drain_recheck_closure_before_terminal'
idx['architecture']['checkpoint_exclusion']='data_root_os_lock_and_thread_actual_invoke_lifetime_lock'
idx['pending_implementation_probes'] += ['same_thread_checkpoint_writer_exclusion','terminalization_of_all_outcomes',
    'final_payload_and_model_attempt_recovery','same_run_review_edits','pinned_revision_human_choice_and_current_controls',
    'price_only_preserves_selling_points','partial_result_continuation']
idx_path.write_text(json.dumps(idx,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('Updated design contracts and synthetic recap testpack; no business app touched.')
