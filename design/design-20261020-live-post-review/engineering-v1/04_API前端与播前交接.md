# API、Agent工作台、人工确认与播前交接

版本LIVE-REVIEW-ENG-1.0，所有新增API均为待实现计划。现有Agent UI仍为产品主体：右侧对话用于提出任务和解释，左侧展示、校对数据与报告；点击交互区沿用原前端方案的聚焦放大逻辑。这里定义后端数据和事件，不另起一套以声播管理页为主体的UI。

## 1 服务挂载与安全边界

现有声播8765接收浏览器同源请求`/api/review/v1/*`，桥接到既有Agent8791的`/review/v1/*`。Agent app启动增加ReviewRuntime生命周期，独立review DB／checkpoint、作业和trace命名空间；不向浏览器发送服务令牌，不借18791授权测试页作推理base_url。

实际Agent挂载入口是`src/shengbo_prelive/api.py:create_app`及其lifespan（本机75／92行附近），由`src/shengbo_prelive/__main__.py`启动；W4通过集成者在该app挂Review router／资源，不能假定另有http_api.py。voice的server.py由W1接点owner统一合入bridge挂载，避免多窗口各改启动生命周期。

复用已有Origin／Host／同源会话校验思想，Review bridge仅代理固定路径／方法和已确认scope。principal由受信会话解析，模型／请求JSON中的actor_id或account_id不能授予身份。凭据仅在已有provider／受控服务令牌存储中，禁止放state、SSE、报错或Artifacts。

Review初始化失败显示feature_unavailable及可重试状态，不能阻止原声播和播前可用。重型报表读取／计算off-loop；浏览器SSE断开只断展示，不能关闭共享HTTP transport或中断后台run。应用关闭按各资源owner释放引用，不让Review关闭播前资源。

## 2 请求与响应合同

浏览器Mutation统一提交client_request_id、expected_generation／revision和必要preview_hash；服务生成稳定operation_id或按已登记ID复用。相同ID不同请求hash为409 OPERATION_PAYLOAD_CONFLICT。普通GET不需要operation账本；异步提交202只表示accepted，必须有job／run查询入口。

