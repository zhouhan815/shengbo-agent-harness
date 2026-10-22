# 旧方案、声播播放证据与播前复盘接口只读核查

核查日期：2026-10-20（北京时间）。范围：本地代码、旧方案、已安装 SDK 元数据。未启动模型、未调用平台、未重启服务、未修改业务源码；本文件记录当前可证实能力和新设计应补的内容。

## 1. 本次新范围与旧方案的关系

| 项目 | 旧方案 | 新方案应如何处理 |
| --- | --- | --- |
| 播中 | `04_播中运营Agent任务.md`含候选分析、受限自动FAQ句尾插播、建议调序、动作提交和回执 | 保留旧文档；新方案不启用任何模型驱动的插播、改稿、换序、改价、停播动作。采集程序只读旁路，不位于逐句播放关键路径 |
| 播后 | `05_播后复盘Agent任务.md`已有归档完整性、确定性统计、按证据查询、发现/假设区分、改进提案 | 可沿用；从“动作效果复盘”收敛为“实际播放、观众反馈、经营数据与内容表现复盘” |
| 数据合同 | `01_统一数据与工具契约.md:48-76`设计了NormalizedEvent、SessionArchive、ReviewBundle | 这些是设计合同，不是全部已落地。不能把旧任务文档当作当前已实现的完整归档服务 |
| 播前闭环 | `engineering-v2/09_复盘输入与话术闭环设计.md`已有RecapDocument、逐条处置、精确引用 | 导出复盘候选及给播前使用的结构化建议。由用户选择并显式关联下一场；不自动改商品、活动或脚本正本 |

旧资料存在过时语义，例如`08_业务闭环与动态数据详细设计.md:371-385`仍写收尾/转场，:535曾设想跨循环变换开场。当前代码已明确“保存位置固定，循环不变”，新方案以当前代码和用户最新需求为准；旧文档作为历史保留。

## 2. 当前真实模块与播放单位

| 代码或数据单位 | 当前事实 | 证据 |
| --- | --- | --- |
| 兼容模块键 | 7个：`opening_long`、`opening_short`、`product_intro`、`selling_points`、`campaign`、`cta`、`transition` | `shengbo-ops-agent/src/shengbo_contracts/business.py:294-298` |
| 活跃可生成键 | 6个，排除独立`transition`；长/短开场是替代关系 | 同上；`export_schema():633-639`返回`independent_transition=False` |
| 一版普通实际播放 | 只取一个开场，再取商品介绍、卖点、活动、促单，通常5模块；无活动模块时可4模块 | `ecommerce-voice-agent/app/prelive/playback_compiler.py:125-135` |
| 导入商家原稿 | `origin=imported`整稿原样作为`product_intro`播放，不强制拆成5/7模块、不补造开场 | `playback_compiler.py:126-129` |
| 长短开场 | 保存队列的第1、6、11…项长开场，其余短；每个“商品+版本+出现位置”分别计数 | `playback_compiler.py:56-60`、:163-175 |
| 循环/手动跳播 | 同一个已保存位置继续用原先模式；不是按本次实际播出次数重新每五次计数 | 文件头:1-5；server.py:537-545循环回index=0 |
| 商品/脚本修订 | 编译期使用精确`script_revision_ref`、`module_ref`；播出队列携带`pinned_product_ref`、`publication_ref` | `playback_compiler.py:119-158`；`prelive_bridge.py:255-272` |
| 版本/出现/尝试 | `variant_id`表示话术方案；`script_revision_ref`表示该方案精确修订；`occurrence_id`表示队列中的出现位置；`attempt_id`表示某一次实际尝试 | `prelive_bridge.py:263-272`；`server.Room.set_current():477-482` |

因此，不应为复盘重新制造七段话术。报告以真实`module_key`展示。导入原稿没有可靠模块边界时，保留`merchant_original`分组；如要语义分段，仅作为带范围的分析标注，不能改写原稿的真实播放结构。

## 3. 当前可以读取什么

