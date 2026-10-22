"""Production main-Skill export and current writing scope; no paid generation.

These checks verify which creative rules the native Skill loader exposes and
that its examples/budgets do not silently alter business or FAQ scope. They do
not score a real provider's marketing prose.
"""
from pathlib import Path
import re

from deepagents.backends import StateBackend
from deepagents.backends.utils import file_data_to_string
from deepagents.middleware.skills import SkillsMiddleware

from shengbo_prelive.runtime import RuntimeEngine


SKILL_PATH = "/skills/main/prelive-orchestration/SKILL.md"


def exported_skill():
    files = RuntimeEngine._initial_files(None, "main")
    return files, file_data_to_string(files[SKILL_PATH])


def writing_scope(text):
    return text.split("## 完整上下文与模块计划", 1)[1].split("## 编排、批准与交付", 1)[0]


def test_actual_main_skill_exports_updated_body_and_native_discovery(monkeypatch):
    files, body = exported_skill()
    source = Path(__file__).resolve().parents[1] / "skills/main/prelive/SKILL.md"
    # StateBackend keeps the file's CRLF bytes; read_text would normalize them
    # to LF on Windows and manufacture a false content-loss failure.
    assert body == source.read_bytes().decode("utf-8")
    backend = StateBackend()
    monkeypatch.setattr(backend, "_read_files", lambda: files)
    metadata = SkillsMiddleware(backend=backend, sources=["/skills/main/"]).before_agent({"files": files}, None, {})
    assert not metadata["skills_load_errors"]
    assert any(item["name"] == "prelive-orchestration" for item in metadata["skills_metadata"])
    assert "场景小故事→真实特点利益→有力度的主播推荐→动作邀请" in body
    assert all(path.startswith("/skills/main/") for path in files)


def test_scene_planning_uses_existing_instruction_and_fact_anchored_imagination():
    scope = writing_scope(exported_skill()[1])
    assert "现有modules[].instruction" in scope
    assert "人物、时刻、麻烦、动作、期待、商品事实、推荐立场、参考字数" in scope
    assert "人物和时刻可以合理想象" in scope
    assert "不说成真实亲历或已获实测" in scope
    assert "不是新增DTO字段或朗读标题" in scope
    assert "同一次whole生成中统筹全部模块" in scope
    assert "不新增摘要模型、逐模块初稿或风格检查模型" in scope
    assert "先固定实际用途目的" in scope
    assert "依据商品事实与商家最新明确目的" in scope
    assert "用途目的、可展开的动作/情境、情绪期待界限" in scope
    assert "人物和时刻可以扩展，主要用途不能换" in scope
    assert "训练用品以练习、运动、入门为主" in scope
    assert "兴趣用于引入，不以纯逗乐替代" in scope
    assert "不保证孩子快乐或球技提升" in scope
    assert "其他商品按自身实际用途确定范围" in scope


def test_content_budgets_sum_to_long_form_goal_without_forcing_fixed_scene_count():
    scope = writing_scope(exported_skill()[1])
    values = re.search(r"开场(\d+)字、亮品(\d+)字、主体(\d+)字、促单(\d+)字，合计约(\d+)字", scope)
    assert values
    *parts, total = map(int, values.groups())
    assert sum(parts) == total and 1920 <= total <= 2400
    assert "每条可安排约280—320有效字的场景小故事" in scope
    assert "不能把“五轮”变成每品固定模板" in scope
    assert "没有逐模块硬门禁" in scope
    assert "8—10分钟" in scope and "1920—2400字" in scope


def test_spoken_faq_is_excluded_while_product_faq_and_natural_resonance_remain():
    _, body = exported_skill()
    scope = writing_scope(body)
    assert "本次直播脚本不编FAQ，也不表演模拟现场问答" in scope
    assert "有人问/答/能吗？可以呀！" in scope
    assert "自然共鸣反问" in scope
    assert "商品原有faq保持不改不删" in scope
    assert "此处只规定新脚本表达" in scope
    assert "关键购买问答" not in scope and "问答承接" not in scope
    # The catalog rule is deliberately retained: this writing change must not
    # tell product_facts to discard a merchant's FAQ information.
    assert "明确的问题和回答只保存到faq" in body
    assert "不改变资料整理、商品FAQ生成或后续真实评论回复能力" in scope


def test_cta_has_separate_information_roles_and_nonuniform_delivery():
    scope = writing_scope(exported_skill()[1])
    assert "促单先一轮收束主需要与购买理由，再一轮说明本次准确获得物/选购信息并带到明确行动" in scope
    assert "价格、颜色、组成各安排在有用的位置" in scope
    assert "紧邻段落不逐项再报一遍" in scope
    assert "间隔充分、换决策角度时可有目的地回扣核心价值" in scope
    assert "描述可以有一小段克制的解释" in scope
    assert "不要求每句高亢、每段同一公式或每句感叹号" in scope


def test_demonstrations_are_other_product_style_and_task_orchestration_stays_flexible():
    _, body = exported_skill()
    scope = writing_scope(body)
    assert "他品示范前提只有“旅行装、小巧、口腔清洁用途”" in scope
    assert "行李摊了一床" in scope and "我太喜欢它这份省心了" in scope
    assert "不把他品属性当事实" in scope
    assert "prompts/live-commerce-writing.md" in scope
    assert "不另立一套平行文风" in scope
    assert "旧冻结计划、输入和恢复协议不静默改写" in scope
    assert "这些是便利能力，不是强制流程" in body
    assert "只重写一版时对照保留的另一版" in scope


def test_actual_main_graph_construction_injects_scene_budget_without_old_narrative_ban(monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    import shengbo_prelive.runtime as runtime

    # Execute the production graph-construction entry, intercepting its native
    # factory boundary. No store, service, real model or model invocation needed.
    engine = object.__new__(RuntimeEngine)
    engine.model = FakeMessagesListChatModel(responses=[AIMessage(content="never invoked")])
    engine.saver = None
    engine.context_input_token_limit = 48000
    engine.context_summary_trigger_tokens = 12000
    engine.context_summary_keep_tokens = 3000
    captured = {}
    sentinel = object()

    def capture(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(runtime, "create_deep_agent", capture)
    monkeypatch.setattr(runtime, "build_tools", lambda _engine, _role: [])
    monkeypatch.setattr(runtime, "main_middleware", lambda _engine: [])
    assert engine._build_graph("main") is sentinel
    actual_prompt = captured["system_prompt"]
    assert actual_prompt == runtime.SYSTEM_MAIN
    assert captured["name"] == "prelive-main"
    assert "不要再用不同叙事场景" not in actual_prompt
    assert "不同具体场景与内容预算" in actual_prompt
    assert "充分展开场景销售" in actual_prompt
    assert captured["skills"] == ["/skills/main/"]
