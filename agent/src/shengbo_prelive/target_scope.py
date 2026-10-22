"""Deterministic action targets; readable context never grants write scope.

Only declared command fields and exact, receipted artifacts are inspected.
Arbitrary ref-shaped prose is context, not an authorization coordinate.
"""
from __future__ import annotations

from shengbo_contracts.common import ContractError, canonical_hash


def ref_key(ref):
    return tuple((ref or {}).get(key) for key in ("kind", "id", "revision", "content_hash"))


def action_product_ids(run):
    return set(run.get("target_product_ids", run.get("goal", {}).get("target_product_ids", []))) | set(run.get("created_product_ids", []))


def trusted_plan_items(store, run, plan_ref):
    """A local exact receipt is a plan witness, never the broader context bundle."""
    for operation in reversed(store.list("operation")):
        args = operation.get("args")
        data = operation.get("receipt", {}).get("data", {})
        if (operation.get("key") != "save_script_plan" or operation.get("status") != "committed"
                or not isinstance(args, dict) or operation.get("request_hash") != canonical_hash(args)
                or ref_key(data.get("script_plan_ref")) != ref_key(plan_ref)):
            continue
        owner = run if operation.get("run_id") == run["id"] else store.get("run", operation.get("run_id"), required=False)
        if owner and owner.get("conversation_id") == run.get("conversation_id") and owner.get("scope") == run.get("scope"):
            return args.get("items", [])
    return None


async def resolve_plan_items(client, store, run, plan_ref):
    items = trusted_plan_items(store, run, plan_ref)
    if items is not None:
        return items
    prep_id = run.get("scope", {}).get("preparation_id")
    if not prep_id:
        return None
    value = await client.query("read_preparation", {"preparation_id": prep_id, "sections": ["script_plan"]},
        scope=run["scope"], run_id=run["id"])
    plan = value.get("script_plan") or {}
    body, exact = plan.get("body") or {}, plan.get("ref") or {}
    if (ref_key(exact) != ref_key(plan_ref) or ref_key(value.get("body", {}).get("plan_ref")) != ref_key(exact)
            or exact.get("kind") != "script_plan" or canonical_hash(body) != exact.get("content_hash")
            or body.get("preparation_id") != prep_id):
        return None
    return body.get("items")


