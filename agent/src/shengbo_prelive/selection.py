"""Trusted target binding for an explicit workbench field/module selection.

The readable label and current page are presentation only. Immutable references,
the conversation scope and the selected field/module constrain every write.
"""
from __future__ import annotations

from copy import deepcopy
import re

from pydantic import TypeAdapter, ValidationError
from shengbo_contracts.business import MODULE_KEYS
from shengbo_contracts.common import ArtifactRef, ContractError, RecordRef, canonical_hash

_REF = TypeAdapter(RecordRef | ArtifactRef)


def same_ref(left, right):
    return bool(isinstance(left, dict) and isinstance(right, dict) and left and right and all(left.get(key) == right.get(key) for key in
                                      ("kind", "id", "revision", "content_hash")))


def _deny(code, message, **details):
    raise ContractError(code, message, status=409 if code.endswith("STALE") else 403, details=details)


def changed_paths(before, after, prefix=""):
    """Return exact JSON pointers; replacing a list is one field operation."""
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            path = prefix + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in before or key not in after:
                result.append(path)
            else:
                result.extend(changed_paths(before[key], after[key], path))
        return result
    return [] if before == after else [prefix]


def selection_items(binding):
    """A collection is an explicit union of exact targets, never a scope hint."""
    if not binding:
        return []
    return binding.get("items", []) if binding.get("kind") == "selection_set" else [binding]


