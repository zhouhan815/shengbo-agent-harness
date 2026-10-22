"""Runtime verification of voice-owned, narrowly scoped human audition consent."""
from shengbo_contracts.common import ContractError, canonical_hash
from .selection import same_ref, selection_items


async def guard(engine, run, args):
    proof = await engine.client.query("read_script_audio_preview", {"ref": args["preview_approval_ref"]},
        scope=run["scope"], run_id=run["id"])
    body = proof.get("body") or {}
    binding = body.get("binding") or {}
    human = body.get("human_receipt") or {}
    if (proof.get("status") != "current" or not same_ref(proof.get("ref"), args["preview_approval_ref"])
            or binding.get("run_id") != run["id"] or binding.get("preparation_id") != run["scope"].get("preparation_id")
            or binding.get("preparation_id") != args["preparation_id"]
            or body.get("binding_hash") != args["binding_hash"] or canonical_hash(binding) != args["binding_hash"]
            or not human.get("actor_id") or not human.get("operation_id") or not human.get("approved_at")
            or not human.get("authorization_quote") or human["authorization_quote"] not in run["input"]["text"]):
        raise ContractError("PREVIEW_HUMAN_RECEIPT_MISMATCH", "无法核实本次用户对精确局部试听的授权", status=403)
    modules = set(run.get("module_allowlist") or [])
    if not modules or set(binding.get("module_keys", [])) - modules:
        raise ContractError("PREVIEW_MODULE_SCOPE_DENIED", "试听模块超出冻结目标", status=403)
    targets = set(run.get("target_product_ids", [])) | set(run.get("created_product_ids", []))
    if binding.get("product_ref", {}).get("id") not in targets:
        raise ContractError("TARGET_DENIED", "试听商品超出本次目标", status=403)
    selected = selection_items(run.get("selection_binding"))
    if selected:
        if any(item.get("sentence_ids") or item.get("field_path") for item in selected):
            raise ContractError("PREVIEW_SELECTION_SCOPE_DENIED", "局部句子或商品字段选区不能授权整模块试听", status=403)
        permitted = [item for item in selected if item.get("kind") == "script_revision"
            and same_ref(item.get("current_ref"), binding["script_ref"])]
        if not permitted and not any(item.get("kind") == "product" and item.get("product_id") == binding["product_ref"]["id"] for item in selected):
            raise ContractError("PREVIEW_SELECTION_SCOPE_DENIED", "试听稿不属于本次明确选区", status=403)
        keys = {item["module_key"] for item in permitted if item.get("module_key")}
        if keys and all(item.get("module_key") for item in permitted) and set(binding["module_keys"]) - keys:
            raise ContractError("PREVIEW_SELECTION_SCOPE_DENIED", "试听超出选中模块", status=403)
    return proof