| 能力 | 现有入口/函数 | 可读取内容 | 重要边界 |
| --- | --- | --- | --- |
| 当前状态 | `GET /api/rooms/{room_id}/state`，server.py:1141；`Room.snapshot():459` | session、epoch、queue/index/current/audio/position/status、player_id、output_source | 当前内存快照，不等于完整历史归档 |
| 旁路监听 | `WS /ws?role=monitor&room=...`，server.py:1294-1337 | 每次broadcast的状态，含当前商品/脚本/模块/句/attempt | 监听晚接入或断连会缺历史；当前无可靠事件游标重放协议 |
| 全量兼容导出 | `GET /api/export`，server.py:1286-1291 | product/script/config、playback快照、room_logs、viewer_events、live_metrics、reply记录 | 混合多房间/多场次；需自行按session切分；不是SessionArchive；不宜让模型反复读整库 |
| 场次历史 | typed query `read_session_history`，service.py:2475-2481 | room/config各修订、preparations、prelive_session、最近limit条room_log | logs按room筛选而非单session；正常历史仍需明确选目标session |
| 稿件/商品/本场快照 | typed queries`read_product`、`read_script`、`read_preparation`、`read_context` | 不可变精确修订和原文 | 必须优先读播出publication引用，不用最新商品头覆盖上场实际内容 |
| 音频/编排计划 | publication中的compiled_plan、audio_manifest和approval引用 | 本场实际选中的语音、精确稿件与活动/TTS快照 | “已有音频文件”不能证明播放过；“已开播”不能证明平台有流量 |
| 输出设备诊断 | `GET /api/audio/output-status`；app/audio_output.py:20-98 | Windows输出设备、浏览器会话、静音/音量/采样peak | :95明确浏览器可能含其他网页，不能证明OBS/直播伴侣已经采集。并非录音或平台回传 |

正式typed查询入口由`app/prelive/routes.py:79-81`的`/queries`承载（路由前缀应复用现有client配置，不凭文档硬编码另一路服务）。现有工作流不需要暴露控制写入口给综合Agent。

## 4. 谁能证明“播了什么”

| 层级 | 当前证据 | 能证明 | 不能证明 |
| --- | --- | --- | --- |
| 准备/编排层 | 编译计划、音频manifest、publication | 本场准备播放哪些精确内容 | 该内容实际发声 |
| 启动层 | `_start()`保存prelive_session.started_at、playback.started，service.py:1355-1359 | 后端接受并创建了这一场播出任务及队列 | 第一声已在HTMLAudio、OBS或平台播放 |
| 播放器层 | VoicePlayer发送playing、progress、ended；server校验owner/epoch/attempt | owner网页媒体元素报告开始/进度/结束，ended后服务器推进 | 人耳听到、系统未静音、OBS有音频、平台推流或观众听到 |
| 本机音频层 | 设备/浏览器peak诊断 | 指定设备或浏览器进程附近采样时有电平（视设备可用） | 电平属于本场/某句；OBS已采集；平台已接收 |
| OBS/平台层 | 当前核查未找到完整OBS采集/输出帧及平台观众可听回证 | 新设计若接入相应独立证据，可单列状态 | 不得以`source='obs'`标签替代真正OBS观测 |

关键来源：`web/audio-player.js:87-97`为HTMLAudio事件发送；`server.py:685-715`回执处理；`web/player.js:15`的播出页调用`connect(roomId,'obs')`，因此即使只是普通浏览器打开该页也会标`obs`，标签不可作为OBS运行证明。

## 5. 现有日志能留什么、缺什么

