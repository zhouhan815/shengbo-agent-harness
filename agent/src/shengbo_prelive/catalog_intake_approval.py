"""Exact catalog intake decisions and the guard shared by every product save."""
from copy import deepcopy
from decimal import Decimal
import math

from shengbo_contracts.common import ContractError, canonical_hash, same_artifact_ref


CATALOG_INTAKE_KIND = "catalog_intake_approval"
_CURRENCIES = {"CNY": "元人民币", "TWD": "新台币", "USD": "美元", "AUD": "澳元"}


def _deny(message="当前商品入库方案或确认记录已变化，请重新核对。"):
    return ContractError("CATALOG_INTAKE_APPROVAL_REQUIRED", message, status=403)


def make_catalog_intake_plan(run, *, branch_task_id, report_hash, candidates):
    sources = run.get("input", {}).get("material_refs", [])
    if not branch_task_id or not report_hash or not sources or not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
        raise ContractError("CATALOG_INTAKE_PLAN_INVALID", "本次资料需要明确的商品规格和可核对的来源。", status=422)
    rows = []
    for fields in candidates:
        if not isinstance(fields, dict):
            raise ContractError("CATALOG_INTAKE_PLAN_INVALID", "商品候选格式不完整。", status=422)
        sku, name, currency, price = (fields.get(key) for key in ("sku_id", "name", "currency", "price"))
        if (not isinstance(sku, str) or not sku or sku != sku.strip() or len(sku) > 200
                or not isinstance(name, str) or not name.strip()
                or not isinstance(currency, str) or not currency.strip()
                or type(price) not in {int, float} or price < 0
                or isinstance(price, float) and not math.isfinite(price)):
            raise ContractError("CATALOG_INTAKE_PLAN_INVALID", "商品名称、规格编号、币种和日常售价需要明确。", status=422)
        rows.append({"sku_id": sku, "fields": deepcopy(fields), "fields_hash": canonical_hash(fields)})
    if len({row["sku_id"].casefold() for row in rows}) != len(rows):
        raise ContractError("CATALOG_INTAKE_PLAN_INVALID", "商品规格编号存在重复，请先核对身份。", status=422)
    plan = {"version": "catalog-intake-v1", "run_id": run["id"], "scope": deepcopy(run["scope"]),
            "goal_hash": canonical_hash(run["goal"]), "input_hash": canonical_hash(run["input"]),
            "branch_task_id": branch_task_id, "report_hash": report_hash,
            "source_refs": deepcopy(sources), "candidates": rows}
    return {**plan, "plan_hash": canonical_hash(plan)}


def _require_plan(run):
    from .document_import import import_entry
    plan = run.get("catalog_intake_plan")
    scope_kind = run.get("scope", {}).get("kind")
    if not (run.get("catalog_intake_enabled") is True and isinstance(plan, dict)
            and plan.get("version") == "catalog-intake-v1" and plan.get("run_id") == run.get("id")
            and plan.get("scope") == run.get("scope")
            and (scope_kind == "catalog" or scope_kind == "preparation" and import_entry(run) == "room")
            and run.get("goal", {}).get("catalog_delivery") == "basic_facts_only"
            and plan.get("goal_hash") == canonical_hash(run.get("goal"))
            and plan.get("input_hash") == canonical_hash(run.get("input"))
            and plan.get("source_refs") == run.get("input", {}).get("material_refs")
            and plan.get("plan_hash") == canonical_hash({key: value for key, value in plan.items() if key != "plan_hash"})):
        raise _deny()
    return plan


def _branch_for_plan(engine, run, plan, *, accepted=False):
    branch = engine.store.get("branch", plan["branch_task_id"], required=False)
    report = (branch or {}).get("report")
    candidates = [row["fields"] for row in plan["candidates"]]
    if not (branch and branch.get("run_id") == run["id"] and branch.get("role") == "product_facts"
            and branch.get("status") in ({"ACCEPTED"} if accepted else {"PREPARED", "ACCEPTED"})
            and branch.get("report_hash") == plan["report_hash"]
            and isinstance(report, dict) and canonical_hash(report) == plan["report_hash"]
            and report.get("fields") == {"products": candidates}
            and branch.get("task", {}).get("source_refs") == plan["source_refs"]
            and report.get("source_refs") == plan["source_refs"]):
        raise _deny("商品方案的事实报告或资料版本无法核对。")
    return branch


