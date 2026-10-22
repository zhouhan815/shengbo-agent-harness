# 评价体系的 Skill 与 GitHub 组件调研

调研日期：2026-10-20。范围是评价体系设计与复用选型；未安装全局 Skill、未修改生产依赖或业务代码、未采集新的直播数据。用户确认“Steam”指 Skill。

## 1. 结论

建议使用“少量按需分析 Skill + 确定性的统计工具”。Skill 告诉模型怎样选择口径、核查证据和解释结果；计算库负责时间对齐、去重、分组、区间和评分计算。未找到经过充分验证、可以直接套在当前声播与抖音数据上的一体化电商直播评分 Agent。

首期计算组件优先为 pandas 与 statsmodels；SciPy 是有可比较历史/实验数据后的可选统计能力；scikit-learn 只用于检验评论分类器，不必作为直播评分的核心依赖。不应引入一整套新的采集器、重型分析平台或视频 OCR 链路。

当前三类证据的边界：

| 数据来源 | 可以支持的评分/分析 | 不能自动支持的结论 |
|---|---|---|
| 当前 jwwsjlm/douyinLive 适配器 | 已观察评论、点赞消息及其 count、进房消息、在线快照及数据覆盖；按采集时段描述 | 真实离房、个人观看时长、商品点击、订单、支付、退款；原始购物消息出现不等于已经解析出交易 |
| 声播播出记录 | 商品、版本、开场类型、句子/模块与本地播放时段；补齐持久日志后可稳定复算 | 本地开始播放不等于观众此刻听到；需要输出/平台延迟校准，暂停/缓冲应分段 |
| 商家手工导出的经营报表 | 经字段核对后的整场/商品/时间桶经营结果，以及同一支付集合的退款情况 | 只有整场汇总时，不能把成交“分给”某一句；没有同一人群标识时，不能凭两个 UV 汇总构造严格转化漏斗 |

## 2. 检索与核查方式

执行了 `find-skills` 与 `github-research` 的检索/代码核查原则。先浏览 skills.sh，再执行只读检索：

```text
npx --yes skills find statistical-analysis
npx --yes skills find analytics
npx --yes skills find ab-test
```

随后通过 GitHub API 核对仓库元数据、目录树与 commit，实际读取 SKILL.md 和所列源码函数；没有只凭搜索简介选型。按本次有限组件选型缩小了通用 GitHub Research 的大规模筛选数量，没有为了达到仓库数量引入无关组件。

检索结果出现目录变迁：`coreyhaines31/marketingskills` 的旧 `skills/ab-test-setup/SKILL.md` 已不可用，当前实际文件为 `skills/ab-testing/SKILL.md`；K-Dense 当前文件在 `skills/statistical-analysis/`，不是旧的 `scientific-skills/`。安装时必须以固定 commit 和当前目录为准。

## 3. 已阅读的 Skill：选择与边界

安装量为 2026-10-20 检索快照，数量只作生态成熟度参考，不证明统计结论正确。

| Skill 与来源 | 检索快照 | 已核查的实际内容 | 建议如何使用 | 不应照搬的地方 |
|---|---:|---|---|---|
| Anthropic `statistical-analysis` | 约 4.5K 安装；仓库约 30.5K stars | `data/skills/statistical-analysis/SKILL.md`；均值/中位数、分位数、异常、效应量、因果边界、多比较 | 作为轻量分析解释的参考：报告原始值、基线、样本与不确定性；异常先调查再决定是否排除 | 没有直播/声播数据接入代码；“30 个事件”的经验规则不能作为通用可靠性保证；其中对 p 值的简化措辞不应原样进入产品 |
| K-Dense `statistical-analysis` | 约 2.2K 安装；仓库约 53K stars | `skills/statistical-analysis/SKILL.md`、`scripts/assumption_checks.py`；明确采样单位、依赖、效应与不确定性；诊断不能证明独立性 | 有真实实验或复杂历史比较时按需参考；其“预先明确估计对象与聚类单位”很适合防止把每句/每分钟误当独立样本 | Skill 涉及 Pingouin/PyMC/ArviZ 等较重依赖，首期不应整套安装到现有 Agent 环境；Shapiro/Levene 的不拒绝不等于假设已经成立 |
| `coreyhaines31/marketingskills` 的 `ab-testing` | 约 74.2K 安装；仓库约 55.6K stars | `skills/ab-testing/SKILL.md`，metadata version 2.0.2；主指标、护栏、MDE、样本量、停止规则与探索性发现 | 只在要验证“下场改某一句/某模块是否改善”时调用；输出一份实验候选，不在每次复盘强制执行 | Web 页面 50/50 分流不能直接套在一个公共直播间；时间上 AB 轮播有时段、流量与前一稿残留效应。样本量快速表是示意，必须用实际基线、MDE 与采样结构重算 |

