# 声播播前 Agent

首期播前运营 Harness。Python 3.12 + Deep Agents 0.7.25；主 Agent 按任务选择工具与三类并行分析，不要求每次从头跑完整固定流程。

## 2026-10-17 第一版修复

`src/shengbo_prelive/workflows.py` 提供七个可选轻量工具流：资料与商品、分析协调、上下文汇合、计划执行、编排审核、差异语音、候选发布。主Agent仍可选择原有工具、按需并行、补研究、调整计划或提前完成，不存在强制跑完七步的总流程。流程返回缺项、等待或需重规划时由主Agent决定下一步。

- 报告默认只重复返回新增/变化的全文；`report_mode=full`或精确引用可以完整回读。读取记录不代表语义验收。
- 主Agent可在 `finish_turn` 中明确 `wait_for_branch_ids`，自己决定利用部分成果继续，还是等待已选依赖全部完成。
- 话术只播所选的一种开场、介绍、卖点、可选活动、促单。保存位置1/6/11长开场，其余短开场；循环沿原队列，重排只补真实缺开场，历史内容保持可读。
- 文案模块及供应商返回分别落盘；已知失败只补缺项，未知结果先对账。可选retry受原任务、原操作回执、精确selection、版本、批准和次数约束，不为旧任务追加权限。
- 供应商公开用量先记录再验收内容；畸形工具参数不采集原文。工作流每个内部步骤、实际请求和耗时单独留证。
- 确认卡片的商品、方案标签、修订、顺序和音色来自业务正本，模型说明不替代实际批准。

真实多轮验收入口（会使用已授权账号和DeepSeek额度；仅本机语音，不采用或开播）：

```powershell
& .venv/Scripts/python.exe -X utf8 verification/run_efficiency_live_v1.py --confirm-live --mode suite --root verification/efficiency-v1-new-case --rounds 3 --voice-port 18965 --agent-port 18991 --deadline-seconds 1200 --simulate-operator
```

每轮目录保存实际执行事件、明确标记的模拟测试员人工API回执及 `timing.json` / `timing.md`。`src/shengbo_prelive/timing_report.py` 可只读重算总耗时、阶段活动、并行分支、人工真实阻塞及实际请求。10分钟是优化目标，不是预先承诺；必须看本次完整证据。

## 启动与使用

双击桌面 **声播** 快捷方式，会同时确保声播业务服务 `127.0.0.1:8765` 和播前 Agent `127.0.0.1:8791` 运行，随后打开网页。已有服务会复用。

1. 首页即播前 Agent 工作台，右侧是完整对话，左侧是成果与配置。点击或键盘进入任一侧会放大该侧，不重建编辑器。
2. 从“直播间”入口进入左侧卡片，选择直播间并创建或恢复本场；商品库共享维护，本场只引用明确版本。切换左侧不会改变现有会话或已提交任务范围。
3. 上传材料，通过对话要求整理商品、研究顾客、配置本场或局部改稿。左侧也可独立修改、保存。只有明确点击“让 Agent 修改此项”才绑定精确字段或模块。
4. “执行过程”默认关闭，开启后查看主/子任务、参数、结果和确认卡片；关闭只影响显示。成果入口在左侧定位，自动跟随默认关闭。
5. 左侧按模块核对话术并编排顺序，整包批准后准备语音，再生成候选、人工采用。左侧“执行与调试”可直接逐句监控、人工插播、获取 OBS 链接、配置平台接入及测试评论。`/studio` 仅为旧入口。

示例：`为当前直播间做完整播前准备，使用我已选择的商品。主题是居家舒适，先生成文案供我检查，暂不生成语音。`

`只更新这个商品的资料和顾客研究，列出受影响的话术，先不要改稿。`

## 服务与数据边界

