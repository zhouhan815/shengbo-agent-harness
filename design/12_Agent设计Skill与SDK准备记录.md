# Agent设计Skill与Deep Agents SDK准备记录

更新日期：2026年10月15日。

最新工程设计见[后端工程设计 v2](engineering-v2/00_后端工程设计.md)。本文件记录SDK/Skill准备，不能据已安装SDK推断本业务Agent已实现；SQLite持久saver及真实provider/恢复探针仍需按v2实施。

## 一 用户本轮决定与两类方法

用户已明确：Harness设计采用此前自制agent-harness-design；另核对一个“第三方、星数较高、帮助构思Agent”的Skill，并把Deep Agents相关开发资料与SDK准备齐全。

| 工作 | 处理结果 |
| --- | --- |
| Harness方法 | 自制agent-harness-design已恢复至用户技能目录 |
| 第三方构思Skill | 核实下列候选；没有证据证明此前最终选定哪一个，不把自制方法冒充第三方 |
| Deep Agents官方Skill | 固定上游提交，22项齐全：新增21项，保留已有core |
| Python SDK | 独立环境安装完成，依赖无冲突，7项离线探针通过 |

Skill是开发指导与方法文件，SDK是实际运行的Python依赖。本轮分别准备，不将下载Skill等同于验证SDK或接通模型。

## 二 第三方高星构思候选

下列星数为2026年10月15日GitHub只读API结果，属于整个仓库，不是单个Skill的星数。原始SKILL内容已核对，单技能安装量不作为质量评分。