| 内部路由（外部加/api） | 输入 | 返回与语义 | 身份／副作用 |
| --- | --- | --- | --- |
| POST /review/v1/monitor-sessions | voice_room／session、external room、publication ref、绑定说明、client_request_id | SessionBinding＋generation；重复同操作返回原绑定 | 人工controller；只建立监控对象，不开播 |
| GET /review/v1/monitor-sessions/{id} | scope与session id | 当前绑定、订阅／采集状态、可用归档、gaps | 本account只读 |
| POST /review/v1/monitor-sessions/{id}/start | expected_generation、所用source配置引用 | SubscriptionReceipt，可能waiting_source；不能重置已连接相同source | 人工controller；管理自身订阅，不执行直播动作 |
| POST /review/v1/monitor-sessions/{id}/stop | expected_generation、reason | 本订阅释放回执、可封存范围 | 人工controller；不调用停止直播／全局disconnect |
| POST /review/v1/monitor-sessions/{id}/seal | expected_generation、drain_policy | 202 job或ArchiveRef＋明确partial | 人工／受权后台协调器；封存分析资料 |
| GET /review/v1/archives/{id}/revisions/{rev} | 完整ref校验 | 不可变manifest、capability、coverage、evidence入口 | 只读 |
| POST /review/v1/imports | 流式文件＋目标session＋client_request_id | BlobRef＋预览job；不直接当正式交易 | 人工controller，受限原件写入 |
| GET /review/v1/imports/{id}/preview | import id | fields／units／grain／scope候选、关键警告和原行定位 | 只读；脱敏样例，不返回顾客地址 |
| POST /review/v1/imports/{id}/confirm | mapping_ref／hash、商品匹配、human selection、expected_generation | 正式mapping／dataset或局部拒绝清单 | 人工确认；复用已确定profile可免重复问同口径 |
| POST /review/v1/conversations | monitor_session、可选archive_ref、显示语言 | conversation id＋初始workspace | 本account；本地对话对象 |
| POST /review/v1/conversations/{id}/messages | text、goal_mode可选、refresh_mode可选、selected_refs、client_request_id | 202 run_id／GoalRef＋assessment_status；明确请求可直接冻结，自由对话由K00／F01同服务冻结解释 | 用户消息；保留raw text，scope不由模型扩大 |
| GET /review/v1/runs/{id} | run id | status、goal、active／waiting、已完成refs、pending问题与budget摘要 | 只读；不靠UI从日志猜当前状态 |
| GET /review/v1/jobs/{id} | job id＋scope | JobStatus、输入hash、result_refs／错误、generation、attempts、waiting_run_refs | 只读；独立作业在SSE断线／重启后仍可查；accepted不等于成功 |
| POST /review/v1/runs/{id}/cancel | expected_generation、reason | cancel_requested，后续查询确认已停新动作 | 用户；不停止source监控，不撤回已发生模型费用 |
| POST /review/v1/decisions/{id}/answers | decision_ref、所选值、preview_hash、expected_generation | confirmed或stale；同一问题幂等恢复 | 用户，不能跨run回答别的decision |
| GET /review/v1/events | conversation／session过滤、after或Last-Event-ID | SSE，稳定stream_position；断线补发＋快照 | 本account只读；过滤后的序号可有空洞，不当漏消息数 |
| GET /review/v1/workspace | conversation、选中的archive／report／product | 左侧投影：来源／覆盖、商品指标、模块、报告、待确认 | 只读；不把SSE状态当业务正本 |
| GET /review/v1/reports/{id}/revisions/{rev} | 报告ref及view | 真实正式报告、分数、限制、建议 | 只读；按原版本展开，不默认换latest |
| GET /review/v1/evidence/{id} | opaque ref、view、cursor、limit | 安全证据正文／原行／时间线，覆盖与下一页 | 只读；不可输入本机任意路径 |
| POST /review/v1/adoptions/preview | report_ref、selected_recommendation_ids、target_preparation_ref | 精确计划／差异、已有recap保留策略、preview_hash、到期条件 | 人工意向；不写下一场 |
| POST /review/v1/adoptions/confirm | preview_ref＋hash、human selection、operation_id、target expected_revision | 202 handoff operation或committed；可查询 | 唯一human_business入口；不在模型allowlist |
| GET /review/v1/operations/{id} | operation id | proposed／imported／linking／linked／conflict／unknown与对账 | 只读；超时先查，不新建key盲重试 |

UI目标mode可为full_review、answer_question、score_only或prepare_handoff；原始用户文本与要求不可改写。未明确模式时主Agent用K00或F01的task_assessment提出有限解释，服务核scope、required_deliverables及refresh_mode后冻结；不再增加固定的“识别任务类型”模型调用。明确“重算／重新分析”的义务绑定CalculationReceipt，不能只换新报告标题。单问某商品不被强制生成完整报告。

## 3 左侧／右侧具体展示

| 区域 | 后端提供的内容 | 可微调的范围 | 修改后的失效与锁 |
| --- | --- | --- | --- |
| 左侧来源 | 场次、原件／报表、字段映射、采集及缺口、是否已校准 | 人工选表／确认映射、商品对应、补资料 | 新dataset／archive revision，不改旧报告；Agent正在确认同mapping时锁该项，纯只读不锁 |
| 左侧经营卡 | 净额、目标完成、各单项相对分、退款龄期、样本和缺项 | 用户新设目标或更正口径应创建新版本 | 不覆写历史90分；旧目标分保留，清楚标新目标计算 |
| 左侧商品／模块 | 实际播放项、长短开场、内容四项与响应分、句子／评论时间线 | 筛选／选中查看；对语义标签作人工更正并留依据 | 筛选不是改稿；标签更正新revision，重算受影响统计 |
| 左侧报告／建议 | 观察、解释假设、反例、修改目标、指标与验证办法 | 选择采纳／暂缓；人工备注 | 选择不等于已改声播；操作卡绑定精确报告和下一场 |
| 右侧对话 | 用户问题、Agent公开解释、任务进度、缺项确认、最终交付 | 可以追加调查、纠正目标或问某个分数怎么来 | 新要求进新的Goal／受控修订，不悄换正在跑的归档 |
| 右侧执行过程开关 | 主子任务块、工具安全参数、结果摘要、耗时／缓存、缺项 | 只控制展示，可展开安全原文；默认关 | 关闭不停止trace、不取消run、不改变计分；不显示隐藏思维链／凭据 |