async def resolve_selection(client, run, *, writing):
    raw = run["input"].get("selection")
    if not raw:
        return None
    if "items" in raw:
        rows = raw["items"]
        if raw.get("target") or not isinstance(rows, list) or not 1 <= len(rows) <= 20:
            _deny("SELECTION_INVALID", "多选需要1至20个明确选中项，不能同时夹带单选目标")
        bindings, identities, refs = [], set(), {}
        for row in rows:
            if not isinstance(row, dict) or "items" in row:
                _deny("SELECTION_INVALID", "多选项不能嵌套")
            item = await resolve_selection(client, {**run, "input": {**run["input"], "selection": row}}, writing=writing)
            if not item:
                _deny("SELECTION_INVALID", "多选中的每项都必须有明确目标")
            identity = canonical_hash({key: item.get(key) for key in
                ("kind", "base_ref", "field_path", "module_key", "sentence_ids", "selected_text_hash")})
            if identity in identities:
                _deny("SELECTION_INVALID", "同一字段或模块不能重复选择")
            identities.add(identity)
            for ref in [item.get("base_ref"), item.get("product_ref"), *item.get("product_refs", [])]:
                if not ref:
                    continue
                ref_key = (ref["kind"], ref["id"])
                if ref_key in refs and not same_ref(refs[ref_key], ref):
                    _deny("SELECTION_STALE", "同一对象的多选项引用了不同修订，请重新选择")
                refs[ref_key] = ref
            bindings.append(item)
        products = [ref for (kind, _), ref in refs.items() if kind == "product"]
        return {"kind": "selection_set", "items": bindings, "product_refs": products,
                "product_ids": [ref["id"] for ref in products], "preparation_id": run["scope"].get("preparation_id")}
    target = raw.get("target")
    if not isinstance(target, dict) or not target.get("object_ref"):
        _deny("SELECTION_INVALID", "选中项缺少精确对象引用，请重新选择")
    try:
        ref = _REF.validate_python(target["object_ref"]).model_dump(mode="json")
    except ValidationError:
        _deny("SELECTION_INVALID", "选中项引用格式无效，请重新选择")
    kind = ref["kind"]
    if kind not in {"product", "preparation", "script_revision", "analysis"}:
        _deny("SELECTION_KIND_DENIED", "该类型尚不支持直接作为修改目标")
    prep_id = run["scope"].get("preparation_id")
    destination = raw.get("destination") or {}
    if not isinstance(destination, dict):
        _deny("SELECTION_INVALID", "选中项定位信息格式无效")
    selected_prep = raw.get("preparation_id") or destination.get("preparation_id")
    if selected_prep and selected_prep != prep_id:
        _deny("SELECTION_SCOPE_CONFLICT", "选中项不属于当前对话本场，请为目标本场新建对话")
    field = target.get("field_path")
    module = target.get("module_key")
    if field is not None and not isinstance(field, str) or module is not None and not isinstance(module, str):
        _deny("SELECTION_INVALID", "字段路径与模块名称必须是文本")
    if field and module:
        _deny("SELECTION_INVALID", "同一个选中项不能同时指定字段与话术模块")
    binding = {"kind": kind, "base_ref": ref, "current_ref": ref,
               "field_path": field, "module_key": module, "preparation_id": prep_id}
    if kind == "product":
        exact = await client.query("read_product", {"ref": ref}, controller=True)
        head = await client.query("read_product", {"product_id": ref["id"]}, controller=True)
        if writing and not same_ref(ref, head["ref"]):
            _deny("SELECTION_STALE", "商品已更新，请查看差异并重新选择后再修改", current_ref=head["ref"])
        if module or field and (not re.fullmatch(r"/[A-Za-z_]+", field) or field[1:] not in exact["body"]):
            _deny("SELECTION_INVALID", "选中商品字段无效")
        binding["product_id"] = ref["id"]
        binding["product_ref"] = ref
    elif kind == "preparation":
        if not prep_id or ref["id"] != prep_id:
            _deny("SELECTION_SCOPE_CONFLICT", "不能修改当前对话以外的本场配置")
        prep = await client.query("read_preparation", {"preparation_id": prep_id}, controller=True)
        if writing and not same_ref(ref, prep["ref"]):
            _deny("SELECTION_STALE", "本场已更新，请检查最新配置并重新选择", current_ref=prep["ref"])
        if module or field and not re.fullmatch(r"/draft/[A-Za-z_]+(?:/[A-Za-z0-9_~/-]+)?", field):
            _deny("SELECTION_INVALID", "本场字段必须是 /draft/ 下的配置项")
        binding["current_ref"] = prep["ref"]
        snapshot = await client.query("get_workspace_snapshot", {"preparation_id": prep_id}, controller=True)
        if writing and not same_ref(snapshot["preparation"]["ref"], prep["ref"]):
            _deny("SELECTION_STALE", "读取本场草案期间版本发生变化，请重新选择")
        draft = (snapshot.get("preparation_details") or {}).get("draft")
        binding["session_draft"] = deepcopy((draft or {}).get("body", {}).get("draft", {}))
        binding["draft_ref"] = (draft or {}).get("ref")
    elif kind == "analysis":
        if field or module:
            _deny("SELECTION_INVALID", "研究建议按完整报告修订，不以商品字段或话术模块定位")
        report = await client.query("read_analysis_report", {"ref": ref}, controller=True)
        report_prep = report["body"].get("preparation_id")
        if report_prep != prep_id:
            _deny("SELECTION_SCOPE_CONFLICT", "研究报告不属于当前对话作用域")
        if prep_id:
            prep = await client.query("read_preparation", {"preparation_id": prep_id}, controller=True)
            if writing and not any(same_ref(ref, item) for item in prep["body"]["analysis_refs"]):
                _deny("SELECTION_STALE", "研究报告已不在本场，请重新选择")
        product_refs = report["body"].get("product_refs", [])
        binding.update(analysis_role=report["body"]["role"], product_refs=product_refs,
                       product_ids=[item["id"] for item in product_refs], analysis_report=report["body"]["report"])
        if len(product_refs) == 1:
            binding.update(product_id=product_refs[0]["id"], product_ref=product_refs[0])
    else:
        if not prep_id:
            _deny("SELECTION_SCOPE_CONFLICT", "话术模块必须在绑定该本场的对话中修改")
        script = await client.query("read_script", {"ref": ref}, controller=True)
        prep = await client.query("read_preparation", {"preparation_id": prep_id}, controller=True)
        if script["body"]["preparation_id"] != prep_id:
            _deny("SELECTION_SCOPE_CONFLICT", "话术不属于当前对话本场")
        if writing and not any(same_ref(ref, item) for item in prep["body"]["script_refs"]):
            _deny("SELECTION_STALE", "该话术版本已被替换，请重新选择当前版本")
        if field or module is not None and module not in MODULE_KEYS:
            _deny("SELECTION_INVALID", "选中的话术模块无效")
        if module:
            body = script["modules"].get(module)
            if not body:
                _deny("SELECTION_INVALID", "选中的模块没有可编辑正文")
            sentence_ids = target.get("sentence_ids") or []
            selected = body["sentences"]
            if sentence_ids:
                if not isinstance(sentence_ids, list) or any(not isinstance(x, str) for x in sentence_ids):
                    _deny("SELECTION_INVALID", "句子选择必须为ID清单")
                positions = [i for i, row in enumerate(selected) if row["id"] in sentence_ids]
                if (len(set(sentence_ids)) != len(sentence_ids) or len(positions) != len(sentence_ids)
                        or positions != list(range(min(positions, default=0), max(positions, default=-1) + 1))):
                    _deny("SELECTION_INVALID", "只支持精确连续句子选择")
                selected = [selected[i] for i in positions]
                if [row["id"] for row in selected] != sentence_ids:
                    _deny("SELECTION_INVALID", "句子顺序与基稿不一致")
                binding["sentence_ids"] = sentence_ids
            digest = canonical_hash(selected)
            if target.get("selected_text_hash") != digest:
                _deny("SELECTION_STALE", "模块正文已变化或缺少正文哈希，请重新选择")
            binding["selected_text_hash"] = digest
        product_ref = script["body"]["product_ref"]
        if target.get("product_ref") and not same_ref(target["product_ref"], product_ref):
            _deny("SELECTION_TARGET_CONFLICT", "话术与所选商品版本不一致")
        binding.update(product_id=script["body"]["product_id"], product_ref=product_ref,
                       variant_id=script["body"]["variant_id"])
    return binding


