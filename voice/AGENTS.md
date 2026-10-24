# 声播业务后端开发约定

- 2026-10-21正式TTS提效：`PreliveService.drain_jobs`仍由唯一coordinator持有`_worker_lock`；仅MiniMax正式audio按句批内默认3路，`PRELIVE_MINIMAX_AUDIO_CONCURRENCY`只允许1–3。每批全部收束后才重新扫描持久队列，脚本、试听、本机COM保持串行。每句沿原批准、外部request key、账本、WAV校验和CAS提交；`job.batch_claimed`记录实际批数。取消不能再次cancel正在写失败账本的child，须await全部收束；已提交success优先于迟到的取消。job失败终态需同时检查queued/running，不能让尾批早失败掩盖仍运行的句子。新增离线测试在`tests/test_formal_audio_concurrency_v1.py`，模拟计时不代表供应商真实加速比。

- 2026-10-21连续试听：`app/prelive/script_audio_preview.py`持有独立精确人工授权，复用`voice_preview.py` durable attempt一次合成有序模块全文；未知结果不重发，回执缓存不重复计算本次usage。不得用局部试听授权批准整包或改manifest。`speech.minimax_request_body`为MiniMax唯一请求体入口；主播风格使用真实voice_setting，角色说明不进入朗读正文。新只读list查询及话术详情audio卡可刷新重读，正式整包音频编排保持既有路径。

- 2026-10-20：消费者商品库不兼任采购档案。确切供货渠道/链接/店名/进价/供货库存及评价不进入商品、规格、FAQ或知识文档；来源与过程只留Agent审计。统一策略位于app/consumer_catalog.py，保存拒绝不得静默改批准内容，历史话术/回复输入同用消费者视图。平台选择不能自动编造发货小时数或退货承诺；产地不能推为发货地。

## 多轮修改后的结构一致性复查（2026-10-19用户要求）

- 每个有效修改与测试批次后，回查受影响入口、业务执行、持久化与返回契约，检查重复实现、新旧规则冲突、旧回退绕开新规则和状态正本分裂；不能仅以单元测试通过作为完成依据。
- 与播前Agent联动时，核对工具/Skill/DTO/中间件/错误恢复能否按同一协议衔接；局部函数通过不代表完整Agent工具往返与恢复通过。
- 区分已证实缺陷、维护风险、待验收范围，保留问题与测试证据。代码长或兼容路径存在本身不是故障证明。
- 按共同根因合并修复和相关测试，避免每个小改都全量回归；先复用既有证据，再对新问题做定向检查，不增加无必要的模型调用。
- 每批交付说明规则的唯一职责落点、旧路径保留或移除的理由、验证结果和遗留项。保持Agent选择工具/函数/工作流的规划灵活性。

- 2026-10-20归类修正：问答型内容统一存faq，不再把FAQ作为knowledge_documents的主题；叙述型知识按已知事实覆盖，不为精简规格而丢弃可用知识。抖音默认说明唯一来源为Voice app/data/platform_rules/douyin-20261020.json，经schema.platform_policies共享；现行规则按商家承诺时效，48小时是可选商家承诺而非全平台默认。平台选择可自动填已核验模板，保留人工自定义，不静默改已保存/批准内容。

- 2026-10-21脚本可靠性与欢迎批次：新whole请求仅写single-source-utterance-v1的text，收据text_contract先归档再严格验证；旧无marker按原协议恢复。事实ID闭集由prelive/fact_reference_contract统一装配，whole/module归档商品快照由archive_validation_product按固定draft完整核对。长模块大幅删除歧义由script_patch_scope在写入前检查，不扩大UI选区或批准权限。
- 进场欢迎唯一规则在app/welcome.py，生成/导入准备5条商品名称关联模板，runtime以当前商品、session和滚动60秒门禁准入及播放复核，符号数字昵称过滤为空时播‘欢迎新来的朋友’。复用原TTS/插播；busy/过期/商品改变丢弃，不积压。测试与真实本机欢迎音频在Agent项目output/20261021-script-reliability，正式网球新稿尚未批准语音。

