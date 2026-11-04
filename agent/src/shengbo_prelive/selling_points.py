"""Evidence-bound selling points, with exact approval or report writeback.

The existing Agent writes the candidate. This module never calls a model and
never treats customer hypotheses as product facts. Business writes retain the
original grant, target, selected-revision and idempotency fences.
"""
from __future__ import annotations

from copy import deepcopy

from shengbo_contracts.common import (
    ArtifactRef, ContractError, canonical_hash, canonical_json, now_iso, same_artifact_ref,
)
from shengbo_contracts.business import COMMAND_ARGS
from .action_contracts import uncommitted_input_error
from .selling_point_evidence import FACT_FIELDS, build_evidence_catalog

KINDS = {"selling_points_generation", "selling_points_confirmation"}
POINT_FIELDS = {"/core_selling_points", "/selling_points"}
GENERATION_ACTION = "selling_points_action"
AUTOMATIC_WRITEBACK = "generate_and_save"


def _input(message, *, code="SELLING_POINTS_INPUT_INVALID", details=None):
    return uncommitted_input_error(code, message, status=422, details=details)


def _ref(value, kind="product"):
    try:
        parsed = ArtifactRef.model_validate(value, strict=True).model_dump(mode="json")
    except (ValueError, TypeError):
        raise _input("需要合法、精确的商品或研究引用") from None
    if parsed["kind"] != kind:
        raise _input("引用种类与当前动作不一致")
    return parsed


def _text(value, name, limit=4000, *, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value.strip()):
        raise _input(f"{name}需为有效文字，最多{limit}字")
    return value.strip()


def _same(left, right):
    return same_artifact_ref(left, right)


def _submitted_generation_refs(value, scope, room_id):
    view = value.get("view_context") or {}
    if (view.get(GENERATION_ACTION) not in ("generate", AUTOMATIC_WRITEBACK) or view.get("view") != "products"
            or view.get("room_id") or view.get("preparation_id") or view.get("product_material_action")
            or scope.get("kind") != "catalog" or scope.get("preparation_id") or room_id
            or value.get("intent_mode") != "new_task" or value.get("related_run_id")
            or value.get("material_refs") or not value.get("text", "").strip()):
        return ()
    selection = value.get("selection") or {}
    rows = selection.get("items") if "items" in selection else [selection]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20 or ("items" in selection and "target" in selection):
        return ()
    refs = {}
    for row in rows:
        target = row.get("target") if isinstance(row, dict) and "items" not in row else None
        if (not isinstance(target, dict) or set(target) != {"object_ref", "field_path"}
                or target.get("field_path") != "/core_selling_points"):
            return ()
        try:
            ref = _ref(target["object_ref"])
        except ContractError:
            return ()
        if not _same(ref, target["object_ref"]) or ref["id"] in refs:
            return ()
        refs[ref["id"]] = ref
    observed = value.get("observed_versions") or []
    if (len(observed) != len(refs) or {row.get("id") for row in observed} != refs.keys()
            or any(not _same(row, refs[row["id"]]) for row in observed)):
        return ()
    return tuple(refs.values())


def validate_selling_points_request(request, conversation):
    if GENERATION_ACTION in request.view_context and not _submitted_generation_refs(
            request.model_dump(mode="json"), conversation["scope"], conversation.get("room_id")):
        raise ContractError("SELLING_POINTS_REQUEST_INVALID", "请从商品库重新指定要整理的核心卖点并发送。", status=422)


def validate_selling_points_intent(run, proposal):
    refs = _submitted_generation_refs(run.get("input") or {}, run.get("scope") or {}, run.get("room_id"))
    if refs and (proposal.objective != "catalog_update" or proposal.room_id or proposal.preparation_id
            or set(proposal.target_product_ids) - {ref["id"] for ref in refs}
            or set(proposal.requested_product_paths) - POINT_FIELDS
            or (automatic_writeback_requested(run) and set(proposal.required_analyses) - {"audience_scenes"})):
        raise _input("从报告发送的卖点任务限于选中的商品及核心卖点字段，请用catalog_update识别原任务。")