async def guard_selected_write(client, run, key, args):
    binding = run.get("selection_binding")
    if not binding:
        return
    if binding.get("kind") == "selection_set":
        await _guard_selection_set(client, run, key, args)
        return
    kind, field, module = binding["kind"], binding.get("field_path"), binding.get("module_key")
    fields = binding.get("field_paths", [field] if field else [])
    modules = binding.get("module_keys", [module] if module else [])
    infrastructure = {"import_blob", "accept_analysis_report"}
    if kind == "analysis":
        if key not in infrastructure:
            _deny("SELECTION_EFFECT_DENIED", "选中研究建议仅授权修订该研究，不自动修改商品、配置或话术")
        if key == "accept_analysis_report":
            if args["role"] != binding["analysis_role"] or args.get("product_refs", []) != binding["product_refs"]:
                _deny("SELECTION_TARGET_DENIED", "只能修订选中报告的研究角色与精确商品范围")
    if fields:
        allowed = infrastructure | ({"save_product"} if kind == "product" else {"save_session_draft"})
        if key not in allowed:
            _deny("SELECTION_EFFECT_DENIED", "该操作超出当前选中字段；请另行提出完整任务")
    if modules and key not in infrastructure | {"save_context_bundle", "save_script_plan", "submit_script_change", "patch_script", "compile_playback_plan"}:
        _deny("SELECTION_EFFECT_DENIED", "该操作超出当前选中的话术模块")
    if binding.get("product_id") and key == "accept_analysis_report":
        ids = {ref["id"] for ref in args.get("product_refs", [])} | set(args["report"].get("product_ids", []))
        if ids - {binding["product_id"]}:
            _deny("SELECTION_TARGET_DENIED", "研究报告不能写入选中商品以外的分析")
    if kind == "product" and key == "update_preparation_products":
        prep = await client.query("read_preparation", {"preparation_id": run["scope"]["preparation_id"]}, controller=True)
        other_before = [ref for ref in prep["body"]["selected_product_refs"] if ref["id"] != binding["product_id"]]
        other_after = [ref for ref in args["selected_product_refs"] if ref["id"] != binding["product_id"]]
        if other_before != other_after:
            _deny("SELECTION_TARGET_DENIED", "不能增删或替换未选中商品的本场引用")
    if kind == "product" and key == "refresh_product_reference":
        if any(args[name]["id"] != binding["product_id"] for name in ("expected_old_product_ref", "new_product_ref")):
            _deny("SELECTION_TARGET_DENIED", "只能刷新明确选中的商品引用")
    if key == "save_product":
        if kind != "product" or args.get("product_id") != binding["product_id"]:
            _deny("SELECTION_TARGET_DENIED", "只能修改用户明确选中的商品")
        head = await client.query("read_product", {"product_id": binding["product_id"]}, controller=True)
        if not same_ref(head["ref"], binding["current_ref"]):
            _deny("SELECTION_STALE", "选中商品在任务期间发生变化，不能自动覆盖", current_ref=head["ref"])
        if args.get("expected_revision") != head["ref"]["revision"]:
            _deny("SELECTION_STALE", "写入基准不是当前选中商品版本")
        paths = {patch["path"] for patch in args["patches"] if patch["op"] != "preserve"}
        allowed_paths = set(fields)
        # The prose field is a deterministic compatibility projection, not a
        # second independently editable benefit. Permit it only when identical.
        if "/core_selling_points" in allowed_paths and "/selling_points" in paths:
            if args.get("fields", {}).get("selling_points") == "\n".join(args.get("fields", {}).get("core_selling_points", [])):
                allowed_paths.add("/selling_points")
        if fields and paths - allowed_paths:
            _deny("FIELD_SCOPE_DENIED", "修改字段超出用户左侧明确选中的字段")
    if key == "save_session_draft" and (kind == "preparation" or fields):
        if kind != "preparation":
            _deny("SELECTION_TARGET_DENIED", "选中的对象不是本场配置")
        snapshot = await client.query("get_workspace_snapshot", {"preparation_id": binding["preparation_id"]}, controller=True)
        prep = snapshot["preparation"]
        if not same_ref(prep["ref"], binding["current_ref"]) or args.get("expected_revision") != prep["ref"]["revision"]:
            _deny("SELECTION_STALE", "本场配置在任务期间变化，请检查差异后重新选择")
        old = ((snapshot.get("preparation_details") or {}).get("draft") or {}).get("body", {}).get("draft", {})
        if fields:
            allowed = [path[len("/draft"):] for path in fields]
            changed = changed_paths(old, args["draft"])
            if any(not any(path == permit or path.startswith(permit + "/") for permit in allowed) for path in changed):
                _deny("FIELD_SCOPE_DENIED", "本场完整草案改动了选中字段以外的配置", changed_paths=changed)
    if kind == "script_revision" and key in {"save_script_plan", "submit_script_change", "patch_script"}:
        prep = await client.query("read_preparation", {"preparation_id": binding["preparation_id"]}, controller=True)
        if not any(same_ref(binding["current_ref"], ref) for ref in prep["body"]["script_refs"]):
            _deny("SELECTION_STALE", "选中的话术已被其他操作替换")
        if key == "patch_script":
            if not same_ref(args["script_ref"], binding["current_ref"]):
                _deny("SELECTION_TARGET_DENIED", "只能修改选中的话术版本")
            selected_hash = binding.get("selected_text_hash")
            if module and args.get("sentence_ids") and not binding.get("sentence_ids"):
                # A whole-module grant includes a narrower sentence selection;
                # verify that selection against the same exact module revision.
                current = await client.query("read_script", {"ref": binding["current_ref"]}, controller=True)
                rows = current["modules"][module]["sentences"]
                ids = args["sentence_ids"]
                indexes = [i for i, row in enumerate(rows) if row["id"] in ids]
                if (len(ids) != len(set(ids)) or len(indexes) != len(ids)
                        or indexes != list(range(min(indexes, default=0), max(indexes, default=-1) + 1))
                        or [rows[i]["id"] for i in indexes] != ids):
                    _deny("MODULE_SCOPE_DENIED", "所选句不是当前模块的连续范围")
                selected_hash = canonical_hash([rows[i] for i in indexes])
            if module and selected_hash is None:
                current = await client.query("read_script", {"ref": binding["current_ref"]}, controller=True)
                rows = current["modules"][module]["sentences"]
                selected_hash = canonical_hash([row for row in rows if row["id"] in binding["sentence_ids"]]
                    if binding.get("sentence_ids") else rows)
            if module and (args["module_key"] != module or args["selected_text_hash"] != selected_hash):
                _deny("MODULE_SCOPE_DENIED", "只能修改选中的模块和对应正文")
            if binding.get("sentence_ids") and args.get("sentence_ids") != binding["sentence_ids"]:
                _deny("MODULE_SCOPE_DENIED", "只能替换精确选中的句子范围")
        elif key == "save_script_plan":
            if binding.get("sentence_ids"):
                _deny("MODULE_SCOPE_DENIED", "所选句局改使用patch_script，不能重生成整个模块")
            if len(args["items"]) != 1:
                _deny("SELECTION_TARGET_DENIED", "局部话术计划必须只包含选中的一个版本")
            item = args["items"][0]
            if (not same_ref(item.get("base_script_ref"), binding["current_ref"])
                    or not same_ref(item["product_ref"], binding["product_ref"])
                    or item["variant_id"] != binding["variant_id"]):
                _deny("SELECTION_TARGET_DENIED", "话术计划必须基于选中的商品及话术修订")
            script = await client.query("read_script", {"ref": binding["current_ref"]}, controller=True)
            for action in item["modules"]:
                if modules and action["key"] not in modules:
                    old_ref = script["body"]["module_refs"].get(action["key"])
                    # Already-disabled optional modules stay disabled; a populated
                    # unselected module must remain byte-for-byte reused.
                    if old_ref:
                        if action["action"] != "reuse" or not same_ref(action.get("base_module_ref"), old_ref):
                            _deny("MODULE_SCOPE_DENIED", "未选中的模块必须原样复用，不能禁用或重写")
                    elif action["action"] != "disable":
                        _deny("MODULE_SCOPE_DENIED", "未选中且原本禁用的模块不能生成正文")
        elif not same_ref(args["plan_ref"], binding.get("validated_plan_ref")):
            _deny("SELECTION_PLAN_DENIED", "只能执行本次已验证范围的局部话术计划")
    if kind == "product" and key == "save_script_plan":
        if any(item["product_ref"]["id"] != binding["product_id"] for item in args["items"]):
            _deny("SELECTION_TARGET_DENIED", "不能生成未选中商品的话术")
    if kind == "product" and key == "submit_script_change" and not same_ref(args["plan_ref"], binding.get("validated_plan_ref")):
        _deny("SELECTION_PLAN_DENIED", "只能执行本次已验证属于选中商品的计划")
    if kind == "product" and key == "patch_script":
        script = await client.query("read_script", {"ref": args["script_ref"]}, controller=True)
        if script["body"]["product_id"] != binding["product_id"]:
            _deny("SELECTION_TARGET_DENIED", "不能修改未选中商品的话术")


