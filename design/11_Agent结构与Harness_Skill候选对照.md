# Agent结构与Harness Skill查找对照

最新进展：本文件保留上一轮查找时的状态。用户随后已选定自制Harness方法，现已恢复安装并补齐官方配套；另核对了高星第三方构思候选。当前安装和SDK准备状态以[12 准备记录](12_Agent设计Skill与SDK准备记录.md)为准。

查询日期：2026年10月15日。目标是找回此前采用的Agent结构设计Skill，并比较本地和公开候选；本轮不安装、恢复或替换Skill，也不开始构建业务Agent。

## 一 已找回的历史Skill

最有明确证据的是自制的 [agent-harness-design](D:/Codex project/agent-harness-design-research/skill/agent-harness-design/SKILL.md)，中文名“Agent Harness设计与构建方法”。

| 核对事项 | 结果与证据 |
| --- | --- |
| 源包是否存在 | 存在于研究项目的skill/agent-harness-design目录，可直接读取 |
| 以前是否安装过 | [交付说明](D:/Codex project/agent-harness-design-research/交付说明.md)和[研究记录](D:/Codex project/agent-harness-design-research/research/研究过程与取舍.md)记载曾安装到用户.codex/skills，15个文件比对一致 |
| 以前是否实际选用过 | [设计校验记录](设计校验记录.md)、[多窗口分发说明](07_多窗口分发说明.md)及总方案记录曾使用/显式调用agent-harness-design |
| 当前安装状态 | 两个用户技能目录均无该名入口；不能用历史安装记录证明现在仍安装 |
| 是否等于用户说的Ghost Agent | 尚不能确认。它符合“之前做过、用于Agent结构和Harness设计”的描述，但没有找到名称等同的证据 |
| 当前可用方式 | 可以按准确源文件路径读取；恢复安装须在选定后另做，不覆盖其他同名包 |

该自制Skill包含S0—S11：范围、结果、环境能力、控制、Harness蓝图、工具契约、上下文、协作、恢复、选型、评测、交付。它从业务需求出发，不限定某一个SDK；参考了来新璐/ShareAI的Learn Claude Code、Deep Agents及其他一手资料。源码机制与适用限制见其[来源索引](D:/Codex project/agent-harness-design-research/skill/agent-harness-design/references/09-source-map.md)。

## 二 本地与当前任务相关的候选

| Skill | 所在位置/状态 | 适合负责什么 | 本次定位 |
| --- | --- | --- | --- |
| [agent-harness-design 自制版](D:/Codex project/agent-harness-design-research/skill/agent-harness-design/SKILL.md) | 研究源包存在，标准安装入口不存在 | 业务流程转成Agent边界、观察/动作、工具契约、上下文、Harness和验收 | 推荐作为总体设计方法 |
| [deep-agents-core](C:/Users/lizhe/.agents/skills/deep-agents-core/SKILL.md) | 用户技能目录存在 | Deep Agents的框架结构、配置、工具、子Agent、后端和Skill机制 | 默认框架落地时使用 |
| [claude-harness-to-deepagents](C:/Users/lizhe/.codex/skills/claude-harness-to-deepagents/SKILL.md) | 用户技能目录存在 | 仅解释Claude式机制与Deep Agents的真实语义差异 | 确有差异时补充，不承担整套设计 |
| [agent-builder](D:/Codex project/learn-claude-code/skills/agent-builder/SKILL.md) | Learn Claude Code项目中的源文件 | 能力、知识、上下文和模型循环的入门方法 | 教学参考，不能用其泛化主张替代已确认业务约束 |
| [agents-sdk](C:/Users/lizhe/.codex/skills/agents-sdk/SKILL.md) | 用户技能目录存在 | OpenAI Agents SDK应用的搭建、验证与部署 | 其他技术栈备选，不自动改变Deep Agents默认选择 |
| [github-research](C:/Users/lizhe/.agents/skills/github-research/SKILL.md) | 用户技能目录存在 | 开源仓库、实现能力和集成蓝图研究 | 工具/MCP候选的代码调查辅助 |
| [find-skills](C:/Users/lizhe/.codex/skills/find-skills/SKILL.md) | 用户技能目录存在 | 搜索并核验可安装Skill | 本轮发现流程；不代替Agent架构方法 |
| [skill-creator](C:/Users/lizhe/.codex/skills/.system/skill-creator/SKILL.md) | 系统技能文件存在 | 创建、维护可复用Skill | 将设计方法或业务能力封装成Skill时使用 |

