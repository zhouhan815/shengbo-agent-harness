"""R01/R02: isolated scope and formal coverage, without provider/OCR calls."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import IntentAssessment, RunScope
from shengbo_prelive.runtime import RuntimeEngine
from shengbo_prelive.target_scope import guard_command_targets, verify_script_coverage
from test_runtime_business import env, human_command, new_run, product


def ref(kind, identity, revision=1):
    return {"kind": kind, "id": identity, "revision": revision, "content_hash": canonical_hash([kind, identity, revision])}


A, B, C = [ref("product", identity) for identity in ("A", "B", "C")]


class Store:
    def __init__(self, run, operations=(), branches=()):
        self.run, self.operations, self.branches = run, list(operations), list(branches)

    def get(self, kind, identity, **kwargs):
        return self.run if kind == "run" and identity == self.run["id"] else None

    def list(self, kind):
        return {"operation": self.operations, "branch": self.branches}.get(kind, [])


def run_for(products=(A, B), objective="audience_research", **goal):
    return {"id": "run", "conversation_id": "conversation", "scope": {"kind": "preparation", "preparation_id": "prep"},
        "target_product_ids": [value["id"] for value in products], "action_target_refs": list(products),
        "goal": {"objective": objective, "required_analyses": ["audience_scenes"], **goal},
        "allowed_effects": [], "finish": {"outcome": "COMPLETED", "summary": "完成"}}


def operation(key, args, data, identity=None):
    return {"id": identity or key, "run_id": "run", "key": key, "status": "committed", "args": args,
        "request_hash": canonical_hash(args), "receipt": {"command": key, "status": "committed", "data": data}}


class Client:
    def __init__(self, documents):
        self.documents, self.calls = documents, []

    async def query(self, key, args, **kwargs):
        self.calls.append((key, deepcopy(args)))
        identity = args.get("preparation_id") if key == "read_preparation" else args.get("ref", {}).get("id")
        value = self.documents.get(identity)
        if value is None:
            raise ContractError("NOT_FOUND", "隔离记录不存在", status=404)
        return deepcopy(value)


def script(identity, product_ref=A, variant="v1", text="原文", **body):
    script_ref = ref("script_revision", identity)
    return {"ref": script_ref, "body": {"product_id": product_ref["id"], "product_ref": product_ref,
        "variant_id": variant, "module_refs": {"product_intro": ref("module", identity + "-intro")}, **body},
        "modules": {"product_intro": {"sentences": [{"text": text}]}}}


def observe(products=(A, B), scripts=(), analysis=()):
    return {"preparation": {"body": {"selected_product_refs": list(products), "script_refs": list(scripts),
        "analysis_refs": list(analysis)}}, "jobs": [], "pending_job_ids": [], "unknown_job_ids": []}


async def finish(run, observation, documents, operations=(), branches=()):
    async def observed(_):
        return deepcopy(observation)

    async def no_binding(*_):
        return None

    engine = SimpleNamespace(store=Store(run, operations, branches), client=Client(documents),
        _source_binding_owner_runs=lambda value: {value["id"]: value}, _completion_observation=observed,
        _verify_source_only_facts_binding=no_binding)
    return await RuntimeEngine.verify_finish(engine, RunScope(run["id"], "test", "execute"))


@pytest.mark.asyncio
async def test_freeze_keeps_explicit_action_target_and_full_readable_preparation(env):
    engine, service = env
    a, b = await product(service, "目标A"), await product(service, "上下文B")
    room = (await human_command(service, "create_room", {"name": "范围测试"}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    await human_command(service, "update_preparation_products", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "selected_product_refs": [a, b]})
    run = await new_run(engine, "仅修改商品" + a["id"] + "的话术", room_id=room["id"], preparation_id=prep["id"])
    proposal = IntentAssessment(objective="script_change", summary="只改A", authorization_quote=run["input"]["text"], target_product_ids=[a["id"]],
        script_mode="edit", requested_script_variants=[{"product_id": a["id"], "count": 1}])
    run = engine.store.update("run", run["id"], intent_assessment=proposal.model_dump())
    await engine._freeze_goal(run)
    frozen = engine.store.get("run", run["id"])
    assert frozen["target_product_ids"] == [a["id"]]
    assert frozen["action_target_refs"] == [a]
    assert frozen["context_product_refs"] == [a, b]
    assert frozen["goal"]["action_target_refs"] == [a]
    assert frozen["goal"]["context_product_refs"] == [a, b]
    assert frozen["script_mode"] == frozen["goal"]["script_mode"] == "edit"
    assert frozen["goal"]["requested_script_variants"] == [{"product_id": a["id"], "count": 1}]
    args = {"preparation_id": prep["id"], "expected_revision": 1, "bundle_ref": ref("context", "unneeded"),
        "items": [{"product_ref": b, "variant_id": "outside", "modules": []}]}
    with pytest.raises(ContractError, match="TARGET_DENIED"):
        await engine.business_command(RunScope(run["id"], "target-test", "execute"), "save_script_plan", args, "outside-plan")
    assert not [value for value in engine.store.list("operation") if value.get("key") == "save_script_plan"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command,args", [
    ("save_script_plan", {"items": [{"product_ref": B, "variant_id": "b"}]}),
    ("import_script", {"product_ref": B}),
    ("accept_analysis_report", {"role": "audience_scenes", "product_refs": [B], "report": {}}),
])
async def test_nested_commands_cannot_use_readable_other_product(command, args):
    run = run_for((A,)); run["context_product_refs"] = [A, B]
    with pytest.raises(ContractError, match="TARGET_DENIED"):
        await guard_command_targets(Client({}), Store(run), run, command, args)


@pytest.mark.asyncio
async def test_scope_does_not_treat_context_bundle_or_scene_report_as_product_write():
    run = run_for((A,))
    await guard_command_targets(Client({}), Store(run), run, "save_context_bundle", {"product_refs": [A, B]})
    await guard_command_targets(Client({}), Store(run), run, "accept_analysis_report", {"role": "session_config", "product_refs": [A, B]})


@pytest.mark.asyncio
async def test_execute_uses_exact_plan_witness_not_broader_bundle():
    run = run_for((A,)); plan_ref = ref("script_plan", "plan")
    op = operation("save_script_plan", {"items": [{"product_ref": B, "variant_id": "v"}]}, {"script_plan_ref": plan_ref})
    with pytest.raises(ContractError, match="TARGET_DENIED"):
        await guard_command_targets(Client({}), Store(run, [op]), run, "submit_script_change", {"plan_ref": plan_ref})
    body = {"id": "prep", "plan_ref": None}
    prep = {"ref": {**ref("preparation", "prep"), "content_hash": canonical_hash(body)}, "body": body}
    client = Client({"prep": prep})
    with pytest.raises(ContractError, match="TARGET_SCOPE_UNVERIFIED") as caught:
        await guard_command_targets(client, Store(run), run, "submit_script_change", {"plan_ref": plan_ref})
    assert caught.value.details["commit_state"] == "not_committed"
    assert client.calls == [("read_preparation", {"preparation_id": "prep", "sections": ["script_plan"]})]
    op["args"]["items"][0]["product_ref"] = A
    op["request_hash"] = canonical_hash(op["args"])
    await guard_command_targets(Client({}), Store(run, [op]), run, "submit_script_change", {"plan_ref": plan_ref})


@pytest.mark.asyncio
async def test_patch_playlist_and_base_script_resolve_actual_product():
    run = run_for((A,)); other = script("other", B)
    client, store = Client({"other": other}), Store(run)
    for command, args in [("patch_script", {"script_ref": other["ref"]}),
        ("save_playlist", {"items": [{"product_id": "A", "script_revision_ref": other["ref"]}]}),
        ("save_script_plan", {"items": [{"product_ref": A, "base_script_ref": other["ref"]}]})]:
        with pytest.raises(ContractError, match="TARGET_DENIED"):
            await guard_command_targets(client, store, run, command, args)


@pytest.mark.asyncio
async def test_exact_frozen_revision_and_current_run_created_receipt():
    run = run_for((A,)); changed = ref("product", "A", 2)
    args = {"items": [{"product_ref": changed}]}
    with pytest.raises(ContractError, match="TARGET_DENIED"):
        await guard_command_targets(Client({}), Store(run), run, "save_script_plan", args)
    op = operation("save_product", {"product_id": "A"}, {"product_ref": changed})
    await guard_command_targets(Client({}), Store(run, [op]), run, "save_script_plan", args)
    run = run_for(())
    run["created_product_ids"] = ["B"]
    op = operation("save_product", {}, {"product_ref": B})
    await guard_command_targets(Client({}), Store(run, [op]), run, "import_script", {"product_ref": B})


@pytest.mark.asyncio
async def test_role_coverage_requires_every_exact_target_but_not_context_product():
    run = run_for()
    ar, br = ref("analysis", "a-report"), ref("analysis", "b-report")
    documents = {"a-report": {"body": {"role": "audience_scenes", "accepted": True, "preparation_id": "prep", "product_refs": [A]}}}
    observation = observe((A, B, C), analysis=[ar, br])
    result = await finish(run, observation, documents)
    assert not result["ok"] and any("audience_scenes/B" in value for value in result["missing"])
    documents["b-report"] = {"body": {"role": "audience_scenes", "accepted": True, "preparation_id": "prep", "product_refs": [B]}}
    assert (await finish(run, observation, documents))["ok"]
    documents["b-report"]["body"]["product_refs"] = [ref("product", "B", 2)]
    assert not (await finish(run, observation, documents))["ok"]


@pytest.mark.asyncio
async def test_multi_product_report_covers_both_and_scene_report_is_one_obligation():
    run = run_for()
    ar = ref("analysis", "combined")
    documents = {"combined": {"body": {"role": "audience_scenes", "accepted": True, "preparation_id": "prep", "product_refs": [A, B]}}}
    assert (await finish(run, observe(analysis=[ar]), documents))["ok"]
    run = run_for(objective="session_config", required_analyses=["session_config"])
    documents["combined"]["body"].update(role="session_config", product_refs=[])
    result = await finish(run, observe(analysis=[ar]), documents, [operation("save_session_draft", {}, {})])
    assert result["ok"]


@pytest.mark.asyncio
async def test_branch_status_and_empty_report_do_not_prove_per_product_coverage():
    run = run_for(objective="facts_analysis", required_analyses=[])
    branch = {"id": "branch", "run_id": "run", "role": "product_facts", "status": "ACCEPTED", "accepted_ref": ref("analysis", "empty")}
    result = await finish(run, observe(), {"empty": {"body": {"role": "product_facts", "accepted": True, "product_refs": []}}}, branches=[branch])
    assert not result["ok"] and not result["analysis_coverage"]


@pytest.mark.asyncio
async def test_bundle_coverage_is_per_product_and_source_binding_covers_only_its_saved_product():
    run = run_for(objective="materials_to_script")
    ar, br, bundle = ref("analysis", "a"), ref("analysis", "b"), ref("context", "bundle")
    documents = {"a": {"body": {"role": "audience_scenes", "accepted": True, "preparation_id": "prep", "product_refs": [A]}},
        "b": {"body": {"role": "audience_scenes", "accepted": True, "preparation_id": "prep", "product_refs": [B]}},
        "bundle": {"body": {"analysis_refs": [ar]}}}
    observation = observe(analysis=[ar, br]); observation["preparation"]["body"]["bundle_ref"] = bundle
    result = await finish(run, observation, documents)
    assert any("尚未进入本场话术上下文：audience_scenes/B" in value for value in result["missing"])
    assert not any("尚未进入本场话术上下文：audience_scenes/A" in value for value in result["missing"])
    run = run_for(objective="facts_analysis", required_analyses=["product_facts"])
    report = {"body": {"role": "product_facts", "accepted": True, "preparation_id": "prep", "product_refs": []}}
    async def observed(_):
        return observe(analysis=[ar])
    async def binding(*_):
        return {"product_ref": A, "operation_id": "save-A"}
    engine = SimpleNamespace(store=Store(run), client=Client({"a": report}), _source_binding_owner_runs=lambda value: {value["id"]: value},
        _completion_observation=observed, _verify_source_only_facts_binding=binding)
    result = await RuntimeEngine.verify_finish(engine, RunScope("run", "source-bound", "execute"))
    assert any("product_facts/B" in value for value in result["missing"])
    assert result["analysis_coverage"] == {"product_facts": [A]}


def script_job(items):
    return {"ref": ref("prelive_job", "job"), "body": {"items": items}}


def job_item(document, status="succeeded", output=True):
    return {"status": status, "input": {"product_ref": document["body"]["product_ref"], "variant_id": document["body"]["variant_id"]},
        "output": {"script_ref": document["ref"]} if output else None}


@pytest.mark.asyncio
async def test_formal_coverage_requires_both_plan_variants_and_actual_job_outputs():
    run = run_for((A,), "script_change", required_analyses=[])
    first, second = script("first"), script("second", variant="v2")
    op = operation("submit_script_change", {"plan_ref": ref("script_plan", "plan")}, {"job_id": "job"})
    observation = observe((A,), scripts=[first["ref"]])
    observation["jobs"] = [script_job([job_item(first), job_item(second, "failed", False)])]
    result = await verify_script_coverage(Client({"first": first}), Store(run, [op]), run, [op], observation, [A])
    assert any("A/v2" in value for value in result["missing"])
    observation["preparation"]["body"]["script_refs"].append(second["ref"])
    # An old formal version with the same variant cannot conceal a failed job.
    result = await verify_script_coverage(Client({"first": first, "second": second}), Store(run, [op]), run, [op], observation, [A])
    assert any("成功结果" in value for value in result["missing"])
    observation["jobs"] = [script_job([job_item(first), job_item(second)])]
    assert not (await verify_script_coverage(Client({"first": first, "second": second}), Store(run, [op]), run, [op], observation, [A]))["missing"]


@pytest.mark.asyncio
async def test_old_other_product_and_command_name_are_not_task_success():
    run = run_for((A,), "script_change", required_analyses=[])
    other = script("other", B)
    shell = {"id": "shell", "run_id": "run", "key": "submit_script_change", "status": "committed"}
    result = await verify_script_coverage(Client({"other": other}), Store(run, [shell]), run, [shell], observe(scripts=[other["ref"]]), [A])
    assert "本次话术真实修改或生成回执" in result["missing"]
    assert any("A" in value for value in result["missing"])


@pytest.mark.asyncio
async def test_imported_original_and_patch_chain_use_formal_receipts_without_seven_modules():
    run = run_for((A,), "script_change", required_analyses=[])
    original, edited = script("original", origin="imported"), script("edited")
    imported = operation("import_script", {"product_ref": A}, {"script_ref": original["ref"]})
    patched = operation("patch_script", {"script_ref": original["ref"]}, {"script_ref": edited["ref"]})
    operations = [imported, patched]
    result = await verify_script_coverage(Client({"edited": edited}), Store(run, operations), run, operations, observe((A,), scripts=[edited["ref"]]), [A])
    assert not result["missing"] and result["semantic_support_verified"] is False
    edited["modules"]["product_intro"]["sentences"] = []
    assert (await verify_script_coverage(Client({"edited": edited}), Store(run, operations), run, operations, observe((A,), scripts=[edited["ref"]]), [A]))["missing"]


@pytest.mark.asyncio
async def test_legacy_version_count_includes_new_products_and_excludes_old_unproduced_variants():
    # Historical Goal intentionally lacks script_mode; explicit generate has separate v7 tests.
    run = run_for((A,), "materials_to_script", required_analyses=[], requested_script_variants=[{"product_id": None, "count": 2}])
    one, old = script("one"), script("old", variant="old-v2")
    imported = operation("import_script", {"product_ref": A}, {"script_ref": one["ref"]})
    observation = observe((A,), scripts=[one["ref"], old["ref"]])
    result = await verify_script_coverage(Client({"one": one, "old": old}), Store(run, [imported]), run, [imported], observation, [A])
    assert any("版数不足" in value for value in result["missing"])
    second = operation("import_script", {"product_ref": A}, {"script_ref": old["ref"]}, "import-2")
    assert not (await verify_script_coverage(Client({"one": one, "old": old}), Store(run, [imported, second]), run, [imported, second], observation, [A]))["missing"]


def test_explicit_count_contract_is_bounded_and_unique():
    base = {"objective": "script_change", "summary": "两版", "authorization_quote": "生成两版话术"}
    assert IntentAssessment(**base, requested_script_variants=[{"count": 2}]).requested_script_variants[0].product_id is None
    for values in [[{"count": 0}], [{"count": True}], [{"count": 2}, {"count": 3}], [{"product_id": "A", "count": 1}, {"product_id": "A", "count": 2}]]:
        with pytest.raises(ValidationError):
            IntentAssessment(**base, requested_script_variants=values)


@pytest.mark.asyncio
async def test_pure_import_finish_omits_implicit_research_context_but_keeps_explicit_roles():
    run = run_for((A,), "materials_to_script", required_analyses=[], script_mode="import_original")
    original = script("original", origin="imported")
    imported = operation("import_script", {"product_ref": A}, {"script_ref": original["ref"]})
    observation = observe((A,), scripts=[original["ref"]])
    result = await finish(run, observation, {"original": original}, [imported])
    assert result["ok"]
    run["goal"]["required_analyses"] = ["audience_scenes"]
    result = await finish(run, observation, {"original": original}, [imported])
    assert not result["ok"] and any("audience_scenes" in value for value in result["missing"])
    report_ref = ref("analysis", "explicit-research")
    observation["preparation"]["body"]["analysis_refs"] = [report_ref]
    documents = {"original": original, "explicit-research": {"body": {"role": "audience_scenes", "accepted": True,
        "preparation_id": "prep", "product_refs": [A]}}}
    assert (await finish(run, observation, documents, [imported]))["ok"]


@pytest.mark.asyncio
async def test_declared_import_mode_cannot_generate_and_expansion_requires_original_ref():
    run = run_for((A,), "materials_to_script", script_mode="import_original")
    with pytest.raises(ContractError, match="SCRIPT_MODE_CONFLICT") as caught:
        await guard_command_targets(Client({}), Store(run), run, "submit_script_change", {})
    assert caught.value.details["commit_state"] == "not_committed"
    run["goal"]["script_mode"] = "expand_original"
    with pytest.raises(ContractError, match="SCRIPT_MODE_CONFLICT"):
        await guard_command_targets(Client({}), Store(run), run, "save_script_plan", {"items": [{"product_ref": A}]})
    original = script("original")
    original["ref"]["content_hash"] = canonical_hash(original["body"])
    client = Client({"original": original})
    await guard_command_targets(client, Store(run), run, "save_script_plan",
        {"items": [{"product_ref": A, "original_script_ref": original["ref"]}]})
    assert client.calls == [("read_script", {"ref": original["ref"]})]
    other = script("other-original", product_ref=B)
    other["ref"]["content_hash"] = canonical_hash(other["body"])
    with pytest.raises(ContractError, match="TARGET_DENIED"):
        await guard_command_targets(Client({"other-original": other}), Store(run), run, "save_script_plan",
            {"items": [{"product_ref": A, "original_script_ref": other["ref"]}]})


@pytest.mark.asyncio
async def test_main_can_read_observed_exact_context_report_without_granting_write(env):
    engine, _service = env
    run = await new_run(engine, "只修改A，B仅作同本场上下文")
    report_ref = ref("analysis", "observed-context-report")
    run = engine.store.update("run", run["id"], phase="execute", status="RUNNING", target_product_ids=["A"],
        scope={"kind": "preparation", "account_id": "local", "preparation_id": "prep"},
        context_product_refs=[A, B], action_target_refs=[A], observed_refs=[report_ref])
    document = {"ref": report_ref, "body": {"role": "audience_scenes", "preparation_id": "prep",
        "product_refs": [A, B], "report": {"product_ids": ["A", "B"]}}}
    async def query(key, args, **kwargs):
        assert key == "read_analysis_report"
        return deepcopy(document)
    engine.client.query = query
    scope = RunScope(run["id"], "context-reader", "execute")
    result = await engine.business_query(scope, "read_context", {"ref": report_ref})
    assert result["body"]["product_refs"] == [A, B]
    document["body"]["product_refs"] = [A, ref("product", "B", 2)]
    with pytest.raises(ContractError, match="ANALYSIS_READ_DENIED"):
        await engine.business_query(scope, "read_context", {"ref": report_ref})
    with pytest.raises(ContractError, match="TARGET_DENIED"):
        await guard_command_targets(engine.client, engine.store, run, "import_script", {"product_ref": B})