def explicit_generation_refs(run):
    """The trusted human submission authorizes drafting for only these exact refs."""
    value, goal = run.get("input") or {}, run.get("goal")
    refs = _submitted_generation_refs(value, run.get("scope") or {}, run.get("room_id"))
    if refs and goal and (goal.get("objective") != "catalog_update"
            or goal.get("original_input_hash") != canonical_hash(value)
            or {ref["id"] for ref in refs} != set(goal.get("target_product_ids", []))):
        return ()
    return refs


def automatic_writeback_requested(run):
    return (run.get("input", {}).get("view_context", {}).get(GENERATION_ACTION) == AUTOMATIC_WRITEBACK
            and bool(explicit_generation_refs(run)))


def _explicit_generation(run, ref):
    refs = explicit_generation_refs(run)
    if not refs:
        return None
    if not any(_same(ref, selected) for selected in refs):
        raise uncommitted_input_error("SELLING_POINTS_PRODUCT_STALE", "当前商品版本已变，请从商品库重新发起卖点整理。")
    proof = {"source": "submitted_product_selection", "input_hash": canonical_hash(run["input"])}
    if automatic_writeback_requested(run):
        proof["save_authorized"] = True
    return proof


def _automatic_proposal(run, proposal):
    binding = proposal["binding"]
    return (automatic_writeback_requested(run)
            and binding.get("generation_intent") == _explicit_generation(run, binding["product_ref"]))


def verified_writeback_product_ids(engine, run, operations):
    """Verify that a receipted write retained the proposal's original research basis."""
    by_operation = {op["id"]: op for op in operations if op.get("key") == "save_product"}
    saved = set()
    for proposal in engine.store.list("selling_point_proposal"):
        if proposal.get("run_id") != run["id"] or not proposal.get("request_hash"):
            continue
        binding = proposal.get("binding") or {}
        original = binding.get("product_ref") or {}
        op_id = "op_" + canonical_hash([run["id"], "selling_points:" + proposal["id"], "save_product"])[7:39]
        op = by_operation.get(op_id) or {}
        command = proposal.get("save_command") or {}
        receipt = op.get("receipt") or {}
        ref = (receipt.get("data") or {}).get("product_ref") or {}
        if (op.get("status") == "committed" and command == op.get("args")
                and proposal.get("save_command_hash") == op.get("request_hash") == canonical_hash(command)
                and proposal.get("binding_hash") == canonical_hash(binding) and binding.get("research_refs")
                and _automatic_proposal(run, proposal) and receipt.get("operation_id") == op_id
                and receipt.get("command") == "save_product" and receipt.get("status") in {"committed", "replayed"}
                and ref.get("kind") == "product" and ref.get("id") == original.get("id")
                and ref.get("revision") == original.get("revision", 0) + 1 and ref.get("content_hash")):
            saved.add(ref["id"])
    return saved


def _has_selling_points(body):
    return bool(any(str(point).strip() for point in body.get("core_selling_points") or [])
                or str(body.get("selling_points") or "").strip())


def _require_empty_selling_points(body):
    if _has_selling_points(body):
        raise uncommitted_input_error("SELLING_POINTS_PROPOSAL_CLOSED", "该商品已有核心卖点，未覆盖。")


def _targets(run):
    return set(run.get("target_product_ids", []) + run.get("created_product_ids", [])
               + (run.get("goal") or {}).get("target_product_ids", []))


async def _product(engine, scope, run, raw_ref):
    ref = _ref(raw_ref)
    if ref["id"] not in _targets(run):
        raise ContractError("SELLING_POINTS_TARGET_DENIED", "卖点只能用于本次获准目标或本次新建商品", status=403)
    value = await engine.business_query(scope, "read_product", {"product_id": ref["id"]})
    if not _same(value.get("ref"), ref):
        raise uncommitted_input_error("SELLING_POINTS_PRODUCT_STALE", "商品资料已更新，请回读当前商品后重新整理和确认卖点",
            details={"reader": {"query": "read_product", "args": {"product_id": ref["id"]}},
                     "observed_product_ref": value.get("ref")})
    body = value.get("body")
    if not isinstance(body, dict) or canonical_hash(body) != ref["content_hash"]:
        raise ContractError("SELLING_POINTS_BINDING_MISMATCH", "商品正文与精确引用不一致", status=403)
    if body.get("archived") or body.get("sale_status") == "unavailable":
        raise uncommitted_input_error("SELLING_POINTS_PRODUCT_UNAVAILABLE", "商品已停售或撤回，不能据此准备卖点")
    return ref, body