def _round(run):
    value = run.get("catalog_intake_confirmation_round", 1)
    if type(value) is not int or value < 1:
        raise _deny()
    return value


def catalog_intake_question_id(run):
    plan = _require_plan(run)
    return "decision_" + canonical_hash([run["id"], CATALOG_INTAKE_KIND, plan["plan_hash"], _round(run)])[7:39]


def _choices(plan):
    rows = plan["candidates"]
    all_label = "确认入库" if len(rows) == 1 else "全部分别入库"
    choices = [({"value": "save_all", "label": all_label}, [row["sku_id"] for row in rows])]
    if len(rows) > 1:
        for index, row in enumerate(rows, 1):
            sku = row["sku_id"]
            choices.append(({"value": f"save_{index}", "label": f"只保存 {row['fields']['name']}（{sku}）"}, [sku]))
    choices.append(({"value": "skip", "label": "暂不保存"}, []))
    return choices


def _display_price(price, currency):
    amount = Decimal(str(price))
    number = format(amount, ".2f" if amount.as_tuple().exponent >= -2 else "f")
    return f"{number} {_CURRENCIES.get(currency, currency)}"


def prepare_catalog_intake_question(engine, run):
    plan = _require_plan(run)
    _branch_for_plan(engine, run, plan)
    rows = plan["candidates"]
    items = [{key: row["fields"][key] for key in ("name", "sku_id", "price", "currency")} for row in rows]
    prefix = "已收到补充说明。原方案尚未修改，你可以选择按原方案保存或暂不保存。" if _round(run) > 1 else "请核对这次要新增的商品和日常售价。"
    from .document_import import import_entry
    entry = import_entry(run)
    if entry == "catalog":
        prefix += " 确认后会自动创建直播间、绑定所选商品，并将识别到的活动保存为本场草稿。"
    elif entry == "room":
        prefix += " 确认后会绑定到当前直播间，并保存本场配置草稿。已有同款仅沿用。"
    lines = [f"{index}. {item['name']}（{item['sku_id']}） · {_display_price(item['price'], item['currency'])}"
             for index, item in enumerate(items, 1)]
    return {"title": "确认商品入库方案", "prompt": "\n".join([prefix, *lines]),
            "options": [option for option, _ in _choices(plan)],
            "preview": {"type": "catalog_intake", "items": [{**item, "fields": deepcopy(row["fields"])}
                        for item, row in zip(items, rows)]},
            "catalog_intake_binding": {"plan_hash": plan["plan_hash"], "round": _round(run)}, "allow_text": False}


def _require_question(run, question, plan):
    if (question.get("id") != catalog_intake_question_id(run) or question.get("run_id") != run["id"]
            or question.get("kind") != CATALOG_INTAKE_KIND
            or question.get("catalog_intake_binding") != {"plan_hash": plan["plan_hash"], "round": _round(run)}
            or question.get("options") != [option for option, _ in _choices(plan)]):
        raise _deny()


def _selection(plan, answer):
    if isinstance(answer, str):
        for option, selected in _choices(plan):
            if answer == option["value"]:
                return "approved" if selected else "skip", selected, None
    elif (isinstance(answer, dict) and set(answer) == {"choice", "text"} and answer.get("choice") == "revise"
          and isinstance(answer["text"], str) and answer["text"].strip() and len(answer["text"]) <= 6000):
        return "supplement", [], answer["text"]
    raise ContractError("CATALOG_INTAKE_CHOICE_REQUIRED",
        "请点击要保存的商品方案，或提交修改意见。文字说明不会直接确认入库。", status=422)


def verify_catalog_intake_answer(engine, run, question, answer):
    plan = _require_plan(run)
    _require_question(run, question, plan)
    _branch_for_plan(engine, run, plan)
    state, selected, _ = _selection(plan, answer)
    if state == "supplement":
        return None
    return {"decision": "catalog_intake_" + state, "run_id": run["id"], "question_id": question["id"],
            "plan_hash": plan["plan_hash"], "round": _round(run), "selected_sku_ids": selected}


