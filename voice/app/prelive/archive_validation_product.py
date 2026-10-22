"""Verify archived validation facts against immutable generation inputs.

Plain adapters receive the catalog snapshot. ExistingVoiceAdapter applies the
exact draft's commerce overrides before generation. Both are deterministic full
snapshots; neither arbitrary receipt fields nor a live room head is trusted.
"""
from .. import commerce_config as commerce


def verified_archived_validation_product(product, context, archived):
    if not isinstance(archived, dict):
        return None
    if archived == product:
        return {"product": product, "contract": "catalog_snapshot"}
    records = context.get("records", [])
    records = list(records.values()) if isinstance(records, dict) else records
    if not isinstance(records, list):
        return None
    drafts = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if context.get("draft_ref") and record.get("ref") != context["draft_ref"]:
            continue
        body = record.get("body", record)
        if isinstance(body, dict) and isinstance(body.get("draft"), dict):
            drafts.append(body["draft"])
    if len(drafts) != 1:
        return None
    # This is the same calculation as ExistingVoiceAdapter.generate_script
    # followed by generate_one_script's active-fact projection. It uses only
    # the exact context draft and pinned catalog revision, never current heads.
    effective = commerce.active_product_fields(commerce.effective_product(product, {
        "commerce": commerce.parse_room(drafts[0].get("commerce", {}))}))
    if archived != effective:
        return None
    return {"product": effective, "contract": "frozen_room_effective_snapshot"}
