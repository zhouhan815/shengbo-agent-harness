from copy import deepcopy

from shengbo_contracts.common import canonical_hash
from shengbo_prelive.result_destinations import project_results


def ref(kind, identity):
    return {"kind": kind, "id": identity, "revision": 2, "content_hash": "sha256:" + identity}


def receipt(command, data, *, paths=None, status="committed"):
    return {"command": command, "receipt": {"command": command, "status": status, "data": data,
            "changed_paths": paths or []}}


def test_shared_product_routes_to_confirmed_changed_document_ids_and_faq():
    product = ref("product", "product_a")
    changed = receipt("save_product", {
        "product_ref": product,
        "product": {"name": "测试商品", "knowledge_documents": [
            {"id": "doc_unchanged", "title": "用法"},
            {"id": "doc_changed", "title": "使用说明"},
            {"id": "doc_new", "title": "使用说明"},
        ]},
        "changed_knowledge_document_ids": ["doc_changed", "doc_new", "doc_deleted"],
        "destination": {"url": "https://example.com"},
    }, paths=["/knowledge_documents", "/faq"])
    run = {"room_id": "room_a", "scope": {"preparation_id": "prep_a"},
           "receipts": [changed, deepcopy(changed)]}

    destinations = [card["destination"] for card in project_results(run)]
    base = {"authority": "voice", "view": "products", "product_id": product["id"],
            "room_id": None, "preparation_id": None, "object_ref": product}
    assert destinations == [
        {**base, "field_path": "/faq"},
        {**base, "field_path": "/knowledge_documents", "knowledge_document_id": "doc_changed"},
        {**base, "field_path": "/knowledge_documents", "knowledge_document_id": "doc_new"},
    ]


def test_legacy_and_deletion_receipts_still_locate_the_knowledge_section():
    data = {"product_ref": ref("product", "product_a"), "product": {"knowledge_documents": [
        {"id": "doc_unchanged", "title": "使用说明"},
    ]}}
    for metadata in ({}, {"changed_knowledge_document_ids": []}):
        cards = project_results({"receipts": [receipt("save_product", {**data, **metadata}, paths=["/knowledge_documents"])]})
        assert len(cards) == 1
        assert cards[0]["destination"]["field_path"] == "/knowledge_documents"
        assert "knowledge_document_id" not in cards[0]["destination"]
    assert not project_results({"receipts": [receipt("save_product", data, status="accepted")]})


def test_script_and_configuration_results_keep_the_confirmed_preparation_scope():
    script = ref("script_revision", "script_a")
    prep = ref("preparation", "prep_a")
    run = {"room_id": "room_a", "scope": {"preparation_id": "prep_a"}, "receipts": [
        receipt("save_session_draft", {"preparation_ref": ref("preparation", "other")}),
        receipt("patch_script", {"script_ref": script, "replacement": {"module_key": "selling_points"}}),
        receipt("save_session_draft", {"preparation_ref": prep}),
    ]}
    script_destination, config_destination = [card["destination"] for card in project_results(run)]
    assert script_destination == {"authority": "voice", "view": "scripts", "object_ref": script,
        "room_id": "room_a", "preparation_id": "prep_a", "script_id": "script_a", "module_key": "selling_points",
        "room_section": "scripts"}
    assert config_destination == {"authority": "voice", "view": "session", "object_ref": prep,
        "room_id": "room_a", "preparation_id": "prep_a", "product_id": None, "room_section": "session"}
    for card in project_results(run):
        legacy_destination = {key: value for key, value in card["destination"].items() if key != "room_section"}
        assert card["artifact_id"] == "result:" + canonical_hash(legacy_destination).split(":", 1)[1]


def test_session_results_deep_link_to_confirmed_product_override_only():
    draft = ref("draft", "draft_a")
    prep = ref("preparation", "prep_a")
    data = {"preparation_ref": prep, "draft_ref": draft,
            "changed_product_override_ids": ["product_A", "product_A", None], "product_id": "product_B"}
    run = {"room_id": "room_a", "scope": {"preparation_id": prep["id"]},
           "receipts": [receipt("save_session_draft", data), receipt("save_session_draft", {
               **data, "preparation_ref": ref("preparation", "other"), "changed_product_override_ids": ["product_B"]})]}
    destinations = [card["destination"] for card in project_results(run)]
    base = {"authority": "voice", "view": "session", "object_ref": draft, "room_id": "room_a",
            "preparation_id": prep["id"], "room_section": "session"}
    assert destinations == [{**base, "product_id": None}, {**base, "product_id": "product_A",
        "field_path": "/draft/commerce/product_overrides/product_A"}]