| 部分 | 代码 | 数据权威 |
|---|---|---|
| 主/子 Agent、队列、上下文、权限、中断恢复 | `src/shengbo_prelive/runtime.py`、`middleware.py`、`tools.py` | `.runtime/agent.sqlite3`、`checkpoints.sqlite3` |
| 模型 | `providers/` | ChatGPT 已授权 `gpt-6.1-sol`；DPAPI凭据由后端读取及续期 |
| 四份运行 Skill | `skills/{main,product_facts,session_config,audience_scenes}/prelive/SKILL.md` | 每个角色只发现自己的虚拟 Skill 目录，按需读取 |
| 业务服务 | 兄弟项目 `ecommerce-voice-agent/app/prelive/` | 声播 `runtime/library.sqlite3`；商品唯一正本 |
| 业务生成与声音 | `ecommerce-voice-agent/app/prelive_bridge.py` | 文案继续使用既有DeepSeek；声音仍为本机普通话/MiniMax |
| UI与人工动作 | `ecommerce-voice-agent/web/agent-workbench*`、`workspace-editors.js`、`assistant.js` | 服务端cookie、Origin、CSRF；模型无采用/开播权限 |
| 安全调试记录 | `observability.py` | `.runtime/observability/`；脱敏输入/输出及内容寻址payload |

`chatgpt_plan` 与文案、TTS来源分别配置，不静默切换账户或计费来源。18791旧连接测试工具已经停止，它不是推理代理。账号令牌不进入工具参数、前端、Agent state或日志。

所有公共业务DTO集中在 `src/shengbo_contracts/`，合同 `PRELIVE-2.3`；未知字段拒绝。Agent服务拥有执行状态，声播业务服务拥有商品、本场、文案、批准和实际播放的正本。业务回执、精确版本、摘要、实际音频分别验证，不能用模型一句“完成”代替验收。

## 环境与复现

已安装环境位于本项目 `.venv`。全量已解析依赖见 `requirements.lock.txt`；声播只需要安装共享合同及httpx，无需安装另一套Agent SDK。

```powershell
# 恢复Agent环境（Python3.12；在本目录）
python -m venv .venv
& .venv/Scripts/python.exe -m pip install -r requirements.lock.txt
& .venv/Scripts/python.exe -m pip install --no-deps -e .
# 声播的运行环境另外安装共享合同
& '../ecommerce-voice-agent/.venv/Scripts/python.exe' -m pip install httpx
& '../ecommerce-voice-agent/.venv/Scripts/python.exe' -m pip install --no-deps -e .
# 启动两服务；也可直接双击桌面声播
& '../ecommerce-voice-agent/.venv/Scripts/python.exe' '../ecommerce-voice-agent/launcher.py' --no-browser
```

普通自动测试不读取真实OAuth、不调用付费语音、不改正式商品库：

```powershell
& .venv/Scripts/python.exe -m pytest -q
```

真实模型探针需要显式 `--confirm-live`，会使用账号额度，不能把它加入每次普通回归。自动续期的竞争/失败分支用模拟服务测试；长期真实续期需要等自然到期观察。测试记录与具体已知限制见计划目录的 `engineering-v2/18_首期构建与验收记录.md`。

## 维护约定

- 先停止同一运行目录的旧服务再更新，尤其不能让旧三列records写法与新四列修订表并存。桌面启动器不会强杀运行中的直播。
- 运行目录具有独占OS锁。取消需冻结派发、撤销未来写权限、等实际SDK和后台工作退出、核实未知结果，最后才释放范围锁。
- 模型决定“做哪件事”，确定性业务服务处理CAS、金额/优惠校验、精确编排、语音缓存和播出。用户的批准绑定整包内容hash，不是一个可重复使用的布尔值。
- 网络研究失败会返回明确状态，不编造调研证据或成交率。首期只给演示素材清单，不生成视频，不实现播中/播后Agent。
- 完整安全记录仍需磁盘空间，当前没有自动清理保留策略。调试数据也可能包含商家资料，应保持在本机。
