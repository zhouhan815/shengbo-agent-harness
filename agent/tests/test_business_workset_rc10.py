"""Observed exact action references: no graph, providers, or business mutation."""
from copy import deepcopy

import pytest

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.document_reads import business_observation, project_business_workset
from shengbo_prelive.models import RunScope
from test_business_document_reads_rc9 import case, env, reads


def ref(kind, identity):
    return {"kind": kind, "id": identity, "revision": 1, "content_hash": canonical_hash([kind, identity])}


def script_body(label="a", revision=1):
    return {"product_id": "product-1", "variant_id": label, "version_number": 2,
        "script_revision": revision, "module_refs": {
            "opening_short": ref("module", label + "-opening"),
            "selling_points": ref("module", label + "-selling"), "transition": None}}


def record(query, artifact, body, stamp, **overrides):
    return {"kind": "business_query_result", "run_id": "run-1", "branch_task_id": None,
        "scope_hash": canonical_hash("scope-1"), "query": query, "artifact_ref": artifact,
        "ref": ref("runtime_context", "snapshot-" + stamp), "reader_path": "/result",
        "last_returned_at": stamp, "coverage": "full", "read_count": 1,
        "acceptance_implied": False, "approval_implied": False,
        **business_observation(query, {"ref": artifact, "body": body}), **overrides}


def prep_body(script_refs):
    return {"script_refs": script_refs, "generation_epoch": 2, "playlist_ref": ref("playlist", "queue"),
        "compiled_plan_ref": None, "requirements_ref": None, "approval_ref": None,
        "audio_manifest_ref": None, "candidate_publication_ref": None}


def test_latest_observed_prep_selects_exact_revision_and_deduplicates_coverage():
    old, current = ref("script_revision", "old"), ref("script_revision", "new")
    a = record("read_script", old, script_body(revision=1), "01")
    b = record("read_script", current, script_body(revision=2), "02")
    c = record("read_script", current, script_body(revision=2), "03", coverage="section_view")
    p = record("read_preparation", ref("preparation", "prep"), prep_body([current]), "04")
    raw = deepcopy([a, b, c, p])
    view = project_business_workset(raw)
    assert raw == [a, b, c, p]
    assert view["status"] == "observed_snapshot" and view["selected_script_count"] == 1
    assert view["currentness_verified"] is view["approval_implied"] is view["acceptance_implied"] is False
    assert view["observed_only"] is True
    item, = view["scripts"]
    assert item["artifact_ref"] == current and item["reader"]["ref"] == c["ref"]
    assert item["identity"]["version_number"] == 2 and item["identity"]["script_revision"] == 2
    assert item["artifact_ref"]["revision"] == 1, "immutable record revision is not the variant's logical revision"
    assert item["module_refs"] == {k: v for k, v in script_body(revision=2)["module_refs"].items() if v}
    assert item["module_refs_reader"]["path"] == "/result/body/module_refs"
    assert "old" not in str(view) and "transition" not in item["module_refs"]


@pytest.mark.parametrize("key,value", [("kind", "module"), ("id", "different-script"),
    ("revision", 2), ("content_hash", canonical_hash("other-body"))])
def test_all_four_reference_identity_fields_must_match(key, value):
    selected = ref("script_revision", "selected")
    observed = {**selected, key: value}
    rows = [record("read_script", observed, script_body(), "01"),
        record("read_preparation", ref("preparation", "prep"), prep_body([selected]), "02")]
    item, = project_business_workset(rows)["scripts"]
    assert item["module_index_status"] == "script_not_read" and "module_refs" not in item


@pytest.mark.parametrize("scope_field,value", [("run_id", "foreign"),
    ("branch_task_id", "sibling"), ("scope_hash", canonical_hash("other-scope"))])
def test_foreign_read_records_cannot_supply_a_selected_module_index(scope_field, value):
    selected = ref("script_revision", "selected")
    rows = [record("read_script", selected, script_body(), "01", **{scope_field: value}),
        record("read_preparation", ref("preparation", "prep"), prep_body([selected]), "02")]
    assert project_business_workset(rows)["scripts"][0]["module_index_status"] == "script_not_read"


def test_legacy_latest_prep_and_legacy_script_remain_readable_without_inventing_state():
    selected = ref("script_revision", "selected")
    legacy_script = record("read_script", selected, script_body(), "01")
    legacy_script.pop("script_observation")
    prep = record("read_preparation", ref("preparation", "prep"), prep_body([selected]), "02")
    item, = project_business_workset([legacy_script, prep])["scripts"]
    assert item["module_index_status"] == "module_index_not_captured"
    assert item["reader"] == {"ref": legacy_script["ref"], "path": "/result"}
    latest = record("read_preparation", ref("preparation", "later-prep"), {}, "03")
    latest.pop("preparation_observation")
    view = project_business_workset([legacy_script, prep, latest])
    assert view["status"] == "preparation_observation_unavailable" and not view["scripts"]
    assert view["preparation"]["reader"]["ref"] == latest["ref"]


