"""A retained partial ModuleRef is not a complete script-revision baseline."""
from copy import deepcopy
import json

from test_recovery_workset_rc11 import by_key, make_job, project


def test_partial_result_note_explains_legal_choices_without_fabricating_baseline_or_action():
    job = make_job(); original = deepcopy(job)
    view = project(job)
    note = view["note"]
    assert "base_script_ref" in note and "不得伪造" in note
    assert "原job受控retry" in note and "未改输入" in note and "不保证命中" in note
    assert "非人工批准" in note and "未知不重发" in note
    assert job == original
    row = by_key(view, "opening_long")
    assert row["module_ref"] == original["body"]["items"][0]["module_results"]["opening_long"]["module_ref"]
    assert "base_script_ref" not in row["target"]
    assert row["status"] == "committed" and row["accepted_output_reader"]
    assert not view["approval_implied"] and not view["acceptance_implied"]
    assert "automatic_action_taken" not in json.dumps(view)


def test_existing_baseline_is_preserved_and_not_created_from_module_ref():
    job = make_job()
    base = {"kind": "script_revision", "id": "revision_existing", "revision": 2,
        "content_hash": "sha256:" + "c" * 64}
    job["body"]["items"][0]["input"]["base_script_ref"] = base
    original = deepcopy(job)
    view = project(job)
    assert by_key(view, "opening_long")["target"]["base_script_ref"] == base
    assert job == original
