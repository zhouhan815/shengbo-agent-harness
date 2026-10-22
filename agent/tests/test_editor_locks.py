"""Global, minimal editor interlocks; no provider calls or production data."""
from fastapi.testclient import TestClient

from shengbo_prelive.api import create_app
from shengbo_prelive.editor_locks import project_editor_locks
from shengbo_prelive.store import RuntimeStore


def run(store, identity="r1", **changes):
    return store.put("run", {"id": identity, "status": "RUNNING", "scope": {"kind": "catalog", "account_id": "local"},
                             "input": {}, **changes})


def test_selection_locks_only_exact_product_field_across_conversations(tmp_path):
    store = RuntimeStore(tmp_path / "agent.sqlite3")
    for identity, product in (("r1", "p1"), ("r2", "p2")):
        run(store, identity, conversation_id="other-" + identity, status="QUEUED", input={"selection": {
            "target": {"object_ref": {"kind": "product", "id": product}, "field_path": "/price"}}})
    result = project_editor_locks(store)
    assert len(result["locks"]) == 2
    assert result["locks"][0]["targets"] == [{"kind": "product", "id": "p1", "field_paths": ["/price"]}]
    assert "input" not in str(result) and "conversation_id" not in str(result)


def test_script_module_scope_and_unrelated_regions(tmp_path):
    store = RuntimeStore(tmp_path / "agent.sqlite3")
    run(store, scope={"kind": "preparation", "preparation_id": "prep1"},
        selection_binding={"kind": "script_revision", "current_ref": {"kind": "script_revision", "id": "s1"},
                           "product_id": "p1", "module_key": "campaign"},
        allowed_effects=["patch_script"])
    targets = project_editor_locks(store)["locks"][0]["targets"]
    assert targets == [{"kind": "scripts", "id": "prep1", "script_id": "s1", "product_ids": ["p1"], "module_keys": ["campaign"]}]


def test_terminal_release_but_unknown_and_draining_remain_locked(tmp_path):
    store = RuntimeStore(tmp_path / "agent.sqlite3")
    for identity in ("done", "unknown", "draining", "cancelling"):
        run(store, identity, status="CANCELLED" if identity != "cancelling" else "CANCELLING",
            target_product_ids=[identity], allowed_effects=["save_product"])
    store.put("operation", {"id": "op1", "run_id": "unknown", "status": "unknown"})
    result = project_editor_locks(store, {"owners": [{"run_id": "draining"}]})
    assert {item["run_id"] for item in result["locks"]} == {"unknown", "draining", "cancelling"}


def test_script_replacement_locks_both_original_dom_and_new_revision(tmp_path):
    store = RuntimeStore(tmp_path / "agent.sqlite3")
    run(store, scope={"kind": "preparation", "preparation_id": "prep1"}, allowed_effects=["patch_script"],
        selection_binding={"kind": "script_revision", "base_ref": {"id": "old"}, "current_ref": {"id": "new"}, "module_key": "campaign"})
    targets = project_editor_locks(store)["locks"][0]["targets"]
    assert {item["script_id"] for item in targets} == {"old", "new"}
    assert all(item["module_keys"] == ["campaign"] for item in targets)


def test_readonly_and_new_product_do_not_lock_existing_products(tmp_path):
    store = RuntimeStore(tmp_path / "agent.sqlite3")
    run(store, "read", goal={"objective": "read_explain"}, target_product_ids=["p1"], allowed_effects=[])
    run(store, "new", allowed_effects=["save_product"])
    locks = project_editor_locks(store, {"owners": [{"run_id": "read"}]} )["locks"]
    assert len(locks) == 1 and locks[0]["targets"] == [{"kind": "product_create", "id": "catalog"}]


def test_newly_created_product_becomes_locked_while_agent_can_keep_updating_it(tmp_path):
    store = RuntimeStore(tmp_path / "agent.sqlite3")
    run(store, allowed_effects=["save_product"], created_product_ids=["new-product"])
    assert project_editor_locks(store)["locks"][0]["targets"] == [{"kind": "product", "id": "new-product", "field_paths": None}]


def test_preparation_field_and_full_prepare_targets(tmp_path):
    store = RuntimeStore(tmp_path / "agent.sqlite3")
    run(store, "field", scope={"kind": "preparation", "preparation_id": "prep1"},
        allowed_effects=["save_session_draft"], selection_binding={"kind": "preparation", "current_ref": {"id": "prep1"}, "field_path": "/draft/theme"})
    run(store, "full", scope={"kind": "preparation", "preparation_id": "prep2"}, target_product_ids=["p1"],
        allowed_effects=["save_product", "save_session_draft", "submit_script_change", "save_playlist", "save_asset_checklist", "accept_analysis_report", "update_preparation_products"])
    locks = project_editor_locks(store)["locks"]
    assert locks[0]["targets"] == [{"kind": "session", "id": "prep1", "field_paths": ["/draft/theme"]}]
    assert {item["kind"] for item in locks[1]["targets"]} == {"product", "session", "scripts", "playlist", "assets", "insights", "product_selection"}


def test_editor_lock_endpoint_requires_trusted_proxy_and_returns_projection():
    class Engine:
        def editor_locks(self):
            return {"schema_version": "PRELIVE-2.3", "locks": [{"run_id": "r", "targets": [{"kind": "product", "id": "p"}]}]}
    with TestClient(create_app(Engine(), "fixture-service-token")) as client:
        assert client.get("/api/assistant/v2/editor-locks").status_code == 401
        result = client.get("/api/assistant/v2/editor-locks", headers={"X-Shengbo-Service-Token": "fixture-service-token"})
        assert result.status_code == 200
        assert result.json()["locks"][0]["targets"][0]["id"] == "p"