固定来源：

- [Anthropic 实际 Skill](https://github.com/anthropics/knowledge-work-plugins/blob/a266ab8c3c754a3ccf7a634044fc1595fc06e50e/data/skills/statistical-analysis/SKILL.md)，commit `a266ab8c3c754a3ccf7a634044fc1595fc06e50e`，Apache-2.0。
- [K-Dense 实际 Skill](https://github.com/K-Dense-AI/scientific-agent-skills/blob/593377bb048752abb0152deab9fbb1083a6087df/skills/statistical-analysis/SKILL.md)，commit `593377bb048752abb0152deab9fbb1083a6087df`，MIT；仓库最新 release `v2.72.0`。
- [K-Dense 实际诊断脚本](https://github.com/K-Dense-AI/scientific-agent-skills/blob/593377bb048752abb0152deab9fbb1083a6087df/skills/statistical-analysis/scripts/assumption_checks.py)。已读 `_clean`、`check_normality`、`check_regression_diagnostics`、`comprehensive_assumption_check` 等入口及其边界；不是读取直播事件的工具。
- [A/B 实际 Skill](https://github.com/coreyhaines31/marketingskills/blob/e85f3cbc2e4a79bf33b2a12e9bdd9087d414735b/skills/ab-testing/SKILL.md)，commit `e85f3cbc2e4a79bf33b2a12e9bdd9087d414735b`，MIT；仓库 release `v2.13.0` 与单个 Skill 版本不是同一个编号。

这里是对第三方 Skill 的研究，不表示本轮已安装并让产品 Agent 执行它们。两个统计 Skill 同名，后续不能直接同时注册成相同名称；推荐选取一条明确的项目 Skill 入口，引用外部方法资料，保留项目自身的数据资格规则，不把三份完整指令机械叠加。

## 4. 计算代码：已定位到函数

下表的 release/commit 是核查快照，不是对生产环境强制升级的要求。实施时核对现有 Python 和锁定依赖后再选兼容版本。

| 组件 | 版本快照、许可 | 已读源码入口 | 对本项目的直接作用 | 必须由我们补充的规则 |
|---|---|---|---|---|
| pandas | release `v3.0.6`；BSD-3-Clause；commit `bb4297c4e6fc0fd8afc2d05c4bd4b51596d20264` | `pandas/core/reshape/merge.py::merge_asof`、`_AsOfMerge` | 将事件时间与校准后的句子/模块起点匹配；按场次/商品分组；结合已有聚合处理分钟桶 | 同场隔离、原始时间/接收时间选择、平台延迟、模块结束边界、暂停区间、最大可接受距离；仅做 asof 最近匹配会把停播后的事件错挂到上一句 |
| statsmodels | release `v0.15.0`；BSD-3-Clause；commit `408f6c08e6131a796b856b53b59a03c49308d6fd` | `statsmodels/stats/proportion.py::proportion_confint` 的 Wilson 分支；`power_proportions_2indep`；`statsmodels/stats/multitest.py::multipletests` | 对合格的点击/支付等二项比例给区间；计划独立样本比较的检测能力；一组预先定义的比较做多重校正 | 不能把互动次数当观众人数；同一人多次出现不是独立样本；退款金额率不是二项比例；BH 对依赖结构有条件，依赖不清时不自动使用 |
| SciPy | release `v1.18.1`；BSD-3-Clause；commit `8ae9128877eefbe5394146edc422f6bbaa77f1e9` | `scipy/stats/_resampling.py::bootstrap`、`_bootstrap_resample`、`permutation_test` | 估计可比较场次/随机化区组的收入率、响应率差异的不确定性；用于验证候选改稿 | 默认重采样行不能解决时间相关。应以可辩护的场次/区组为采样单位；`paired=True` 仅保留配对，不自动变成分块 bootstrap；极端稀疏数据可能无法算 BCa 区间 |
| scikit-learn（可选开发工具） | release `1.9.1`；BSD-3-Clause；commit `4e624cfb16a326a88215003be3b57e61f6ff3fc5` | `sklearn/metrics/_classification.py::classification_report`、`f1_score`、`precision_recall_fscore_support`、`confusion_matrix` | 对“咨询/购买意向/抱怨/闲聊/歧义”评论标注器做人工金标准验收，报告各类精确率、召回率与宏平均 F1 | 这些函数不替我们理解中文；必须先有独立人工标注样本。不出现某类时不能悄悄当满分；需呈现 support 与不可计算值 |

代码与文档入口：

- [pandas 源码](https://github.com/pandas-dev/pandas/blob/bb4297c4e6fc0fd8afc2d05c4bd4b51596d20264/pandas/core/reshape/merge.py#L650)、[merge_asof 官方文档](https://pandas.pydata.org/docs/reference/api/pandas.merge_asof.html)。源码要求按连接时间键升序，支持 `by`、`tolerance`、`direction`；不会自行校验直播业务区间。
- [Wilson 分支源码](https://github.com/statsmodels/statsmodels/blob/408f6c08e6131a796b856b53b59a03c49308d6fd/statsmodels/stats/proportion.py#L296)、[比例区间文档](https://www.statsmodels.org/stable/generated/statsmodels.stats.proportion.proportion_confint.html)、[多比较源码](https://github.com/statsmodels/statsmodels/blob/408f6c08e6131a796b856b53b59a03c49308d6fd/statsmodels/stats/multitest.py#L100)。
- [SciPy 重采样源码](https://github.com/scipy/scipy/blob/8ae9128877eefbe5394146edc422f6bbaa77f1e9/scipy/stats/_resampling.py#L301)、[bootstrap 官方文档](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html)。
- [分类验收源码](https://github.com/scikit-learn/scikit-learn/blob/4e624cfb16a326a88215003be3b57e61f6ff3fc5/sklearn/metrics/_classification.py#L2971)、[classification_report 官方文档](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.classification_report.html)。

## 5. 直接相关的直播开源项目：参考什么、不采纳什么

### 5.1 douyin-live-dashboard：评分锁死的真实工程案例

[仓库](https://github.com/Senkuu123/douyin-live-dashboard)，核查 commit `433e1ebbe80459e07e1af361b78176ee5587f511`，约 4 stars，最后推送 2026-08-26；GitHub API 未识别到 LICENSE，因此本轮只研究设计，不建议直接复制其代码。

实际读取了 [dashboard-model.ts](https://github.com/Senkuu123/douyin-live-dashboard/blob/433e1ebbe80459e07e1af361b78176ee5587f511/apps/desktop/renderer/src/dashboard-model.ts) 的 `hillNormalize`、`momentumRatio`、`measureBaselines`、`groupIssues`、`healthScore`、`scoreContributions`。也读取了 [评分改造记录](https://github.com/Senkuu123/douyin-live-dashboard/blob/433e1ebbe80459e07e1af361b78176ee5587f511/docs/pm-20260814-score-algorithm-redesign.md)。

可借鉴三个设计经验：固定基准不能轻易产生永久满分/零分；与当前值一起滚动的基准会遮住真实变化；每个分项应保留原值与贡献供用户检查。

不建议照搬其电商分数。其评分包含礼物等娱乐指标，并用多个房间的小样本模板作默认尺度；源码还会将部分缺失值归零。当前项目应按商家/商品/活动选择可比基线，并让不合格数据保持未知，不能把该项目的模板当抖音官方标准。

Hill 曲线可作为候选归一化函数，但本项目采用什么函数应以可解释性、稳定性与校准结果决定。即使用 Hill，也必须在基线缺失/为零时返回“待建立基线”，不能把分母替换成极小数从而制造高分。曲线只把数值映射到分数，不证明话术导致成交。

### 5.2 Douyin Social Commerce Intelligence：不适合直接接入当前实时评价

[仓库](https://github.com/zhouwanting923-web/douyin-commerce-intelligence)，commit `f1aa288632fe8cfaff52a967810b68596e163e03`，约 4 stars，MIT。

已读实际 [SKILL.md](https://github.com/zhouwanting923-web/douyin-commerce-intelligence/blob/f1aa288632fe8cfaff52a967810b68596e163e03/skills/analyze-douyin-social-video/SKILL.md)。它处理有完整字幕的本地短视频，依赖 macOS Apple Vision，输出视觉证据与报告；它自身明确禁止没有证据的销量/转化推断。

可参考外置时间戳证据、引用注册表、缓存复用和“观察/推断”分离；当前 Windows 声播已经拥有文本和播出计划，无需额外增加字幕 OCR、七张截图等不相关环节。它不是能直接给声音人直播评分的成品工具。

### 5.3 继续沿用现有采集器

[jwwsjlm/douyinLive](https://github.com/jwwsjlm/douyinLive) 是当前公开互动数据来源，不需要为了评分改换采集器。仓库能力和本地适配器实际落实的字段应分别核查；README 出现某消息类型不代表本地已经有完整业务字段。本文沿用本地只读审计结果，不把其他项目的关注/来源/画像等能力加到本地的已支持列表。

## 6. 复用接口怎么放进 Deep Agents Harness

以下接口为设计建议，不是已实现函数。

| Harness 元件 | 责任 | 输入/输出重点 | 触发时机 |
|---|---|---|---|
| 数据工具 `get_metric_panel` | pandas/现有数据库完成聚合与合法性检查 | `archive_revision`、`metric_spec_id`、分母集合、覆盖区间；返回值、样本数、缺项原因和证据引用 | 主 Agent 选择查看整场、商品或模块表现 |
| 统计工具 `compare_occurrences` | 选择已注册的统计程序，生成差值/区间/可比性检查 | 同商品/价格/活动/开场类型/流量层；声明采样单位与设计；不能让模型任意运行统计代码 | 主 Agent 判断确有可比历史或实验后才调用 |
| 评分工具 `calculate_scorecard` | 执行固定版本的公式与适用条件 | `scoring_version`、基线/目标引用、合格指标；输出各项分数、缺失权重和总分可用状态 | 有明确目标或合格历史时；不能让模型自己改权重补高分 |
| 评论工具 `classify_comment_batch` | 小批量语义分类；不负责金额/率的计算 | 评论证据 ID、可用商品上下文、有限标签与不确定类别；结果保留分类器版本 | 需要理解咨询/反对意见时，按需调用并缓存 |
| 项目 Skill `review_scope_and_data_eligibility` | 指导模型检查资料、分母、阶段及可用结论 | 规则与工具使用说明；原始资料仍在外部证据存储 | 数据资格不清或切换复盘模式时 |
| 项目 Skill `commercial_funnel_diagnosis` | 解释漏斗瓶颈，区分销量、收入、退款、利润 | 使用工具算出的数值，引用事实，不输出虚构成交归因 | 经营数据足够时 |
| 项目 Skill `next_session_experiment_and_feedback` | 用 A/B Skill 方法提出可验证改进 | 一个主指标、可选护栏、MDE/采样方案、复查时间；不擅自改直播配置 | 发现可操作问题且用户愿意验证时 |
| 资格/证据中间件 | 拦截跨场次、缺证据、越权写入与不合格分数 | 机械规则不由模型自由决定；记录为什么降级 | 工具执行与报告提交边界 |

主 Agent 仍决定要研究哪个问题、要不要调用专家、要不要比较历史。工具内部的公式、去重、单位校验固定，是为了可复算，不会把整个任务固定成每场必跑所有步骤。

## 7. 评价体系需要增加的具体数值能力

这些是推荐的项目计算规则与验收方法，不是外部平台公布的统一优秀阈值。

| 数值能力 | 计算/展示方式 | 验证方法 |
|---|---|---|
| 比例区间 | 合格二项指标统一给分子/分母、比例及 95% Wilson 区间；比例只是描述，不能凭小样本排绝对名次 | 0 成功、全成功、小样本、同一人多次事件用例；若分母无效则不调用区间函数 |
| 版本差异 | 显示绝对百分点差与相对变化；基线为零时相对变化为不可计算，不给无穷大 | 人工可复算的两组数据；对齐 SKU、活动、流量与观察窗口 |
| 历史基线 | 保存固定的历史样本集合、分位数、选择条件与 baseline_revision；评分生成后不随本场结果回写基准 | 同一数据+同一版本重复执行分数一致；增加新场次只能生成新基线版本 |
| 时间对齐 | 输出校准偏移、偏移不确定范围、匹配/未匹配/混合窗口数；边界附近做敏感性比较 | 模块切换、暂停、跨场次、延迟改变和报表分钟桶跨多个模块的反例 |
| 评论语义统计 | 每类消息数、独立发言人数与各自口径；购买意向不计为订单；歧义类保留 | 独立人工金标准，查看混淆矩阵与每类 support；先找误判，再决定是否用于分数 |
| 分数可计算性 | 同时展示评分、已覆盖权重、待补资料。关键成交项缺失则总经营分保持不可计算；可展示独立的内容/技术分 | 丢掉一个低分指标不能使总分自动变高；未知不能替换成 0 或满分 |
| 多项探索 | 相似商品/版本的多项比较应标出探索性质；若统计检验则预先确定比较家族与校正法 | 不能尝试大量分组后只保留最好的 p 值 |

## 8. 首期实际接入顺序

1. 先统一指标契约、已采用脚本引用和完整播放片段；用现有数据做去重、覆盖与时间对齐。合格前只出可观察事实。
2. 接入经营报表后校验字段、集合和粒度，再算合格漏斗与金额。程序计算，模型解释。
3. 评分函数与配置版本化。项目自设阈值、人工目标、同店历史阈值分别记录来源，不写成行业标准。
4. 先以手算样例和反例验收；再收集真实场次建立基线。首期无需先上复杂回归或深度情感模型。
5. 需要验证改稿时，再按需启用实验 Skill 与统计比较工具；无足够可比数据时保留候选建议与验证计划。

本次只完成检索、源码核查与设计选型；没有运行这些第三方分析代码验证本地生产数据，也不声称评价体系已经经过真实成交效果验证。