def _questions(engine, run, kind, product_ref=None, proposal_id=None):
    rows = [q for q in engine.store.list("question") if q.get("run_id") == run["id"] and q.get("kind") == kind]
    if product_ref is not None:
        rows = [q for q in rows if _same((q.get("selling_points_binding") or {}).get("product_ref"), product_ref)]
    if proposal_id is not None:
        rows = [q for q in rows if (q.get("context") or {}).get("proposal_id") == proposal_id]
    return rows


def _choice(answer):
    return answer.get("choice") if isinstance(answer, dict) else answer


def _approved_question(engine, run, proposal):
    rows = _questions(engine, run, "selling_points_confirmation", proposal_id=proposal["id"])
    q = rows[-1] if rows else None
    receipt = (q or {}).get("verified_human_receipt") or {}
    expected = {"decision": "selling_points_confirmed", "question_id": (q or {}).get("id"),
                "run_id": run["id"], "proposal_id": proposal["id"],
                "binding_hash": proposal["binding_hash"], "answer_event_ref": (q or {}).get("answer_event_ref")}
    return q if (q and q.get("status") == "ANSWERED" and _choice(q.get("answer")) == "approve"
        and q.get("answer_event_ref") and receipt == expected
        and q.get("selling_points_binding") == proposal["binding"]
        and q.get("selling_points_binding_hash") == proposal["binding_hash"]) else None


def _proposal(engine, run, identity, *, latest=True):
    value = engine.store.get("selling_point_proposal", identity, required=False)
    if not value or value.get("run_id") != run["id"]:
        raise ContractError("SELLING_POINTS_TARGET_DENIED", "候选不属于本次任务", status=403)
    if canonical_hash(value.get("binding")) != value.get("binding_hash"):
        raise ContractError("SELLING_POINTS_BINDING_MISMATCH", "卖点候选正文绑定不一致", status=403)
    if latest and value.get("status") != "SAVED":
        others = [p for p in engine.store.list("selling_point_proposal") if p.get("run_id") == run["id"]
                  and (p.get("binding") or {}).get("product_ref", {}).get("id") == value["binding"]["product_ref"]["id"]]
        if others and others[-1]["id"] != identity:
            raise uncommitted_input_error("SELLING_POINTS_PROPOSAL_CLOSED", "已有新的卖点候选，请确认最新候选；旧批准不能用于新内容")
    return value