def test_generated_result_requires_own_receipted_job_and_current_saved_script():
    script = ref("script_revision", "script_new")
    old = ref("script_revision", "script_old")
    foreign = ref("script_revision", "script_other")
    job = {"ref": ref("prelive_job", "job_a"), "body": {
        "kind": "scripts", "run_id": "run_a", "preparation_id": "prep_a", "items": [
            {"status": "succeeded", "output": {"script_ref": script}},
            {"status": "failed", "output": {"script_ref": old}},
            {"status": "succeeded", "output": {"script_ref": ref("script_revision", "no_longer_current")}},
        ],
    }}
    other_job = {"ref": ref("prelive_job", "job_b"), "body": {**job["body"], "run_id": "other_run",
        "items": [{"status": "succeeded", "output": {"script_ref": foreign}}]}}
    run = {"id": "run_a", "room_id": "room_a", "scope": {"preparation_id": "prep_a"}, "receipts": [
        receipt("submit_script_change", {"job_id": "job_a"}, status="accepted"),
        receipt("submit_script_change", {"job_id": "job_b"}, status="accepted"),
    ]}
    assert project_results(run) == []
    run["acceptance"] = {"observation": {"preparation": {"body": {"script_refs": [script, old, foreign]}},
                                       "jobs": [job, other_job]},
                         "script_coverage": {"formal_script_refs": [script, old, foreign]}}
    assert [card["ref"] for card in project_results(run)] == [script]
    recovered = ref("script_revision", "script_recovered")
    recovered_job = {"ref": other_job["ref"], "body": {**job["body"],
        "items": [{"status": "succeeded", "output": {"script_ref": recovered}}]}}
    final = {"preparation": {"body": {"script_refs": [script, recovered, foreign]}},
             "jobs": [job, recovered_job]}
    assert [card["ref"] for card in project_results(run, final_observation=final)] == [script, recovered]


def test_research_result_is_limited_to_visible_committed_audience_reports():
    report = ref("analysis", "audience_a")
    facts = ref("analysis", "facts_a")
    pending = ref("analysis", "audience_pending")
    outside = ref("analysis", "audience_other")
    foreign = ref("analysis", "audience_other_prep")
    def accepted(report_ref, preparation="prep_a"):
        return receipt("accept_analysis_report", {"state": "accepted", "analysis_ref": report_ref,
            "preparation_ref": ref("preparation", preparation)})
    run = {"id": "run_a", "room_id": "room_a", "scope": {"preparation_id": "prep_a"}, "receipts": [
        accepted(report), accepted(facts), accepted(pending), accepted(outside), accepted(foreign, "prep_b"),
    ]}
    branch = {"run_id": "run_a", "role": "audience_scenes", "status": "ACCEPTED"}
    branches = [
        {**branch, "accepted_ref": report},
        {**branch, "accepted_ref": facts, "role": "product_facts"},
        {**branch, "accepted_ref": pending, "status": "PREPARED"},
        {**branch, "accepted_ref": outside, "run_id": "other_run"},
        {**branch, "accepted_ref": foreign},
    ]
    assert [card["destination"] for card in project_results(run, branches)] == [{
        "authority": "voice", "view": "insights", "object_ref": report, "room_id": "room_a",
        "preparation_id": "prep_a", "product_id": None, "room_section": "insights",
    }]


def test_audio_result_needs_own_completed_job_and_exact_current_manifest():
    job = ref("prelive_job", "job_a")
    manifest = ref("audio_manifest", "audio_a")
    preparation = {"ref": ref("preparation", "prep_a"),
                   "body": {"room_id": "room_a", "audio_manifest_ref": manifest}}
    job_body = {"kind": "audio", "status": "succeeded", "run_id": "run_a", "room_id": "room_a",
                "preparation_id": "prep_a", "audio_manifest_ref": manifest}
    run = {"id": "run_a", "room_id": "room_a", "scope": {"preparation_id": "prep_a"},
           "receipts": [receipt("prepare_required_audio", {"job_ref": job, "job_id": job["id"]}, status="accepted")]}
    assert not project_results(run)
    def project(**job_changes):
        return project_results(run, final_observation={"preparation": preparation,
            "jobs": [{"ref": {**job, "revision": 4}, "body": {**job_body, **job_changes}}]})
    assert not project(status="running")
    assert not project(run_id="foreign")
    assert not project(preparation_id="prep_b")
    assert not project(room_id="room_b")
    assert not project(audio_manifest_ref={**manifest, "revision": 1})
    assert not project(audio_manifest_ref=None)
    assert [card["destination"] for card in project()] == [{
        "authority": "voice", "view": "scripts", "object_ref": manifest, "room_id": "room_a",
        "preparation_id": "prep_a", "room_section": "scripts",
    }]