def test_bounded_expansion_reports_overflow_count_and_exact_original_reader():
    refs = [ref("script_revision", "script-" + str(i)) for i in range(10)]
    rows = [record("read_script", r, script_body(str(i)), f"{i:02}") for i, r in enumerate(refs)]
    prep = record("read_preparation", ref("preparation", "prep"), prep_body(refs), "99")
    view = project_business_workset([*rows, prep])
    assert view["selected_script_count"] == 10 and view["expanded_script_count"] == 8
    assert view["omitted_script_count"] == 2 and len(view["scripts"]) == 8
    assert view["available_module_index_count"] == 8
    assert "script_refs" not in view["preparation"]["fields"]
    window = view["preparation"]["script_refs_window"]
    assert window["total"] == 10 and window["complete"] is False and window["items"] == refs[:8]
    assert window["reader"] == {"ref": prep["ref"], "path": "/result/body/script_refs"}


@pytest.mark.parametrize("bad", [{"unknown_module": ref("module", "x")},
    {"selling_points": ref("public_response", "not-a-formal-module")},
    {"selling_points": {**ref("module", "x"), "content_hash": "sha256:bad"}},
    {"selling_points": {**ref("module", "x"), "revision": True}}])
def test_invalid_refs_never_become_actionable_or_false_acceptance(bad):
    selected = ref("script_revision", "script")
    body = {**script_body(), "module_refs": bad}
    row = record("read_script", selected, body, "01")
    assert row["script_observation"]["invalid_fields"] == ["module_refs"]
    view = project_business_workset([row,
        record("read_preparation", ref("preparation", "prep"), prep_body([selected]), "02")])
    item, = view["scripts"]
    assert item["module_index_status"] == "module_index_not_captured" and "module_refs" not in item
    assert item["reader"]["ref"] == row["ref"] and view["approval_implied"] is False


def test_disagreeing_observations_of_same_immutable_ref_do_not_choose_a_module_set():
    selected = ref("script_revision", "script")
    a = record("read_script", selected, script_body(), "01")
    b = record("read_script", selected, {**script_body(), "module_refs": {"selling_points": ref("module", "changed")}}, "02")
    p = record("read_preparation", ref("preparation", "prep"), prep_body([selected]), "03")
    item, = project_business_workset([a, b, p])["scripts"]
    assert item["module_index_status"] == "ambiguous_observations" and "module_refs" not in item


def test_missing_preparation_fields_are_not_projected_as_null_or_ready():
    p = record("read_preparation", ref("preparation", "prep"), {"script_refs": []}, "01")
    view = project_business_workset([p])
    fields = view["preparation"]["fields"]
    assert fields == {"script_refs": []}
    assert "approval_ref" in view["preparation"]["missing_fields"]
    assert "generation_epoch" in view["preparation"]["missing_fields"]
    assert view["currentness_verified"] is False


@pytest.mark.parametrize("bound", [0, 9, True])
def test_script_index_bound_cannot_be_silently_removed(bound):
    with pytest.raises(ValueError):
        project_business_workset([], max_scripts=bound)


@pytest.mark.asyncio
async def test_real_successful_read_indexes_preserve_original_result_snapshot_and_acl(case):
    engine, service, run, scope, prep, script, _ = case
    before_script = await service.query("read_script", {"ref": script})
    actual_script = await engine.business_query(scope, "read_script", {"ref": script})
    before_prep = await service.query("read_preparation", {"preparation_id": prep["id"]})
    actual_prep = await engine.business_query(scope, "read_preparation", {"preparation_id": prep["id"]})
    assert before_script == actual_script and before_prep == actual_prep
    ledger = deepcopy(reads(engine, scope))
    view = project_business_workset(ledger)
    item, = view["scripts"]
    assert item["module_refs"] == {k: v for k, v in before_script["body"]["module_refs"].items() if v}
    assert item["identity"]["variant_id"] == before_script["body"]["variant_id"]
    assert view["preparation"]["fields"]["compiled_plan_ref"] == before_prep["body"]["compiled_plan_ref"]
    assert view["preparation"]["fields"]["approval_ref"] is None
    assert view["approval_implied"] is view["acceptance_implied"] is view["currentness_verified"] is False
    for reader in (item["reader"], item["module_refs_reader"], view["preparation"]["reader"]):
        exact = engine.run_history(scope, {**reader, "limit": 100, "max_chars": 1000000})["document"]
        if reader == item["module_refs_reader"]:
            assert exact == before_script["body"]["module_refs"]
        elif reader == item["reader"]:
            assert exact == before_script
        else:
            assert exact == before_prep
    assert ledger == reads(engine, scope)
    other = engine.create_conversation({"scope": {"kind": "catalog"}})
    foreign = engine.store.put("run", {**run, "id": "foreign-workset-reader", "conversation_id": other["id"]})
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        engine.run_history(RunScope(foreign["id"], "foreign", "execute"), item["module_refs_reader"])
    with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
        engine.run_history(scope, {**item["module_refs_reader"], "ref": {**item["reader"]["ref"], "content_hash": "sha256:bad"}})
    assert not engine.store.list("operation") and not engine.store.list("question")