def advance_selection_binding(binding, key, receipt, args=None):
    """Only a known committed receipt can advance a binding's current baseline."""
    if not binding:
        return None
    if binding.get("kind") == "selection_set":
        result = deepcopy(binding)
        result["items"] = [advance_selection_binding(item, key, receipt, args)
                           if _receipt_matches(item, key, args or {}) else deepcopy(item) for item in binding["items"]]
        refs = {item["product_ref"]["id"]: item["product_ref"] for item in result["items"] if item.get("product_ref")}
        if key == "save_product" and receipt.get("data", {}).get("product_ref"):
            ref = receipt["data"]["product_ref"]
            refs[ref["id"]] = ref
        result["product_refs"] = list(refs.values())
        return result
    result = deepcopy(binding)
    data = receipt.get("data", {})
    if key == "save_product" and data.get("product_ref") and binding["kind"] == "product":
        result["current_ref"] = data["product_ref"]
        result["product_ref"] = data["product_ref"]
    if binding["kind"] == "preparation" and data.get("preparation_ref"):
        result["current_ref"] = data["preparation_ref"]
        if key == "save_session_draft" and args:
            result["session_draft"] = deepcopy(args["draft"])
            result["draft_ref"] = data.get("draft_ref")
    if key == "save_script_plan" and data.get("script_plan_ref"):
        result["validated_plan_ref"] = data["script_plan_ref"]
    if key == "patch_script" and data.get("script_ref") and binding["kind"] == "script_revision":
        result["current_ref"] = data["script_ref"]
        result.pop("validated_plan_ref", None)
        # Sentence IDs and spoken text are normalised by the business service.
        # The next attempted write must hash its exact read-back, not a guess
        # derived from the submitted strings.
        if not args or not binding.get("module_key") or binding["module_key"] == args.get("module_key"):
            result["selected_text_hash"] = None
            if binding.get("sentence_ids"):
                result["sentence_ids"] = data.get("replacement", {}).get("sentence_ids", [])
    if key == "accept_analysis_report" and data.get("analysis_ref") and binding["kind"] == "analysis":
        result["current_ref"] = data["analysis_ref"]
        if args:
            result["analysis_report"] = args["report"]
    return result