| 情况 | 现有实现 | 缺口 | 新设计最小补点 |
| --- | --- | --- | --- |
| 发起一尝试 | `set_current():477-483`创建attempt后写`prepare_requested` | detail只有文本，没有attempt及精确引用 | 同事务/同状态变更点保存`attempt_created`结构化事件，携带不可变身份链 |
| 音频就绪/失败 | :511-525写audio_ready(audio_id)或synthesis_failed(sentence_id) | 无耗时/具体attempt连接；旧名不能区分缓存文件损坏与合成 | 事件关联attempt，分类`audio_asset_ready/prepare_failed`及受控error_code；不记录原始异常或凭据 |
| 开始/缓冲/失败/拦截 | :691-715写`client_*` | detail仅sentence_id；playing可能暂停恢复/缓冲恢复多次；无客户端时间 | 保存每次状态变更与segment序号，连续播放段分别计时，不能按playing次数当讲解次数 |
| progress | :694-695只改内存position；客户端约350ms发送一次 | 进度历史不持久化、无心跳窗口、卡顿难精确复算 | 限频持久化progress/heartbeat（建议1秒采样，可配置）+起止事件，保留raw positions与接收时钟 |
| 完成 | :696-699写client_ended，played追加sentence_id/attempt_id/text/time，截最近1000 | played内存且无开始时间、商品/模块/occurrence；日志中无attempt | append-only完整attempt终态；未收到ended则unknown/interrupted，不用理论时长补完成 |
| 暂停/恢复/跳播/重试/换品/排序 | `Room.action():552-683`，最终`control_accepted`只记action名 | 不能完整重建参数、被跳过范围、队列前后版本、原attempt中断原因 | 只旁录人工动作：actor/source、action_id、类型、旧/新queue_ref、受影响attempt、reason；新Agent无执行权 |
| 循环 | `advance():537-545`回index=0 | 未单列cycle index；同一句多次播放需外部推断 | `cycle_started/cycle_index`，每次attempt新的ID；同一occurrence每轮与重试分开 |
| 播放器连接 | `/ws:1311-1348`owner接管/断连 | 初连不一定独立日志；只断连暂停，无last_heartbeat/stale判定 | player_connected/owner_changed/disconnected、owner_id/client_instance、source标签与真实检测性质分列 |
| 场次结束 | `release_live_slot():1398-1406`标stopped_at；重启恢复:1384-1390标interrupted及reason | 普通停止原因及未终结attempt未完整保存 | 确认结束事件、end_reason、最后状态、未决attempt；断线不立即当正常结束 |
| 时间精度 | server.py:48-49`now()`为ISO时区秒级 | 同秒事件不能严格排序；并发平台延迟难区分 | occurred_at/received_at/ingested_at（毫秒以上）、collector monotonic_ns/sequence；同时保留source时间精度及不确定度 |
| 持久化 | Room.log():450-457写records及events.jsonl，UI仅120；prelive_events有全局autoincrement seq | room_log无全局/场次事件序号；prelive_events主要业务事务事件，不能假定含所有播放ACK | 新独立只读遥测表/流和source cursor，增量归档。UI120、played1000上限不当归档上限 |

`read_session_history`声称`recorded_starts_and_actual_player_logs`准确描述了目前有开始记录和部分播放器日志，但不意味着已有完整`playback_attempts`合同。以上为功能差距，不据此宣布当前播放器无法播放。

## 6. 建议归档的最小身份/时间字段

这是后续设计建议，当前未实现：

```text
event_id, schema_version, event_type, local_seq, source_seq?, source_event_id?
room_id, live_session_id, platform_room_id?, platform_session_id?
publication_ref, preparation_ref, draft_ref, compiled_plan_ref, playlist_ref,
approval_ref, review_hash, product_ref, script_revision_ref, module_ref,
variant_id, version_number, module_key, source_sentence_id, playback_sentence_id,
compiled_entry_id, occurrence_id, original_position, opening_mode,
queue_id, attempt_id, cycle_index, segment_index,
owner_id, client_instance_id, output_source_label, data_origin,
occurred_at?, received_at, ingested_at, monotonic_ns, clock_domain,
timestamp_precision, clock_offset_estimate_ms?, position_seconds?, duration_seconds?,
status, reason_code?, correlation_id?, raw_evidence_ref, payload_hash
```

- 归档`source_sentence_id`与编译后`playback_sentence_id`映射；不能把哈希合成音频的requirement_id当曝光/尝试ID。
- `data_origin`至少分真实平台、真实本地播放器、人工导入、模拟；模拟值不能进入真实经营报表分母。
- `occurred_at`未知可null，received不能悄悄替代真实发生；源只分钟级就按分钟分析，不拆成每秒假数据。
- 正常播放时不要每次progress触发LLM；程序收集/去重/压缩，播后由Agent按证据窗口读取。
- 断连后标观察缺口，使用源游标尝试恢复；只有可完整重放才把缺口关闭。重复、乱序、重连以稳定ID去重，不能按同文案误删循环事件。

## 7. 实际模块对应什么评价目标

