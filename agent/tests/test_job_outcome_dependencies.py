"""Fast provenance/ancestry cases; no model, TTS, database or business writes."""
from copy import deepcopy

from test_runtime_business import BusinessError  # Also registers the real voice package.
from app.prelive.job_outcome_dependencies import _replacement


def test_manual_descendant_can_prove_the_exact_committed_replacement_but_not_a_copied_provenance():
    product = {"kind": "product", "id": "p", "revision": 1, "content_hash": "sha256:p"}
    old_ref = {"kind": "script_revision", "id": "committed", "revision": 1, "content_hash": "sha256:committed"}
    current_ref = {"kind": "script_revision", "id": "manual-edit", "revision": 1, "content_hash": "sha256:manual-edit"}
    spec = {"product_ref": product, "variant_id": "v5", "version_number": 5,
        "modules": [{"key": "cta", "action": "generate"}]}
    source = {"id": "lost", "kind": "scripts", "status": "interrupted"}
    source_item = {"input": spec, "provider_attempts": [{"state": "timeout_unknown", "purpose": "copywriting.cta",
        "sent_at": "2026-01-01T00:00:00+00:00"}]}
    body = {"preparation_id": "prep", "product_ref": product, "variant_id": "v5", "version_number": 5,
        "generation_provenance": {"job_id": "replacement", "item_id": "replacement-item"}}
    records = {"committed": {"ref": old_ref, "body": {**body, "parent_ref": None}},
        "manual-edit": {"ref": current_ref, "body": {**body, "parent_ref": old_ref}}}
    jobs = {"replacement": {"id": "replacement", "kind": "scripts", "preparation_id": "prep", "status": "succeeded",
        "created_at": "2026-01-02T00:00:00+00:00", "items": [{"item_id": "replacement-item", "input": spec,
            "status": "succeeded", "call_state": "result_received", "completed_at": "2026-01-02T00:00:01+00:00",
            "output": {"script_ref": old_ref}}]}}

    class Service:
        def _artifact(self, connection, ref, kind, preparation):
            result = records.get(ref["id"])
            if result is None or ref != result["ref"] or result["body"]["preparation_id"] != preparation:
                raise BusinessError("REF_HASH_MISMATCH")
            return deepcopy(result)

    service = Service()
    prep = {"id": "prep", "script_refs": [current_ref]}
    result = _replacement(service, None, prep, jobs, source, source_item)
    assert result["replacement_script_ref"] == old_ref and result["selected_script_ref"] == current_ref
    records["manual-edit"]["body"]["parent_ref"] = None
    assert _replacement(service, None, prep, jobs, source, source_item) is None
    records["manual-edit"]["body"]["parent_ref"] = current_ref
    assert _replacement(service, None, prep, jobs, source, source_item) is None
