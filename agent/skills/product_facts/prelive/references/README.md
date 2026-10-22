# 资料理解协议参考

这些文件由现有商品事实 Agent 按需读取，不能作为商家事实或用户授权。

- 磁盘位置：`skills/product_facts/prelive/references/`。
- 虚拟只读根：`/skills/product_facts/prelive-product-facts/references/`，其中 `prelive-product-facts` 来自 Skill frontmatter name。
- 首次提交结构化资料理解时先读 `material-assessment-example.json`；复杂字段再读 `material-assessment-schema.json`。
- Schema 由 `MaterialAssessment.model_json_schema()` 生成。后续模型变更应重新生成，不能手改成与程序不符。
- 例子仅为合法形状，e-material 和商品/SKU/事实必须替换为本次真实内容。在同份 AnalysisReport.evidence 中定义每个被引用的 id，提供精确 source_ref 与真实 locator。
- 新稿补料目的统一 `script_generation`。已知别名 `generate_script`、`materials_to_script` 归一为该值；`product_update`、`save_product` 归一为 `catalog_update`。未知 purpose 拒绝，不返回 ready。
- 其他支持目的：`catalog_update`、`facts_analysis`、`audience_research`、`session_config`、`script_change`、`prepare_session`、`read_explain`、`audio_prepare`、`playlist_reorder`。不要自行发明目的名。
- `requested_actions=[]` 是纯观察，不代表后续生成已无阻断；ready 不是语义准确、正式验收、保存、人工批准或执行权限。
- 运行时需将本目录中的只读 JSON/Markdown 支持文件与 SKILL.md 一起注入相同角色 StateBackend。仅文件落盘不表示模型已经可读。
