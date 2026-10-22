# 声播播前Agent项目上下文

- 2026-10-21 MiniMax连续试听：独立`audio_preview`目标与`preview_script_audio`工具仅执行真实人工回执固定的script/module原文、顺序、voice/style。人工命令`approve_script_audio_preview`不等同整包语音批准；专用HITL核验真实回执再恢复，未知付费请求先对账。第一版长开场＋商品介绍已由实际Agent一次请求生成114.148秒WAV，usage_characters=1094。入口与证据见output/20261021-minimax-two-modules/连续语音试听与控制说明.md。正式整包句级编排尚未迁移段级，不能把试听成功冒充整场播音连贯度已验收。

- 工程设计入口：`D:/Codex project/shengbo-ops-agent-plan/README_当前设计入口.md`。
- 2026-10-16模型来源决定：未来Agent采用Deep Agents框架，模型来源`chatgpt_plan`，首个目标模型`gpt-6.1-sol`。
- 用户和独立连接交接报告已确认本机ChatGPT授权及真实短文本调用成功；不等于本项目的Deep Agents工具往返、摘要、恢复或自动续期已经实现。
- 历史上先保存设计；2026-10-16用户后续已明确要求构建完整播前Agent，覆盖此前“仅设计”阶段。代码已开始实装，最新状态见计划目录engineering-v2/18_首期构建与验收记录.md。不能依据旧阶段说明停止已授权构建。
- 开始模型接入前读取：`D:/Codex project/shengbo-ops-agent-plan/engineering-v2/17_ChatGPT授权模型来源与后续接入记录.md`及`D:/Codex project/shengbo-chatgpt-connection/给Agent开发窗口的接入说明.md`。
- 不要求用户复制令牌。未来在同一Windows用户下按交接安全读取DPAPI；凭据不得进入对话、日志、checkpoint或版本库。18791是连接测试页，不是推理base_url。
- Agent模型来源选择不自动改变声播业务文案DeepSeek或TTS本机普通话／MiniMax配置；不静默回退其他账户/计费来源。

- 2026-10-16实装验收：最终完整Runtime真实ChatGPT只读探针5次调用完成COMPLETED；自动续期的真实长期自然过期尚未观察。普通测试不得自动调用真实模型或付费TTS。

- 2026-10-16最新播放规则：播完队列从第一项按保存顺序循环，直到人工停止，不再提供单轮结束。Agent修改期间左侧对应对象/字段/模块禁编辑；跨会话editor-locks投影包括新建商品、取消排空和未知写入，纯只读不锁。修复记录见engineering-v2/21_复查问题修复与循环编辑保护.md。

## 多轮修改后的结构一致性复查（2026-10-19用户要求）

- 2026-10-20消费者商品规则：商品正本仅收录直播顾客选购、使用与服务所需信息；供货渠道/链接/店名/进价/供货库存评价不存入商品字段。来源原件、证据、存疑参数和任务过程仅留Agent资料审计，不能迁入知识文档绕过。资料理解在现有product_facts一次分类，Voice的app/consumer_catalog.py是唯一程序准入与历史模型输入策略。字段按简洁规格、消费者知识/FAQ、未知不适用分配；不得删限定语后将存疑值变成确定事实。
- 明确商品字段限制用IntentAssessment.requested_product_paths及原始指令范围引用，不再用跨句的“只/仅+售价”正则推断；左侧多选必须逐项保持精确对象/版本/字段/模块范围。

