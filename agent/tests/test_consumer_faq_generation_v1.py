"""FAQ skill/source/tool wiring over isolated native SDK and Voice stores.

The model is scripted: this verifies model-visible inputs, fact/missing data
placement, and receipted save behavior, not live-model writing quality.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

from deepagents.backends.utils import file_data_to_string
from langchain_core.messages import AIMessage
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.models import IntentAssessment
from shengbo_prelive.runtime import CURRENT_SCOPE
from test_product_materials_exact_candidate import case, stats
from test_runtime_business import env, human_command, new_run, Product
from test_selection_dependency_recovery_v1 import ScriptedModel


PREFIX = "/skills/product_facts/prelive-product-facts/"
BANK = Path(__file__).resolve().parents[1] / "skills/product_facts/prelive/references/consumer-faq-examples.json"
EXAMPLES = json.loads(BANK.read_text(encoding="utf-8"))["examples"]


@pytest.mark.asyncio
@pytest.mark.parametrize("example", EXAMPLES, ids=lambda value: value["case_id"])
async def test_native_facts_worker_gets_style_and_real_source_then_saves_faq_separately(env, example):
    engine, service = env
    await engine.start()
    engine.scheduler.cancel()
    await asyncio.gather(engine.scheduler, return_exceptions=True)
    text = "依据上传资料新建商品并整理购买问答，只整理基础商品信息，不开展顾客研究。"
    run = await new_run(engine, text)
    name = "TESTONLY问答-" + example["case_id"]
    source_text = name + "，商家确认零售价19.9元。\n" + "\n".join(example["provided_facts"])
    source = engine.materials.accept(run["conversation_id"], "verified-facts.txt", "text/plain", source_text.encode())["material_ref"]
    engine.store.update("run", run["id"], input={**run["input"], "material_refs": [source]},
        intent_assessment=IntentAssessment(objective="catalog_update", summary="仅基础商品与购买问答",
            authorization_quote=text, required_analyses=["product_facts"], catalog_delivery="basic_facts_only",
            catalog_delivery_quote=text).model_dump(mode="json"))
    await engine._freeze_goal(engine.store.get("run", run["id"]))
    run = engine.store.update("run", run["id"], task_assessment={"summary": "按真实来源整理FAQ", "capabilities": ["product_facts"]})
    branch_id = "faq-" + example["case_id"]
    engine.store.put("branch", {"id": branch_id, "run_id": run["id"], "role": "product_facts", "status": "RUNNING",
        "task": {"role": "product_facts", "instruction": "只依本次资料整理购买问答，未知留分析缺项。",
            "product_ids": [], "source_refs": [source], "input_refs": []}})
    fields = Product(name=name, sku_id=example["case_id"], price=19.9, faq=example["faq"]).model_dump()
    report = {"summary": "已知购买问题可答；使用或服务条件不足的仅列缺项。", "fields": fields,
        "source_refs": [source], "missing_fields": example["missing_fields"],
        "evidence": [{"id": "actual-facts", "source_ref": source, "locator": "text:1", "quote": source_text}],
        "material_assessment": {
            "product_candidates": [{"candidate_id": "faq-product", "name": name, "identity_status": "new", "evidence_ids": ["actual-facts"]}],
            "field_observations": [{"observation_id": "faq-facts", "product_candidate_id": "faq-product",
                "field_path": "/faq", "value": example["faq"], "claim_kind": "fact", "evidence_ids": ["actual-facts"]}],
            "missing_info": [{"item_id": "missing-" + str(index), "product_candidate_id": "faq-product",
                "field_path": "/faq", "level": "recommended", "why": "没有本商品支持事实，暂不写成答案",
                "question": question, "can_skip": True} for index, question in enumerate(example["unanswered_questions"])]}}
    stages = {"product_facts": 0, "main": 0}

    def call(role, name, args):
        return AIMessage(content="", tool_calls=[{"id": role + "-faq-" + str(stages[role]), "name": name, "args": args}])

    async def logic(messages):
        role = CURRENT_SCOPE.get().role
        stage = stages[role]
        stages[role] += 1
        if role == "product_facts":
            if stage == 0:
                return call(role, "read_file", {"file_path": PREFIX + "SKILL.md", "offset": 0, "limit": 2000})
            if stage == 1:
                assert "消费者购买问答" in messages[-1].content
                assert "consumer-faq-examples.json" in messages[-1].content
                return call(role, "read_file", {"file_path": PREFIX + "references/consumer-faq-examples.json", "offset": 0, "limit": 2000})
            if stage == 2:
                assert "隔离教学样例" in messages[-1].content
                return call(role, "read_source", {"ref": source, "section": "text:1"})
            if stage == 3:
                assert example["provided_facts"][0] in messages[-1].content
                return call(role, "read_business_schema", {})
            if stage == 4:
                assert "faq_generation" in messages[-1].content
                assert "文风示例与合成问答不是商品证据" in messages[-1].content
                return call(role, "submit_product_facts_report", {"report": report})
            raise AssertionError("The worker must stop when its report is prepared")
        if stage == 0:
            return call(role, "accept_analysis_reports", {"branch_task_ids": [branch_id]})
        if stage == 1:
            branch = engine.store.get("branch", branch_id)
            assert branch["status"] == "ACCEPTED"
            return call(role, "wf_product_materials", {"action": "save_from_facts", "parameters": {
                "branch_task_id": branch_id, "accepted_report_ref": branch["accepted_ref"]}})
        assert stage == 2
        return call(role, "finish_turn", {"outcome": "COMPLETED", "summary": "隔离问答已保存，未确认条件留分析缺项。"})

    engine.model = ScriptedModel(logic)
    engine.workers["product_facts"] = engine._build_graph("product_facts")
    await engine._invoke(run, branch=engine.store.get("branch", branch_id))
    prepared = engine.store.get("branch", branch_id)
    assert prepared["status"] == "PREPARED"
    assert prepared["report"]["missing_fields"] == example["missing_fields"]
    engine.main = engine._build_graph("main")
    await engine._invoke(engine.store.get("run", run["id"]))
    assert stages == {"product_facts": 5, "main": 3}
    operations = [op for op in engine.store.list("operation") if op["key"] == "save_product"]
    assert len(operations) == 1 and operations[0]["status"] == "committed"
    saved = (await service.query("read_product", {"ref": operations[0]["receipt"]["data"]["product_ref"]}))["body"]
    assert saved["faq"] == example["faq"] and saved["knowledge_documents"] == []
    assert all(question not in saved["faq"] for question in example["unanswered_questions"])
    assert all(claim not in saved["faq"] for claim in example["must_not_invent"])
    formal = (await service.query("read_analysis_report", {"ref": engine.store.get("branch", branch_id)["accepted_ref"]}))["body"]["report"]
    assert formal["missing_fields"] == example["missing_fields"]
    assert len(formal["evidence"]) == 1 and formal["evidence"][0]["locator"] == "text:1"
    assert all(item["field_path"] == "/faq" for item in formal["material_assessment"]["missing_info"])
    assert not engine.store.list("question"), "Optional missing FAQ facts must not force a human interruption"
    reads = [item for item in engine.store.list("skill_read") if item["branch_task_id"] == branch_id]
    assert reads and all(item["role"] == "product_facts" for item in reads)
    assert file_data_to_string(engine._initial_files("product_facts")[PREFIX + "references/consumer-faq-examples.json"]) == BANK.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_faq_style_update_preserves_existing_answers_price_and_other_product(env):
    from shengbo_prelive.product_materials import save_accepted_product
    engine, service = env
    original_faq = "问：支持定制吗？\n答：不支持定制。"
    combined_faq = original_faq + "\n\n" + EXAMPLES[1]["faq"]
    run, scope, _, branch, ref = await case(engine, service, {"faq": combined_faq})
    original = Product(name="TESTONLY已确认问答", sku_id="faq-existing", price=33,
        material="已确认材质", faq=original_faq, knowledge_documents=[{"id": "care", "title": "保养", "content": "按说明保养。"}]).model_dump()
    target = (await human_command(service, "save_product", {"schema_hash": service.schema()["schema_hash"], "fields": original, "patches": []}))["data"]["product_ref"]
    other = (await human_command(service, "save_product", {"schema_hash": service.schema()["schema_hash"],
        "fields": Product(name="TESTONLY其他商品", sku_id="faq-other", price=77, faq=original_faq).model_dump(), "patches": []}))["data"]["product_ref"]
    other_before = deepcopy(await service.query("read_product", {"ref": other}))
    engine.store.update("run", run["id"], target_product_ids=[target["id"]], allowed_product_paths=["/faq"])
    saved = await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref,
        "product_ref": target}, tool_call_id="faq-only-style-save")
    body = (await service.query("read_product", {"ref": saved["data"]["product_ref"]}))["body"]
    assert body["faq"].startswith(original_faq) and body["faq"] == combined_faq
    assert {key: body[key] for key in original if key != "faq"} == {key: value for key, value in original.items() if key != "faq"}
    assert await service.query("read_product", {"ref": other}) == other_before
    op = next(op for op in engine.store.list("operation") if op["key"] == "save_product")
    assert op["args"]["patches"] == [{"path": "/faq", "op": "set", "value": combined_faq}]
    before = stats(service)
    with pytest.raises(ContractError, match="TARGET_DENIED"):
        await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref,
            "product_ref": other}, tool_call_id="faq-deny-other-product")
    assert stats(service) == before