新增Review UI不再放一个能绕过Agent／权限的“直接修改声播数据库”入口。已有声播播控继续由现有应用所有者管理，Review模块只读其当时版本。

## 4 SSE与调试事件

EventEnvelope统一含event_id、schema_version、stream_position、run_id可空、run_sequence可空、invocation_id可空、parent_span_id可空、role可空、session_id／job_id／operation_id可空、timestamp_utc、type、status、safe_payload_ref、duration_ms可空。归档／导入可以发生在任何run前，不伪造run_id；每事件至少有session／job／operation／run一种真实所属。stream_position是Review库跨run持久序号；run_sequence只在单run排序，不能互相覆盖。

| 事件类 | 示例 | 前端行为 |
| --- | --- | --- |
| task／branch | run.started、delegation.started／finished、run.waiting_input | 新建任务块／子块，公开任务目标与进度，不展示模型私有推理 |
| tool | tool.proposed、tool.started、tool.finished、tool.reused、tool.failed | 同一call_id归一块；折叠参数、合法ref、结果概要、origin、耗时 |
| data／metric | import.preview_ready、archive.revised、metric.calculated、score.unavailable | 更新对应左侧对象；旧报告不随新数据悄变 |
| model／context | model.attempt.sent、model.usage_observed、context.projected、context.summarized | 调试可看用途／用量／预算，不输出令牌、隐藏reasoning或整份私密订单 |
| delivery／adoption | review.committed、run.completed、adoption.waiting／linked／conflict | 通过正式refs刷新报告／下一场状态；accepted不显示已关联成功 |

先保存状态＋trace再发事件；断线以Last-Event-ID补发。游标过期返回CURSOR_EXPIRED＋snapshot，不能将UI补发成功等同源事件完整。公开payload由统一投影器产生，后端受控payload也必须先脱敏，不能前端隐藏但接口可取完整密钥。

## 5 四类人工确认点

| 确认点 | 何时出现 | 确认内容 | 不需要额外确认的事 |
| --- | --- | --- | --- |
| 场次／来源绑定歧义 | 同房有多个voice session、外部房间不一致、已有源配置不同 | 本次监控对象和采用的已有source | 对同一已确认绑定重连／恢复消费，不重复问 |
| 报表关键口径歧义 | 人数／次数、父／子单、币种、时区、退款状态或SKU对应不明 | 显示过的header／映射快照 | 读取／预览、确定性统计、相同profile无需再逐字段问 |
| 缺必要资料 | 用户要成交但缺本场经营来源，或关键表无法解析 | 补什么、可先交付哪种有限结果 | 不再问“能否导出”这一已确认事实，不要求每个读取批准 |
| 采纳反馈到下一场 | 用户选建议并准备关联指定preparation | 精确报告、建议ID、旧／新目标refs、影响、保留原有recap策略 | 生成报告和候选无需额外批准；后续播前文本到语音仍走原批准，不由本模块代批 |

该Reviewer没有自动业务写工具，因此不为所有read加HumanInTheLoopMiddleware。人工API确认是普通业务回执，与SDK任意tool interrupt并非同一机制；如后续开放Agent主动提交采纳，再另加明确HITL并验证重放语义。

## 6 采纳到播前的完整路径