- 2026-10-21用户最新要求开放客户端话术逐句编辑，覆盖历史“详情正文只读”的交互限制。仍使用既有patch_script版本、句子hash、幂等与区域锁保护，保存新修订后原编译/音频manifest/批准沿既有逻辑失效。共享写作正本v6.1先固定商品实际用途及商家明确目的，再允许有依据的场景扩展；其他品类按自己的用途判断。当前任务整版重写第1版，不执行Agent逐句润色循环，不改第2版。
- 2026-10-21欢迎/评论实时插播：Room.queue_interaction/prepare_pending_insertion在主稿播放同时准备短音频，未ready继续下一主句，ready后只在自然ended切入，插播后恢复下一句；单槽busy跳过、过期/切品/切session/源版本变化不播，不新建第二队列。欢迎同时按准入和实际playing滚动60秒；昵称过滤在welcome.py。自己的抖音comment与test-events接comment_entry，live_reply.py复用原消费者BM25/DeepSeek引用校验，一次短回复后既有MiniMax；reply_lab带reply_turn_id的纯文字测试仍不自动播，防递归。唯一评论prompt限制只回答当前问题，不念后台配置或添无据场景。
- 正式主稿仍句级预录WAV，连续试听整段音频不等于正式句级播放；MiniMax网络分块不是句界。r27前三句真实循环+2欢迎/3回复已真实浏览器验证，供应商冷短音频3.2—9.3秒、总响应7.5—16.3秒，非亚秒/非真实抖音OBS验收。失败/边界/播放器测试与部署入口在Agent项目output/20261021-live-interaction/欢迎语与自动回复切换测试报告.md。Room日志新增毫秒观测，客户端PLAYBACK-BOUNDARY-1无正文/昵称/URL/凭据。

- 2026-10-21点赞新增规则覆盖此前互动全部busy丢弃的约定：欢迎仍滚动60秒；点赞不设全场60秒，`app/like_thanks.py`唯一负责按用户3秒静默/最多8秒封口合并、不同用户FIFO待感谢、原始本次count求和，禁止将total累计赞归属个人。Room仅持有该文本批次收集器，所有声音仍经原单一insertion句尾槽，不新增并行声音播放器。排队不因busy丢弃；仅实际插播槽可用才准备TTS，结束恢复主稿下一句。价格来自commerce正本，未知不报金额；切品/改价的未播音频退回重建，TTS失败明确记录failed_unconfirmed且不自动重复付费。ensure_like_worker同时处理重新入队和新场次交接；已started的暂停恢复不重复准入。stop取消旧场批次但保留事件去重。
- 本批已由真实播前Agent内部preview_script_audio合成完整r27，四模块原文连续WAV431.304秒，1次MiniMax请求、供应商公开用量4220；修订/本场/第2版未改、未批准正式整包。详情顶部音频卡可试听；上方“完整语音尚未生成”只适用于上一批记录。整段试听本身不支持精确句尾插播，正式播出仍用句级预录。短互动保持45秒HTTP等待上限，超过1000字符的整篇试听180秒；未知结果沿原operation查询，不自动重发。最新完整记录见Agent项目output/20261021-full-audio-and-likes/完整试听与点赞联测报告.md。

- 2026-10-22用户决定：正式话术（整版/模块）改用`DEEPSEEK_MODEL=deepseek-v4-pro`并开启思考`DEEPSEEK_SCRIPT_THINKING=enabled`、`DEEPSEEK_REASONING_EFFORT=high`；统一入口app/prelive/deepseek_options.py（思考时max_tokens+16000、超时300秒；未设置时保持原关闭思考/120秒）。实时欢迎与评论回复保持`DEEPSEEK_REPLY_MODEL=deepseek-flash`且关闭思考。播前Agent同时改用同一DeepSeek账户（见Agent项目AGENTS.md）。
- 2026-10-22晚：整版与模块话术请求改为流式接收（deepseek_options.py的`streamed`与`read_provider_body`），本地拼回非流式结构后走原校验；请求指纹（input_hash）仍按原body计算，大小上限只计答案正文。原因：开思考的非流式请求在本机约60秒被切断，记为timeout_unknown并触发对账停机。tests/test_module_generation.py中test_fact_reference_failures_are_not_swallowed失败、test_missing_key_or_oversize_context_never_sends长时间不结束，均在此改动前已存在。