async def guard_command_targets(client, store, run, command, args):
    """Check the command's affected products before an operation is sent.

    Preparation selection and context bundles can preserve unrelated products;
    their existing dedicated guards remain responsible for those coordinates.
    """
    mode = run.get("goal", {}).get("script_mode", "generate")
    if (run.get("goal", {}).get("script_delivery_requirement") == "reuse_existing"
            and command in {"submit_script_change", "patch_script"}):
        raise ContractError("SCRIPT_MODE_CONFLICT", "本次明确沿用已有有效话术，不能生成新稿或改写原稿；变更要求须走已有任务范围机制", status=409,
            details={"command": command, "commit_state": "not_committed", "script_delivery_requirement": "reuse_existing"})
    if mode == "import_original" and command == "submit_script_change":
        raise ContractError("SCRIPT_MODE_CONFLICT", "本次仅授权原稿逐字导入，不执行文案生成", status=409,
            details={"command": command, "commit_state": "not_committed", "required_action": "import_script"})
    if mode == "expand_original" and command == "save_script_plan" and any(not item.get("original_script_ref") for item in args["items"]):
        raise ContractError("SCRIPT_MODE_CONFLICT", "扩版计划必须指定真实原稿的精确original_script_ref", status=409,
            details={"command": command, "commit_state": "not_committed", "required_field": "items[].original_script_ref"})
    refs, identities = [], []
    if command == "save_product":
        identities = [args["product_id"]] if args.get("product_id") else []
    elif command == "save_script_plan":
        refs = [item["product_ref"] for item in args["items"]]
        for item in args["items"]:
            for name in ("base_script_ref", "original_script_ref"):
                if not item.get(name): continue
                script = await client.query("read_script", {"ref": item[name]}, scope=run["scope"], run_id=run["id"])
                body = script.get("body", {})
                if body.get("product_id") != item["product_ref"]["id"]:
                    raise ContractError("TARGET_DENIED", "基准话术与本次计划商品不一致", status=403)
    elif command == "submit_script_change":
        items = await resolve_plan_items(client, store, run, args["plan_ref"])
        if items is None:
            raise ContractError("TARGET_SCOPE_UNVERIFIED", "该计划缺少可核验的精确商品范围，请先按本次目标保存显式items计划", status=409,
                details={"command": command, "commit_state": "not_committed", "required_action": "save_script_plan",
                         "context_is_plan_scope": False})
        refs = [item["product_ref"] for item in items]
    elif command == "patch_script":
        script = await client.query("read_script", {"ref": args["script_ref"]}, scope=run["scope"], run_id=run["id"])
        refs = [script["body"]["product_ref"]]
    elif command == "save_playlist":
        for item in args["items"]:
            script = await client.query("read_script", {"ref": item["script_revision_ref"]}, scope=run["scope"], run_id=run["id"])
            body = script["body"]
            if body.get("product_id") != item["product_id"]:
                raise ContractError("TARGET_DENIED", "编排条目商品与精确话术引用不一致", status=403)
            refs.append(body["product_ref"])
    elif command == "accept_analysis_report" and args.get("role") != "session_config":
        refs = args.get("product_refs", [])
        identities = args.get("report", {}).get("product_ids", [])
    elif command == "import_script":
        # This command is registered by the import capability, not this guard.
        refs = [args["product_ref"]]
    else:
        return
    allowed_ids = action_product_ids(run)
    if ({ref["id"] for ref in refs} | set(identities)) - allowed_ids:
        raise ContractError("TARGET_DENIED", "动作包含本次用户明确目标以外的商品", status=403,
                            details={"command": command, "commit_state": "not_committed"})
    # Legacy runs have no exact frozen refs. They retain ID scope, while the
    # business service still checks its exact current preparation references.
    frozen = run.get("action_target_refs", run.get("goal", {}).get("action_target_refs"))
    if frozen is not None and refs:
        allowed_refs = list(frozen)
        for operation in store.list("operation"):
            saved = operation.get("receipt", {}).get("data", {}).get("product_ref")
            if (operation.get("run_id") == run["id"] and operation.get("key") == "save_product"
                    and operation.get("status") == "committed" and saved and saved["id"] in allowed_ids):
                allowed_refs.append(saved)
        if {ref_key(ref) for ref in refs} - {ref_key(ref) for ref in allowed_refs}:
            raise ContractError("TARGET_DENIED", "动作商品修订不是冻结目标或本次真实保存的精确引用", status=403,
                                details={"command": command, "commit_state": "not_committed"})


def completion_product_refs(run, preparation, operations):
    """Current target revisions, excluding merely readable preparation products."""
    allowed = action_product_ids(run)
    if "target_product_ids" not in run and "target_product_ids" not in run.get("goal", {}):
        allowed = {ref["id"] for ref in preparation.get("selected_product_refs", [])}
    refs = {ref["id"]: ref for ref in run.get("action_target_refs", run.get("goal", {}).get("action_target_refs", [])) if ref["id"] in allowed}
    for ref in preparation.get("selected_product_refs", []):
        if ref["id"] in allowed:
            refs.setdefault(ref["id"], ref)
    for operation in operations:
        saved = operation.get("receipt", {}).get("data", {}).get("product_ref")
        if operation.get("key") == "save_product" and saved and saved["id"] in allowed:
            refs[saved["id"]] = saved
    return list(refs.values())


