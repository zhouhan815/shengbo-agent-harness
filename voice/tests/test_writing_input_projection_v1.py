"""Full source fields acquire writing roles without summaries or source mutation.

No model calls. The service harness uses scripted HTTP for exact payload/CAS
checks; these tests do not establish real marketing copy quality.
"""
from copy import deepcopy
import json
import unittest

import pytest

from app.prelive.storage import BusinessError, digest
from app.prelive.writing_input_projection import project_writing_context, PROJECTION_REVISION
import test_whole_script_generation_v1 as whole


def ref(kind, identity):
    return {"kind": kind, "id": identity, "revision": 1, "content_hash": "sha256:" + "a" * 64}


def source_context():
    product = {"name": "另一种商品", "usage_instructions": "仅适用已注明的范围。",
               "return_policy": "适用退货需商品完好。", "shipping_time": "依订单承诺时效。"}
    report = {
        "summary": "旧阶段已完成审查，仅进行研究，不生成话术。",
        "fields": {
            "segments": [{"id": "S1", "who": "初次使用者", "need": "想更方便完成真实用途",
                "purchase_reason": "已有特点对应需要", "scene_ids": ["C1"],
                "concerns": ["实际使用条件是什么？"], "status": "适用为事实，需求为推断"}],
            "scenes": [{"id": "C1", "situation": "在已确认环境中使用", "task": "完成真实用途",
                "support": "商品已确认用途", "value": "不用分别找所列物品",
                "evidence_ids": ["E1"], "classification": "事实与场景推断分开"}],
            "benefit_priorities": [{"rank": 1, "angle": "方便准备", "audience": "初次使用者",
                "customer_value": "所列物品一起选择", "explanation_focus": "说清所列内容",
                "scene_ids": ["C1"], "fact_evidence_ids": ["E1"],
                "boundary": "不因此推出未提供的性能"}],
            "concerns": [
                {"question": "未知参数是多少？", "gap": "参数未提供",
                 "handling": "未知参数不能补写", "evidence_ids": ["E1"]},
                {"question": "已知选项怎样选？", "handling": "仍应依据当前有效选项判断",
                 "evidence_ids": ["E1"]},
                {"question": "这项没有gap也有答案吗？", "evidence_ids": ["E1"]}],
            "context": {"merchant_background": "商家已确认背景", "scope": "只研究，不输出直播稿"},
            "known_product_facts": {"real_limit": "仅适用已注明的范围。"},
            "downstream_handoff": {"avoid": "接下来只给主Agent写五条候选"},
            "history_disposition": {"action": "旧研究流程安排"},
            "novel_business_section": {"new_value": "完整保留未来业务字段，不靠白名单丢弃"}},
        "insights": [{"id": "I1", "conclusion": "真实特点对应真实需要",
            "application": "说明用途与实际选项", "evidence_ids": ["E1"], "classification": "研究结论"}],
        "hypotheses": ["需求偏好是场景假设，不新增性能。"],
        "evidence": [{"evidence_id": "E1", "ref": ref("product", "p"),
            "locator": "/usage_instructions", "excerpt": product["usage_instructions"],
            "source_type": "product_record", "supports": "旧证据核查过程，不充当写作任务"}],
        "asset_suggestions": [{"recommended": "旧素材待办"}],
        "missing_fields": ["旧缺项排查"],
        "future_research_business": {"metric": "未来新增的完整研究业务值"}}
    return {"product_refs": [ref("product", "p")], "analysis_refs": [ref("analysis", "a")],
        "records": [{"ref": ref("product", "p"), "body": product},
            {"ref": ref("analysis", "a"), "body": {"role": "audience_scenes", "accepted": True,
                "report": report, "lineage": {"old_process": "不生成话术"}}},
            {"ref": ref("draft", "d"), "body": {"draft": {"theme": "中性商品讲解",
                "objective": "历史本场目标", "commerce": {"coupon": {"minimum_spend": 300,
                    "amount": 60, "condition": "先领券，不与其他优惠叠加"}}}}}],
        "user_decisions": [{"type": "approved_selling_points", "items": ["已确认营销卖点完整原文"]},
            {"type": "full_rewrite_contract", "instruction": "当前任务写主播销售口播"},
            {"type": "unclassified_decision", "value": "未来用户业务决定不静默丢弃"}],
        "prior_script_refs": [ref("script_revision", "old")], "variant": {"variant_id": "v1", "version_number": 1}}