- 每个有效修改与测试批次完成后，必须回查本批改动及受影响的调用链，不能只凭测试通过宣布完成。
- 重点检查：新旧路径是否并存且语义冲突；同一规则是否散落重复；工具、Skill、数据契约、中间件、业务执行与错误恢复是否前后一致；旧分支/回退是否绕开新规则；状态、引用与人工批准是否仍有清楚的正本。
- 从真实入口核验到结果：局部函数通过不等于Agent模型—工具反馈—恢复链路通过；新增错误码须检查是否能被调用方处理并继续。
- 区分已证实缺陷、结构维护风险和待验收能力；文件变长或存在兼容路径本身不直接判为故障。报告注明代码与测试时点，不能把旧故障当成当前仍复现。
- 优先按共同根因合并修复和测试，减少逐小改全量回归；复查以差异/调用链/既有证据为起点，出现新问题再定向验证，不机械增加模型调用或检查阶段。
- 保留模型自主规划：可选择调用工具/函数/工作流；确定性的权限、事实引用、版本与副作用约束由程序统一保证。不要通过复制一层提示词或临时补丁掩盖结构冲突。
- 每批交付记录：解决的问题、唯一职责落点、被替换或保留的旧路径及理由、验证证据、结构影响、仍未解决项。未经实测的内容不得标为完成。
- 2026-10-19统一审查与第三次修改更新计划入口：`output/20261019-consolidated-review/第三次修改与更新方案.html`；当前复现缺陷与旧22项映射保存在同目录结构化审查记录中。该轮为审查/计划，不代表所列修复已实施；后续交付应逐项更新状态。

- 2026-10-20归类修正：问答型内容统一存faq，不再把FAQ作为knowledge_documents的主题；叙述型知识按已知事实覆盖，不为精简规格而丢弃可用知识。抖音默认说明唯一来源为Voice app/data/platform_rules/douyin-20261020.json，经schema.platform_policies共享；现行规则按商家承诺时效，48小时是可选商家承诺而非全平台默认。平台选择可自动填已核验模板，保留人工自定义，不静默改已保存/批准内容。

- 2026-10-20核心卖点优化：写作规则唯一入口为skills/main/core-selling-points/SKILL.md，参考文件保存少量用户真实“资料→顾客利益”样例及外部方法来源；不是直播脚本模板。先区分提示词、商品数据、实际上下文和工具证据限制，不把弱输出都归咎于提示词，也不机械叠加外部Skill。顾客分析要区分适用范围、商家经营背景与本次重点；查询无研究不算完成研究。明确要求研究时由既有audience_scenes执行并正式验收，候选引用当前商品版本和正式研究。
- 卖点营销正文与证据/审计限制分开；允许有依据的场景与利益转译，不以“资料清楚、方便核对”代替购买理由，不从参考商品迁移功效。消费者knowledge、target_audience与knowledge_documents具体content可直接作为事实引用；标题、ID、卖点本身和假设不能作为性能依据。保持原生成意愿、候选确认、批准绑定与保存门禁。方法与真实两版试写及一次局部修订记录见output/20261020-selling-point-upgrade/；此类多因素前后对比不能冒充提示词单因素实验或转化提升证明。

- 2026-10-20正式稿阻断修复：历史正式顾客研究的精确ref尚未在当前run登记，不等于真正越权。主Agent的read_context先由research_access在冻结商品版本/当前本场范围内受限发现；未解析引用用ANALYSIS_REFERENCE_UNRESOLVED只读工具反馈，M5有限纠正。真正403、其他场次/商品、子任务未派发引用及未知写入门禁不能放宽；Voice研究检索也须在分页前隔离其他场次正文。
- 直播间选择是绑定原任务的窄身份决定，不是整体批准。typed room_selection答复经真实read_room核验后仅补齐原意图room_id；调度器直接再走完整_freeze_goal，不重复模型意图判断或同房间确认。商品/字段/模块/版本/CAS和语音批准仍独立验证。修复与164项相关用例、部署和真实只读验证记录入口：output/20261020-research-room-recovery/问题记录与修复报告.md；其中事故的等待/恢复耗时不可冒充正常生成时长。