def _field(body, path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise _input("每条依据必须给出product_field，例如/specifications/0/value", code="SELLING_POINTS_EVIDENCE_INVALID")
    pieces = [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]
    group = "shelf_life" if pieces[0].startswith("shelf_life_") else pieces[0]
    if pieces[0] not in FACT_FIELDS or group in body.get("hidden_optional_fields", []):
        raise _input("依据须来自启用的商品事实字段；卖点、价格、评价、研究假设和元数据不能充当性能依据",
                     code="SELLING_POINTS_EVIDENCE_INVALID", details={"allowed_fact_fields": sorted(FACT_FIELDS)})
    if pieces[0] == "knowledge_documents" and not (
            len(pieces) == 3 and pieces[1].isdigit() and pieces[2] == "content"):
        raise _input("知识文档依据须定位具体正文/knowledge_documents/序号/content，不能引用标题、ID或整个文档包",
                     code="SELLING_POINTS_EVIDENCE_INVALID")
    value = body
    try:
        for piece in pieces:
            value = value[int(piece)] if isinstance(value, list) and piece.isdigit() else value[piece]
    except (KeyError, IndexError, TypeError, ValueError):
        raise _input("商品事实字段路径不存在，请核对实际商品正文", code="SELLING_POINTS_EVIDENCE_INVALID") from None
    if value is None or value == "" or value == [] or value == {}:
        raise _input("所引商品事实为空，请先补充事实；不能生成未知商品性能", code="SELLING_POINTS_EVIDENCE_INVALID")
    return value


async def _research(engine, scope, run, refs, product_ref):
    if not isinstance(refs, list) or len(refs) > 20:
        raise _input("research_refs需为最多20个正式研究引用")
    normalized = []
    for raw in refs:
        ref = _ref(raw, "analysis")
        value = await engine.business_query(scope, "read_context", {"ref": ref})
        body = value.get("body") or {}
        report = body.get("report") or {}
        deps = [*body.get("product_refs", []), *body.get("readset", []), *report.get("dependencies", [])]
        product_deps = [p for p in deps if isinstance(p, dict) and p.get("kind") == "product"]
        if (not _same(value.get("ref"), ref) or body.get("accepted") is not True
                or body.get("role") != "audience_scenes" or not any(_same(p, product_ref) for p in deps)
                or any(not _same(p, product_ref) for p in product_deps)
                or set(report.get("product_ids", [])) - {product_ref["id"]}):
            raise _input("顾客研究必须已正式验收并绑定当前商品版本；旧研究应先复核绑定，不能替代商品事实",
                         code="SELLING_POINTS_RESEARCH_INVALID")
        if ref not in normalized:
            normalized.append(ref)
    return normalized


async def prepare_question(engine, scope, run, args):
    """Build UI content from server-owned records, never the Agent's preview."""
    context = args.get("context") or {}
    if args["kind"] == "selling_points_generation":
        if set(context) != {"product_ref"}:
            raise _input("卖点生成选择仅接受context.product_ref")
        ref, body = await _product(engine, scope, run, context["product_ref"])
        if _explicit_generation(run, ref):
            raise _input("用户已发送这款商品的卖点整理指令，可直接提交候选，请使用wf_selling_points.propose。")
        binding = {"product_ref": ref}
        has_points = _has_selling_points(body)
        return {"context": binding, "selling_points_binding": binding,
            "selling_points_binding_hash": canonical_hash(binding), "allow_text": True,
            "prompt": f"{body.get('name', '此商品')}已整理进商品库。" +
                ("已有卖点，是否重新整理？" if has_points else "目前没有整理好的核心卖点，需要我根据商品事实和顾客场景帮你生成吗？") +
                "可以补充希望面向的人群、场景或表达方向；未知商品事实不会补造。",
            "preview": {"product_ref": ref, "product_name": body.get("name"), "has_selling_points": has_points},
            "options": [{"value": "generate", "label": "帮我整理卖点"},
                        {"value": "skip", "label": "暂不生成"}]}
    if args["kind"] != "selling_points_confirmation":
        return {}
    if set(context) != {"proposal_id"}:
        raise _input("卖点确认仅接受context.proposal_id")
    proposal = _proposal(engine, run, context["proposal_id"])
    if _automatic_proposal(run, proposal):
        raise _input("这批空白卖点已获得生成并保存授权。使用wf_selling_points.propose完成处理。")
    if proposal.get("status") == "SAVED":
        raise uncommitted_input_error("SELLING_POINTS_PROPOSAL_CLOSED", "此候选已经保存，无需再次确认")
    ref, body = await _product(engine, scope, run, proposal["binding"]["product_ref"])
    binding = proposal["binding"]
    return {"context": {"proposal_id": proposal["id"]}, "selling_points_binding": binding,
        "selling_points_binding_hash": proposal["binding_hash"], "allow_text": True,
        "prompt": f"请核对{body.get('name', '此商品')}的{len(binding['items'])}条候选卖点及依据。确认后只保存下方卖点到商品库，不会生成话术或语音。需要改动请选调整并填写方向。",
        "preview": {"proposal_id": proposal["id"], "product_ref": ref, "product_name": body.get("name"), **binding},
        "options": [{"value": "approve", "label": "确认并保存这些卖点"},
                    {"value": "revise", "label": "调整后再确认"}, {"value": "reject", "label": "暂不保存"}]}


async def validate_answer(engine, run, question, answer):
    """Validate an explicit human choice without writing before answer commit."""
    generation = question["kind"] == "selling_points_generation"
    choices = {"generate", "skip"} if generation else {"approve", "revise", "reject"}
    if isinstance(answer, str):
        normalized = answer
    elif (isinstance(answer, dict) and set(answer) <= {"choice", "text"} and "choice" in answer
          and isinstance(answer.get("choice"), str)):
        text = _text(answer.get("text", ""), "补充方向", 6000, allow_empty=True)
        normalized = {"choice": answer["choice"], "text": text}
    else:
        raise _input("请选择明确选项；补充说明需使用choice和text")
    choice = _choice(normalized)
    if choice not in choices or choice == "approve" and isinstance(normalized, dict) and normalized["text"]:
        raise _input("确认保存只能批准当前预览；有修改请先选择调整")
    # Answer path has no model scope. Read with controller identity, but do not
    # grant writes; verify the original run target and exact current product.
    binding = question.get("selling_points_binding")
    if not isinstance(binding, dict) or canonical_hash(binding) != question.get("selling_points_binding_hash"):
        raise ContractError("SELLING_POINTS_BINDING_MISMATCH", "确认问题缺少精确内容绑定", status=403)
    # Refusal / revision instructions grant no write authority. They must remain
    # answerable even when an external editor changed the old product or a newer
    # candidate was prepared; otherwise an OPEN question would trap the run.
    if choice not in {"generate", "approve"}:
        return normalized, None
    ref = _ref(binding.get("product_ref"))
    if ref["id"] not in _targets(run):
        raise ContractError("SELLING_POINTS_TARGET_DENIED", "商品已不在任务目标内", status=403)
    current = await engine.client.query("read_product", {"product_id": ref["id"]}, controller=True)
    if not _same(current.get("ref"), ref):
        raise uncommitted_input_error("SELLING_POINTS_PRODUCT_STALE", "商品已变化，请重新读取后确认新内容")
    verified = None
    if not generation:
        proposal = _proposal(engine, run, (question.get("context") or {}).get("proposal_id"))
        if proposal["binding"] != binding or proposal["binding_hash"] != question["selling_points_binding_hash"]:
            raise ContractError("SELLING_POINTS_BINDING_MISMATCH", "确认预览与候选正文不一致", status=403)
    if choice in {"generate", "approve"}:
        verified = {"decision": "selling_points_generation_requested" if generation else "selling_points_confirmed",
            "question_id": question["id"], "run_id": run["id"], "binding_hash": question["selling_points_binding_hash"]}
        if generation:
            verified["product_ref"] = ref
        else:
            verified["proposal_id"] = proposal["id"]
    return normalized, verified


async def execute(engine, scope, args, tool_call_id):
    run = await engine.assert_scope(scope)
    if scope.role != "main" or scope.phase != "execute":
        raise ContractError("SELLING_POINTS_TARGET_DENIED", "仅主Agent执行阶段可整理、确认或保存卖点", status=403)
    action = args.get("action", "inspect")
    params = deepcopy(args.get("parameters") or {})
    allowed = {"inspect": {"product_ref", "proposal_id"}, "offer": {"product_ref"},
               "propose": {"product_ref", "items", "research_refs", "direction"}, "save": {"proposal_id"}}
    if action not in allowed or not isinstance(params, dict) or set(params) - allowed[action]:
        raise _input("卖点工具流动作或参数不符合约定")
    automatic = automatic_writeback_requested(run)
    if action == "inspect":
        if params.get("proposal_id"):
            return {"proposal": _proposal(engine, run, params["proposal_id"])}
        ref, body = await _product(engine, scope, run, params.get("product_ref"))
        candidates = [p for p in engine.store.list("selling_point_proposal") if p.get("run_id") == run["id"]
                      and p.get("binding", {}).get("product_ref", {}).get("id") == ref["id"]]
        return {"product_ref": ref, "core_selling_points": body.get("core_selling_points", []),
            "selling_points": body.get("selling_points", ""), "has_selling_points": _has_selling_points(body),
            "proposals": candidates, "fact_fields": sorted(FACT_FIELDS),
            "selling_point_evidence": build_evidence_catalog(ref, body),
            "note": "缺少卖点表达可整理；缺少事实须补充资料，不能按顾客偏好发明性能。"}
    if action == "offer":
        if explicit_generation_refs(run):
            ref, _ = await _product(engine, scope, run, params.get("product_ref"))
            return {"status": "generation_requested", "product_ref": ref,
                    "generation_intent": _explicit_generation(run, ref),
                    "next": ("已获得生成并保存授权。使用propose校验并写入该款空白卖点。" if automatic else
                             "已收到用户整理卖点指令，可直接propose候选。保存仍须由用户确认候选。")}
        return await engine.request_decision(scope, {"kind": "selling_points_generation", "prompt": "是否整理核心卖点？",
            "context": {"product_ref": params.get("product_ref")}}, tool_call_id + "-offer")
    if action == "propose":
        if automatic:
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise _input("自动保存需要稳定的工具调用编号。")
            # A replay may start after our write advanced the selected head.
            # Use the persisted proposal and command before checking that head.
            identity = "sp_auto_" + canonical_hash([scope.run_id, tool_call_id])[7:39]
            request_hash = canonical_hash(params)
            prior = engine.store.get("selling_point_proposal", identity, required=False)
            if prior:
                if prior.get("request_hash") != request_hash:
                    raise ContractError("SELLING_POINTS_BINDING_MISMATCH", "同一次卖点保存请求的参数已变化。", status=403)
                return await _save_proposal(engine, scope, run, identity)
        ref, body = await _product(engine, scope, run, params.get("product_ref"))
        explicit = _explicit_generation(run, ref)
        if automatic:
            _require_empty_selling_points(body)
        offers = _questions(engine, run, "selling_points_generation", product_ref=ref)
        offer = offers[-1] if offers else {}
        receipt = offer.get("verified_human_receipt") or {}
        expected_receipt = {"decision": "selling_points_generation_requested", "question_id": offer.get("id"),
            "run_id": run["id"], "binding_hash": canonical_hash({"product_ref": ref}),
            "product_ref": ref, "answer_event_ref": offer.get("answer_event_ref")}
        if not explicit and (offer.get("status") != "ANSWERED" or _choice(offer.get("answer")) != "generate"
                             or not offer.get("answer_event_ref") or receipt != expected_receipt):
            raise uncommitted_input_error("SELLING_POINTS_GENERATION_CHOICE_REQUIRED", "请先通过offer询问用户，取得整理卖点及方向的明确选择")
        raw_items = params.get("items")
        if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 60:
            raise _input("候选需要1至60条卖点")
        items = []
        catalog = build_evidence_catalog(ref, body) if automatic else None
        for item_index, raw in enumerate(raw_items):
            if not isinstance(raw, dict) or set(raw) - {"selling_point", "audience_need", "use_scene", "evidence", "limitations"}:
                raise _input("候选条目只接受卖点、顾客需求、使用场景、依据和限制")
            item = {key: _text(raw.get(key), key) for key in ("selling_point", "audience_need", "use_scene")}
            item["limitations"] = _text(raw.get("limitations", ""), "limitations", allow_empty=True)
            if not isinstance(raw.get("evidence"), list) or not 1 <= len(raw["evidence"]) <= 20:
                raise _input("每条卖点至少提供一个实际商品事实依据", code="SELLING_POINTS_EVIDENCE_INVALID")
            item["evidence"] = []
            for evidence_index, e in enumerate(raw["evidence"]):
                if isinstance(e, dict) and set(e) == {"evidence_id"} and isinstance(e["evidence_id"], str):
                    if catalog is None:
                        catalog = build_evidence_catalog(ref, body)
                    fact = catalog.get(e["evidence_id"])
                    if fact is None:
                        raise _input("事实编号不属于当前商品版本，请使用该款的 selling_point_evidence 编号。",
                            code="SELLING_POINTS_EVIDENCE_INVALID", details={"item_index": item_index,
                                "evidence_index": evidence_index, "next": "read_product.selling_point_evidence"})
                    item["evidence"].append({"product_field": fact["product_field"], "fact": fact["fact"], "source_ref": ref})
                    continue
                if automatic:
                    raise _input("报告入口的卖点依据须使用 {evidence_id}，请从当前商品证据目录选择编号。",
                        code="SELLING_POINTS_EVIDENCE_INVALID", details={"item_index": item_index,
                            "evidence_index": evidence_index, "next": "read_product.selling_point_evidence"})
                if not isinstance(e, dict) or set(e) - {"product_field", "fact", "source_ref"}:
                    raise _input("依据需包含product_field、fact，可选当前商品source_ref", code="SELLING_POINTS_EVIDENCE_INVALID")
                actual = _field(body, e.get("product_field"))
                fact = _text(e.get("fact"), "事实摘录", 6000)
                searchable = actual if isinstance(actual, str) else canonical_json(actual)
                if fact not in searchable:
                    raise _input("事实摘录不在当前商品字段正文中；请引用原文，不要把推断写成事实", code="SELLING_POINTS_EVIDENCE_INVALID")
                if e.get("source_ref") and not _same(e["source_ref"], ref):
                    raise _input("依据source_ref必须为此精确商品版本", code="SELLING_POINTS_EVIDENCE_INVALID")
                item["evidence"].append({"product_field": e["product_field"], "fact": fact, "source_ref": ref})
            items.append(item)
        if len("\n".join(item["selling_point"] for item in items)) > 15000:
            raise _input("核心卖点合计不能超过15000字")
        research = await _research(engine, scope, run, params.get("research_refs", []), ref)
        if automatic and not research:
            raise _input("请先复用或验收该款顾客研究，并在提交卖点时传入其精确引用。",
                         code="SELLING_POINTS_RESEARCH_INVALID")
        human_direction = offer.get("answer", {}).get("text", "") if isinstance(offer.get("answer"), dict) else ""
        direction = _text(params.get("direction") or human_direction, "direction", 6000, allow_empty=True)
        binding = {"product_ref": ref, "items": items, "research_refs": research,
                   "direction": direction, "requested_direction": human_direction}
        if explicit:
            binding["generation_intent"] = explicit
        if not automatic:
            identity = "sp_" + canonical_hash([scope.run_id, tool_call_id, binding])[7:39]
        proposal = engine.store.get("selling_point_proposal", identity, required=False)
        if not proposal:
            proposal = engine.store.put("selling_point_proposal", {"id": identity, "run_id": scope.run_id,
                "binding": binding, "binding_hash": canonical_hash(binding), "status": "PROPOSED", "created_at": now_iso(),
                **({"request_hash": request_hash} if automatic else {})})
            engine._emit(scope.run_id, "selling_points.proposed", {"title": "核心卖点候选已准备", "proposal_id": identity,
                "product_ref": ref, "item_count": len(items), "binding_hash": proposal["binding_hash"]}, scope)
        if automatic:
            if proposal.get("request_hash") != request_hash:
                raise ContractError("SELLING_POINTS_BINDING_MISMATCH", "同一次卖点保存请求的参数已变化。", status=403)
            return await _save_proposal(engine, scope, run, identity)
        decision = await engine.request_decision(scope, {"kind": "selling_points_confirmation", "prompt": "请确认候选核心卖点",
            "context": {"proposal_id": identity}}, tool_call_id + "-confirm")
        return {"proposal_id": identity, "proposal": proposal, "decision": decision}
    return await _save_proposal(engine, scope, run, params.get("proposal_id"))


async def _save_proposal(engine, scope, run, proposal_id):
    proposal = _proposal(engine, run, proposal_id)
    if proposal.get("status") == "SAVED":
        return {"status": "SAVED", "proposal_id": proposal["id"], "receipt": proposal["receipt"], "product_ref": proposal["saved_product_ref"]}
    automatic = _automatic_proposal(run, proposal)
    if not automatic and not _approved_question(engine, run, proposal):
        raise uncommitted_input_error("SELLING_POINTS_CONFIRMATION_REQUIRED", "候选尚未获得对应内容的正式确认；不能保存被拒绝、待修改或未确认的卖点")
    points = [item["selling_point"] for item in proposal["binding"]["items"]]
    command = proposal.get("save_command")
    if command is not None:
        if canonical_hash(command) != proposal.get("save_command_hash"):
            raise ContractError("SELLING_POINTS_BINDING_MISMATCH", "原保存命令已变化，不能重放", status=403)
        # A known committed command may have advanced the product before this
        # local proposal was marked SAVED. Replay the original idempotent
        # command before querying today's head; unknown outcomes stay fenced in
        # business_command. This is not a second logical write.
    else:
        ref, body = await _product(engine, scope, run, proposal["binding"]["product_ref"])
        if automatic:
            _require_empty_selling_points(body)
        schema = await engine.business_query(scope, "get_business_schema", {})
        fields = {k: deepcopy(v) for k, v in body.items() if k in schema["schema"]["product"]["properties"]}
        fields.update(core_selling_points=points, selling_points="\n".join(points))
        command = {"product_id": ref["id"], "expected_revision": ref["revision"], "schema_hash": schema["schema_hash"],
            "fields": fields, "patches": [{"path": "/core_selling_points", "op": "set", "value": points}]}
        if automatic:
            command = COMMAND_ARGS["save_product"].model_validate(command).model_dump(mode="json", exclude_none=True)
        engine.store.update("selling_point_proposal", proposal["id"], save_command=command, save_command_hash=canonical_hash(command))
    receipt = await engine.business_command(scope, "save_product", command, "selling_points:" + proposal["id"])
    saved_ref = receipt.get("data", {}).get("product_ref")
    if not saved_ref:
        raise ContractError("SELLING_POINTS_RECEIPT_INVALID", "商品保存回执缺少精确引用，请核对原操作；不要重新生成或盲重发")
    engine.store.update("selling_point_proposal", proposal["id"], status="SAVED", receipt=receipt,
                        saved_product_ref=saved_ref, saved_at=now_iso())
    engine._emit(scope.run_id, "selling_points.saved", {"title": "核心卖点保存完成", "proposal_id": proposal["id"],
        "product_ref": saved_ref, "item_count": len(points), "operation_id": receipt.get("operation_id")}, scope)
    return {"status": "SAVED", "proposal_id": proposal["id"], "receipt": receipt, "product_ref": saved_ref,
        "next": ("本款核心卖点已保存。继续处理其他已选空白商品，全部保存后finish_turn。现有顾客研究用于原事实版本，无需再做研究或核对话术。"
                 if automatic else "商品资料已更新；按用户原任务决定是否继续生成话术，不自动改写旧稿。")}


async def guard_product_write(engine, scope, run, args, *, tool_call_id=None):
    """Prevent the old raw save tool from bypassing an active candidate gate."""
    product_id = args.get("product_id")
    changed = {p.get("path") for p in args.get("patches", []) if p.get("op") != "preserve"}
    explicit = any(ref["id"] == product_id for ref in explicit_generation_refs(run))
    if not product_id or (not explicit and not changed.intersection(POINT_FIELDS)):
        return
    proposals = [p for p in engine.store.list("selling_point_proposal") if p.get("run_id") == run["id"]
                 and p.get("binding", {}).get("product_ref", {}).get("id") == product_id]
    offers = [q for q in _questions(engine, run, "selling_points_generation")
              if q.get("selling_points_binding", {}).get("product_ref", {}).get("id") == product_id]
    if not proposals and not offers and not explicit:
        return  # Existing explicit merchant-provided product update path.
    proposal = _proposal(engine, run, proposals[-1]["id"]) if proposals else None
    if explicit and automatic_writeback_requested(run):
        if (not proposal or not _automatic_proposal(run, proposal)
                or tool_call_id != "selling_points:" + proposal["id"]
                or canonical_hash(args) != proposal.get("save_command_hash")):
            raise _input("请用wf_selling_points.propose校验依据并写入空白卖点。",
                         code="SELLING_POINTS_CONFIRMATION_REQUIRED")
    elif not proposal or not _approved_question(engine, run, proposal):
        raise uncommitted_input_error("SELLING_POINTS_CONFIRMATION_REQUIRED", "本任务已进入卖点整理分支，请先确认候选；原商品保存工具也不能绕过确认")
    ref = proposal["binding"]["product_ref"]
    points = [item["selling_point"] for item in proposal["binding"]["items"]]
    if (not changed.intersection(POINT_FIELDS) or changed - POINT_FIELDS or args.get("expected_revision") != ref["revision"]
            or args.get("fields", {}).get("core_selling_points") != points
            or args.get("fields", {}).get("selling_points") != "\n".join(points)
            or any(p.get("op") != "set" or p.get("value") != (points if p.get("path") == "/core_selling_points" else "\n".join(points))
                   for p in args.get("patches", []) if p.get("op") != "preserve")):
        raise ContractError("SELLING_POINTS_BINDING_MISMATCH", "实际写入内容与已确认候选不一致；批准不覆盖其他字段、文本或商品版本", status=403)

