# ChatGPT 套餐模型适配

2026-10-16 实现。模型来源为 `chatgpt_plan`，默认目标 `gpt-6.1-sol`。

## 入口与边界

`build_chatgpt_model(model='gpt-6.1-sol', credentials=None, observer=None, *, before_send=None, on_request=None, on_response=None, transport=None, async_transport=None, timeout=120, max_input_chars=600000, verify_catalog=True)` 返回 LangChain `BaseChatModel`。构造本身不读凭据、不联网；第一次真实调用检查账户目录，10 分钟后再次检查，不改换模型。`transport` 是测试注入，不是可替换的生产API地址。没有隐式提供商/账户/计费回退。

- `observer(event)` 仅含 `type/attempt_id/provider/model/status/usage/code/http_status/outcome_unknown` 等安全元数据；事件为 `model.sent/model.completed/model.failed`。`model.protocol` 仅含 SSE 事件类型计数、字段名称及输出类型等安全形状，不包含文本、参数、认证或标识值。
- `before_send(payload)` 在任何凭据读取和网络之前收到最终完整请求体的副本，用于可信预算门槛；异常原样阻止发送。调用返回之后产生的目录检查和凭据异常不代表推理已计费。
- `on_request({'attempt_id','payload'})` 记录无认证的请求内容，保留全部框架指令、用户历史、工具定义及结果。去掉仅供模型重放的 reasoning 项；不做凭据字段名式的删减，以免破坏业务中叫 token 的普通字段。
- `on_response({...})` 保留完成态输出与工具参数和数值 token 用量，去掉所有 reasoning 项。回调须同步且不得执行不受控网络动作；由上层为并行调用绑定 run/task 相关元数据，并负责安全业务载荷存储。
- `invoke/ainvoke/stream/astream/bind_tools` 已实现。上游始终 SSE；`stream/astream` 缓冲到 `response.completed` 才返回一次完成 chunk，避免断流文本被当作最终成果。用户的过程面板来自工具、任务与中间件事件，不冒充模型内部思维。

## 凭据

复用独立连接工具 `Vault/DPAPI`，不缓存记录，不重建安装标识；正式接管前必须停止旧连接测试服务，因为旧服务不参与本锁。每个会话使用同一 `refresh.lock`，同时持有进程内锁与 OS 跨进程文件锁，锁内重读。临近过期自动刷新并原子保存替换后的记录；刷新前先保存加密的 in-flight 标记。网络返回未知、进程退出、旋转后保存失败时禁止自动重放旧 refresh token，保留资料并要求重新授权/人工排查。明确 429/5xx 最多两次；无原始响应异常输出。

`CredentialManager.status()` 是安全状态查询；无账户 ID、邮箱、安装标识、令牌或存储路径。真实 access token 仅在私有变量和请求认证头中存在，模型 Pydantic 字段、checkpoint、日志回调中均没有认证信息。

## 为什么使用小型适配器

已核验已安装 `langchain-openai=1.7.2`、`openai=3.31.0`。其 Responses 与 namespace 类型可用，但默认工具转换为顶层平铺 function，而本路由要求 namespace/additional_tools；普通可选参数也不保证预览路由白名单、完整流终态与动态凭据续期。这里仅实现模型线协议，不替换 Deep Agents Harness。参数白名单拒绝 temperature/max_output_tokens/previous_response_id 等不支持控制；system 全部转换 developer，全量自携历史。业务工具在本地执行，function_call 的 call_id、name、JSON args 与 ToolMessage 对应。没有托管 MCP/Code Interpreter/tool_search 转换。

## 已验证与尚待验证

`tests/test_provider_chatgpt.py` 的 20 项网络隔离测试通过，覆盖线程与真实子进程锁竞争、旋转保存、未知刷新、权限撤回、协议参数、工具往返、sync/async/stream、断流/失败/未完成、私密字段不进回调、最终预算门槛，以及真实 Deep Agents SDK 加 mock 网络执行本地工具。完成事件的 output 为空时，仅在已收到 `response.completed` 的前提下重组已收集的 output_item/text/arguments 事件；断流仍不算成功。商品截图仅支持限定 PNG/JPEG/WebP data URL，图片不按base64字符数误算文本预算，图片另有10MiB字节硬限制；本地路径与远端URL被拒绝。`payload_text_chars(payload)` 可供上层文本预算使用，视觉成本仍需上层单独计量。

真实授权调用探针在 `tests/probe_provider_live.py`，只有显式 `--confirm-live` 才调用。2026-10-16 在根任务已停止旧测试服务并授权后，执行一次工具往返探针：两次真实模型调用均成功，执行一次本地加法工具，最终答案验证为 42；协议诊断只保存安全形状。另一次完整 RuntimeEngine 只读探针的两次真实调用及预算、中间件、工具注入、trace 均成功，但任务因模型提供的授权引文未逐字匹配用户指令而被 Harness 拦截，没有副作用；该次不作为端到端任务完成证据。没有进行长期真实续期或故障期间的旋转测试。后续完整业务验收由根任务统一执行并记录结果，避免并行消费账户调用。

官方核验来源：

- https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations
- https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions
- https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference

注意：当前 `profile.max_input_tokens=128000` 是本适配保守预算上界，不是对账户模型上下文容量的官方声明。实际账本仍须按最终请求体执行应用预算。

## 最终集成更新（2026-10-16）

修复可纠正工具输入、摘要阈值与完成后直接结束后，完整RuntimeEngine真实只读探针5次请求全部成功，状态COMPLETED、acceptance_ok=true；原始上下文与预算检查、工具执行及安全payload记录均实际运行。该测试使用隔离空业务库，不等于真实商家完整播前流程或长期OAuth续期已观测。此前探针的BLOCKED记录保留为故障修复过程。