- 2026-10-20话术版本交互与长度更新：主工作台仅在话术与排序显示完整商品版本卡片，点卡片按固定模块顺序只读查看、切版，模块标题精确选择交Agent修改，不提供正文输入/模块保存或模块重排。新增版本仅在本场配置准备右侧Agent任务，保护未发送草稿并核验真实对话本场绑定；不能仅在文字里换preparation_id。完整新稿目标8—10分钟，由Voice app/prelive/script_duration.py统一240有效口播字/分（1920—2400字）文字估算，实际时长以音频为准；短开场不缩短整版主体。整版单次生成后分块，旧稿精确全文及同批差异方向通过冻结引用传入，不读取动态head改变恢复输入；长短开场不可合计，历史transition不计实播时长。Agent、Skill、整版/局部提示词、存储诊断与前端估时须一致，不保留旧25—50字促单或500—700字整版规则。
- 2026-10-20用户七组参考脚本学习：主播式销售表达唯一正本为Voice `prompts/live-commerce-writing.md`，通过固定loader同时进入whole/module真实SYSTEM与prompt revision；主Skill只编排版本简报和验收，不复制整套文风。长稿以直接圈需求、问答、商品特点转利益、真实价格/到手内容与选择下单动作推进，避免旁观叙事、生活感悟和弱邀请。样例只供表达结构，抗老胶囊的医疗/背书、其他商品价格与虚假稀缺/演示/供货信息不迁移。改写同两方案保留编号与历史修订，不因追求风格增设检查模型或逐模块初稿生成。
- 2026-10-20整版重写输入用途修正：新增计划`existing-script-text-v2`将本批被完整重写的基稿作为身份与来源引用，只传精确ref/模块哈希，不把被替换正文或旧诊断当写作范本。正常新增版本仍传既有稿全文作差异对照，局改保留精确原文；v1冻结输入不改。不能把“保留方案身份”误作“保留原措辞”，也不能为求新文风删除当前商品、研究和用户决定。
- 写作输入分层后续更新：新整版计划使用`existing-script-text-v3`和Voice `writing_input_projection.py`。完整人群/场景/利益结论及关系原值保留，研究资格与未知断言分区，旧任务/handoff/审计过程不作当前写作命令；源ContextBundle不改，可按精确ref追溯。非标准或只有summary的研究保留其唯一结论；商品、本场真实活动和消费者重要限制不删。历史v1/v2及局改不套新投影，维持冻结输入恢复。没有新增模型摘要、审稿Agent或强制总流程。
- 2026-10-21提示词标准：共享口播规则v2明确带货主播兼脚本策划角色；按任务/资料用途、购买问题、事实转利益、统筹模块、同次检查五步创作，只输出正式正文与协议JSON。过程不混进口播，不因此额外摘要或多轮生成。真实研究须绑定实际bundle，不能仅在user_decisions声称引用；当前绑定门禁、动态fact-id enum及收尾效率的遗留记录见output/20261020-live-script-style/本次话术改进与验收.md。

- 2026-10-21脚本可靠性批次：指定历史研究以Goal.required_reuse_analysis_refs冻结，F03显式绑定正式全文，不能用user_decisions中ID冒充研究入包。生成/恢复/验收均核验原固定上下文；普通无研究任务不加门槛。
- 新whole请求采用single-source-utterance-v1，每句只写text，程序精确派生display/spoken。新receipt归档text_contract后严格校验；旧无marker保持原冻结协议。归档验证的基础/effective商品须按冻结draft完整重算，不忽略字段，whole/module共用Voice archive_validation_product。
- patch_script长模块大幅缩短且sentence_ids=[]时须明确intentional_full_module_replacement；这是范围歧义反馈而非内容裁判，不扩大UI/Goal权限。中性false不改变旧操作重放身份。
- 人工答案/失效问题与run恢复收件箱同事务保存；重复答复和重启只修复无新OPEN问题的已解决窗口。欢迎运行时规则与最终报告见output/20261021-script-reliability/本次修复与测试报告.md。保留模型自主规划，修复按共同根因合批，不用重复付费生成替代定位。

