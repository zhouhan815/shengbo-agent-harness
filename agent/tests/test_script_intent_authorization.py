"""Current-user evidence for the explicit text-only script workflow."""
from copy import deepcopy
import json

import pytest

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import IntentAssessment, RunScope
from shengbo_prelive.policy import bind_intent_authorization, validate_intent
from test_runtime_business import env, human_command, new_run, product


HEADER = "请为当前本场新增完整话术版本："
SCOPE_MARKER = "当前明确范围与已有稿引用："
FOLLOW_UP_SPAN = "复用当前已保存且有效的本场配置和商品资料，只为以下两款分别新增完整直播话术："
FOLLOW_UP_TEXT = (
    "请接着完成刚才中断的这场任务。" + FOLLOW_UP_SPAN + "\n"
    "- 栖光营地灯 长续航款：新增 1 版，使用长开场。\n"
    "- 栖光营地灯 轻量款：新增 1 版，使用短开场。\n"
    "每版一次整稿生成，再按模块保存。仅生成并保存文字，保持商品和本场配置，不生成语音、不批准或开播。"
)
TEXT_EFFECTS = {"save_context_bundle", "save_script_plan", "submit_script_change", "retry_failed_items"}
PRODUCTS = ["product_a", "product_b"]


def make_proposal(ids=PRODUCTS, **changes):
    return IntentAssessment.model_validate({"objective": "materials_to_script", "summary": "新增完整文字",
        "target_product_ids": list(ids), "preparation_id": "prep", "script_mode": "generate",
        "requested_script_variants": [{"product_id": identity, "count": 1} for identity in ids], **changes})


def make_run(text=FOLLOW_UP_TEXT, *, ids=PRODUCTS, **changes):
    return {"scope": {"kind": "preparation", "account_id": "local", "preparation_id": "prep"}, "input": {
        "text": text, "intent_mode": "follow_up", "observed_versions": [{"kind": "product", "id": i} for i in ids], **changes}}


def button_text(prep, refs, **snapshot):
    manifest = {"preparation_id": prep, "product_counts": [{"product_ref": ref, "count": 1} for ref in refs], **snapshot}
    return f"{HEADER}\n- 产品A：新增 1 版。\n- 产品B：新增 1 版。\n每版整篇生成。只生成文字。\n{SCOPE_MARKER}\n" + json.dumps(manifest, ensure_ascii=False)


@pytest.mark.parametrize("quote", [None, "复用当前已保存且有效的本场配置和商品资料，只为以下两款分别新增完整直播话术：栖光营地灯 长续航款：新增 1 版。"])
def test_supported_follow_up_uses_exact_current_input_instead_of_model_copy(quote):
    run, proposal = make_run(), make_proposal(authorization_quote=quote)
    original = deepcopy(run)
    bound = bind_intent_authorization(run, proposal)
    assert bound.authorization_quote == FOLLOW_UP_SPAN
    assert proposal.authorization_quote == quote and run == original
    result = validate_intent(run, bound)
    assert result["authorization_source"] == "current_script_request"
    assert set(result["allowed_effects"]) == TEXT_EFFECTS
    assert not result["requires_decision"]


@pytest.mark.parametrize('text, expected', [
    ('基于已有原稿，扩成两版短直播口播，只要文本。', False),
    ('基于已有原稿，扩写成三版口播，不生成语音。', False),
    ('基于已有原稿，不要扩成两版短直播口播。', True),
])
def test_explicit_natural_language_expansion_and_negation(text, expected):
    proposal = IntentAssessment(objective='script_change', script_mode='expand_original',
        summary='原稿扩版', authorization_quote=text)
    assert validate_intent(make_run(text), proposal)['requires_decision'] is expected


@pytest.mark.parametrize("reason", ["mismatched-prep", "unobserved-target", "wrong-count", "attachment", "edit", "unrelated-message"])
def test_no_program_quote_for_unsupported_or_expanded_shape(reason):
    run, proposal = make_run(), make_proposal()
    if reason == "mismatched-prep":
        proposal.preparation_id = "other"
    elif reason == "unobserved-target":
        run["input"]["observed_versions"].pop()
    elif reason == "wrong-count":
        proposal.requested_script_variants[0].count = 2
    elif reason == "attachment":
        run["input"]["material_refs"] = ["uploaded-material"]
    elif reason == "edit":
        proposal.script_mode = "edit"
    else:
        run["input"]["text"] = "请查看上次两款商品的稿件，不要新增。"
        run["inherited_context"] = {"prior_goal": {"authorization_quote": FOLLOW_UP_SPAN}}
    assert bind_intent_authorization(run, proposal).authorization_quote is None
    with pytest.raises(ContractError, match="INTENT_EVIDENCE_INVALID"):
        validate_intent(run, proposal)


