# ChatGPT授权模型来源与后续接入记录

合同基线：PRELIVE-2.3。记录日期：2026-10-16。阶段：**只保存设计与交接，尚未实现模型适配或自动续期。**

## 1. 本轮决定与证据边界

用户已明确选择“先保存到设计，构建Agent时再实现”。未来声播播前Agent的模型来源采用`chatgpt_plan`，首个目标模型为`gpt-6.1-sol`，框架继续使用Deep Agents。本轮不接管现有连接服务，不读取/解密凭据，不运行模型测试，不改业务代码。

| 项目 | 当前记录的事实 | 证据性质 |
| --- | --- | --- |
| 本机授权 | 已完成Sign in with ChatGPT，已取得套餐使用权限 | 用户说明及独立连接工具交接报告；本轮未重新登录或探测 |
| 真实短文本 | `gpt-6.1-sol`调用已通过，交接报告记载`response.completed` | 已有连接测试的历史结果，不等于业务Agent验收 |
| 本地测试 | 交接报告记载20项通过 | 本轮未复跑，不能计入本窗口新增测试数 |
| Deep Agents业务实现 | 现有项目仍为SDK准备环境，业务Harness待实现 | 与00/02当前设计状态一致 |
| 后续适配 | 自动续期、工具往返、多轮、摘要、恢复、并发和失败处理 | 待未来实施及验收 |

“授权已好”不表示所有模型、额度或协议永久可用。未来启动/选模时按当前账户目录和实际错误检查；不可用时给出明确状态，不静默切到其他账户、普通API计费或DeepSeek。保留旧provider设计供明确选择，不能默认回退。

## 2. 后续开发必须先读的本地入口

| 资产 | 精确位置／用途 |
| --- | --- |
| 原始接入交接 | [给Agent开发窗口的接入说明](<D:/Codex project/shengbo-chatgpt-connection/给Agent开发窗口的接入说明.md>)；本册是索引与设计决定，不替代原文 |
| 独立连接代码 | `D:/Codex project/shengbo-chatgpt-connection/connection.py`；未来阅读Vault、DPAPI、身份校验、续期和SSE逻辑，不每次调用新建App |
| 加密凭据位置 | `C:/Users/lizhe/AppData/Local/ShengboChatGPT/connection.dpapi`；仅记录定位，不复制内容到本设计 |
| 安装标识位置 | 同目录`host.json`；沿用既有安装标识与注册，不能随机重建；本册不保存其值 |
| 既有无秘密验收 | 连接工具目录`.runtime/verification.json`及`连接排障结论.md`；未来按需核验 |
| 模型开发项目 | `D:/Codex project/shengbo-ops-agent`；解释器`.venv/Scripts/python.exe` |
| 声播业务应用 | `D:/Codex project/ecommerce-voice-agent`；TTS和文案服务不因本决定自动切换 |

连接工具通常在`127.0.0.1:18791`，实际位置以其`.runtime/server.json`为准。**该端口只是授权/连接测试页，不是OpenAI兼容推理代理，不可填为模型base_url。** 本轮没有读取运行状态或停止它。

## 3. 模型来源、Harness与声播服务的边界

| 用途 | 未来模型配置决定 | 本轮是否已接通 |
| --- | --- | --- |
| 主Agent、三类分析Agent、SDK摘要等Agent模型请求 | 明确选择`chatgpt_plan / gpt-6.1-sol`；通过同一模型工厂与受保护凭据管理器 | 否，仅保存选择；正式工具和摘要兼容待验证 |
| 声播按冻结计划生成/修改话术 | 沿用既有DeepSeek业务服务设计；是否替换由独立配置决定 | 本轮未改动 |
| 语音生成 | 本机普通话／MiniMax | 本轮未改动；不从OAuth推断语音权限 |
| Harness | `create_deep_agent(model=BaseChatModel兼容对象)`，沿用M1—M9、S1—S4和现有任务/权限/恢复机制 | 不改为Codex SDK或再嵌套完整Codex Agent |

模型工厂应按`purpose/provider/model`选择实例；同一provider对象可以共享安全凭据服务，但每个调用保留自己run/branch/purpose的预算和trace身份。SDK摘要必须显式使用已选来源，不能隐式回到SDK默认供应商。未来先核验锁定langchain-openai能否满足要求，必要时才补最小BaseChatModel适配，不在本轮预先宣称某个现成类可直接兼容。

## 4. 未来凭据接管与自动续期要求

以下为实施约束，不是已完成动作。沿用原应用的合法注册、账户身份、安装标识和DPAPI存储，不要求用户复制令牌。