- 2026-10-23连续主稿修复：`app/audio_timeline.py`负责供应商词时间戳归档与严格原文对齐；同一word_begin/word_end对应多音素时合并时间而不重复字词，标点释放时间归前句，禁止按字数估时间。`continuous_audio.py`是独立的人类采用门禁，绑定已采用publication、精确script_ref、试听回执、音频hash和timelinehash；试听批准本身不改变正式播出。既有正式manifest不原地改写，按一版显式采用覆盖；新稿/新整包不继承旧覆盖。当前只将网球第1版用用户本轮明确授权采用为连续音频，其他六版未重新合成。
- 播放仍由唯一VoicePlayer输出；`web/continuous-player.js`用同一AudioBufferSource跨普通句界连续播放，句子事件只是逻辑书签。互动音频提前解码并在音频时钟的已对齐句尾停止，ended携带冻结的boundary_choice；晚就绪互动留到下一句，server不能因晚到状态把已经继续的主稿拦腰切断。暂停保留绝对音频offset；其他版本仍兼容旧句级路径。AudioContext在明确用户点击时预解锁，MediaStream.play不得在启动首个source前阻塞等待；同一个srcObject不得每句重复赋值。
- 统一主播正本在prosody.continuous_host_style；整篇happy/speed1/pitch0/vol1，同一音色Friendly_Person；对应实时互动继承其真实参数但不请求字幕。情绪说明不冒充MiniMax自然语言角色prompt。参数统一通过不等于听感已获用户认可。
- WebSocket仅delta=1客户端按队列标识复用已收队列；进度发送state_delta，旧客户端保留完整state。连续时钟source_started/source_retained/boundary/scheduled安全诊断写room_log；无昵称、凭据和URL。音频文件hash可按路径+size+mtime_ns+ctime_ns+期望hash缓存，变动即失效，不取消开播与商品停售检查。
- 真实数据与验证在verification/20261023-continuous-audio。只调用一次MiniMax整版合成（436.922秒，38.329秒完成），额外短互动为实播测试；没有重写脚本或重复付费重发未知请求。第一轮首次授权等待故障已记录并修复；用户最终听感、真实OBS音轨和其他六版新连续音频仍是独立待验收项。

- 2026-10-24播放控制：页头提供暂停/继续、当前版本从头播放、结束直播；监控复用同一restartVersionWithSound。Room.restart_version仅回退当前连续occurrence的起点，保留队列/场次/长短开场和正式音频；expected_session与version绑定，普通换句不以epoch拦截，但跨版本仍拒绝。playback_seek_revision使连续引擎在准备期间立即停旧source，ready后真正seek，重播当前句同用该标记。结束只停止声播输出，不关闭抖音。验证在verification/20261024-playback-controls，测试不调用模型/TTS。

- 2026-10-24输出设备：工作台播放条直接显示“播放设备”选择器，默认自动跟随Windows默认；已保存的手动设备不擅自覆盖。audio-routing唯一负责浏览器deviceId与setSinkId，Windows设备仅按唯一同名映射，禁止把Windows端点ID当浏览器sinkId。系统默认变化/devicechange重读，解析到物理设备时显式设sink，避免浏览器按应用旧路由；权限不足保留浏览器默认并明确未匹配。指定设备断开暂停且提示，不静默切扬声器。商品详情/音色/任务样本试听通过registerMedia共用路由，不自动播。界面不把选择某设备称为OBS已连接；OBS需采集同一设备。验证verification/20261024-output-device。

- 2026-10-24首句卡住/有进度无声：支持AudioContext.setSinkId的浏览器正式连续音频改为source→gain→analyser→context.destination，所选设备通过同一routing.registerMedia的outputRoute适配器同步到Context，禁止同时再走MediaStream导致双播；非支持浏览器保留MediaStream兼容。createContext先用无物理输出时钟初始化，直接模式在unlock按所选sink切到实际输出。时钟实际推进才ack playing，3秒不推进/未启动停止并明确错误，明确用户重试可重建失败ctx。clock_confirmed/stalled记录白名单状态/时钟/graph_peak/output_mode，不能把source.start或图内信号当作最终听感。真实Chrome用户确认声音和进度正常、耳机会话电平正值，证据verification/20261024-first-sentence-stall。当前保持用户播出，未重合成音频。