def test_unrecognized_or_tampered_button_manifest_keeps_legacy_evidence_requirement():
    refs = [{"kind": "product", "id": identity} for identity in PRODUCTS]
    for text in (button_text("other-prep", refs), button_text("prep", refs[:1]), HEADER + "\n只生成文字。\n" + SCOPE_MARKER + "\n{}"):
        run = make_run(text, intent_mode="new_task")
        assert bind_intent_authorization(run, make_proposal()).authorization_quote is None


def test_valid_original_evidence_does_not_allow_unrequested_mutations():
    run = make_run()
    for effect in ("save_product", "save_session_draft", "update_preparation_products", "prepare_required_audio"):
        proposal = bind_intent_authorization(run, make_proposal(proposed_effects=[effect]))
        with pytest.raises(ContractError, match="GOAL_SCOPE_CONFLICT"):
            validate_intent(run, proposal)


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_real_assessment_freezes_only_exact_selected_products_and_script_effects(env, selected):
    engine, service = env
    refs = [await product(service, "产品A"), await product(service, "产品B")]
    room = (await human_command(service, "create_room", {"name": "双商品隔离测试"}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    chosen = refs if selected else refs[:1]
    await human_command(service, "update_preparation_products", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "selected_product_refs": chosen})
    source = button_text(prep["id"], refs)
    run = await new_run(engine, source, room_id=room["id"], preparation_id=prep["id"])
    proposal = make_proposal([ref["id"] for ref in refs], preparation_id=prep["id"], authorization_quote="模型改写的原文")
    result = engine.save_assessment(RunScope(run["id"], "intent-test", "intent"), proposal.model_dump())
    assert result["assessment_saved"]
    current = engine.store.get("run", run["id"])
    assert current["intent_assessment"]["authorization_quote"] == HEADER
    if selected:
        await engine._freeze_goal(current)
        final = engine.store.get("run", run["id"])
        assert final["goal"]["original_input_hash"] == canonical_hash(current["input"])
        assert final["goal"]["action_target_refs"] == refs
        assert set(final["allowed_effects"]) == TEXT_EFFECTS
    else:
        with pytest.raises(ContractError, match="PRODUCT_NOT_SELECTED"):
            await engine._freeze_goal(current)
    after = (await service.query("read_preparation", {"preparation_id": prep["id"]}))["body"]
    assert after["selected_product_refs"] == chosen


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_workbench_generation_binds_saved_draft_before_freezing(env, changed):
    engine, service = env
    ref = await product(service, "产品A")
    room = (await human_command(service, "create_room", {"name": "保存配置测试"}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    prep = (await human_command(service, "update_preparation_products", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "selected_product_refs": [ref]}))["data"]["preparation_ref"]
    saved = (await human_command(service, "save_session_draft", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "draft": {"theme": "已核对的配置"}}))["data"]
    if changed:
        await human_command(service, "save_session_draft", {"preparation_id": prep["id"],
            "expected_revision": saved["preparation_ref"]["revision"], "draft": {"theme": "生成前另有修改"}})
    run = await new_run(engine, button_text(prep["id"], [ref], draft_ref=saved["draft_ref"]),
                        room_id=room["id"], preparation_id=prep["id"])
    proposal = make_proposal([ref["id"]], preparation_id=prep["id"])
    engine.save_assessment(RunScope(run["id"], "intent-test", "intent"), proposal.model_dump())
    current = engine.store.get("run", run["id"])
    if changed:
        with pytest.raises(ContractError, match="CONTEXT_INPUTS_CHANGED"):
            await engine._freeze_goal(current)
    else:
        await engine._freeze_goal(current)
        goal = engine.store.get("run", run["id"])["goal"]
        assert goal["required_draft_ref"] == saved["draft_ref"]
        assert goal["action_target_refs"] == [ref]
        assert goal["requested_script_variants"] == [{"product_id": ref["id"], "count": 1}]
        await human_command(service, "save_session_draft", {"preparation_id": prep["id"],
            "expected_revision": saved["preparation_ref"]["revision"], "draft": {"theme": "冻结后另有修改"}})
        newer = await service.query("read_preparation", {"preparation_id": prep["id"]})
        with pytest.raises(ContractError, match="CONTEXT_INPUTS_CHANGED"):
            await engine.business_command(RunScope(run["id"], "write-test", "execute"), "save_context_bundle",
                                          newer["context_inputs"], "substitute-newer-draft")