| 实际模块 | 主要目的 | 有数据时观察 | 不能直接下结论 | 下一场候选建议 |
| --- | --- | --- | --- | --- |
| opening_long | 本场主题、欢迎与需求入口；吸引愿意继续听的人 | 进入队列在30/60秒的留存、实际听到开场的覆盖、内容相关评论 | 在线人数减少不能单独归因于开场；无进入/离开轨迹不算留存 | 缩短重复欢迎；更快点明本场主题/目标需求 |
| opening_short | 快速进入商品/角度，降低循环重复感 | 首句到商品介绍的继续观看、长度、重复表达、版本切入问题 | 它在不同队列位置/流量时段，不能直接同长开场比赢率 | 去客套，明确品类/需求；保留原位置开场规则 |
| product_intro | 让顾客知道是什么及关键规格/适用差异 | 身份/规格问题、商品点击、完成覆盖、听到时长 | 商品点击不等于确认购买；原稿只标product_intro不代表整稿都是简介 | 前置用户反复困惑且有依据的身份/规格信息 |
| selling_points | 将特点与顾客场景、购买理由连接 | 对应主题问题/意向评论、商品点击/收藏/加购（若真实可得）、相对曝光时长 | 评论频率不等于真实需求总体；先排除投放/货架/价格等变化 | 换具体场景与表达，少重复；明确仍是假设的顾客定位 |
| campaign | 准确说明报价和优惠成立条件 | 价格/用券/门槛重复问题、合规事实、商品点击/下单/支付延迟窗 | 支付滞后，订单不能按支付那一秒归给当前模块；不能擅自降价 | 简化重复，保留条件，用有来源的例子解释 |
| cta | 简短行动引导 | 后续点击、加购、订单（按来源归因窗）、购买步骤问题 | 最后触点不代表唯一成交贡献；不能用点赞当成交 | 中性、短、适合当前真实购买入口的引导 |
| transition | 当前不独立播放，兼容历史读取 | 旧档若确实播放另列legacy模块 | 不为做七行评分而造出当前转场表现 | 不新增独立转场文案 |

无曝光、点击、订单和归因数据时，最多报告“关注/问题信号”和内容覆盖，不能声称某商品转化率更高。无销量的事实不等于销量为零。模块评分不使用同一指标硬打总分；最终经营结果、过程信号、技术质量、证据完整性分别呈现。

## 8. 给播前的已有入口与建议接法

| 项目 | 当前代码/合同 | 状态和设计建议 |
| --- | --- | --- |
| 导入复盘 | `ImportRecapArgs(raw_blob_ref, document:dict, synthetic)`，business.py:102-105；service.py:1499-1535 | 当前有真实业务命令：检查blob哈希、finding非空/ID唯一、部分证据quote/hash、模拟标记。不能把设计里的全类型约束都说成已实现 |
| 绑定下一场 | `link_recap_to_preparation`，service.py:1537-1543 | 显式精确ref+CAS绑定，清bundle/plan；不写商品/旧话术。需要在新方案限定仅用户明确选择后触发 |
| 全文读取 | `read_recap`，service.py:2501-2509 | 只读精确记录和受控blob，UTF-8文本/Markdown/JSON校验hash后可读原文 |
| 上下文汇合 | `save_context_bundle`，service.py:778-806 | 所有当前已绑定recap不能漏掉；records保留精确数据，非从聊天摘要重建 |
| 话术计划处置 | `save_script_plan`，service.py:906-912 | 当前检查绑定finding是否都有处置ID；不是完整强类型RecapDisposition语义校验。新导出要用稳定finding_id及精确recap_ref，避免跨复盘同名ID冲突 |
| 设计目标 | `engineering-v2/09:38-129` | origin/scope/evidence/findings/proposed_actions/suggested_modules/human_agreement等可做新ReviewBundle→RecapDocument适配目标 |

推荐导出：一份可读HTML/Markdown复盘＋一份版本化JSON（原始指标算式/证据索引/观察/假设/具体建议/验证方法/适用商品修订/模块/有效期）；JSON通过现有import_recap保存，只在用户选择下一场时显式link。建议“商品2关注信号更强，可试放开头”记录为`lineup_change`待采纳建议，不直接改队列。研究推测和复盘建议不得升级为商品事实。

## 9. 已安装 SDK 与当前设计边界

通过本地`.venv/Scripts/python.exe`的`importlib.metadata.version`只读核验：deepagents 0.7.25、langchain 1.4.6、langchain-core 1.6.10、langgraph 1.2.15，与pyproject锁定一致。未安装或升级。

新综合Agent可复用Deep Agents的主Agent、有界只读证据工具、可选并行专题子Agent和检查点；采集、归档、确定性指标由程序服务完成。无需搬用旧播中动作子Agent，也无需在每条弹幕或播放progress上唤醒模型。先保证数据能正确联到“本场→出现位置→商品版本→模块→句→尝试→实际观察时间”，再让Agent解释数字。

本核查未对真实平台采集器的全部字段作判断；由并行的渠道代码核查提供独立证据。未证明OBS/平台实播、真实订单归因或整场数据完整。