async def verify_script_coverage(client, store, run, operations, observation, product_refs):
    """Read formal output for this task's targets, variants and explicit counts."""
    operations = [op for op in operations if op.get("run_id") == run["id"] and op.get("status") == "committed"]
    goal = run["goal"]
    mode = goal.get("script_mode")
    legacy = "script_delivery_requirement" not in goal and ("script_mode" not in goal
        or goal["objective"] == "prepare_session" and mode == "generate")
    delivery = ("legacy" if legacy else goal.get("script_delivery_requirement")
        if goal["objective"] == "prepare_session" and mode == "generate" else
        "new_generation" if mode == "generate" else mode)
    preparation = observation.get("preparation", {}).get("body", {})
    current_refs = preparation.get("script_refs", [])
    allowed = {ref_key(ref) for ref in product_refs}
    exact_outputs = [operation["receipt"]["data"]["script_ref"] for operation in operations
                     if operation.get("key") in {"patch_script", "import_script"}
                     and operation.get("receipt", {}).get("data", {}).get("script_ref")]
    # Different variants may be produced in separate model-selected batches.
    # Only a later attempt for the same exact product+variant supersedes it.
    by_variant = {}
    jobs_by_id = {job.get("ref", {}).get("id"): job.get("body", {}) for job in observation.get("jobs", [])}
    # A job missing from an observation cannot turn its submitted plan into an
    # empty requirement. The same exact local plan witness supplies its items.
    for operation in operations:
        if operation.get("key") == "submit_script_change":
            items = trusted_plan_items(store, run, operation.get("args", {}).get("plan_ref"))
            job = jobs_by_id.get(operation.get("receipt", {}).get("data", {}).get("job_id"))
            plan_items = items if items is not None else [item.get("input", {}) for item in (job or {}).get("items", [])]
            for item in plan_items:
                if item.get("product_ref") and item.get("variant_id") and ref_key(item["product_ref"]) in allowed:
                    key = (ref_key(item["product_ref"]), item["variant_id"])
                    by_variant[key] = {"item": item, "job": job}
    required = [entry["item"] for entry in by_variant.values()]
    required_keys = {(ref_key(item["product_ref"]), item["variant_id"]) for item in required}
    scoped_scripts, missing, covered = [], [], set()
    documents = {}
    async def read_script(ref):
        key = ref_key(ref)
        if key not in documents:
            documents[key] = await client.query("read_script", {"ref": ref}, scope=run["scope"], run_id=run["id"])
        return documents[key]
    for ref in current_refs:
        script = await read_script(ref)
        body = script.get("body", script)
        # Legacy frozen goals without explicit targets retain their readable
        # preparation scope, but new goals never expand from an empty scope.
        if ref_key(body.get("product_ref")) not in allowed:
            continue
        scoped_scripts.append((ref, body, script.get("modules") or {}))
    for ref, body, modules in scoped_scripts:
        text_count = 0
        module_refs = body.get("module_refs") or {}
        if module_refs:
            for key, module_ref in module_refs.items():
                if module_ref is None:
                    continue
                module = modules.get(key)
                sentences = module.get("sentences") if isinstance(module, dict) else None
                if not isinstance(sentences, list) or not sentences or any(not isinstance(sentence, dict) or not isinstance(sentence.get("text"), str) or not sentence["text"].strip() for sentence in sentences):
                    missing.append("话术模块缺少真实非空正文：" + str(key))
                    continue
                text_count += len(sentences)
        else:
            sentences = body.get("sentences")
            if isinstance(sentences, list) and sentences and all(isinstance(sentence, dict) and isinstance(sentence.get("text"), str) and sentence["text"].strip() for sentence in sentences):
                text_count = len(sentences)
        if not text_count:
            missing.append("脚本只有引用或空壳，缺少实际可播文字")
        else:
            covered.add((ref_key(body.get("product_ref")), body.get("variant_id")))
    for product_ref in product_refs:
        if not any(key == ref_key(product_ref) for key, _variant in covered):
            missing.append("目标商品缺少实际话术正文：" + product_ref["id"])
    for key, variant in sorted(required_keys - covered):
        missing.append("本次话术计划尚未覆盖正式稿：" + str(key[1]) + "/" + variant)
    current_keys = {ref_key(ref) for ref, _body, _modules in scoped_scripts}
    async def valid_patch(op):
        before = op.get("args", {}).get("script_ref")
        after = op.get("receipt", {}).get("data", {}).get("script_ref")
        if not before or not after:
            return False
        if legacy:
            return True
        old, new = await read_script(before), await read_script(after)
        old_body, new_body = old.get("body", {}), new.get("body", {})
        replacement = op["receipt"]["data"].get("replacement") or {}
        return (ref_key(old.get("ref", before)) == ref_key(before) and ref_key(new.get("ref", after)) == ref_key(after)
            and ref_key(new_body.get("parent_ref")) == ref_key(before)
            and ref_key(old_body.get("product_ref")) == ref_key(new_body.get("product_ref"))
            and old_body.get("variant_id") == new_body.get("variant_id")
            and old_body.get("preparation_id") == new_body.get("preparation_id")
            and (not replacement or ref_key(replacement.get("old_ref")) == ref_key(before)
                and ref_key(replacement.get("new_ref")) == ref_key(after)))

    # A patch receipt may itself be the claimed edit, without a generated root
    # being traversed below. Validate that edge too, using the same authority.
    for op in operations:
        if op.get("key") == "patch_script" and not await valid_patch(op):
            missing.append("本次话术补丁谱系不一致，不能代替精确正式稿")
    async def final_ref(ref):
        # Real receipted patch edges advance a formal output; imports and their
        # descendants never acquire a generated root merely by being edited.
        seen = set()
        while ref and ref_key(ref) not in seen:
            seen.add(ref_key(ref))
            edges = [op for op in operations if op.get("key") == "patch_script"
                and ref_key(op.get("args", {}).get("script_ref")) == ref_key(ref)
                and op.get("receipt", {}).get("data", {}).get("script_ref")]
            if not edges:
                return ref
            targets = {ref_key(op["receipt"]["data"]["script_ref"]) for op in edges}
            if len(targets) != 1:
                break
            op = edges[0]
            after = op["receipt"]["data"]["script_ref"]
            if not await valid_patch(op):
                break
            ref = after
        missing.append("本次话术补丁谱系不一致或有环，不能代替精确正式稿")
        return None

    terminal_outputs = [await final_ref(ref) for ref in exact_outputs]
    task_output_keys = {ref_key(ref) for ref in terminal_outputs if ref}
    imported_output_keys = {ref_key(ref) for op in operations if op.get("key") == "import_script"
        and (ref := op.get("receipt", {}).get("data", {}).get("script_ref")) and ref_key(ref) in current_keys}
    generated_output_keys, expansion_output_keys = set(), set()
    for entry in by_variant.values():
        expected, job = entry["item"], entry["job"]
        candidates = [item for item in (job or {}).get("items", []) if ref_key(item.get("input", {}).get("product_ref")) == ref_key(expected["product_ref"])
            and item.get("input", {}).get("variant_id") == expected["variant_id"]]
        output = next((item.get("output", {}).get("script_ref") for item in candidates
                       if item.get("status") == "succeeded" and isinstance(item.get("output"), dict)), None)
        if output and not legacy:
            candidate = next(item for item in candidates if item.get("status") == "succeeded"
                and isinstance(item.get("output"), dict) and item["output"].get("script_ref") == output)
            document = await read_script(output)
            body, provenance = document.get("body", {}), document.get("body", {}).get("generation_provenance") or {}
            if ((job or {}).get("kind") != "scripts" or (job or {}).get("run_id") != run["id"]
                    or (job or {}).get("preparation_id") != run.get("scope", {}).get("preparation_id")
                    or ref_key(document.get("ref", output)) != ref_key(output)
                    or ref_key(body.get("product_ref")) != ref_key(expected["product_ref"])
                    or body.get("variant_id") != expected["variant_id"]
                    or body.get("preparation_id") not in {None, (job or {}).get("preparation_id")}
                    or candidate.get("call_state") in {"sent", "timeout_unknown", "outcome_unknown"}
                    or provenance and (provenance.get("job_id") != (job or {}).get("id")
                        or provenance.get("item_id") != candidate.get("item_id")
                        or ref_key(provenance.get("plan_ref")) != ref_key((job or {}).get("plan_ref")))):
                output = None
        output = await final_ref(output) if output else None
        if not output or ref_key(output) not in current_keys:
            missing.append("本次生成任务尚无对应当前正式稿的成功结果：" + expected["product_ref"]["id"] + "/" + expected["variant_id"])
        else:
            task_output_keys.add(ref_key(output))
            generated_output_keys.add(ref_key(output))
            if expected.get("original_script_ref"):
                original_ref = expected["original_script_ref"]
                actual_original = next((item.get("input", {}).get("original_script_ref") for item in candidates
                    if item.get("status") == "succeeded"), None)
                if legacy:
                    expansion_output_keys.add(ref_key(output))
                elif ref_key(actual_original) == ref_key(original_ref):
                    original_body = (await read_script(original_ref)).get("body", {})
                    if (ref_key(original_body.get("product_ref")) == ref_key(expected["product_ref"])
                            and original_body.get("variant_id") != expected["variant_id"]
                            and original_body.get("preparation_id") in {None, (job or {}).get("preparation_id")}):
                        expansion_output_keys.add(ref_key(output))
    for ref, terminal in zip(exact_outputs, terminal_outputs):
        if not terminal or ref_key(terminal) not in current_keys:
            missing.append("本次话术保存回执未对应当前正式稿：" + ref["id"])
    eligible_keys = (current_keys if delivery == "reuse_existing" or legacy and goal["objective"] == "prepare_session" else
        generated_output_keys if delivery == "new_generation" else expansion_output_keys if delivery == "expand_original" else
        imported_output_keys if delivery == "import_original" else task_output_keys)
    if not legacy and mode == "generate" and delivery not in {"new_generation", "reuse_existing"}:
        missing.append("本场话术交付要求尚未明确，不能以旧稿或默认模式代替")
    if delivery in {"new_generation", "expand_original"}:
        for product_ref in product_refs:
            if not any(ref_key(body.get("product_ref")) == ref_key(product_ref) and ref_key(ref) in eligible_keys
                    for ref, body, _modules in scoped_scripts):
                missing.append("目标商品缺少本run成功生成的正式话术：" + product_ref["id"])
    for requirement in run.get("goal", {}).get("requested_script_variants", []):
        applicable = [ref for ref in product_refs if not requirement.get("product_id") or ref["id"] == requirement["product_id"]]
        if not applicable:
            missing.append("明确版数要求没有已绑定的目标商品")
        for product_ref in applicable:
            variants = {body.get("variant_id") for ref, body, _modules in scoped_scripts
                        if ref_key(body.get("product_ref")) == ref_key(product_ref) and body.get("variant_id")
                        and ref_key(ref) in eligible_keys}
            if len(variants) < requirement["count"]:
                missing.append("目标商品正式话术版数不足：" + product_ref["id"] + "/" + str(len(variants)) + "/" + str(requirement["count"]))
    changed = {operation.get("key") for operation in operations} & {"submit_script_change", "patch_script", "import_script"}
    # A committed command name alone is not a generated or imported script.
    proven_change = bool(required_keys or exact_outputs) if legacy else bool(eligible_keys & current_keys)
    if run["goal"]["objective"] in {"script_change", "materials_to_script"} and (not changed or not proven_change):
        missing.append("本次话术真实修改或生成回执")
    if mode == "import_original" and not imported_output_keys:
        missing.append("本次原稿逐字导入的正式保存回执")
    if not product_refs or not scoped_scripts:
        missing.append("实际话术正文")
    return {"missing": list(dict.fromkeys(missing)), "required_variants": [
        {"product_ref": item["product_ref"], "variant_id": item["variant_id"]} for item in required],
        "formal_script_refs": [ref for ref, _body, _modules in scoped_scripts], "semantic_support_verified": False,
        "delivery_requirement": delivery, "legacy_compatibility": legacy,
        "delivery_counts": {"generated": len(generated_output_keys), "expanded": len(expansion_output_keys),
            "imported": len(imported_output_keys), "eligible_current": len(eligible_keys & current_keys)}}