def _operations(engine, run, plan):
    candidates = {row["sku_id"]: row["fields_hash"] for row in plan["candidates"]}
    operations = {}
    for op in engine.store.list("operation"):
        if op.get("run_id") != run["id"] or op.get("key") != "save_product":
            continue
        fields = (op.get("args") or {}).get("fields") or {}
        sku = fields.get("sku_id")
        if (sku not in candidates or canonical_hash(fields) != candidates[sku]
                or op.get("request_hash") != canonical_hash(op["args"])):
            raise _deny("存在与当前入库方案不一致的商品写入记录，请先核对。")
        status = op.get("status")
        previous = operations.get(sku)
        if previous and previous["status"] in {"committed", "sent", "unknown"}:
            raise _deny("同一规格存在多笔保存记录，请先核对。")
        ref = None
        if status == "committed":
            receipt = op.get("receipt") or {}
            ref = receipt.get("data", {}).get("product_ref")
            if (receipt.get("operation_id") != op["id"] or receipt.get("command") != "save_product"
                    or receipt.get("status") not in {"committed", "replayed"}
                    or not isinstance(ref, dict) or ref.get("kind") != "product" or not ref.get("id")):
                raise _deny("已保存商品缺少可核对的回执。")
        operations[sku] = {"operation_id": op["id"], "status": status, "product_ref": ref,
                           "error_code": op.get("error_code")}
    return operations


def resolve_catalog_intake_decision(engine, run):
    plan = _require_plan(run)
    identity = catalog_intake_question_id(run)
    question = engine.store.get("question", identity, required=False)
    result = {"state": "pending", "selected_sku_ids": [], "decision_id": identity, "answer_event_ref": None,
              "text": None, "operations_by_sku": _operations(engine, run, plan)}
    if not question or question.get("status") != "ANSWERED":
        return result
    _require_question(run, question, plan)
    state, selected, text = _selection(plan, question.get("answer"))
    event_ref = question.get("answer_event_ref")
    if not event_ref or not question.get("answer_request_id"):
        raise _deny()
    verified = question.get("verified_human_receipt")
    if state != "supplement":
        proof = verify_catalog_intake_answer(engine, run, question, question["answer"])
        if verified != {**proof, "answer_event_ref": event_ref}:
            raise _deny()
    elif verified is not None:
        raise _deny()
    if set(result["operations_by_sku"]) - set(selected):
        raise _deny("有商品保存记录不属于当前已确认范围，请先核对。")
    return {**result, "state": state, "selected_sku_ids": selected, "answer_event_ref": event_ref, "text": text}


def guard_catalog_intake_write(engine, run, key, args, operation_id):
    if key != "save_product" or not (run.get("catalog_intake_enabled") is True or run.get("catalog_intake_plan")):
        return
    plan = _require_plan(run)
    resolved = resolve_catalog_intake_decision(engine, run)
    fields = args.get("fields") or {}
    sku = fields.get("sku_id")
    candidate = next((row for row in plan["candidates"] if row["sku_id"] == sku), None)
    if (resolved["state"] != "approved" or sku not in resolved["selected_sku_ids"]
            or not candidate or canonical_hash(fields) != candidate["fields_hash"]
            or args.get("product_id") is not None or args.get("expected_revision") is not None
            or args.get("patches") or args.get("field_dispositions") or args.get("identity_decision_ref")):
        raise _deny("只能按已确认方案新增所选规格。修改商品内容需要重新确认。")
    prior = resolved["operations_by_sku"].get(sku)
    if prior:
        if prior["status"] in {"sent", "unknown"}:
            raise ContractError("RECONCILIATION_REQUIRED", "该规格已有在途或待核对的原保存操作。", status=409)
        if prior["status"] == "committed" and prior["operation_id"] != operation_id:
            raise _deny("该规格已有原保存操作，请核对原回执。")
    branch = _branch_for_plan(engine, run, plan, accepted=True)
    replacements = branch.get("source_ref_replacements") or []
    supplied = args.get("source_manifest_refs") or []
    if (len(replacements) != len(plan["source_refs"]) or len(supplied) != len(replacements)
            or any(sum(same_artifact_ref(source, row.get("old_ref")) for row in replacements) != 1 for source in plan["source_refs"])
            or any(sum(same_artifact_ref(source, row.get("new_ref")) for source in supplied) != 1 for row in replacements)):
        raise _deny("商品写入使用的资料与已确认且正式采纳的来源不一致。")
