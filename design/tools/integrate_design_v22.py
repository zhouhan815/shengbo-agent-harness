"""Integrate PRELIVE-2.2 design increments; never imports the business application."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
D = ROOT/'engineering-v2'

def edit(file, pairs=(), append=''):
    p=D/file
    t=p.read_text(encoding='utf-8')
    for a,b in pairs:
        if a not in t: raise ValueError(f'missing {file}: {a[:85]}')
        t=t.replace(a,b)
    # Contract upgrade applies to current canonical files; fixtures retain their creation provenance.
    t=t.replace('PRELIVE-2.1', 'PRELIVE-2.2')
    if append: t=t.rstrip()+'\n\n'+append.strip()+'\n'
    p.write_text(t,encoding='utf-8')

edit('00_后端工程设计.md',[
 ('| [11 原始运行记录](11_原始运行记录与调试台设计.md) | 主从树、时间线、原始输入输出、SDK与业务埋点、调试接口 |',
  '| [11 原始运行记录](11_原始运行记录与调试台设计.md) | 主从树、时间线、原始输入输出、SDK与业务埋点、调试接口 |\n'
  '| [12 用户编排](12_用户编排与重排设计.md) | 截图式卡片拖拽、同商品连续／穿插／随机、精确预览和应用 |\n'
  '| [13 任务与上下文](13_任务识别与历史上下文设计.md) | 同主模型识别任务、历史研究复用、增量维护、按角色投影、摘要与定向交接 |\n'
  '| [14 验收与素材](14_任务验收与素材准备设计.md) | 按本次Goal验收真实成果、首期素材清单与文件引用 |\n'
  '| [15 工具流与协调](15_可复用工具流与Agent协调边界.md) | F01—F11可复用流程、模型判断／确定性服务／协调程序的边界 |'),
 ('RECAP[', 'RECAP[') if False else ('本文及分册中的 Python 是拟实现的签名和算法，不是已经运行的应用代码。',
  '本文及分册中的 Python 是拟实现的签名和算法，不是已经运行的应用代码。当前合同PRELIVE-2.2，追加修订日期2026-10-16。'),
 ('recap_service.py / approval_service.py  # 复盘业务权威、文案→语音批准',
  'recap_service.py / approval_service.py  # 复盘业务权威、文案→语音批准\n    audience_research_service.py / asset_service.py  # 历史研究绑定、素材清单'),
 ('context/selection.py       # 冻结/验证选区、仅本链 replacement 续接',
  'context/selection.py       # 冻结/验证选区、仅本链 replacement 续接\n    planning/assessment.py    # 同主模型提议的TaskAssessment及范围验证\n    acceptance/compiler.py / acceptance/service.py  # 按用户Goal编译和回读验收'),
], '''
## 12. 本次任务、历史研究与工具流增量

同一主Agent根据用户目标选择能力；S2/S3/S4不是每轮全跑。`save_task_assessment`记录可观察计划，确定性服务比较原Goal与允许effects；计划不是授权。普通“更新商品资料”按用户2026-10-16已确认策略执行：商品＋相关研究维护＋受影响话术清单，不自动改稿。历史研究存声播业务库，先查回、比对，再复用或局部重分析。

本期素材是需求清单与已有文件引用，不生成视频、不接实时hook。按目标需要清单时必须有真实持久产物；缺可选视觉素材不阻塞纯声音。`finish_turn`内部依据GoalAcceptanceSpec/Result验收，不能只凭模型说有稿或数据库有一个空版本行。

可复用工具流F01—F11在15列出：解析、历史影响、商品/配置保存、上下文/文案计划、模块生成、用户重排编译、人工批准后TTS、复盘/素材及目标验收。主Agent选择能力；Supervisor等程序负责执行、并发、等待和恢复。不新增一个强制总工作流或排序Agent。
''')

edit('01_类型与持久化.md',[
 ('selected_product_refs、draft_ref、analysis_refs、recap_refs、bundle_ref', 'selected_product_refs、draft_ref、analysis_refs、recap_refs、asset_checklist_ref、bundle_ref'),
 ('每条依赖；研究假设不成为商品规格事实', '每条依赖、prior_research_refs、insight_deltas、asset_suggestions；研究假设不成为商品规格事实'),
 ('原资料/证据索引、规则/模板版本、readset', '原资料/证据索引、历史选择/研究有效性依据、相关素材清单refs、规则/模板版本、readset'),
 ('revision、用户选择的有序items、loop_mode；item_id独立，重复script不去重',
  'revision、用户选择的有序items、loop_mode、product_group_order、reorder_provenance；occurrence_id独立，重复script不去重'),
 ("objective: Literal['catalog_update', 'prepare_session', 'script_change',\n                       'audio_prepare', 'read_explain']",
  "objective: Literal['catalog_update', 'facts_analysis', 'audience_research',\n                       'session_config', 'playlist_reorder', 'prepare_session',\n                       'script_change', 'audio_prepare', 'read_explain']"),
 ('| `agent_events/outbox` |', '| `task_assessments/goal_acceptance_artifacts` | Agent不可变运行产物；计划不自授权限，验收Spec来自原Goal，Result引用声播实际证据 |\n| `agent_events/outbox` |'),
 ('| `business_blobs` |', '| `audience_research_bindings` | 声播业务索引：product_id/context/accepted research ref、effective版本、消费paths/相容证明；无第二份研究正文 |\n'
  '| `asset_file_links` | 商品／清单／受控blob引用索引；文件available必须实际可回读，建议不冒充文件 |\n'
  '| `business_blobs` |'),
], '''
## 12. PRELIVE-2.2类型与权威补充

| 类型／规范位置 | 保存与关键边界 |
| --- | --- |
| TaskKind／TaskAssessment（13） | 主模型提议、本地Agent运行产物；计划不得改变冻结Goal或grant；Spec关联放runs，不形成不可变artifact互相回填循环 |
| AudienceResearchBinding／ResearchResolution（13） | 声播accepted报告＋版本适用索引／证明；按精确product_id/context和消费paths，不按名称或仅最新报告判断 |
| InsightHandoff（13） | 已accepted结果的定向交接，主创建新manifest/refine任务；不共享worker messages或未接受partial |
| PlaylistReorderSpec／Preview（12） | 纯读预览，保存时重算和CAS；只排列已选occurrence，不增加写命令 |
| GoalAcceptanceSpec／Result（14） | Agent不可变运行记录，业务证据仍来自声播；finish/Guard结束前重验，不以模型passed为准 |
| AssetRequirement／AssetFileRef／AssetChecklist（14） | 声播业务artifact/受控blob；商品级可复用，本场精确引用；建议与实际已有文件分开 |

命名兼容：持久编排模型规范名为PlaylistDraft，03/12服务签名中的Playlist是同类型别名，不是第二份存储；编译模型规范名为CompiledPlaybackPlan，早期PlaybackPlan是同类型别名。`PlaybackOccurrence`规范字段为occurrence_id/product_id/script_revision_ref；旧UI的编排item_id仅在适配层一对一映射，输出统一occurrence_id。后台job的item_id是独立概念，不得与播放项混用。

GoalContract九种objective与13的细类一一映射；script_change区分patch/new_variant由目标动作记录，不暗中增加权限。并非每个Goal都需完整本场；例如facts_analysis可只交ACCEPTED报告，catalog_update需商品、有效研究维护和影响清单但不改稿。研究binding和素材属于长期业务持久化，不写入Skill正文或仅依赖StateBackend文件。
''')

edit('02_运行时与中间件.md',[
 ('    pending = await agent_repo.pending_jobs(scope.run_id)\n    decision = evaluate_turn_outcome(outcome, observed, pending)',
  '    spec = await turns.acceptance_spec_from_original_goal(scope)\n    acceptance = await turns.evaluate_goal_acceptance(scope, spec.ref, observed)\n    pending = await agent_repo.pending_jobs(scope.run_id)\n    decision = evaluate_turn_outcome(outcome, observed, pending, acceptance)'),
 ('        business_evidence_refs=observed.receipts,', '        business_evidence_refs=observed.receipts,\n        acceptance_result_ref=acceptance.ref,'),
], '''
## 15. 任务识别、上下文压缩与当前目标验收

S1新增13的TaskAssessment规则，仍由同一主模型选择能力；`save_task_assessment`为本地固定主工具。M3每次模型请求提供原Goal、当前Assessment/Spec refs、禁做项、关键条件/版本、缺口、历史研究索引；旧稿、原资料、完整历史研究等按需读。全文仍存权威业务refs，不能以摘要覆盖。

长上下文沿SDK已有Summarization/Filesystem offload，实际DeepSeek模型窗口、保留输出/工具余量和触发阈值需探针冻结。长工具正文先安全持久保存后压缩传输；摘要后M3重注当前约束/批准/价格，不信摘要中的旧状态；关键原文超限时按商品/模块拆分。M9+callbacks保存摘要前后实际输入/输出及原文refs。详细Always/Progressive/Excluded矩阵在13。

M4在派发和业务写前要求有效TaskAssessment，其proposed effects须是原Goal的子集；read_source等初始读取不要求提前完成判断，`save_task_assessment`本身也不授业务权。主可修订计划但不能改原Goal。角色之间不互传messages；仅必要时accepted结果→主创建InsightHandoff→新版InputManifest→新子任务，默认每角色一次有依据refine，受总预算约束。

CompletionService依据原Goal编译14的Spec，读声播业务证据和本地任务状态形成Result；finish_turn原模型参数不增加passed/approved/完成标准等可自报字段。M7结束前再次校验Result/readset。没要求生成话术的任务不因没有稿件失败；要求生成的任务必须回读真实正文/模块和job终态，不接受标题/占位/accepted回执假完成。素材清单在目标需要时属于真实产物，但缺可选视觉不阻断纯声音。
''')

edit('03_声播业务服务与事务.md',[
 ("    'import_recap': recap_service.import_recap,", "    'import_recap': recap_service.import_recap,\n    'save_asset_checklist': asset_service.save_asset_checklist,"),
 ('| ApproveTextForAudioCommand | RevokeTextApprovalCommand | CreateRoomCommand', '| ApproveTextForAudioCommand | RevokeTextApprovalCommand | SaveAssetChecklistCommand | CreateRoomCommand'),
 ('下节28项字典', '下节29项字典'),
 ('28种命令的class.command', '29种命令的class.command'),
 ('| read_recap |', '| preview_playlist_reorder | 精确playlist/base revision、三模式spec/seed | 纯读重排预览/hash/诊断；取消/no-op不使批准失效 |\n'
  '| find_audience_research | product_ref/context/cursor | 候选accepted研究、相容性/差异/全文refs；不产生未落盘artifact_ref |\n'
  '| read_session_history | room/prep/明确历史基准/cursor | 来源真实的配置历史与diff，区分草案/采用/真实直播 |\n'
  '| inspect_change_impact | target和限定候选patch或committed change | 纯读精确依赖影响清单，potential/confirmed分开，不改稿 |\n'
  '| read_asset_checklist | exact checklist或显式product/prep版本 | 完整清单、已有文件状态/来源及缺项；不生成素材 |\n'
  '| read_recap |'),
 ('本表22项', '本表27项'),
], '''
## 22. PRELIVE-2.2历史研究、用户重排和素材服务

| 能力／接线 | 实际执行责任与输入 | 产出／副作用边界 |
| --- | --- | --- |
| AudienceResearchService.resolve_for_product | 已接受报告、精确商品revision/context、消费paths及来源hash | 只读适用性预览；确定性持久Resolution附于既有save_product/accept_analysis_report事务，不在query里偷偷写 |
| save_product → bind_in_uow | 同UoW保存商品/FieldDisposition、核验研究来源与逐path相容性；新品临时身份凭真实身份回执绑定正式ID | 原报告readset不改写，新建绑定/证明；真有事实差异则需定向重分析，不无限循环自身revision |
| accept_analysis_report → research版本绑定 | 角色typed报告、old/new insight及source/readset；目标身份已确定时登记索引 | 完整新研究版本，保留旧版/证据；跨场检索按ID/context，未接受partial不入索引 |
| preview_playlist_reorder | playlist精确base、grouped/interleave/full_shuffle、组序/seed；事务外普通算法 | 完整候选和可复现hash/诊断；零写入、无TTS |
| save_playlist增量 | 拖拽精确occurrence顺序，或可空reorder_proposal＋可信用户决定；同run_mutation验证CAS/精确refs/完整排列 | 仅实际顺序/绑定变化才新修订并失效compile/approval/candidate；预览/取消/no-op不失效；按12实现 |
| save_asset_checklist | 14的typed提议、expected ref/revision、scope与mutation；校验文件/用途/来源，CAS保存artifact/索引 | 真实Checklist回执；不上传新文件、不生成视频；本场仅引用商品清单，不复制商品正本 |
| read_completion_observation扩展 | 原Goal及可信Spec的受限typed证据请求 | 回读真实商品/研究维护/影响清单/话术正文/素材/批准和任务证据，禁止任意查询代码 |

新增 `save_asset_checklist(args,mutation,principal)->AssetChecklistReceipt`、固定query注册接线至 `asset_service.read_asset_checklist`；其余新增queries分别至playback_compiler和audience_research_service/history_service/dependency_service，不按字符串反射找函数。候选变更检查不等于持久产物，只有附带真实已有ref的结果可以进下游bundle；需要保存的相容性/影响检查证据由既有变更事务或本地运行观察固定归档。

schema与算法以12—14唯一正文为准。研究索引只存适用性和refs，正文仍为声播business_artifacts；Agent不直接读写声播DB。商品库更新不自动刷新已固定旧版本的Preparation，不自动改稿。采用当前候选前仍按03/10校验当前性，固定旧ref是编辑/追溯机制，不是绕过就绪校验。
''')

edit('04_业务执行与上下文.md',[
 ('接收材料与最低身份', '接收本次目标、材料与必要身份'),
 ('| 1 输入 | 接受材料；', '| 1 输入 | 先识别本次任务并检查可复用历史、保存TaskAssessment；接受材料；'),
 ('未知身份时先补问，不能瞎猜覆盖', '任务需要保存商品且身份不明时先补问；只读说明不强迫新建商品'),
 ('| 3 顾客/场景 | 区分购买者与使用者；', '| 3 顾客/场景 | 先查当前商品历史研究及差异，必要才增量/重分析；区分购买者与使用者；'),
 ('| 6 用户编排 | 接受明确商品/版本/顺序，重复项给独立ID；',
  '| 6 用户编排 | 接受明确商品/版本/顺序或用户重排方式；按截图卡片拖拽，或三模式preview后apply；重复项给独立ID；'),
 ('用户确实选过相同对象/顺序；不能从模型建议推为决定', '用户确实选过相同对象/顺序，或明确授权当前所选集合的排序方法并应用；不能从模型建议推为决定'),
 ('| `read_text_review(compiled_plan_ref,requirements_ref)',
  '| `find_audience_research(product_ref,context,cursor=None)` | client.query(find_audience_research) | 历史研究候选/版本与相容性预览；全文按精确ref读取 |\n'
  '| `read_session_history(room_id,preparation_id,baseline_refs=None,cursor=None)` | client.query(read_session_history) | 有权场次的配置历史/diff及来源种类 |\n'
  '| `inspect_change_impact(target_ref,proposed_or_committed_change,context)` | client.query(inspect_change_impact) | 精确potential/confirmed依赖变化，零写入 |\n'
  '| `preview_playlist_reorder(playlist_ref,expected_revision,spec)` | client.query(preview_playlist_reorder) | 三种确定算法纯读预览，不改批准/编排 |\n'
  '| `read_asset_checklist(target,version=None,cursor=None)` | client.query(read_asset_checklist) | 素材需求/已有文件/缺项，不把建议当已制作 |\n'
  '| `read_text_review(compiled_plan_ref,requirements_ref)'),
 ('| `import_recap(raw_blob_ref,document:RecapDocument)`',
  '| `save_task_assessment(proposal:TaskAssessmentProposal)` | Agent本地TaskAssessmentService | 同主模型计划；确定性校验不扩大原Goal；不新增voice command |\n'
  '| `save_asset_checklist(proposal,expected_ref=None)` | client.command(save_asset_checklist) | 受控文件/需求/来源与scope检查、CAS保存正式清单 |\n'
  '| `import_recap(raw_blob_ref,document:RecapDocument)`'),
 ('用户版本/顺序，提出一张选择卡', '用户版本/顺序或明确排序方法，提出一张选择卡'),
], '''
## 12. 本轮新增的选择规则与真实产物

`TaskAssessment`与九类Goal的对应、角色输入矩阵、研究历史绑定、定向handoff与摘要触发以13为准。只调用本次目标需要的环节：事实报告可以在2A完成；本场配置可只跑2B；catalog_update默认商品＋研究维护＋话术影响清单，不自动执行4B—8。明确改稿或完整准备才在其scope内继续。

历史研究先 `find_audience_research/read_context`，消费paths/context未变则复用并保存或回读有效绑定证据；真有变化只更新相关insight，完整研究的新版本仍保留旧证据。商品临时身份和research binding的唯一写入责任在03/13。

素材由研究报告 `asset_suggestions`提出，主接受后 `read_asset_checklist → 汇总提议 → save_asset_checklist`，4B引用本场涉及的精确清单。需要清单的目标必须保存可定位成果；只改一个金额或纯重排无需重复素材分析。缺可选展示图片/视频是advisory，首期不生成视频或自动插画面。

步骤6遵循12卡片列表：用户可拖动、同商品连续、尽量穿插（组内可随机）或完全随机；方法和应用意图明确时可以承接自然语言指令，不能擅自增加商品/版本。纯预览和取消不失效；实际应用改变编排后重新编译并使旧整包批准过期，已有音频缓存可复用。

完成前走14原Goal验收。要求“生成话术”就回读实际非空正文/目标模块；要求“更新资料”就验收商品、研究维护和影响清单，没改稿反而符合范围。复盘处置必须与实际成果交叉检查，不能只在表里写“已修复”。F01—F11工具流与主/子协调位置见15。
''')

edit('05_实施顺序与验收.md', append='''
## 13. PRELIVE-2.2任务选择、历史研究、编排和素材验收

新增12的P01—P18、13的CTX-01—18、14的ACC01—16。代表出口：明确局部任务不强迫走完全链；研究可跨场精确查回并按依赖增量维护；用户更新商品不改稿；拖拽和三模式重排不丢项/偷换稿；未应用预览不使批准失效；实际稿件/清单必须存在，不能空占位假完成；压缩后Goal/价格/版本/批准状态不丢。

先做typed schema/服务函数与临时库集成，再接同主模型任务判断，再接三角色并行与定向refine，最后供应商/前端联调。F01—F11的服务成功率和主Agent能力选择质量分开报告；低层服务测试通过不等于模型选对工具。

本次独立设计探针 `tools/probe_playlist_design.py` 对2,681种有限计数分布的排列完整性/组内次序/线性最少相邻数/复现检查通过，结果在fixtures/编排算法设计探针结果.json。它没有导入业务应用，不是实际UI/API/数据库/TTS测试；实施须将同样性质验证落在正式重排函数上。

按当前冻结模型profile实施强制压缩案例：长工具结果、长历史研究、旧批准/旧价格混入摘要，最终请求仍应有当前Goal/限制/版本和完整原文入口。SDK默认阈值与实际DeepSeek context window需探针验证后配置，不能凭通用模型窗口给出固定比例保证。
''')

edit('06_前端对接合同.md',[
 ("view: Literal['product','config','analysis','script','playlist','audio','review','materials']",
  "view: Literal['product','config','analysis','script','playlist','audio','review','materials','asset_checklist','acceptance']"),
 ('| 选稿/重复项/排序 | save_playlist(user decision) | occurrence独立ID，顺序可CAS，缺文本可见 |',
  '| 选稿/重复项/排序 | 截图式卡片拖拽；preview_playlist_reorder纯读预览→用户应用→save_playlist | occurrence独立ID、精确版本、CAS；状态分层；纯预览不作废批准 |'),
], '''
## 10. 本轮卡片编排、任务上下文与素材成果

| 前端需要的投影 | 后端合同 | 交互要求 |
| --- | --- | --- |
| 截图式脚本卡片 | 12的PlaylistOrderItem/PlaylistCardStatus | 拖拽手柄、位置、商品/版本、移除；N播放项/M版本分开；已选高亮与正在播放不同 |
| 重排弹层 | preview_playlist_reorder→resolved_spec/seed/hash/完整候选 | 同商品连续、尽量穿插、完全随机；查看预览/再次随机/取消/应用；组内可保留或随机 |
| 保存/冲突 | 既有save_playlist CAS/receipt | 拖动保存中不可显示已保存；409保留本地意图，不覆盖远端；no-op不递增revision |
| 时长和Ready | 12状态分层及duration_kind | 音频未齐只显示估计/未知，不能文本估算精确秒数；文本/音频/审核/编排分别显示 |
| 本次任务范围 | TaskAssessment安全投影及原Goal | 可见实际目标、准备调用/复用/阻塞能力；不展示成每次必须完成全部阶段 |
| 历史研究/配置 | 13的检索/差异/ResearchResolution | 显示复用哪版、为何更新或保留、哪些稿件受影响；“建议修改”不是已改稿 |
| 素材准备与验收 | 14的AssetChecklist/GoalAcceptanceResult、ArtifactDestination | 已有文件、建议/缺项、用途；本次要求及真实成果逐项定位，语义评测与硬检查分开 |

这些是设计交接，不在本轮实现页面。素材清单与文件用既有materials/blob接口，只新增固定业务清单命令/查询；不为后期视频/hook预造按钮。用户已经确认普通更新只维护商品/研究并列话术影响清单，不自动触发话术生成。
''')

edit('07_设计审查与修订记录.md', append='''
## 6. PRELIVE-2.2本轮需求与审查

2026-10-16新增12—15：用户拖拽和三模式重排、同主模型任务识别、跨场研究/配置历史、上下文投影/摘要、必要定向handoff、按Goal验收、素材清单、F01—F11工具流目录。用户明确选定catalog_update默认商品＋研究维护＋话术影响清单，明确要求后才改稿。

当前各实现仍待开发。重排独立算法设计探针与文件静态检查只验证有限设计性质；不据此声明Agent会永远正确分类或真实直播转化提升。验收应分别观察模型能力选择错误、服务拒绝、真实越界提交、任务成果和原始调用记录。
''')

edit('08_架构图与中间件Skill挂载说明.md',[
 ('EDIT["商品库／本场配置／话术／编排"]', 'EDIT["商品库／本场配置／话术<br/>卡片拖拽／三模式重排／素材清单"]'),
 ('MAIN["主Agent · S1 播前统筹<br/>M1/M3/M4/M5/M6/M7/M9"]',
  'MAIN["主Agent · S1 播前统筹<br/>TaskAssessment／按Goal选择能力<br/>M1/M3/M4/M5/M6/M7/M9"]'),
 ('COMPILE["用户选稿顺序→编译<br/>第1/6/11项长开场→AudioRequirements"]',
  'COMPILE["用户选稿／拖拽或重排预览→应用→编译<br/>第1/6/11项长开场→AudioRequirements"]'),
 ('I[1 接收', 'I[1 接收') if False else ('O[用户选择顺序→编译所需开场]', 'O[用户选稿／拖拽或三模式重排→应用→编译]'),
], '''
## 12. 本轮可复用工具流与上下文架构

新增讲解图：[分层与工具流PNG](figures/03-toolflows-context.png)、[可编辑SVG](figures/03-toolflows-context.svg)。完整F01—F11目录及职责见15；任务判断、历史研究、按角色上下文和压缩规则见13，按目标验收与素材见14。

主Agent先识别目标、读历史并保存结构化计划，再按需调用工具流或S2/S3/S4。历史商品/研究/配置/复盘/素材存声播业务库；每次模型只读当前动作所需内容，原文精确ref始终可回取。摘要与offload不改写业务原件，也不复活旧批准。

用户重排方式属于可信排序决定，算法不调用LLM；纯预览零业务变化，实际保存才使对应编译/批准失效。横向协作采用accepted typed handoff和新任务，不共享worker历史。按原Goal验收，不把“只更新资料”错误要求成整场候选。
''')

for file in ['09_复盘输入与话术闭环设计.md','10_人工确认与语音门槛设计.md','11_原始运行记录与调试台设计.md']:
    edit(file)
edit('10_人工确认与语音门槛设计.md', append='''
## 12. 编排预览与真实变更的区别（本轮补充）

12的重排预览、取消、再次随机但未应用，以及保存相同精确顺序的no-op，均不改变被审核内容，不使批准失效。只有实际应用使playlist/文本/voice/消费事实等绑定输入变化，才按本册失效规则处理；后续缓存可复用，但新包仍须可信human批准。按Goal只生成文本的任务可以完成文案交付，不意味着获准TTS。
''')

# Resolve cross-review P2: precise occurrence refs and preview/no-op semantics.
edit('14_任务验收与素材准备设计.md',[
 ('item多重集合和用户决定一致', 'occurrence多重集合、每项product_id＋精确script_revision_ref及用户决定一致'),
 ('旧批准因重排失效', '已应用且绑定编排实际变化才使旧批准失效；预览／取消／no-op不失效'),
 ('occurrence ID集合及数量不丢不增；只重排', 'occurrence ID集合及数量不丢不增、每项product_id和精确script_revision_ref不偷换；只重排'),
 ('`PlaylistDraft`、确定性重排信息', '`PlaylistDraft`（服务别名Playlist）、确定性重排信息'),
 ('要求编译时的 `PlaybackPlan`', '要求编译时的 `CompiledPlaybackPlan`（早期别名PlaybackPlan）'),
])

index_path=D/'设计索引.json'
index=json.loads(index_path.read_text(encoding='utf-8'))
index['contract_version']='PRELIVE-2.2'
index['documents'] += ['12_用户编排与重排设计.md','13_任务识别与历史上下文设计.md','14_任务验收与素材准备设计.md','15_可复用工具流与Agent协调边界.md']
index['main_business_tools'] += ['preview_playlist_reorder','find_audience_research','read_session_history','inspect_change_impact','save_task_assessment','read_asset_checklist','save_asset_checklist']
index['registry_counts']={'commands':29,'queries':27,'main_business_tools':39}
index['architecture']['task_selection']='same_main_model_assessment_with_goal_bounded_validation'
index['architecture']['cross_branch_exchange']='accepted_report_then_main_typed_handoff_new_task'
index['architecture']['catalog_update_default']=['save_product','maintain_or_reuse_research','list_script_impacts','no_automatic_script_changes']
index['architecture']['goal_acceptance']='original_goal_compiler_and_actual_evidence_not_model_self_report'
index['architecture']['context_management']='role_projection_progressive_refs_sdk_summary_offload_critical_reinjection'
index['architecture']['playlist_reorder_modes']=['grouped','interleave','full_shuffle']
index['pending_implementation_probes'] += ['task_selection_and_scope','audience_history_incremental_binding','context_compression_constraint_preservation','playlist_drag_reorder_preview_apply','goal_evidence_and_asset_checklist']
index_path.write_text(json.dumps(index,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

baseline=ROOT/'10_播前Agent首期流程基线.md'
t=baseline.read_text(encoding='utf-8')
t+='''

## 2026-10-16补充：任务选择、历史复用、编排和素材

用户确认：只说“更新商品资料”时更新商品及相关顾客研究，列出受影响话术，明确要求后才改稿。主Agent同一模型判断任务细类，按Goal选择必要能力，不每次走全部阶段。研究与本场历史精确版本保存在声播业务库，按消费依赖复用/增量；完整原文保留，模型按角色按需读取，长历史由SDK摘要/offload后补当前关键约束。

步骤6按用户截图卡片支持拖拽、同商品连续、尽量穿插、完全随机。算法只排已选occurrence，随机seed可复现；纯预览不改变批准，真实应用变化后重新编译并审阅。素材首期是准备需求与已有文件引用，不生成视频或实时hook；按Goal逐项验收真实成果，未要求稿件时不强迫生稿。

完整合同见engineering-v2/12—15，当前PRELIVE-2.2；本轮仍是设计增量，未实施业务应用。
'''
baseline.write_text(t,encoding='utf-8')
print('PRELIVE-2.2 integrated; no application code or provider calls.')