- 2026-10-21营销口播v5.2：真实原例短摘录进入唯一共享写作资源，intro保留协议槽但一句20—60字；主Skill可在既有模块计划给少量当前商品声口示范，不新增模型阶段或固定总流程。已有商品/研究→一次整版真实R4已保存第1版修订25，1308字、估算5分27秒、主Agent文风自评70/100，尚未达到用户原例或8—10分钟；不能把31项提示资源测试或实际wire哈希通过当作质量验收。用户要求减少机械试写，本轮只生成一稿后报告；结果与剩余根因在output/20261021-marketing-energy-v5/round-04/本轮自评与一次生成验收.md。归档续接权限仅对显式受信follow-up的原scripts归档本地重验，核验当前窄grant及原回执；不可新付费、删未授权模块或恢复旧grant。离线22不同用例通过并部署，旧R3实际恢复/纯恢复自动finish仍未验收。

- 2026-10-21用户最新覆盖“只读话术详情”交互：客户端应允许逐句编辑与保存，复用现有patch_script精确版本/句子hash/区域锁，保存新修订并沿既有规则清除旧编译、音频manifest及批准。Agent占用对应区域时仍不能输入，不能为手工编辑放宽写权限或版本冲突校验。本轮明确停止Agent逐句风格修补，按更新后的共享提示一次整版重写第一版，第二版不动。新整版用途边界先确定商品实际目的及商家最新目标，再展开人物、时刻和已知支持的动作；不把训练用品改成纯娱乐玩具，不编造确定情绪或训练结果。通用规则按其他品类自身用途判断，不统一套运动场景。
- 本次已部署并实测整版r27：1987有效口播字，文字估时8分16.75秒，run268.549秒，其中DeepSeek一次15.140秒；第二版精确ref不变。真实客户端已确认逐句编辑入口并取消验证编辑，未写改稿。颜色三色清单3处、价格组成2处仍重复，run按内容质量收尾BLOCKED，不是权限/断连失败；没有全面达标或90%文风证明，不自动再生成/逐句润色。交付与未解决项：output/20261021-scenario-sales-v6/本次整版重写与客户端编辑交付.md。
- 2026-10-21按用户新指令保存r27评分/优化方向，未改正文；完成声音实时欢迎与评论闭环修复和隔离真实浏览器MiniMax验证。实时欢迎/回复走Voice已有Room句尾调度＋一次短回复检索/模型，不经播前主Agent多轮规划；提前准备音频不阻塞当前主稿，ready后句尾插入并恢复下一句，欢迎至少60秒，单槽不堆积。真实2欢迎/3回复均恢复正确主句，首2回复的无据附加场景与后台措辞另修prompt并以第3条复测。正式完整r27语音仍未生成，未连真实平台/OBS，不能把三句循环验证冒充整版听感验收。完整记录：output/20261021-live-interaction/欢迎语与自动回复切换测试报告.md。
- 2026-10-21后续整篇试听及点赞：完整r27已在现有Agent内部preview_script_audio按本次精确人工授权一次MiniMax Turbo合成，431.304秒；这是试听，不改变话术/本场或批准正式整包。原“未生成”是上一批历史状态。点赞最新要求不设全场60秒，按用户连续赞短合并、不同人逐个排队；Voice app/like_thanks.py是策略唯一入口，Room复用既有单一插播槽。3条点赞事件/2人真实MiniMax+浏览器播放验证2次感谢并恢复正确主句。事件模拟和protobuf消息路由通过，真实平台/OBS音轨仍待联测。证据与剩余边界：output/20261021-full-audio-and-likes/完整试听与点赞联测报告.md。保持Agent规划灵活性，本批不添加大模型点赞判断、Skill或强制总工作流。

## 2026-10-22 Deep Agents优化批次（Claude Code）

