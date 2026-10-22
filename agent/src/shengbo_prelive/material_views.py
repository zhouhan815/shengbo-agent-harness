"""Small model-facing views and measurements; original manifests stay immutable."""
from copy import deepcopy
from shengbo_contracts.common import canonical_hash, now_iso, same_artifact_ref


def _visual_scope(engine, scope):
    run = engine.store.get("run", scope.run_id)
    branch = engine.store.get("branch", scope.branch_task_id) if scope.branch_task_id else None
    product_ids = branch["task"].get("product_ids", []) if branch else [
        *run.get("target_product_ids", []), *run.get("created_product_ids", [])]
    return {"run_id": scope.run_id, "branch_task_id": scope.branch_task_id,
            "product_ids": sorted(set(product_ids)),
            "scope_kind": "selected_products" if product_ids else "source_candidates"}


def record_visual_reviews(engine, scope, report, task, report_hash):
    """Persist the declared scoped working set without changing the report/hash."""
    assessment = report.get("material_assessment") or {}
    selected_scope = _visual_scope(engine, scope)
    for review in assessment.get("visual_reviews") or []:
        identity = "visual_review_" + canonical_hash([
            scope.run_id, scope.branch_task_id, report_hash, review["review_id"]])[7:]
        engine.store.put("visual_review", {
            "id": identity, "run_id": scope.run_id, "branch_task_id": scope.branch_task_id,
            "source_ref": review["source_ref"], "locator": review["locator"],
            "selected_scope": selected_scope, "report_hash": report_hash,
            "review": deepcopy(review), "created_at": now_iso(),
            "semantic_support_verified": False, "acceptance_implied": False})
    engine._emit(scope.run_id, "material.visual_review.recorded", {
        "title": "视觉工作集判断已记录", "selected_scope": selected_scope,
        "review_count": len(assessment.get("visual_reviews") or []),
        "coverage": "declared_subset" if assessment.get("visual_reviews") else "unknown",
        "delivery_is_understanding": False, "semantic_support_verified": False}, scope)


def visual_review_observation(engine, scope, source_ref):
    """A bounded view of current reports for this source and user product scope.

    A source index may contain 92 images. Absence from this declared subset is
    always unknown; neither many text characters nor delivery closes the set.
    """
    selected_scope = _visual_scope(engine, scope)
    reviews = []
    for row in engine.store.list("visual_review"):
        if row.get("run_id") != scope.run_id or not same_artifact_ref(row.get("source_ref"), source_ref):
            continue
        if scope.branch_task_id and row.get("branch_task_id") != scope.branch_task_id:
            continue
        branch = engine.store.get("branch", row["branch_task_id"], required=False)
        if not branch or branch.get("report_hash") != row.get("report_hash"):
            continue  # Old report revisions cannot fill a current working set.
        branch_products = set(row["selected_scope"].get("product_ids", []))
        if selected_scope["product_ids"] and not branch_products <= set(selected_scope["product_ids"]):
            continue
        reviews.append({"branch_task_id": row["branch_task_id"], "report_hash": row["report_hash"],
                        "review_scope": deepcopy(row["selected_scope"]),
                        **deepcopy(row["review"])})
    return {"selected_scope": selected_scope, "coverage": "declared_subset" if reviews else "unknown",
            "unreported_locator_state": "unknown", "reviews": reviews[:24],
            "review_count": len(reviews), "reviews_truncated": len(reviews) > 24,
            "full_reports_reader": "read_analysis_batch(report_mode='full')",
            "delivery_is_understanding": False, "semantic_support_verified": False}


def source_view(value):
    result = deepcopy(value)
    manifest = result.get("manifest")
    if not isinstance(manifest, dict):
        return result
    # Return prose once, not in text + manifest.text + every locator + page.
    manifest.pop("text", None)
    for key in ("locators", "pages"):
        entries = manifest.get(key)
        if isinstance(entries, list):
            manifest[key] = [{**{k:v for k,v in item.items() if k != "text"},
                              "text_chars":len(item.get("text", ""))} for item in entries]
    result["manifest_is_projection"] = True
    result["view_note"] = "正文只返回一次；定位列表保留id和字符数。原始manifest及原文件未改，具体块或图片用read_source(ref, section=定位id)，完整文字可显式section='full'。"
    return result


def record_material_read(engine, scope, value, *, section, duration_ms):
    reference=value["source_material_ref"]
    identity="source_read_"+canonical_hash([scope.run_id,scope.branch_task_id,reference,section])[7:]
    previous=engine.store.get("document_read",identity,required=False) or {}
    manifest=value.get("manifest",{})
    visual = manifest.get("visual_workset")
    if visual is None and value.get("view") == "visual_workset":
        visual = {key: manifest[key] for key in ("items", "total", "next_section") if key in manifest}
    record={"id":identity,"run_id":scope.run_id,"branch_task_id":scope.branch_task_id,
        "kind":"material_source_result","artifact_ref":reference,"section":section,
        "reader":{"tool":"read_source","ref":reference,"section":section},
        "read_count":previous.get("read_count",0)+1,"last_returned_at":now_iso(),
        "returned_text_chars":len(value.get("text","")),"duration_ms":duration_ms,
        "coverage":manifest.get("coverage",{}),"source_name":value.get("name"),
        "visual_review_coverage":value.get("visual_review_observation",{}).get("coverage","unknown"),
        "visual_review_scope":value.get("visual_review_observation",{}).get("selected_scope"),
        "visual_workset":deepcopy(visual),
        "acceptance_implied":False}
    engine.store.put("document_read",record)
    engine._emit(scope.run_id,"material.read.completed",{
        "title":"资料读取完成","source_ref":reference,"section":section,
        "duration_ms":duration_ms,"text_chars":record["returned_text_chars"],
        "read_count":record["read_count"],"coverage":record["coverage"],
        "visual_review_coverage":record["visual_review_coverage"],
        "semantic_acceptance":False},scope)
    return record
