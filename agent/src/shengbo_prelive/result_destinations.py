"""Project confirmed Voice business results onto the workbench's known views."""
from __future__ import annotations

from pydantic import ValidationError

from shengbo_contracts.business import MODULE_KEYS
from shengbo_contracts.common import SCHEMA_VERSION, ArtifactRef, canonical_hash, same_artifact_ref


PRODUCT_FIELDS = {
    "/name": "商品名称", "/sku_id": "商品编号", "/sale_status": "在售状态",
    "/sales_platform": "销售平台", "/price": "日常售价", "/currency": "币种",
    "/image": "商品图片", "/core_selling_points": "核心卖点", "/specifications": "规格参数",
    "/faq": "常见问题", "/knowledge": "商品知识库", "/knowledge_documents": "补充知识文档",
    "/material": "材质", "/ingredients": "成分", "/target_audience": "目标顾客",
    "/storage_conditions": "储存条件", "/shelf_life_value": "保质期", "/shelf_life_unit": "保质期单位",
    "/shipping_origin": "发货地", "/shipping_time": "发货时效", "/return_policy": "售后政策",
    "/usage_instructions": "使用方法", "/hidden_optional_fields": "可选字段",
}
PREPARATION_RESULTS = {
    "save_session_draft": ("draft_ref", "draft", "session", "查看已保存本场配置"),
    "update_preparation_products": ("preparation_ref", "preparation", "products", "查看已保存本场商品"),
    "refresh_product_reference": ("preparation_ref", "preparation", "products", "查看已保存本场商品"),
    "save_playlist": ("playlist_ref", "playlist", "scripts", "查看已保存播放顺序"),
    "publish_preparation": ("publication_ref", "publication", "scripts", "查看待采用的播出版本"),
    "save_asset_checklist": ("asset_checklist_ref", "asset_checklist", "overview", "查看已保存展示材料清单"),
}


def _ref(value, kind):
    try:
        ref = ArtifactRef.model_validate(value, strict=True)
    except ValidationError:
        return None
    return value if ref.schema_version == SCHEMA_VERSION and ref.kind == kind and ref.id else None


def _artifact(label, ref, **destination):
    destination = {"authority": "voice", "object_ref": ref, **destination}
    artifact_id = "result:" + canonical_hash(destination).split(":", 1)[1]
    # The existing view/ref still opens the same result in older workbenches.
    # The combined room workbench can also address its section explicitly.
    if destination.get("preparation_id"):
        destination["room_section"] = destination["view"]
    return {"artifact_id": artifact_id,
            "label": label, "ref": ref, "destination": destination}


def _products(receipt, data):
    ref = _ref(data.get("product_ref"), "product")
    if ref is None:
        return
    product = data.get("product") or {}
    name = product.get("name") or "商品"
    base = {"view": "products", "product_id": ref["id"], "preparation_id": None, "room_id": None}
    paths = set(receipt.get("changed_paths") or data.get("changed_paths") or [])
    if "/selling_points" in paths:
        paths.add("/core_selling_points")
    docs = {doc["id"]: doc for doc in (product.get("knowledge_documents") or [])
            if isinstance(doc, dict) and isinstance(doc.get("id"), str) and doc["id"]}
    created = data.get("created") is True
    if created:
        if docs:
            paths.add("/knowledge_documents")
        if product.get("faq"):
            paths.add("/faq")
    changed_docs = [docs[identity] for identity in (data.get("changed_knowledge_document_ids") or [])
                    if isinstance(identity, str) and identity in docs]
    if created or not (paths & PRODUCT_FIELDS.keys() or changed_docs):
        yield _artifact(f"查看商品 · {name}", ref, **base)
    for path, field_name in PRODUCT_FIELDS.items():
        if path == "/knowledge_documents" and changed_docs:
            for doc in changed_docs:
                title = doc.get("title") or field_name
                yield _artifact(f"查看知识文档 · {title}", ref, **base, field_path=path,
                                knowledge_document_id=doc["id"])
        elif path in paths and (not created or path in {"/knowledge_documents", "/faq"}):
            yield _artifact(f"查看{field_name} · {name}", ref, **base, field_path=path)


def _script(ref, scope, label, module_key=None):
    target = {"module_key": module_key} if module_key in MODULE_KEYS else {}
    return _artifact(label, ref, **scope, view="scripts", script_id=ref["id"], **target)