| 候选 | 仓库星数 | 内容定位 | 与本次的关系 |
| --- | --- | --- | --- |
| [langchain-architecture](https://github.com/wshobson/agents/blob/main/plugins/llm-application-dev/skills/langchain-architecture/SKILL.md) | 40,824 | LangChain/LangGraph架构、Agent模式、状态、记忆、工具及文档处理 | 最符合“高星、Agent技术架构”的描述；上一轮CLI确实搜到，汇总表漏列，应补充 |
| [project-development](https://github.com/muratcankoylan/Agent-Skills-for-Context-Engineering/blob/main/skills/project-development/SKILL.md) | 18,142 | 从任务适配、原型到阶段流水线、成本与单/多Agent取舍 | 最贴合“先构思Agent”；可作为自制Harness方法的互补参考 |
| [agents-best-practices](https://github.com/DenisSergeevitch/agents-best-practices/blob/main/SKILL.md) | 2,383 | 通用业务Agent及MVP Harness蓝图，含工具、MCP、上下文、运行与评估 | 仍是通用候选，本地仅找到本轮查找记录，不能当成旧选定证据 |

同一上下文工程仓库的multi-agent-patterns、tool-design分别可辅助协作拓扑和工具设计，不承担全部立项构思。候选方法不改变已确认的首期业务流程，也不自动改变Deep Agents框架或DeepSeek模型选择。历史是否最终采用仍未确认，本轮未将这三项安装为全局Skill。

核验记录：[第三方候选元数据](tooling/third-party-design-skill-verification-20261015.json)。

## 三 Skill安装结果

### 3.1 自制Harness方法

安装入口：[agent-harness-design](C:/Users/lizhe/.codex/skills/agent-harness-design/SKILL.md)。

从已有研究源包恢复15个文件，逐文件SHA256与源包一致，通过系统quick_validate格式校验。没有使用另一个作者的同名公开Skill替换本包。

记录：[恢复与哈希](tooling/custom-harness-skill-restore-20261015.json)。

### 3.2 官方配套

来源：[langchain-ai/langchain-skills](https://github.com/langchain-ai/langchain-skills)，固定提交16a992f09ab3ccfb642dd4f08d350d833d1990bf。用系统Skill Installer安装缺失项，目标为C:/Users/lizhe/.agents/skills。

| 组别 | 已具备Skill |
| --- | --- |
| Deep Agents核心与专题，共6项 | deep-agents-core、deep-agents-memory、deep-agents-orchestration、managed-deep-agents、deepagents-python-quickstart、deepagents-typescript-quickstart |
| 生态与依赖，共2项 | ecosystem-primer、langchain-dependencies |
| LangChain配套，共5项 | langchain-fundamentals、langchain-middleware、langchain-rag、langchain-python-quickstart、langchain-typescript-quickstart |
| LangGraph配套，共7项 | langgraph-cli、langgraph-decision-models、langgraph-fundamentals、langgraph-human-in-the-loop、langgraph-persistence、langgraph-python-quickstart、langgraph-typescript-quickstart |
| 评测，共1项 | eval-engineering |
| 独立并行专题，共1项 | swarm |

已有deep-agents-core未覆盖，其入口内容与固定上游提交一致；其余21项新增安装。22项和自制包共23个入口均通过格式检查。官方README的数量未及时反映新增条目，本次按固定提交实际目录盘点22项。

swarm原文使用顶层compatibility，系统格式检查器不接受该键。保留原始文件备份，仅将同一说明移至metadata.compatibility，依赖含义保持一致。该Skill要求的QuickJS/swarm_task运行入口没有因安装文件而自动接入；云托管及评测专题的外部服务也没有启动。本轮运行验证限于第四节独立Python SDK。

安装清单：[固定源与缺项盘点](tooling/official-skill-install-plan-20261015.json)；格式、文件哈希及局部兼容修订：[安装验证](tooling/skill-install-validation-20261015.json)。

新安装Skill从下一轮开始可供发现和调用，使用时按实际问题加载，不要求一次读完全部22项。

## 四 独立Python SDK准备

环境位置：D:/Codex project/shengbo-ops-agent/.venv。该目录只准备依赖、说明和离线技术探针，没有实现电商业务Agent，也不修改现有声播环境。

基础解释器为Python 3.12.14，实际安装版本如下：

| 包 | 版本 |
| --- | --- |
| deepagents | 0.7.25 |
| langchain | 1.4.6 |
| langgraph | 1.2.15 |
| langchain-deepseek | 1.1.1 |
| langchain-mcp-adapters | 0.3.2 |

直接依赖与必要传递依赖均在独立虚拟环境内安装，完整锁定文件见[requirements.lock.txt](D:/Codex project/shengbo-ops-agent/environment/requirements.lock.txt)。[pip check](D:/Codex project/shengbo-ops-agent/environment/reports/pip-check.txt)显示无依赖冲突。

离线探针7/7通过：隔离环境、依赖导入、工厂签名、图编译、本地工具执行、模型替身与工具往返、零网络访问。模型替身调用2轮，本地工具执行1次，结果42；消息顺序human→ai→tool→ai。网络连接/DNS尝试为0，继承环境已清空，追踪已关闭，没有构造真实托管模型客户端或调用付费API。

证据：[SDK运行说明](D:/Codex project/shengbo-ops-agent/environment/README.md)、[完整安装报告](D:/Codex project/shengbo-ops-agent/environment/reports/installation-report.json)、[离线探针](D:/Codex project/shengbo-ops-agent/environment/reports/offline-probe.json)、[实际包版本](D:/Codex project/shengbo-ops-agent/environment/reports/installed-packages.json)、[复跑入口](D:/Codex project/shengbo-ops-agent/environment/tools/run_offline_checks.ps1)。pip依赖检查加7项探针共8项验证通过，完整锁包含78项运行依赖。

版本提示：当前0.7.25的待办规划需要显式启用，和core Skill中“默认包含TodoList”的静态示例存在差异。后续按实际SDK签名、源码及当前官方文档设计，不把已下载Skill的静态示例当成永远有效的API。此次验证不证明真实DeepSeek兼容性、外部MCP连接或电商业务流程已经完成。

## 五 下一步设计入口

1. 业务输入读[10 首期流程基线](10_播前Agent首期流程基线.md)。
2. Harness总体方法读已恢复的自制Skill。
3. Deep Agents开发读core；上下文/持久化、协作分别按需读memory和orchestration。
4. 第三方构思方法待选定后使用，不能用候选Skill覆盖既定流程。
5. 将每一步映射为工具能力、可复用声播接口、本地库或MCP，再定义输入输出、状态、并行汇合、局部重试和验收。

## 六 后续已确认的使用方式与设计交付

用户随后确认采用“自制方法负责总体设计、deep-agents-core负责SDK实现、专题Skill按需使用”，第三方高星方法只作补充参考，不再作为竞争的总体设计入口。当前已完成详细设计：[13 总体方案](13_播前Agent_Harness总体设计.md)、[14 逐步行动与合同](14_逐步执行与工具数据契约.md)、[15 Learn Claude Code对照与验收实施](15_LearnClaudeCode对照与验收实施计划.md)。

本轮按实际0.7.25、Learn Claude Code本机s01—s17和声播源码核验；没有修改SDK锁、安装新持久组件、调用真实DeepSeek/MiniMax或开发业务Agent。旧候选和安装记录保留历史含义。
