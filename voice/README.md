# 声播 · 播前 Agent 工作台

默认首页为 Agent 主体的网页工作台：右侧完整对话负责发出播前准备任务，左侧展示和微调商品资料、顾客研究、本场配置、模块话术及播放顺序。点击或键盘进入哪侧，哪侧放大；草稿、滚动和选区保留。声播原控制台作为播出执行端保留在 `/studio`，不再作为 Agent 首页。

## 打开应用

日常使用：双击桌面的 **声播** 快捷方式。它会自动启动本地服务，确认就绪后打开默认浏览器；重复点击会复用已运行的服务。电脑重启后仍可直接双击使用。启动故障记录在项目的 runtime/launcher.log 和 runtime/server.desktop.log。


Agent 工作台：http://127.0.0.1:8765/

播出执行控制台：http://127.0.0.1:8765/studio

OBS 播出链接：进入某个直播间并开始直播后，在监控页复制该房间的链接。不要预先固定使用 default 窗口。

**音频也可不复制链接**：进入直播间，在左侧顶部“音频连接（可选）”选择本机播放或连接 OBS / 直播伴侣。设备选择保存在当前浏览器；首次需在 OBS 添加对应音源。详见 [音频连接 OBS 使用说明](docs/音频连接OBS使用说明.md)。

仅本电脑访问。服务启动后在浏览器中使用；电脑重启后需重新启动后端。

启动已有环境：在项目目录运行 `启动应用.ps1`，它会检查已有服务，否则在后台启动。需要看终端日志时运行 `start.ps1`，或：

```powershell
Set-Location -LiteralPath 'D:\Codex project\ecommerce-voice-agent'
& '.\.venv\Scripts\python.exe' -m uvicorn app.server:app --host 127.0.0.1 --port 8765 --no-access-log
```

新机器安装：Python 3.12，`python -m venv .venv`，再运行 `.venv\Scripts\python.exe -m pip install -r requirements.txt`。本机普通话模式需 Windows 安装 Huihui 普通话音色。无需 ffmpeg；云端声音也由浏览器播放 WAV。

## 第一次体验

1. 双击桌面“声播”，打开左右双区 Agent 工作台。右侧输入要求、上传材料；左侧检查结果。过程开关默认关闭，打开查看主/子任务、脱敏工具参数和实际结果。
2. 商品库统一维护长期资料；直播间选择引用商品。从“直播间”入口进入左侧卡片，选择直播间并创建或恢复本场，左侧配置主题、优惠、声音等；当前对话保留原任务范围，切换左侧不自动改会话。
3. 点击某字段或话术模块旁的“让 Agent 修改此项”，再在右侧输入要求。普通点击和选中文字不会指定修改范围；存在未保存修改时，发送前选择保存、放弃或返回编辑。
4. 左侧可以直接保存商品、配置与话术、排序和重排。点击 Agent 的成果入口，左侧定位对应结果；后台消息不抢焦点，自动跟随默认关闭。
5. 核对整包文案并批准语音，再准备音频、生成候选并人工采用。本场采用不等于开播；播出执行台和 OBS 来源继续使用原声音人后端。
6. 未保存编辑保留本机恢复缓冲，刷新后需主动恢复；服务端版本变化先检查差异。错误和未知提交结果保留输入，不静默覆盖或重复计费。

原直播间配置、抖音数据接入调试、评论测试、任务及评测已补进左侧；`/studio` 继续保留旧入口，其历史功能与接口说明见以下章节。首期不自动运行播中、播后 Agent。

## 在 OBS 中测试

新增“浏览器”来源，填监控页复制的 OBS 播出链接，建议画布／来源为 1080×1920；横屏也会保持竖屏居中。通过 OBS 的“交互”窗口操作首次声音启用或“接管播出声音”。OBS 专用链接连接后自动接管声音，本机试听停止；监控内嵌预览始终静音。若需切回本机，在监控页点击“切回本机声音”。

