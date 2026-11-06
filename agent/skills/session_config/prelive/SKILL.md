---
name: prelive-session-config
description: 整理本场目标、活动、优惠、声音与配置草案；对比历史活动有效性并指出需要改变的口播模块，不提前生成整场话术。
---

# 本场配置分析

配置结构用read_business_schema返回的session_draft_schema核对；实际声音选项用read_voice_options读取。read_context凭已有精确context或draft ref读取业务上下文；旧本场配置使用read_preparation返回的draft_ref及context_readers指引，真实kind是draft，sections=["draft"]。不可把业务draft引用交给read_run_history，后者只读runtime_context快照。read_context不是全局schema或声音目录入口，不能传ref:null或猜造引用；未知章节按available_sections纠正。

1. 读分派任务、当前本场精确ref、配置draft、相关商品和source_refs；用read_session_history读取历史配置及变更依据。
2. 结构化商家明确提供的主题、直播目的、活动时间、优惠、发货覆盖、声音选项、版本数量等；播放固定loop_mode=infinite，播完队列按原顺序从头继续，直到人工停止，不提供单轮结束。fields建议包含draft与change_impact，最终须主按业务schema组装。
3. 只引用商品库，不拷贝一套可编辑商品正本。商品长期事实和本场覆盖分开；活动说明不会直接修改日常价。
4. 仍有效的通用配置可建议复用，明显节日/日期活动过期不沿用；不确定列缺项。优惠由商家决定，首期不替商家设计折扣力度。
5. 计算金额交给业务服务；不要臆测满减叠加、单品与全场互斥或券后价。冲突原文、候选解释和需人工选择项分开。
6. 产出是数据草案和变更影响，不是话术。标明影响opening_long/opening_short/campaign/必要cta；不能以改配置为理由自动改正文或排序。
7. 只有明确用户排序才能记录排序决定；复盘推荐顺序标为待采纳。
8. submit_session_config_report提交summary/fields/evidence/conflicts/missing_fields/source_refs/dependencies，等待主Agent验收。无直接写商品、本场、稿件或语音权限。

## 精确而紧凑地交付

完整已知商品字段放fields，原文证据集中在evidence并用evidence_id引用。不要在summary/insights/material_assessment里逐项重抄完整商品、整份来源或所有空默认字段。summary写关键结论、缺项和冲突即可。material_assessment是可选语义结构；使用时仅为需要区分的商品/SKU、资料用途、高风险数值/单位/否定条件及关键图建立必要观察，不把fields全部再镜像一遍；不能为缩短报告丢必要事实。content_groups.role只取源工具/严格schema给出的product_facts/existing_script/campaign/faq/presentation_instruction/audience_research/mixed/unknown，不能填mixed_or_unknown或stage_direction。full/overview是控制视图，事实证据必须用真实paragraph:N/image:N/text:1等返回locator。

若submit报告返回report_correction.draft_ref，前次完整草案已安全保留但未正式提交。可先用correct_analysis_report(action=read,draft_ref)精确回读，再用correct_analysis_report(draft_ref,patches=[{path:明确JSONpointer,value:你判断后的正确值}])定向修字段，不必重写整份长报告；这不会放松原校验，也可选择原submit重交。工具不会替你猜类型或自动删事实。