def v3_spec():
    return {"generation_mode": "whole_script", "version_context_contract": "existing-script-text-v3"}


@pytest.mark.parametrize("marker", [None, "existing-script-text-v1", "existing-script-text-v2"])
def test_archived_v1_v2_contexts_remain_identical(marker):
    original = source_context()
    snapshot = deepcopy(original)
    result = project_writing_context(original, {"generation_mode": "whole_script", "version_context_contract": marker})
    assert result == snapshot and digest(result) == digest(snapshot)
    assert original == snapshot


def test_local_edits_do_not_opt_into_whole_version_projection():
    original = source_context()
    assert project_writing_context(original, {**v3_spec(), "generation_mode": "module"}) is original


def test_complete_business_values_and_relationships_survive_with_qualification_paths():
    original = source_context()
    snapshot = deepcopy(original)
    result = project_writing_context(original, v3_spec())
    report = result["records"][1]["body"]["report"]
    source = original["records"][1]["body"]["report"]
    constraints = {item["source_path"]: item["value"] for item in report["claim_constraints"]}
    for name in ("segments", "scenes", "benefit_priorities"):
        before, after = source["fields"][name], report["fields"][name]
        assert len(before) == len(after)
        for index, row in enumerate(before):
            for key, value in row.items():
                if key in {"boundary", "classification", "status"}:
                    assert constraints[f"/body/report/fields/{name}/{index}/{key}"] == value
                else:
                    assert after[index][key] == value
    for key, value in source["insights"][0].items():
        if key == "classification":
            assert constraints["/body/report/insights/0/classification"] == value
        else:
            assert report["insights"][0][key] == value
    assert report["fields"]["novel_business_section"] == source["fields"]["novel_business_section"]
    assert report["future_research_business"] == source["future_research_business"]
    assert report["research_assumptions"]["value"] == source["hypotheses"]
    assert report["fields"]["scenes"][0]["value"].startswith("不用"), "No negative-word deletion"
    assert original == snapshot


def test_unknown_concerns_are_constraints_and_no_gap_is_not_an_answerability_claim():
    result = project_writing_context(source_context(), v3_spec())
    report = result["records"][1]["body"]["report"]
    assert len(report["customer_questions"]) == 2
    assert all(item["answerability"] == "verify_against_current_facts" and item["required_to_speak"] is False
               for item in report["customer_questions"])
    assert not any("未知参数" in str(item) for item in report["customer_questions"])
    gap = next(item for item in report["claim_constraints"] if item["source_path"].endswith("/concerns/0"))
    assert gap["value"] == source_context()["records"][1]["body"]["report"]["fields"]["concerns"][0]
    assert gap["use"] == "unverified_claim_boundary_not_a_spoken_question"


def test_old_workflow_commands_are_source_references_not_the_current_writing_task():
    original = source_context()
    result = project_writing_context(original, v3_spec())
    data = json.dumps(result, ensure_ascii=False)
    for process_text in ("旧阶段已完成审查", "只研究，不输出直播稿", "旧素材待办", "旧缺项排查",
                         "旧证据核查过程", "接下来只给主Agent写五条候选", "旧研究流程安排"):
        assert process_text not in data
    report = result["records"][1]["body"]["report"]
    evidence = report["evidence"][0]
    for key in ("evidence_id", "ref", "locator", "excerpt", "source_type"):
        assert evidence[key] == original["records"][1]["body"]["report"]["evidence"][0][key]
    assert "/body/report/evidence/0/supports" in report["projection_provenance"]["process_sections_retained_only_in_source"]
    assert result["records"][1]["source_artifact_ref"] == original["records"][1]["ref"]
    assert result["current_assignment"]["explicit_user_writing_requests"][0]["value"] == original["user_decisions"][1]
    assert [item["value"] for item in result["user_business_decisions"]] == [original["user_decisions"][0], original["user_decisions"][2]]


