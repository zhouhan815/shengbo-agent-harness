"""Immutable, product-scoped writing references for distinct full versions.

The context bundle fixes the previous revisions and the job fixes the variant
briefs. Never pull live preparation heads here: retries must see identical input.
Previous marketing copy is comparison evidence, not a new source of facts or a
positive style example. Version 1 inputs remain unchanged for archived jobs.
"""
from copy import deepcopy


def _full_rewrite(spec):
    actions = spec.get("modules", [])
    return (spec.get("generation_mode") == "whole_script" and bool(spec.get("base_script_ref"))
            and any(action.get("action") == "generate" for action in actions)
            and all(action.get("action") in {"generate", "disable"} for action in actions))


def _identity(ref):
    return (ref["id"], ref["revision"], ref["content_hash"])


def _reference_metadata(record):
    body = record["body"]
    modules = {key: ref for key, ref in body["module_refs"].items() if ref}
    return {"ref": deepcopy(record["ref"]), "variant_id": body["variant_id"],
            "version_number": body["version_number"], "module_keys": list(modules),
            "module_hashes": {key: ref["content_hash"] for key, ref in modules.items()}}


def _project_script_records(records):
    projected = []
    for record in records:
        if record.get("ref", {}).get("kind") != "script_revision":
            projected.append(deepcopy(record))
            continue
        body = record["body"]
        projected.append({"ref": deepcopy(record["ref"]), "body": {
            key: deepcopy(body[key]) for key in ("preparation_id", "product_id", "product_ref",
                "variant_id", "version_number", "script_revision", "module_refs") if key in body},
            "reference_role": "script_identity_only", "prior_text_provided": False})
    return projected


def script_version_context(service, c, job, spec):
    # Historical archived responses must retain their exact dependency input.
    contract = spec.get("version_context_contract")
    if contract not in {"existing-script-text-v1", "existing-script-text-v2", "existing-script-text-v3"}:
        return {}
    v2 = contract in {"existing-script-text-v2", "existing-script-text-v3"}
    rewrite = v2 and _full_rewrite(spec)
    product_id = spec["product_ref"]["id"]
    # Entirely replaced versions are no longer positive continuation material.
    # Freeze targets from job inputs, never discover mutable preparation heads.
    rewrite_targets = {_identity(item["input"]["base_script_ref"])
        for item in job["items"] if item["input"].get("product_ref", {}).get("id") == product_id
        and _full_rewrite(item["input"])} if rewrite else set()
    if rewrite:
        rewrite_targets.add(_identity(spec["base_script_ref"]))
    refs = list(job["context"].get("prior_script_refs", []))
    if spec.get("base_script_ref") and spec["base_script_ref"] not in refs:
        refs.append(spec["base_script_ref"])
    previous, seen = [], set()
    for ref in refs:
        identity = _identity(ref)
        if identity in seen:
            continue
        record = service._artifact(c, ref, "script_revision", job["preparation_id"])
        body = record["body"]
        if body["product_id"] != product_id:
            continue
        seen.add(identity)
        if rewrite and identity in rewrite_targets:
            previous.append({**_reference_metadata(record),
                "reference_role": "superseded_rewrite_target", "prior_text_provided": False})
        else:
            value = {"ref": record["ref"], "variant_id": body["variant_id"],
                "version_number": body["version_number"],
                "modules": {key: service._artifact(c, module_ref, "module")["body"]["sentences"]
                            for key, module_ref in body["module_refs"].items() if module_ref}}
            if v2:
                value.update(reference_role="difference_comparison_only", prior_text_provided=True)
            previous.append(value)
    briefs = []
    for item in job["items"]:
        variant = item["input"]
        if variant.get("product_ref", {}).get("id") != product_id:
            continue
        briefs.append({"variant_id": variant["variant_id"], "version_number": variant["version_number"],
                       "module_instructions": {a["key"]: a.get("instruction", "")
                                               for a in variant["modules"] if a["action"] != "disable"}})
    result = {"same_product_existing_scripts": previous, "script_variant_briefs": deepcopy(briefs),
            "version_reference_policy": "旧稿仅供区分写法，商品事实以当前商品正本为准；当前方案与同批其他方案采用不同切入、场景和讲解展开，不能只换称呼。"}
    if v2:
        result.update(records=_project_script_records(job["context"].get("records", [])),
            writing_operation={"kind": "rewrite_existing_version" if rewrite else "generate_new_variant",
                "target_script_ref": deepcopy(spec.get("base_script_ref")) if rewrite else None,
                "preserve_variant_identity": rewrite, "preserve_prior_wording": False,
                "requested_module_keys": [a["key"] for a in spec["modules"] if a["action"] == "generate"]},
            writing_reference_contract={"revision": contract,
                "superseded_rewrite_target": "整版被替换，仅保留精确身份和模块内容哈希，不提供旧正文；不是缺资料，不索取旧文继续写。",
                "difference_comparison_only": "未被本轮整版替换的已有稿全文仅用于避免重复，不是文风或新增事实依据。",
                "fact_source": "当前商品正本、正式顾客研究与已确认用户决定；研究不补造商品事实。"})
    return result
