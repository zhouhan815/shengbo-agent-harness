# 声播播前业务服务 v2

这是声播进程内的业务后端，使用现有 `runtime/library.sqlite3`，商品仍是
`records(kind='product')`，直播间仍是 `records(kind='config')`。导入模块本身
不会读取密钥、调用模型或开播；`PreliveService(runtime_path, adapter)` 初始化
才会幂等迁移指定的数据库。迁移前须停止仍使用三列 `INSERT ... VALUES` 的旧进程。

## 可信入口

- `await service.execute_command(command, args, mutation, principal, scope=...)`
- `await service.query(query, args, principal=..., scope=...)`
- `create_router(service, principal_resolver)` 挂载 `/api/prelive/v2`。
- `principal_resolver` 必须由主应用从可信会话或服务认证中构造身份及 scope；
  请求体和模型工具不能提交身份。
- `COMMAND_ARGS`、`QUERY_ARGS` 和 `VOICE_COMMAND_ADAPTER` 位于共享
  `shengbo_contracts.business`，是 30 个命令、27 个查询的唯一输入合同。
- 回执遵循共享 `CommandReceipt`，JSON 内容 hash 遵循共享规范
  `sha256:`＋canonical JSON SHA-256；文件 hash 明确名为 `sha256_bytes`。

## 外部能力适配器

```python
async def generate_module(context: dict, module_key: str,
                          product: dict, previous: list[dict]) -> list[str]: ...
async def render_audio(text: str, room_id: str, voice: dict) -> dict: ...
async def start_publication(room_id: str, publication: dict,
                            compiled_entries: list[dict]) -> dict: ...
def voice_options() -> dict: ...
```

语音结果须包含实际 `runtime` 目录内 WAV 的 `path`、`audio_id`、`url` 和
`duration`。服务验证 WAV 及文件 hash；没有真实文件不得标记 ready。
开播结果须包含实际 `session_id`。`compiled_entries` 已展开 1/6/11 长开场的
有限超周期，并为每句附上已验证音频；播放器不能再次按旧话术全稿重建队列。
同一来源句可重复出现，播放 ACK 的身份必须同时包括 compiled entry/播放 attempt。

启动后调用 `service.wake_worker()` 收拢持久队列；关闭时
`await service.close()`。播放器真正停止后调用
`service.release_live_slot(room_id)`。首期使用单个声播进程和唯一后台协调器；
正式 MiniMax 音频按句批内并发，默认3路，环境变量
`PRELIVE_MINIMAX_AUDIO_CONCURRENCY` 仅允许1–3；脚本、试听和本机音频仍串行。
每批完成或取消收束后才重新扫描队列，保留每句批准、幂等、用量和提交记录。
这里没有宣称分布式调度能力。重启发现已发送但未结算的工作会标记 unknown，
禁止自动重复付费。

## 数据及人工门槛

短事务内统一核验 CAS、scope、grant、write generation、epoch、幂等操作，
再提交数据、不可变历史、回执和事件。网络生成、TTS、原始文件落盘在写事务外；
外部结果绑定准备任务前重新核验。撤权后迟到结果只能成为隔离审计产物。

主/子 Agent 的报告必须使用共享 `AnalysisReport`。Agent 私有材料必须先将
原始 bytes 导入 `import_blob`，再通过 `accept_analysis_report.source_manifests`
传送完整 manifest。声播核验原始 hash，产生正式 source ref，并保留原 PREPARED
报告 hash、完整旧稿及新 accepted hash 的 lineage；不把私有引用冒充业务正本。

文本→TTS 必须有当前整包人工批准。重排、改文案或音色会使整包批准失效；
缓存只复用实际必需句。候选发布不采用、不开播；采用和开播均是独立人工动作。
普通新商品 head 的相关差异由用户选择保留或刷新，停售和明确撤回始终阻断。
保留选择不会改变文本/音色/epoch，也不会使原有效批准失效。

模型不能调用的补充人工路由：

- `/human/product-identity`：同名商品的新增区分决定；
- `/human/product-control`：当前商品/修订/信息的明确撤回控制。

`legacy_script_projection(id)` 是现有声播页面需要的只读兼容形状，不产生
第二份可编辑稿件。旧整稿编辑入口必须对 `prelive_managed` 对象转为 v2 patch
或明确拒绝，不能写出独立旧稿副本。

## 已验证范围

`tests/test_prelive_service.py` 与 `tests/test_prelive_routes.py` 在临时目录、
实际 SQLite、真实 WAV 文件和可注入的 fake provider 下验证业务闭环；
不读取 `.env`、实际运行库或凭据，不调用供应商、不真实开播。
HTTP 用例需要 Agent 环境中的 httpx；缺少依赖时明确 skip。

改动影响首期按商品依赖保守列出，不伪造句级消费精度；没有逐句 ACK 关联的
话术使用年龄返回 unknown。素材清单是推荐/已有文件状态，未接视频生成。
费用结果未知只记录 unknown，不虚构零费用或自动重新付款。