1. 实施前先读原交接和无秘密状态，明确当前刷新者；正式接管时按原文停止独立测试服务，停止服务不撤销授权。
2. 只有一个凭据刷新者。Agent数据根OS锁只保护Agent进程，不能证明独立连接工具已停止；同会话必须跨进程协调。
3. 专用CredentialManager在锁内重新读加密记录，临近过期时向官方OAuth端点续期，先原子保存替换后的凭据再供内存请求使用；不可用旧缓存覆盖新记录。
4. 短暂网络故障保留凭据并有界退避；明确失效/撤销则停止请求并提示重新授权。无论哪种错误，都不自动换账户或计费来源。
5. 凭据不进入Settings公开序列化、Agent state/checkpoint、提示词、工具参数/结果、业务库、trace、导出或版本库。模型请求日志不采集Authorization/Cookie等认证头；凭据服务异常只输出安全错误码，不能包含原始token响应。

会话续期与轮换的官方依据：[Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)。只适用于本机同一Windows用户可合法解密的环境，不能假定WSL、云端或其他用户可直接使用。

## 5. 协议适配与原有设计的差异

| 接口边界 | 后续实现需满足 |
| --- | --- |
| 推理传输 | 公共`https://api.openai.com/v1/responses`，OAuth bearer由后端注入；`store=false`、`stream=true` |
| 消息上下文 | 保留LangChain/Deep Agents全部必要指令、Skills元信息及业务约束；provider边界转为该路由支持的instructions/developer形式；每次自行提供必需历史 |
| 工具 | 本地函数通过受支持的namespace或additional_tools表达；稳定映射AIMessage.tool_calls、ToolMessage、call ID和参数；实际执行仍在本地Harness |
| 不支持的参数 | 不照搬DeepSeek的temperature/max_tokens为temperature/max_output_tokens，也不发送metadata/previous_response_id等该路由禁用项；实施时重新核验官方限制 |
| 完成判定 | 读到`response.completed`才成功；failed/incomplete/断流分别处理；HTTP 200或开始出字不足以证明成功 |
| 预算与trace | M6检查转换后的最终请求；原4000输出限制仅适用于支持它的provider，不能宣称该OAuth路由有同样硬上限。保留实际usage或unknown，区分LangChain输入与最终安全载荷 |

不把本地业务工具换成API托管MCP、file search、Code Interpreter或tool_search。工具/SSE/结构化结果等细节需以实施时锁定SDK和最小探针为准；本册没有完成它们的编码设计或兼容承诺。

官方核对入口：[Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)、[Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)。这些协议文档本轮已查阅，账户授权成功事实则来自本地交接，二者不混同。

## 6. 未来实施顺序与验收出口

| 阶段 | 工作 | 验收证据 |
| --- | --- | --- |
| A 接管 | 唯一刷新者、DPAPI管理、原注册保留 | 假凭据/假网络竞争测试先通过；没有明文输出，没有双刷新者 |
| B 适配文本 | 正式模型工厂返回BaseChatModel兼容对象 | 少量真实短文本非空，当前模型/安全终态可查，独立于旧连接测试标志 |
| C 工具往返 | 无副作用本地测试工具 | 调用ID/结果关联正确；工具恰好按契约执行；最终回答使用结果 |
| D Deep Agents | 主/子与摘要配置、sync/async及所需流式路径 | 真实SDK调用可用，框架指令和工具未丢失，无隐式供应商回退 |
| E 续期恢复 | 串行轮换、重启、超时/失败/断流 | 不覆盖新凭据；未知结果不假成功；真实续期仅在官方允许时机验证 |
| F 业务集成 | 再接现有业务工具和M1—M9 | 继续通过05的权限、版本、审批、恢复、结果验收 |

阶段A—F均待实施。当前仅新增交接记录和设计关联，没有新增运行Skill、中间件、adapter、CredentialManager或测试成功数。

## 7. SDK信息与后续读取顺序

原交接记录SDK：deepagents0.7.25、langchain1.4.6、langchain-core1.6.10、langgraph1.2.15、langchain-openai1.7.2、openai3.31.0、httpx0.28.1、PyJWT2.15.1、cryptography50.0.2。此处作为交接时版本记录，本轮未重新安装/升级或复测；未来实施须再读取实际版本与官方文档。

下一次构建顺序：本册→原始接入交接→00/02/05及当前修订→ecosystem-primer/deep-agents-core→实际SDK/源码与官方协议核验→从阶段A实施。无需用户粘贴令牌；需要重新授权时也走合法授权流程，不索取明文。
