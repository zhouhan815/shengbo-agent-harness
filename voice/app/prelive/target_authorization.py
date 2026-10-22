"""Resolve actual write targets inside the same business transaction."""
from .storage import BusinessError


def advance_sentence_constraints(c, constraints, run_id):
    """Follow only business-committed same-run replacements, never model refs."""
    import json
    from copy import deepcopy
    rows = deepcopy(constraints)
    for event_row in c.execute("SELECT payload FROM prelive_events WHERE event_type='business.patch_script' ORDER BY seq"):
        event = json.loads(event_row[0])
        if event.get("run_id") != run_id: continue
        replacement = (event.get("result") or {}).get("replacement") or {}
        for row in rows:
            if (row.get("sentence_ids") and row.get("module_key") == replacement.get("module_key")
                    and row.get("sentence_ids") == replacement.get("old_sentence_ids")
                    and row.get("selected_text_hash") == replacement.get("old_selected_text_hash")
                    and all((row.get("script_ref") or {}).get(key) == (replacement.get("old_ref") or {}).get(key)
                            for key in ("id", "revision", "content_hash"))):
                row.update(script_ref=replacement["new_ref"], sentence_ids=replacement["sentence_ids"],
                           selected_text_hash=replacement["selected_text_hash"])
    return rows


def command_product_ids(service, c, command, args):
    def record(ref, kind=None):
        return service._artifact(c, ref, kind, getattr(args, "preparation_id", None))["body"]
    def script(ref):
        return record(ref, "script_revision")["product_id"]
    def plan_items(items):
        products = set()
        for item in items:
            value = item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else item
            products.add(value["product_ref"]["id"])
            for name in ("base_script_ref", "original_script_ref"):
                if value.get(name): products.add(script(value[name]))
        return products
    if command == "save_product":
        return {args.product_id} if args.product_id else set()
    if command == "save_script_plan": return plan_items(args.items)
    if command == "submit_script_change": return plan_items(record(args.plan_ref, "script_plan")["items"])
    if command == "patch_script": return {script(args.script_ref)}
    if command == "preview_script_audio":
        approved = record(args.preview_approval_ref, "script_audio_preview_approval")
        return {approved["binding"]["product_ref"]["id"]}
    if command == "import_script": return {args.product_ref.id}
    if command == "accept_analysis_report" and args.role != "session_config":
        return {ref.id for ref in args.product_refs}
    if command == "save_playlist":
        return {identity for row in args.items for identity in (row.product_id, script(row.script_revision_ref))}
    if command == "compile_playback_plan":
        return {row["product_id"] for row in record(args.playlist_ref, "playlist")["items"]}
    if command in {"prepare_required_audio", "publish_preparation"}:
        prep = service._prep(c, args.preparation_id)["body"]
        if prep.get("compiled_plan_ref"):
            return {row["product_id"] for row in record(prep["compiled_plan_ref"], "compiled_plan")["entries"]}
    if command == "retry_failed_items":
        job = service.store.get(c, "prelive_job", args.job_id)["body"]
        return {row["input"]["product_ref"]["id"] for row in job["items"]
                if row["status"] in {"failed", "interrupted"} and row["input"].get("product_ref")}
    if command == "update_preparation_products":
        old = {row["id"]: row for row in service._prep(c, args.preparation_id)["body"]["selected_product_refs"]}
        return {ref.id for ref in args.selected_product_refs if ref.id not in old or
                any(getattr(ref, key) != old[ref.id][key] for key in ("revision", "content_hash"))}
    # Context assembly is read access. Never infer write authority from refs
    # embedded in prose, source evidence, or a context bundle.
    return set()


def guard_targets(service, c, command, args, constraints, *, run_id=None):
    allowed = set()
    for row in constraints:
        ref = row.get("object_ref") or row
        if ref.get("kind") == "script_revision":
            allowed.add(service._artifact(c, ref, "script_revision")["body"]["product_id"])
        else:
            value = row.get("product_id") or row.get("id") or ref.get("id")
            if value: allowed.add(value)
    if run_id:
        import json
        # Business-owned committed creation events, never model-supplied IDs.
        for row in c.execute("SELECT payload FROM prelive_events WHERE event_type='business.save_product'"):
            event = json.loads(row[0])
            result = event.get("result") or {}
            if event.get("run_id") == run_id and result.get("created") is True:
                ref = result.get("product_ref") or {}
                if ref.get("kind") == "product":
                    allowed.add(service._artifact(c, ref, "product")["ref"]["id"])
    requested = command_product_ids(service, c, command, args)
    if requested - allowed:
        raise BusinessError("TARGET_DENIED", "嵌套业务目标超出本次授权", status=403)