def test_product_limits_and_full_commerce_theme_stay_verbatim_with_explicit_theme_role():
    original = source_context()
    result = project_writing_context(original, v3_spec())
    assert result["records"][0] == original["records"][0]
    assert result["records"][2]["body"] == original["records"][2]["body"]
    assert result["records"][2]["writing_use"]["theme"] == "business_topic_not_a_writing_style_instruction"
    assert result["writing_input_contract"]["revision"] == PROJECTION_REVISION
    assert result["product_refs"] == original["product_refs"]
    assert result["analysis_refs"] == original["analysis_refs"]
    assert result == project_writing_context(original, v3_spec()), "No clock/live-head-dependent input"


@pytest.mark.parametrize("role,report", [
    ("audience_scenes", {"summary": "唯一业务结论：这项用途应当先介绍。", "fields": {}, "insights": []}),
    ("unknown_research", {"summary": "唯一完整结论，不得被默默丢弃。"}),
    ("unknown_research", {"summary": "这是与fields不同的唯一补充。", "fields": {"segments": ["已有业务数据"]}}),
    ("product_facts", {"summary": "当前事实报告的唯一结论。", "fields": {"custom": "另一条事实"}}),
])
def test_nonstandard_or_summary_only_research_keeps_its_only_business_conclusion(role, report):
    context = {"records": [{"ref": ref("analysis", "summary-only"), "body": {"role": role, "report": report}}]}
    before = deepcopy(context)
    projected = project_writing_context(context, v3_spec())["records"][0]["body"]["report"]
    assert projected["summary"] == report["summary"]
    assert projected["projection_provenance"]["summary_role"] == "retained_research_data_not_task_instructions"
    assert context == before


def test_new_business_sections_named_like_projection_roles_are_not_overwritten():
    source = {"customer_questions": ["原报告新增业务问题"],
        "claim_constraints": {"unique": "原报告完整业务约束"},
        "research_assumptions": ["原报告自己的业务假设"],
        "projection_provenance": {"business": "即便字段同名也完整保留"}}
    context = {"records": [{"ref": ref("analysis", "collisions"), "body": {"role": "future_role", "report": source}}]}
    projected = project_writing_context(context, v3_spec())["records"][0]["body"]["report"]
    preserved = {row["source_path"]: row["value"] for row in
                 projected["projection_provenance"]["source_values_sharing_projection_field_names"]}
    assert preserved == {"/body/report/" + key: value for key, value in source.items()}


class ProjectionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = whole.WholeServiceTests()
        await self.fixture.asyncSetUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_new_v3_plan_actual_wire_and_stale_cas_keep_source_unchanged(self):
        f = self.fixture
        prep, submitted = await f.setup_job(2)
        job = await f.read_job(submitted)
        frozen = deepcopy(job["context"])
        assert all(item["input"]["version_context_contract"] == "existing-script-text-v3" for item in job["items"])
        state = await f.service.query("read_preparation", {"preparation_id": prep["id"]})
        with self.assertRaises(BusinessError) as caught:
            await f.cmd("save_script_plan", {"preparation_id": prep["id"],
                "expected_revision": state["ref"]["revision"] - 1, "bundle_ref": job["context_ref"],
                "items": [{"product_ref": job["items"][0]["input"]["product_ref"],
                           "variant_id": "stale", "modules": whole.actions()}]})
        assert caught.exception.code == "REVISION_CONFLICT"
        assert f.sent == []
        await f.service.drain_jobs()
        after = await f.read_job(submitted)
        assert after["status"] == "succeeded"
        assert after["context"] == frozen
        assert len(f.sent) == 2
        for request in f.sent:
            assert request["context_bundle"]["writing_input_contract"]["revision"] == PROJECTION_REVISION
            assert request["context_bundle"]["current_assignment"]["module_tasks_path"] == "/module_instructions"
        assert f.sent[0]["context_bundle"]["writing_input_contract"] == f.sent[1]["context_bundle"]["writing_input_contract"]