先做本地录制，确认音频和画面都被 OBS 捕获且没有重复音源，再按自己的抖音直播软件权限测试推流。OBS 文档说明浏览器来源能加载网页画面和声音：[官方 Browser Source 文档](https://obsproject.com/kb/browser-source)。本次没有登录抖音或实测 OBS 音频路由。

## 配置与存储

- `.env`：DeepSeek 与 MiniMax Key，只由后端读取，已加入忽略规则。不要把此文件放进作品展示或上传仓库。
- `runtime/library.sqlite3`：商品、脚本、配置；服务重启后保留。播放状态在重启后回到未开始。
- `runtime/room_audio/<直播间ID>/`：本直播间已生成的 WAV 文件，按服务／模型／音色／文本识别；配套句子清单保存在数据库的 `room_audio` 记录。常规开播只播放这些已有文件。
- `runtime/audio`：音色试听和现场插播缓存；旧缓存可以在点击生成语音时复用并存入对应直播间。
- 数据库 `job` 记录：后台生成任务、商品与版本进度、完成结果和失败原因；`viewer_event`、`room_log`：评论事件和房间执行历史；`reply_session`、`reply_turn`：测试聊天、回答、失败和检索依据；`rag_documents`、`rag_chunks`、`rag_manifest` 表：本地检索索引。
- `runtime/events.jsonl`：执行日志；页面可导出本场记录。
- 本机声音：RealtimeTTS.SystemEngine + Windows SAPI，免费、离线合成。
- MiniMax：HTTP T2A，验证通过的 `api.minimax.io` 国际接口；首次合成和新插播消耗账户额度。本机模式不自动切换成云端，云端失败也不会悄悄更换声音。

## 文档与验证

- [抖音声音接入、弹幕真实验证与融合方案（2026-10-15）](github-research-output/douyin-io-20261015/phase6_blueprint/final_report.md)：公开房间45秒收到29条评论；包含OBS/虚拟音频线方案及后续生产接入契约。
- [评论测试台与本地持久化 v1.5](docs/评论测试台与本地持久化_v1.5.md)（最新功能、保存范围、接口和验证记录）
- [直播间配置与预生成语音修复](docs/直播间配置与预生成语音修复_v1.2.md)（当前 UI 与开播流程）
- [话术提示词优化与量化评测](docs/话术提示词优化与量化评测报告_v1.3.md)（原提示词、多轮实验与限制）
- [最终输入与两版完整话术](docs/最终输入与两版话术_v1.3.md)（实际 DeepSeek 输出）
- [直播间流程重构与测试记录](docs/直播间流程重构与测试记录_v1.1.md)（历史版本）
- [交付与测试报告](docs/交付与测试报告_v1.0.md)
- [运营 Agent 构建与开源复用报告](docs/运营Agent构建报告_v1.0.md)
- [原研发方案 v0.2](docs/研发方案_v0.2.md)（历史规划；实际状态以交付报告为准）
- [样本拆解与监控规范](docs/样本拆解与监控规范_v0.1.md)

运行自动测试：`.venv\Scripts\python.exe -m unittest discover -s tests -v`；播放器回归：`node tests/test_audio_player.cjs`。

前端自动保存回归：`node tests/test_persistence.cjs`。真实回复接口小样本测试：`.venv\Scripts\python.exe tools/verify_reply_lab.py`（使用已配置 DeepSeek 额度，在临时数据库副本中运行，结果存入 `docs/评论回复测试_v1.5/`）。

当前是可运行的研究与求职展示原型：具备声音播出和动作入口，已接入抖音公开网页直播间的真实评论、互动和人数候选快照；这些快照尚未与主播后台核对，不能视为完整经营数据。订单、GMV 和平台购物车尚未接入，真实评论不会自动调用问答或插播。话术事实引用与规则检查不能替代人工审稿；时长以实际音频为准，不保证每版6–10分钟。

## 商品与直播间配置 v1.6（2026-10-15）

依据[客户端/网页端功能优化版本1.2 PRD](https://fancyjing.feishu.cn/wiki/JFpDwKI1qiXnadkvZQscYO0rn2b)的商品、总直播间、单商品直播间配置项适配声播。读取的文档 revision_id 为 402；原始资料保存在本机 `runtime/prd-reference-20261015/source.json`，不上传公共仓库。

**归属：商品库统一保存长期商品事实，直播间以 `product_ids` 引用商品，`commerce.product_overrides[product_id]` 只保存本场覆写。** 脚本的 `product_snapshot` 是已有的历史生成依据，不是直播间内另建商品库。

| 归属 | 配置项 / API 字段 |
| --- | --- |
| 商品身份 | `sku_id`（可输入，留空自动分配且不可重复）、`name`（去首尾空格，1–60字）、`sale_status`（`available` / `unavailable`）、`image` |
| 商品日常事实 | `price`、`currency`、`specifications`（按顺序的 `{name,value}` 列表）、`material`、`ingredients`、`target_audience` |
| 商品服务与储存 | `shipping_origin`、`shipping_time`、`return_policy`、`storage_conditions`、`shelf_life_value`、`shelf_life_unit`（`day/month/year`） |
| 商品知识 | `core_selling_points`（有序卡片，至少1条；同步到兼容字段 `selling_points`）、`usage_instructions`、`faq`、已有 `knowledge` 与 `knowledge_documents` |
| 总直播间 `commerce` | `scene`（本场场景与口播要求）、`coupon`、`promotion`、`promotion_scope`、`promotion_product_ids`、`knowledge_documents` |
| 全场优惠 `commerce.coupon` | `mode`（`none/discount/coupon`）、`discount`、`amount`、`currency`、`minimum_spend`、`condition`、`scope`（`all/selected`）、`product_ids` |
| 单商品覆写 `commerce.product_overrides[product_id]` | `mode`（`none/discount/price`）、`discount`、`price`、`exclusive`（默认开启）、`promotion`、`gifts`、`shipping_override`、`shipping_time`；SKU 只读取所引用商品 |

页面配置流程为：设置声音 → 商品库选品（每件商品有“本场配置”）→ 本场优惠与知识 → 生成话术 → 话术顺序与语音 → 抖音接入调试。原开播、逐句监控、本地语音文件与 OBS 输出流程保留。

后端约束与生效方式：

- 优惠使用 Decimal 并保留两位小数，单品优惠先于全场券计算。`exclusive=true` 时单品优惠独占；关闭则允许继续叠加全场券。优惠价高于日常售价、非法折扣、跨直播间商品引用会拒绝保存。
- 报价是**单件**报价。金额门槛根据单品优惠后的金额判断；多件凑单不在这次价格查询范围内。币种不一致不会直接相减或自动换汇。自定义会员等条件、文字与数值门槛/面额冲突时 `confirmed=false, final_price=null`，不会猜测到手价。说明里“满100减10”可以识别为金额条件，其他复杂条件需要人工确认。
- 话术生成读取本场生效价格、促销和场景，以及最新商品名称、规格、材质、成分、人群、卖点。使用说明、FAQ、知识文档保留给问答检索。改变生成所用事实或本场规则后，旧稿及语音仍保留，但必须重生成后开播；单改问答文档不使普通新稿失效。
- 问答检索按直播间隔离，P1 本场结构化价格/发货优先于 P2 商品默认。覆盖前后的来源会标出 `scope/source_level`。本场证据按持久化配置重建，不把本场优惠写进共享商品索引；旧文档中的冲突价格、发货片段不会覆盖结构化规则。场景文本和文档不具有直接修改价格的权限。
- 不可售商品不能新加入直播间、生成脚本、进入问答推荐或重新开播。正在播放的当前音频不被强行截断，但下一句读取时会停止不可售商品。该状态与“从商品库归档”相互独立。
- 旧商品的 `promo_price/promotion` 为兼容数据保留；旧直播间第一次读取时迁移为它自己的单品覆写，不清空原数据。新直播间的优惠应在本场配置中维护。普通旧客户端更新不会把未提交的新字段清空。
- 声播直播间是可复用的持久配置，不按公司 PRD 在结束一场后清除知识/优惠。每次开播仍有独立 `session_id`，问答会话与历史日志保持原有隔离。

后端接口（均为本机现有服务）：

- `GET /api/commerce/schema`：商品与直播间业务配置的 JSON Schema。
- `POST /api/products`、`PUT /api/products/{id}`：结构化商品保存；必填名称、日常售价、核心卖点，状态默认可售。
- `POST /api/rooms`、`PUT /api/rooms/{id}`：保存直播间及嵌套 `commerce`；普通声音/名称更新不清空业务配置。提交部分 `commerce` 一级字段时保留未提交字段；传 `product_overrides` 时按整张映射替换，传某个 `coupon` 时按整个优惠配置替换。
- `GET /api/rooms/{id}/commerce`：本场生效配置、各商品报价、是否可售与发货时效。
- `POST /api/rooms/{id}/price-preview`：`{product_id, commerce?}`，可预览未保存配置且不落库；返回 `daily_price/base_price/final_price/confirmed/reason/steps`。
- `GET /api/rooms/{id}/reply-knowledge`：含本场文档、来源层级和生效报价；既有评论测试接口继续使用此配置。

本次未引入：数字人形象、直播间装修、品牌实体管理、TikTok/Shopee OAuth 同步、Excel/Word 批量商品导入、PDF/DOCX 自动解析、限时秒杀/库存计数、全平台合规词库。其中文档正文与验收案例存在范围差异，特别是品牌必填、秒杀及店铺授权；没有将这些公司平台能力伪装成声播已支持的配置。上传说明/FAQ可先粘贴文本，原有商品 TXT 文档导入仍可用。

验证：142 项 Python 测试；8项前端持久化检查；5项播放器检查；另通过7组真实 HTTP/服务重启检查，覆盖商品回显、129/119价格仲裁、房间隔离、非法输入、结构化RAG、服务重启与不可售阻断。验证不调用收费模型，不修改正式数据库。运行：`.venv\Scripts\python.exe tools/verify_commerce_api.py`；结果保存于 `runtime/prd-reference-20261015/http-verification.json`。修改前代码与SQLite备份位于 `runtime/backups/prd-config-20261015/`。

## 表单交互细化 v1.7（2026-10-15）

商品编辑按基础信息、规格卖点、商品属性与服务规则分组。商品主图采用140px方形上传框，支持立即预览、更换和移除；币种前置，价格、状态、单位采用紧凑控件，保质期提供显式加减按钮。长描述仍保留多行输入空间。标题与保存栏固定，900px、600px窗口下表单未出现横向溢出。

可选配置支持移除与重新添加：商品的 `hidden_optional_fields`、直播间的 `commerce.hidden_sections` 持久记录收起状态。移除后保存会清空对应事实/规则，避免隐藏字段仍影响脚本或问答。保存前重新添加商品字段可恢复当前编辑中的内容；保存后重新添加的是空字段。必填名称、日常售价、状态与核心卖点保留。

全场优惠按折扣/优惠券模式展示相关输入，提供“计算本场金额”按钮与逐商品结果表；单商品弹窗保留“计算券后价”。计算读取当前草稿，不由按钮代为保存业务配置；新选择的商品需先保存选品。修改条件、选品或商品资料后旧结果失效，待确认条件显示“待确认”，不当作0元。

本次验证使用独立数据库副本，未修改正式商品或优惠。浏览器验证了图片上传/重开/删除、保质期加减、可选项移除持久化与恢复、模式切换、107×0.85=90.95的可叠加结果、互斥时107元、额外会员资格待确认、结果失效及窄窗口布局。Python测试149项通过；前端持久化8项与播放器5项检查通过；真实HTTP与重启验证7组通过。截图位于 `docs/screenshots/ui-refine-20261015/`，修改前备份位于 `runtime/backups/ui-refine-20261015/`。

## 抖音接入调试 v1.8（2026-10-15）

每个声播直播间可以分别配对一个抖音网页直播间，在开播前测试真实数据接入，不需要开始声音播放。配置页的“持续采集”和正式监控页的开关控制同一条后台采集任务，不会各建一份采集。

使用步骤：

1. 进入某个声播直播间，打开第六步 **抖音接入调试**。
2. 输入完整公开网页链接，例如 `https://live.douyin.com/505233570304`，也可仅填其数字部分 `505233570304`。这里是**网页直播间号，不是抖音用户 ID、账号主页号或采集协议内部 roomId**。手机分享短链接需要先在浏览器打开，取得对应的网页直播间链接。
3. 选择“公开直播间调试”或“自己的直播间”，保存来源，再开启“持续采集”；开启时也会保存当前输入。测试别人的公开房间时保留调试标记，避免当成自己的经营数据。切换来源前先关闭采集。
4. 观察“正在连接 → 正在确认直播状态 → 已连接抖音”和逐条事件。开关开启、本机插件连接成功均不代表已经连接上游。未开播时保持等待；没有事件时不会填充模拟数据。事件区可按评论、进场、点赞等类型筛选，页面保留最近 200 条。
5. 切到正式直播监控后可使用同一开关，或返回调试页查看详情。切换页面、刷新或关闭网页不会终止后端采集；声音播放暂停、结束也不会自动关闭采集。需要停止时手动关闭开关。
6. 关闭并重启本地后端后，配对来源与历史记录仍在；采集任务不自动恢复，需手动重新连接。若只是关闭浏览器而后端仍在运行，采集会继续。

采集与展示范围：

| 类型 | 当前支持与解释 |
| --- | --- |
| 评论 | 用户昵称、评论内容、上游消息 ID、平台时间与本机接收时间 |
| 进场 | 收到的 `WebcastMemberMessage` 成员事件及原始动作描述；进场事件条数不是当前在线人数，也不是去重访客数 |
| 点赞 | 点赞事件、上游本次数量及累计值；事件条数与点赞次数分别记录 |
| 礼物 | 礼物名、原始数量和连击相关字段；不汇总连击为收入或 GMV |
| 社交互动 | 保留原始动作类型，尚未校验关注/分享枚举，因此统一显示“社交互动” |
| 人数快照 | 展示上游在线人数候选值、累计访问候选原文、对应字段口径及更新时间；`verified=false`，待与自己的主播后台核对 |
| 统计展示 | 保留上游展示文案，不把未明确口径的数字解释为在线人数或成交数据 |

断线、切换采集批次或快照超过 30 秒后，指标会标为历史/过期，缺失值不会显示成 0。网络中断时最多自动重试 5 次，耗尽后可手动重连。服务端报“采集连接结束”只代表上游连接退出，需继续确认直播状态，不能据此断言主播真实下播。

本地持久化：

- `runtime/library.sqlite3` 的 `live_source_config` 记录保存每个声播房间配对的外部房间、用途和采集开关意图。
- `live_source_events` 表保存结构化事件与递增游标，`live_source_totals` 保存各类型的历史事件条数；按声播房间和外部房间分别查询。具有有效上游消息 ID 的事件按房间、方法和消息 ID 去重，无 ID 的事件不伪造去重依据。
- 互动同步保存到 `viewer_event`，人数快照与展示统计保存到 `live_metric`；真实来源标为 `douyin`，包含外部房间号及 `capture_id`。不同声播直播间、不同配对来源和模拟事件保持可区分。
- 前端每次增量请求最多返回 200 条，显示最近 200 条；这是显示窗口和单次查询限制，后台历史不因此截断。关闭开关或重启不会清除历史。
- 插件配置临时文件位于 `runtime/live_source/collector.yaml`；私有服务认证密钥仅在后台进程环境中传递，不写入前端或该配置文件。

声播接口（使用现有本机服务）：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/rooms/{id}/live-source` | 返回来源配置、真实连接状态、历史/本次事件计数、候选指标及最近事件 |
| `PUT /api/rooms/{id}/live-source` | 保存 `{room, purpose, enabled?}`；`purpose` 为 `external_test` 或 `own_room`。保存配置本身不等同于启动任务 |
| `POST /api/rooms/{id}/live-source/connect` | 使用已存配置或传入的 `{room, purpose}` 开启/重新连接后台采集 |
| `POST /api/rooms/{id}/live-source/disconnect` | 关闭采集，保留来源配置及历史 |
| `GET /api/rooms/{id}/live-source/events?after={cursor}` | 增量读取结构化事件，返回 `next_cursor`；每次最多 200 条 |

实现使用 [jwwsjlm/douyinLive](https://github.com/jwwsjlm/douyinLive) **v2.3.1**，MIT 许可证。编译后的 Windows 采集器及许可证、来源/哈希清单位于 `vendor/douyin-live-collector/`；本地补丁只将监听限制为 `127.0.0.1`，声播使用随机私有 Bearer 认证连接。后台按需启动采集器，最后一条采集任务停止时释放进程，不要求用户另开插件窗口。当前按匿名网页协议接收公开事件，平台登录限制、未开播、网络或协议变化均可能导致暂时无法接入；不会绕过验证码或自动获取用户登录态。

本版只读采集，不向抖音发评论、点赞、礼物，不自动调用 DeepSeek、TTS、插播或运营 Agent；不读取订单、成交额/GMV、转化率及主播私有经营面板。声音推流仍按前文 OBS/直播软件方案单独配置，采集开关不负责推流。

验证：**172 项 Python 测试、8 项前端持久化检查、5 项播放器检查通过**。真实 HTTP 集成验证使用独立临时数据库和测试服务，2026-10-15 对公开房间 `505233570304` 观察约 25.5 秒，收到 64 条结构化事件：8 条评论、30 条进场、6 条点赞、11 条社交互动、5 条人数快照、4 条统计展示。测试同时覆盖来源隔离、非法链接拒绝、停止释放、历史保存与后端重启后手动重连；无 LLM/TTS 调用、无平台写入，不修改生产数据库。该实测不保证房间始终开播或未来协议始终可用。

复核命令：`.venv\Scripts\python.exe tools/verify_live_source_api.py --room 505233570304 --seconds 25`。真实接入证据保存于 `runtime/douyin-source-verification/http-real-result.json`；不带 `--room` 的普通接口检查结果保存于 `runtime/douyin-source-verification/http-result.json`（4 组 smoke 检查），二者分别保存。真实接入界面截图为 `docs/screenshots/douyin-source-20261015/debug-real.jpg`。