def _generated_scripts(run, scope, final_observation):
    # An accepted submission proves the job identity only. A saved Voice
    # observation must independently identify the completed, retained output.
    observation = final_observation if final_observation is not None else (
        (run.get("acceptance") or {}).get("observation") or {})
    current = (observation.get("preparation") or {}).get("body", {}).get("script_refs", [])
    by_job = {}
    for job in observation.get("jobs", []):
        body = job.get("body") or {}
        job_ref = _ref(job.get("ref"), "prelive_job")
        if (not job_ref or body.get("kind") != "scripts"
                or body.get("run_id") != run["id"] or body.get("preparation_id") != scope["preparation_id"]):
            continue
        for item in body.get("items", []):
            ref = _ref((item.get("output") or {}).get("script_ref"), "script_revision")
            if (item.get("status") == "succeeded" and ref
                    and any(same_artifact_ref(ref, candidate) for candidate in current)):
                by_job.setdefault(job_ref["id"], []).append(_script(ref, scope, "查看已生成话术"))
    return by_job


def _generated_audio(run, scope, final_observation):
    observation = final_observation if final_observation is not None else (
        (run.get("acceptance") or {}).get("observation") or {})
    preparation = observation.get("preparation") or {}
    ref = _ref(preparation.get("ref"), "preparation")
    body = preparation.get("body") or {}
    manifest = _ref(body.get("audio_manifest_ref"), "audio_manifest")
    if (not ref or not manifest or ref["id"] != scope["preparation_id"]
            or not scope["room_id"] or body.get("room_id") != scope["room_id"]):
        return {}
    by_job = {}
    for job in observation.get("jobs", []):
        result = job.get("body") or {}
        job_ref = _ref(job.get("ref"), "prelive_job")
        if (job_ref and result.get("kind") == "audio" and result.get("status") == "succeeded"
                and result.get("run_id") == run["id"] and result.get("preparation_id") == ref["id"]
                and result.get("room_id") == scope["room_id"]
                and same_artifact_ref(result.get("audio_manifest_ref"), manifest)):
            by_job[job_ref["id"]] = _artifact("查看已生成的本场语音", manifest, **scope, view="scripts")
    return by_job


def project_results(run, branches=(), *, final_observation=None):
    """Use durable business receipts and verified output only, never model links."""
    prep_id = (run.get("scope") or {}).get("preparation_id")
    scope = {"preparation_id": prep_id, "room_id": run.get("room_id")}
    audience_refs = [branch.get("accepted_ref") for branch in branches
                     if branch.get("run_id") == run.get("id") and branch.get("status") == "ACCEPTED"
                     and branch.get("role") == "audience_scenes"]
    confirmed = []
    generated = _generated_scripts(run, scope, final_observation) if prep_id else {}
    audio = _generated_audio(run, scope, final_observation) if prep_id else {}
    for entry in run.get("receipts", []):
        receipt = entry.get("receipt") or {}
        command = entry.get("command")
        data = receipt.get("data") or {}
        if receipt.get("command") != command:
            continue
        if command == "submit_script_change" and receipt.get("status") == "accepted":
            confirmed.extend(generated.get(data.get("job_id"), []))
        if command == "prepare_required_audio" and receipt.get("status") in {"accepted", "replayed"}:
            job = _ref(data.get("job_ref"), "prelive_job")
            if job and job["id"] == data.get("job_id") and job["id"] in audio:
                confirmed.append(audio[job["id"]])
        if receipt.get("status") not in {"committed", "replayed"}:
            continue
        if command == "save_product":
            confirmed.extend(_products(receipt, data))
        elif prep_id and command == "accept_analysis_report" and data.get("state") == "accepted":
            ref = _ref(data.get("analysis_ref"), "analysis")
            preparation = _ref(data.get("preparation_ref"), "preparation")
            if (ref and preparation and preparation["id"] == prep_id
                    and any(same_artifact_ref(ref, candidate) for candidate in audience_refs)):
                confirmed.append(_artifact("查看已保存的顾客与卖点建议", ref, **scope,
                                           view="insights", product_id=None))
        elif prep_id and command in {"patch_script", "import_script"}:
            if ref := _ref(data.get("script_ref"), "script_revision"):
                module = (data.get("replacement") or {}).get("module_key")
                confirmed.append(_script(ref, scope, "查看已保存话术", module))
        elif prep_id and command in PREPARATION_RESULTS and not data.get("unchanged"):
            preparation = _ref(data.get("preparation_ref"), "preparation")
            key, kind, view, label = PREPARATION_RESULTS[command]
            ref = _ref(data.get(key), kind)
            if command == "save_session_draft" and key not in data:
                ref = preparation
            if preparation and preparation["id"] == prep_id and ref:
                confirmed.append(_artifact(label, ref, **scope, view=view, product_id=None))
                if command == "save_session_draft" and ref["kind"] == "draft":
                    for identity in data.get("changed_product_override_ids", []):
                        if isinstance(identity, str) and identity:
                            path = identity.replace("~", "~0").replace("/", "~1")
                            confirmed.append(_artifact("查看已保存的本场商品设置", ref, **scope,
                                view=view, product_id=identity, field_path=f"/draft/commerce/product_overrides/{path}"))
    return list({card["artifact_id"]: card for card in confirmed}.values())