def _receipt_matches(item, key, args):
    if key == "save_product":
        return item.get("kind") == "product" and item.get("product_id") == args.get("product_id")
    if key in {"save_session_draft", "save_script_plan", "submit_script_change", "patch_script", "accept_analysis_report"}:
        if item.get("kind") == "preparation":
            return key == "save_session_draft" and item.get("preparation_id") == args.get("preparation_id")
        if item.get("kind") == "analysis":
            return key == "accept_analysis_report" and item.get("analysis_role") == args.get("role") and item.get("product_refs", []) == args.get("product_refs", [])
        if item.get("kind") == "script_revision":
            if key == "patch_script":
                return same_ref(item.get("current_ref"), args.get("script_ref"))
            if key == "save_script_plan":
                return any(same_ref(row.get("base_script_ref"), item.get("current_ref"))
                           and same_ref(row.get("product_ref"), item.get("product_ref"))
                           and row.get("variant_id") == item.get("variant_id") for row in args.get("items", []))
            return key == "submit_script_change"
    return False


async def _guard_selection_set(client, run, key, args):
    items = selection_items(run["selection_binding"])
    if key == "import_blob":
        return
    if key == "save_product":
        candidates = [item for item in items if item["kind"] == "product" and item["product_id"] == args.get("product_id")]
    elif key == "save_session_draft":
        candidates = [item for item in items if item["kind"] == "preparation"]
    elif key in {"patch_script", "save_script_plan", "submit_script_change"}:
        candidates = [item for item in items if item["kind"] in {"script_revision", "product"}]
        if key == "patch_script":
            candidates = [item for item in candidates if item["kind"] == "product" or
                          same_ref(item["current_ref"], args.get("script_ref")) and
                          (not item.get("module_key") or item["module_key"] == args.get("module_key"))]
    elif key == "accept_analysis_report":
        candidates = [item for item in items if item["kind"] in {"analysis", "product"}]
    elif key in {"save_context_bundle", "compile_playback_plan", "update_preparation_products", "refresh_product_reference"}:
        candidates = [item for item in items if not item.get("field_path")]
    else:
        candidates = []
    if not candidates:
        _deny("SELECTION_EFFECT_DENIED", "该操作不属于任何明确选中的字段或模块")
    if key in {"save_product", "save_session_draft"}:
        combined = deepcopy(candidates[0])
        if all(item.get("field_path") for item in candidates):
            combined["field_paths"] = list(dict.fromkeys(item["field_path"] for item in candidates))
        else:
            combined["field_path"] = None
        await guard_selected_write(client, {**run, "selection_binding": combined}, key, args)
        return
    errors = []
    if key == "save_script_plan" and len(args.get("items", [])) == 1:
        script_ref = args["items"][0].get("base_script_ref")
        matching = [item for item in candidates if item["kind"] == "script_revision" and same_ref(item["current_ref"], script_ref)]
        if matching:
            combined = deepcopy(matching[0])
            combined["module_keys"] = ([item["module_key"] for item in matching]
                                       if all(item.get("module_key") for item in matching) else [])
            combined["module_key"] = combined["module_keys"][0] if combined["module_keys"] else None
            try:
                if any(item.get("sentence_ids") for item in matching):
                    _deny("MODULE_SCOPE_DENIED", "含精确句子选择时请逐项局部修改，不能整模块重新生成")
                await guard_selected_write(client, {**run, "selection_binding": combined}, key, args)
                return
            except ContractError as error:
                errors.append(error)
    # Alternatives are a union: a valid exact selection must authorize the
    # entire operation. Failure never broadens to the rest of the workspace.
    for item in candidates:
        try:
            await guard_selected_write(client, {**run, "selection_binding": item}, key, args)
            return
        except ContractError as error:
            errors.append(error)
    raise errors[0]