| 顺序 | 拟议函数 | 读／写与条件 | 中断后怎么办 |
| --- | --- | --- | --- |
| H1 | feedback.preview_adoption(report_ref, ids, target_ref) | 读正式报告、建议、目标当前版本与已有recap_refs，产生不可变preview | 没用户confirm不调用voice写API |
| H2 | feedback.confirm_adoption(preview_ref, hash, principal, op_id) | 核human、同account、报告未撤回、选择范围和目标CAS；登记稳定request_hash | 重复点击回同op；参数不同拒绝 |
| H3 | build_recap_document(selected, full_report_ref, evidence) | 用稳定namespaced finding ID，保留原source IDs、精确report_ref和原证据摘要／hash；非选项保留在完整报告，不当已拒绝 | 模拟包用隔离测试对象；格式错本地纠正，不调模型改事实 |
| H4 | ensure_voice_blob_and_import_recap(operation) | 用各自稳定子operation ID完成blob staging及import_recap，取得真实PRELIVE-2.3 recap_ref | 每阶段request hash／回执持久；响应不明查该子ID的read_operation_receipt，不能换ID重导 |
| H5 | link_recap_cas(operation, recap_ref) | 重读仅核对差异，仍传确认preview冻结的expected_revision；按preview保留旧recap_refs再追加本次ref，去重 | 现有link整体替换列表；不能只传新ref清旧项，也不能重读新revision后偷偷改CAS。冲突展示新差异 |
| H6 | verify_handoff(operation) | 回读目标preparation确含本次recap_ref，记录linked_generation和最终回执 | 写成功回包丢失可查／回读推进；只import成功但link失败不是整体失败，保留可继续状态 |
| H7 | 既有播前处理 | 读取正式recap，结合当前商品／顾客／本场配置决定新稿或局部修改；逐条disposition | Review不自行重生成话术／语音，不跳过原文本批准门禁 |

旧Recap的findings／evidence仍是部分dict校验：本适配器先严格校验引用支持、对象范围和选项，不以旧接口接受JSON就认定语义合格。若新报告修订将替代旧建议，preview必须明确替代哪些旧recap；默认追加不能隐式删除用户已有准备信息。

父adoption operation_id只在Review使用。voice每条命令按`uuid5(parent_id, plan_version＋':stage:blob')`、`':stage:import_recap'`、`':stage:link_recap'`派生不同且可重复计算的子ID，连同principal、command及request_hash保存。现有voice以principal＋operation_id唯一，多命令共用父ID会触发IDEMPOTENCY_KEY_REUSED。重试／回包丢失只查该阶段原子ID；not_found不证明没有在途请求。若目标内容已变，旧计划停conflict，新preview确认后新plan_version派生新子ID，不能用同key改payload。

`recap_finding_id='rv_'+canonical_hash(精确report_ref＋源finding_id＋源recommendation_id)`，将origin.source_finding_id／source_recommendation_id另存。现有播前把所有recap finding_id合在一个set中核处置，若两份报告都叫finding_1会冲突，所以不能直接保留局部ID作业务主键。一次建议关联多个finding时投影为一条建议对应一个稳定处置项，内部保留supporting_finding_refs[]；不按标题或序号去重。精确同报告同建议重复投影ID相同，不同报告修订必须不同；evidence ID也限定报告命名空间。

CAS冲突不是要求用户重复批准同一个未变化动作。目标／报告／选择实质变更才生成新preview与确认；仅回包丢失、回执可核实就按原operation继续。目标正在生成／已发布／开播时的限制由现有prelive服务判定；Review不能绕过编辑锁或批准失效规则。

## 7 三条完整运行时序

### 场景A：正常整场经营复盘

用户选择本场→controller绑定并采用已有source租约→播中旁路归档→人工关监控后有界排空／封存→上传CSV／XLSX→口径已明直接导入、歧义一次确认→新ArchiveRevision→用户说“复盘本场”→创建Goal／run／checkpoint→主Agent选F01与必要商品面板→程序给四类卡与缺项→复杂内容和经营才并行task→子返回各自结构化证据→主保存草稿并finish→正式报告回执驱动SDK结束和UI更新→用户勾选建议→preview→confirm→Recap导入／CAS关联／回读→播前另开准备任务。

### 场景B：没有逐单或分钟数据

只导出商品汇总→进口声明business_aggregate→整场商品CTR／净额可以算→模块／句子成交分null并给原因→Agent仍可看真实已播稿和评论问题，交付有数据的复盘→用户原目标要求具体模块成效的部分标unavailable／partial，不凭空切分整场金额。

### 场景C：中途重启、晚到退款或采纳冲突

重启加载run／budget／正式成果／unknown→主图checkpoint恢复；已完成子结果／指标按精确ref复用→恢复消息只带变化与reader→旧ArchiveRevision不变→晚到退款生成新archive／report修订→原报告仍可查；旧采纳preview不能批准新结论→若voice关联已成功但回包断线，以原op查询并回读目标；若preparation已改，标conflict提供差异，不清空旧recap、不盲重试。

