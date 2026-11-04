"""A missing daily price is user-supplied data, separate from approval to save."""
from decimal import Decimal
import re
import unicodedata

from shengbo_contracts.common import ContractError, canonical_hash


CATALOG_INTAKE_PRICE_KIND = "catalog_intake_price"
_AMOUNT = re.compile(r"[0-9]{1,8}(?:\.[0-9]{1,2})?")


def _bound_candidate(engine, run, context):
    branch_id = context.get("branch_id") if isinstance(context, dict) else None
    branch = engine.store.get("branch", branch_id, required=False) if isinstance(branch_id, str) else None
    draft = (branch or {}).get("catalog_intake_price_draft")
    if (not run.get("catalog_intake_enabled") or not branch or branch.get("run_id") != run["id"]
            or branch.get("execution_mode") != "catalog_intake" or not isinstance(draft, dict)
            or set(context) != {"branch_id", "draft_hash", "sku_id", "deferred"}
            or context["draft_hash"] != canonical_hash(draft) or type(context["deferred"]) is not bool):
        raise ContractError("CATALOG_PRICE_BINDING_INVALID", "待补价格的商品资料已变化，请重新核对。", status=409)
    candidate = next((row for row in draft["fields"]["products"]
                      if row["sku_id"] == context["sku_id"] and "price" not in row), None)
    if not candidate:
        raise ContractError("CATALOG_PRICE_BINDING_INVALID", "这个商品规格没有待补的日常售价。", status=409)
    return candidate


def prepare_catalog_price_question(engine, run, context):
    candidate = _bound_candidate(engine, run, context)
    target = f"{candidate['name']}（{candidate['sku_id']}）"
    currency = candidate["currency"]
    prefix = "资料已保留，补充价格后可以继续。" if context["deferred"] else "资料中没有这款的日常售价。"
    return {"prompt": f"{prefix}\n请填写{target}的日常售价（{currency}）。填写后还需确认入库。",
            "preview": {"type": CATALOG_INTAKE_PRICE_KIND, "product_name": candidate["name"],
                        "sku_id": candidate["sku_id"], "currency": currency, "deferred": context["deferred"]},
            "options": [] if context["deferred"] else [{"value": "later", "label": "稍后补价格（暂不入库）"}],
            "allow_text": True, "input_placeholder": "输入日常售价金额，例如 89.90"}


def verify_catalog_price_answer(engine, run, question, answer):
    context = question.get("context") or {}
    candidate = _bound_candidate(engine, run, context)
    if answer == "later" and not context["deferred"]:
        price = None
    else:
        raw = answer.get("text") if isinstance(answer, dict) and set(answer) == {"text"} else None
        text = unicodedata.normalize("NFKC", raw).strip() if isinstance(raw, str) else ""
        if not _AMOUNT.fullmatch(text) or Decimal(text) > 10000000:
            raise ContractError("CATALOG_PRICE_AMOUNT_INVALID",
                "请输入 0 到 10,000,000 之间的金额，最多两位小数，例如 89.90。", status=422)
        price = float(Decimal(text))
    return {"decision": "catalog_price_deferred" if price is None else "catalog_price_supplied",
            "run_id": run["id"], "question_id": question["id"], "draft_hash": context["draft_hash"],
            "sku_id": candidate["sku_id"], "currency": candidate["currency"], "price": price}


def read_catalog_price_receipt(engine, run, decision_id):
    question = engine.store.get("question", decision_id)
    if (question.get("run_id") != run["id"] or question.get("kind") != CATALOG_INTAKE_PRICE_KIND
            or question.get("status") != "ANSWERED" or not question.get("answer_event_ref")
            or not question.get("answer_request_id")):
        raise ContractError("CATALOG_PRICE_BINDING_INVALID", "缺少本次商品的正式价格答复。", status=409)
    expected = {**verify_catalog_price_answer(engine, run, question, question["answer"]),
                "answer_event_ref": question["answer_event_ref"]}
    if question.get("verified_human_receipt") != expected:
        raise ContractError("CATALOG_PRICE_BINDING_INVALID", "本次商品的价格答复无法核对。", status=409)
    return expected