- 用户决定覆盖2026-10-16模型来源：Agent默认改用DeepSeek（providers/deepseek.py）。主/分支思考`deepseek-v4-pro`、reasoning_effort=high；请求中仍有模型未看过的图片时该次改用视觉模型`deepseek-flash`，已看过的图片以占位文字替代；自动摘要用`deepseek-flash`且关闭思考。密钥只从环境变量DEEPSEEK_API_KEY或声播`.env`在发送时读取，不进payload、trace、state或日志。`SHENGBO_AGENT_MODEL_PROVIDER=chatgpt_plan`可切回原ChatGPT套餐；SHENGBO_AGENT_MODEL/VISION_MODEL/SUMMARY_MODEL/REASONING_EFFORT可覆盖。适配器保持原before_send预算门、尝试账本、审计与usage结算钩子；thinking+工具时必须回传所有历史reasoning_content，回传内容计入本地预算预估。
- 主Skill按原文拆为prelive-orchestration（核心）及prelive-materials/script-plan/delivery/recovery分册，规则正文未改写；执行阶段首轮按冻结Goal.objective用原生`pinned_skills`预加载（映射SKILL_PINS_BY_OBJECTIVE），分析分支按角色预加载自身Skill，均登记为完整skill_read。新增规则写入对应分册，不再回堆到核心文件。
- M3可信上下文默认作为每次请求末尾的独立消息（不写入state），系统提示与历史保持逐字稳定以便供应商前缀缓存；`SHENGBO_TRUSTED_CONTEXT_PLACEMENT=system`可回退旧布局。摘要包装与M3每次模型调用共用一次投影，摘要发生后强制重算。
- 话术作业完成唤醒时，事件附review_entry指向`wf_script_execution(action=review)`（仅本run提交的作业，可选入口，不代表验收）。
- 为DeepSeek/ChatGPT两种模型注册HarnessProfile：关闭默认通用`task`子Agent，排除write_file/edit_file/execute；删除已不存在的write_todos名称。
- 新监控：context.budget（固定上下文分量）、skills.pinned、M3字段字符数与缓存复用、model.response.usage_details（cached_input_tokens/reasoning_output_tokens）。测试入口verification/claude-opt-20261022/；备份backups/20261022-deepagents-optimization/。
- DeepSeek适配器必须流式接收（`stream:true`+`include_usage`，本地把SSE拼回非流式结构再走原审计/结算/校验）。根因已对照实验确认：本机网络路径会切断60秒无数据的TLS连接，非流式思考请求同步/异步都在60.4–60.5秒断开，流式同步/异步均可跑完79–189秒；与async无关。流中途断开未见finish_reason时按未完成拒收，不重试。修复前副本`backups/20261022-deepagents-optimization/deepseek.py.before-stream-fix`。语音端`whole_script_generation.py`/`module_generation.py`开思考后仍是非流式，同样有60秒风险（待用户决定是否修）。
- 计划纠错（2026-10-22用户批准）：PLAN_CAPABILITY_DENIED与PLAN_BEFORE_READ为可纠正输入错误，上限各1次（middleware `_CORRECTION_LIMITS`），计划未保存时退回invalid_capabilities/allowed_values；第二次错误仍硬停止。执行阶段首份计划若与READ_TOOLS同轮并行发出，退回一次要求读后单独提交，第二次放行。起因：DeepSeek把业务命令ID（save_product、save_script_plan等）写进capabilities，修复前2/2硬停止，修复后实测纠正1次通过。
- 已知未修（2026-10-22实测）：固定上下文涨到约3.3万tokens（工具schema 1.56万、M3 1.39万）使48k上限下9次摘要后CONTEXT_LIMIT，流程停在分析验收后；分支v4-pro+high单次调用可超M6的180秒被取消。另有2个旧测试未跟上新默认：test_runtime_context压缩用例仍在系统提示找M3（tail M3所致），test_context_wire_replay_budget按ChatGPT请求体计数差2 token（默认DeepSeek所致）。