## 三 公开候选的原始内容核验

已查看skills.sh榜单，执行agent harness、ghost agent、agent architecture、tool design等查询，并核对下表原SKILL内容。安装量为查询时CLI显示的近似值，不能当作质量评分；GitHub仓库星数来自同日只读REST查询。

| Skill及原来源 | 实际内容 | 本地状态 | 查询数据 | 判断 |
| --- | --- | --- | --- | --- |
| [deep-agents-core](https://github.com/langchain-ai/langchain-skills/blob/main/config/skills/deep-agents-core/SKILL.md) | LangChain官方Deep Agents配置与Harness架构 | 已有本地副本，尚未在本轮更新 | CLI约15.4K安装；仓库1290星 | 与当前框架选择一致，开发前仍核对SDK版本 |
| [deep-agents-orchestration](https://github.com/langchain-ai/langchain-skills/blob/main/config/skills/deep-agents-orchestration/SKILL.md) | 官方子Agent、任务规划、人工介入及协作边界 | 两个用户技能目录未发现 | 安装量未核验；同仓库1290星 | 适合并行与协作专项补充 |
| [agents-best-practices](https://github.com/DenisSergeevitch/agents-best-practices/blob/main/SKILL.md) | 通用业务Agent和MVP Harness蓝图，覆盖工具、MCP、状态、上下文、协作与评估 | 两个用户技能目录未发现 | CLI约1K安装；仓库2383星 | 可作为新的总设计方法备选或独立审查参考，当前原文版本1.16.0 |
| [agent-harness-design AnastasiyaW版](https://github.com/AnastasiyaW/codex-claude-code-config/blob/main/skills/agent-harness-design/SKILL.md) | 新建Harness/工具系统的操作参考，含工具、预算、信任边界、观测和评估 | 两个用户技能目录未发现 | 安装量未核验；仓库173星 | 与自制Skill同名但来源和内容不同，选用时必须指定仓库 |
| [harness HikaruEgashira版](https://github.com/HikaruEgashira/agent-skills/blob/main/meta/skills/harness/SKILL.md) | 领域团队与Skill架构；实现约定绑定.claude/agents、Claude团队工具和Opus | 两个用户技能目录未发现 | 安装量未核验；仓库1星 | 可研究思路，不建议直接作为本次Deep Agents主方案 |
| [ghost tkersey版](https://github.com/tkersey/dotfiles/blob/main/codex/skills/ghost/SKILL.md) | 从现有仓库提取跨语言行为规范、可移植测试和溯源材料 | 两个用户技能目录未发现 | CLI约22安装；仓库70星 | 是真实ghost技能，但不负责本次从业务需求规划Agent结构；不能据名称认定用户以前选了它 |

网上同名agent-harness-design不是本机自制包的公开发布地址。此处只对已核对内容的候选作判断；其他搜索结果没有因名称相近而进入推荐。

### 可选安装来源记录

仅记录命令，尚未执行：
- 官方协作补充：`npx skills add langchain-ai/langchain-skills --skill deep-agents-orchestration -g`
- 通用设计备选：`npx skills add DenisSergeevitch/agents-best-practices --skill agents-best-practices -g`
- 同名公开参考：`npx skills add AnastasiyaW/codex-claude-code-config --skill agent-harness-design -g`

自制包不使用上述同名公开仓库安装。实际安装前核对目标路径、来源和现有同名内容。

## 四 给本次首期方案的选择建议

| 用途 | 推荐选择 | 原因 |
| --- | --- | --- |
| 主设计方法 | 先采用已找回的自制agent-harness-design源包 | 曾用于本项目，覆盖从业务流程到工具、状态、上下文、协作和Harness的完整链条 |
| 框架落地 | 当前已安装deep-agents-core | 遵循现有Deep Agents默认约定，不重复自建框架机制 |
| 并行/协作专项 | 根据缺口选用官方deep-agents-orchestration | 对应2A/2B/3并行、子任务输入输出与汇合 |
| 可替换的主方法 | 若希望改用公开维护的方法，可比较agents-best-practices | 有业务MVP蓝图、工具与MCP、上下文和运行状态等内容；需按首期范围裁剪 |

这些是供用户选择的建议，本轮只查找比较。下一步业务输入仍是[10 首期流程基线](10_播前Agent首期流程基线.md)，技术方法不能改写4A完整配置和4B三路完整上下文的要求。

## 五 调查记录

- [用户技能目录完整文件清单](Skill本地清单_20261015.json)：68个SKILL.md文件，包含.system；仅统计.codex/skills和.agents/skills，不含插件缓存与项目存档。文件存在不自动证明当前应用已加载。
- [公开搜索记录](Skill公开搜索记录_20261015.json)：保存三条主要CLI查询的原输出，包含未筛选结果；tool design另做补充查询。
- [公开源元数据](Skill公开源核验_20261015.json)：6个GitHub仓库星数及公开状态的只读核验。
- 插件随附Skill的启用状态未纳入68个文件的统计，不将缓存文件等同当前已启用Skill。
- 没有安装、升级、删除或替换Skill，没有运行候选仓库的脚本；仅运行已知Skills CLI的搜索命令和只读查询。

## 六 用户技能目录全量名称

具体文件路径见完整JSON清单。下面按名称排序，便于检索；这不是本次需要全部加载的Skill列表。

| 序号 | Skill名 | 用户目录 |
| --- | --- | --- |
| 1 | agents-sdk | .codex/skills |
| 2 | ai-twitter-rag | .codex/skills |
| 3 | belt | .codex/skills |
| 4 | character-design-sheet | .codex/skills |
| 5 | claude-harness-to-deepagents | .codex/skills |
| 6 | company-research | .agents/skills |
| 7 | competitive-analysis | .codex/skills |
| 8 | comps-analysis | .codex/skills |
| 9 | convert-skill-to-dify-tool | .codex/skills |
| 10 | deep-agents-core | .agents/skills |
| 11 | find-skills | .codex/skills |
| 12 | game-design-theory | .agents/skills |
| 13 | github-research | .agents/skills |
| 14 | idea-generation | .codex/skills |
| 15 | image-portrait | .codex/skills |
| 16 | imagegen | .codex/skills |
| 17 | lark-approval | .agents/skills |
| 18 | lark-apps | .agents/skills |
| 19 | lark-attendance | .agents/skills |
| 20 | lark-base | .agents/skills |
| 21 | lark-calendar | .agents/skills |
| 22 | lark-contact | .agents/skills |
| 23 | lark-doc | .agents/skills |
| 24 | lark-drive | .agents/skills |
| 25 | lark-event | .agents/skills |
| 26 | lark-im | .agents/skills |
| 27 | lark-lizhen-share | .agents/skills |
| 28 | lark-mail | .agents/skills |
| 29 | lark-markdown | .agents/skills |
| 30 | lark-minutes | .agents/skills |
| 31 | lark-note | .agents/skills |
| 32 | lark-okr | .agents/skills |
| 33 | lark-openapi-explorer | .agents/skills |
| 34 | lark-shared | .agents/skills |
| 35 | lark-sheets | .agents/skills |
| 36 | lark-skill-maker | .agents/skills |
| 37 | lark-slides | .agents/skills |
| 38 | lark-task | .agents/skills |
| 39 | lark-vc | .agents/skills |
| 40 | lark-vc-agent | .agents/skills |
| 41 | lark-whiteboard | .agents/skills |
| 42 | lark-wiki | .agents/skills |
| 43 | lark-workflow-meeting-summary | .agents/skills |
| 44 | lark-workflow-standup-report | .agents/skills |
| 45 | lisanliang-xhs-context | .codex/skills |
| 46 | lizhen-weekly-report | .codex/skills |
| 47 | openai-docs | .codex/skills |
| 48 | pdf | .agents/skills |
| 49 | portrait-style-transfer | .codex/skills |
| 50 | pptx-author | .codex/skills |
| 51 | prd-development | .agents/skills |
| 52 | prd-writing-assistant | .codex/skills |
| 53 | product-brainstorming | .agents/skills |
| 54 | prompt-engineering-patterns | .codex/skills |
| 55 | prompt-optimizer | .codex/skills |
| 56 | pyweixin-rpa | .codex/skills |
| 57 | resume-bullet-writer | .codex/skills |
| 58 | review-agent | .codex/skills |
| 59 | rewrite-resume-with-star | .codex/skills |
| 60 | sector-overview | .codex/skills |
| 61 | skill-creator | .codex/skills |
| 62 | skill-installer | .codex/skills |
| 63 | thinking-prompt-optimizer | .codex/skills |
| 64 | twitter-reader | .codex/skills |
| 65 | validate-idea | .agents/skills |
| 66 | xiaohongshu-search | .codex/skills |
| 67 | xiaohongshu-similar-account | .codex/skills |
| 68 | xiaohongshu-title-score | .codex/skills |

