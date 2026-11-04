"""Bind a missing catalog currency to an explicit answer and the exact draft."""
import unicodedata

from shengbo_contracts.common import ContractError, canonical_hash


CATALOG_INTAKE_CURRENCY_KIND = "catalog_intake_currency"
CURRENCIES = {"CNY": "人民币", "TWD": "新台币", "USD": "美元", "AUD": "澳元"}


def _candidate(engine, run, context):
    branch_id = context.get("branch_id")
    branch = engine.store.get("branch", branch_id, required=False) if isinstance(branch_id, str) else None
    draft = (branch or {}).get("catalog_intake_currency_draft")
    if (not run.get("catalog_intake_enabled") or not branch or branch.get("run_id") != run["id"]
            or branch.get("execution_mode") != "catalog_intake" or not isinstance(draft, dict)
            or set(context) != {"branch_id", "draft_hash", "sku_id"}
            or context["draft_hash"] != canonical_hash(draft)):
        raise ContractError("CATALOG_CURRENCY_BINDING_INVALID", "待补币种的商品资料已变化，请重新核对。", status=409)
    candidate = next((row for row in draft["fields"]["products"]
                      if row["sku_id"] == context["sku_id"] and "currency" not in row), None)
    if candidate is None:
        raise ContractError("CATALOG_CURRENCY_BINDING_INVALID", "这个商品规格没有待补的币种。", status=409)
    return candidate


def prepare_catalog_currency_question(engine, run, context):
    candidate = _candidate(engine, run, context)
    target = f"{candidate['name']}（{candidate['sku_id']}）"
    return {"title": "确认日常售价币种",
            "prompt": f"资料里无法确认「{target}」的日常售价币种。已核实的内容已保留，确认币种后可继续。",
            "options": [{"value": code, "label": label} for code, label in CURRENCIES.items()],
            "allow_text": True, "input_placeholder": "输入币种名称或代码，例如人民币"}


def verify_catalog_currency_answer(engine, run, question, answer):
    context = question.get("context") or {}
    candidate = _candidate(engine, run, context)
    raw = answer.get("text") if isinstance(answer, dict) and set(answer) == {"text"} else answer
    text = unicodedata.normalize("NFKC", raw).strip() if isinstance(raw, str) else ""
    currency = next((code for code, label in CURRENCIES.items() if text.upper() == code or text == label), None)
    if currency is None:
        raise ContractError("CATALOG_CURRENCY_INVALID", "请选一个明确的币种，或输入币种名称、代码。", status=422)
    return {"decision": "catalog_currency_supplied", "run_id": run["id"], "question_id": question["id"],
            "draft_hash": context["draft_hash"], "sku_id": candidate["sku_id"], "currency": currency}


def read_catalog_currency_receipt(engine, run, decision_id):
    question = engine.store.get("question", decision_id)
    if (question.get("run_id") != run["id"] or question.get("kind") != CATALOG_INTAKE_CURRENCY_KIND
            or question.get("status") != "ANSWERED" or not question.get("answer_event_ref")
            or not question.get("answer_request_id")):
        raise ContractError("CATALOG_CURRENCY_BINDING_INVALID", "缺少本次商品的正式币种答复。", status=409)
    proof = {**verify_catalog_currency_answer(engine, run, question, question["answer"]),
             "answer_event_ref": question["answer_event_ref"]}
    if question.get("verified_human_receipt") != proof:
        raise ContractError("CATALOG_CURRENCY_BINDING_INVALID", "本次商品的币种答复无法核对。", status=409)
    return proof
