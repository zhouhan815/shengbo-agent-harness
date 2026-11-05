"""Validate ordinary catalog facts without a model-authored provenance ledger."""
from collections import defaultdict
from decimal import Decimal
import math
import re
import unicodedata

from jsonschema import Draft202012Validator

from .catalog_coverage import (_PRICE_HEADER, _SKU_HEADER, _is_consumer_label, _matches, _norm,
                               daily_price_evidence_unit, source_family_anchor)


_CURRENCY_DECLARATION = re.compile(r"^\s*(?:商品|产品|计价|标价|结算)?\s*(?:币种|货币)")
_PROMOTION_PRICE_COLUMN = re.compile(r"(?:直播(?:间)?|本场|活动|促销|券后|秒杀)(?:活动)?(?:到手|售)?价")
_EXPLICIT_PROSE_SKU = re.compile(r"^(?:SKU(?:编号|编码)?|商品(?:编号|编码))\s*[：:]\s*([A-Za-z0-9][\w-]{0,79})$", re.I)
_SKU_PLACEHOLDER = re.compile(r"^(?:sku|n/?a|unknown|null|none|无|未知|暂无|未提供|未填写|待确认|待补充|[-—]+)$", re.I)
_NAME_COLUMNS = {"商品名称", "产品名称", "商品名", "产品名", "品名"}
_VARIANT_COLUMNS = {"款式", "规格", "版本", "型号"}


def _sku_in(text, sku):
    text, sku = (unicodedata.normalize("NFKC", value) for value in (text, sku))
    return bool(re.search(r"(?<![\w-])" + re.escape(sku) + r"(?![\w-])", text, re.ASCII | re.I))


def _sku_key(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _source_product_units(prepared):
    units = prepared.get("source_units", [])
    daily_locations = {unit["location"] for unit in units if _PRICE_HEADER.search(unit.get("column", ""))}
    promotion_only = {unit["location"] for unit in units
                      if _PROMOTION_PRICE_COLUMN.search(unit.get("column", ""))} - daily_locations
    return [unit for unit in units if not unit.get("excluded_reason") and unit["location"] not in promotion_only]


def _product_schema(product, excluded):
    properties = {key: value for key, value in product["properties"].items() if key not in excluded}
    return properties, {key: Draft202012Validator({**value, "$defs": product.get("$defs", {})})
                        for key, value in properties.items()}


def known_catalog_skus(prepared):
    units = _source_product_units(prepared)
    skus = {unit["sku_id"] for unit in units if unit.get("sku_id") and _SKU_HEADER.fullmatch(unit.get("column", ""))}
    skus.update(match[1] for unit in units if (match := _EXPLICIT_PROSE_SKU.fullmatch(unit["text"])))
    return {_sku_key(sku): sku for sku in sorted(skus)}


def facts_only_catalog_report(args, prepared, *, allow_partial=False):
    from .catalog_extract import (_EXCLUDED_FIELDS, _MAX_PRODUCTS, _explicit_currencies, _price_amounts, _reject)

    if (not isinstance(args, dict) or not isinstance(products := args.get("products"), list)
            or len(products) > _MAX_PRODUCTS):
        _reject("提交可用的商品事实列表，无法安全确认的问题单独说明。", major_extraction_failure=True)
    questions = args.get("questions")

    units = _source_product_units(prepared)
    canonical_skus = known_catalog_skus(prepared)
    known_skus = set(canonical_skus.values())
    source_texts = [unit["text"] for unit in units] + [location["text"] for location in prepared.get("locations", [])
                    if location.get("kind") == "image"]
    if "source_units" not in prepared:
        source_texts.extend(location["text"] for location in prepared.get("locations", []))
    variants = [unit for unit in units if unit.get("sku_id") and unit.get("column") in _VARIANT_COLUMNS]
    family = source_family_anchor(prepared.get("locations", []), units, variants)
    properties, validators = _product_schema(prepared["product_schema"], _EXCLUDED_FIELDS | {"sale_status"})
    diagnostics = []
    by_sku = defaultdict(list)
    unstructured_skus = {}
    for item in products:
        row = item.get("fields", item) if isinstance(item, dict) else {}
        sku = row.get("sku_id") if isinstance(row, dict) else None
        if not isinstance(sku, str) or not sku.strip():
            diagnostics.append({"reason": "missing_sku"})
            continue
        sku = sku.strip()
        canonical = canonical_skus.get(_sku_key(sku))
        seen_in_source = any(_sku_in(text, sku) for text in source_texts)
        if (known_skus and canonical is None or _SKU_PLACEHOLDER.fullmatch(sku)
                or not seen_in_source and not re.fullmatch(r"[\w][\w./-]{0,79}", sku)):
            diagnostics.append({"sku_id": sku, "reason": "sku_not_in_source"})
            continue
        sku = canonical or unstructured_skus.setdefault(_sku_key(sku), sku)
        by_sku[sku].append({**row, "sku_id": sku})

    rows = []
    for sku, candidates in by_sku.items():
        if len(candidates) > 1 and any(row != candidates[0] for row in candidates[1:]):
            diagnostics.append({"sku_id": sku, "reason": "conflicting_duplicate_sku"})
            continue
        raw = {**candidates[0], "sku_id": sku}
        row = {}
        for key, value in raw.items():
            if key not in properties or value in (None, "", [], {}):
                continue
            if (key == "price" and (type(value) not in {int, float} or isinstance(value, float) and not math.isfinite(value))
                    or not validators[key].is_valid(value)):
                diagnostics.append({"sku_id": sku, "field": key, "reason": "invalid_field_shape"})
                continue
            row[key] = value.strip() if isinstance(value, str) else value
        if not row.get("name") or not row.get("sku_id"):
            diagnostics.append({"sku_id": sku, "reason": "missing_product_name"})
            continue
        direct_units = [unit for unit in units if unit.get("sku_id") and _sku_key(unit["sku_id"]) == _sku_key(sku)]
        explicit_names = {unit["text"] for unit in direct_units if unit.get("column") in _NAME_COLUMNS}
        labels = {unit["text"] for unit in variants if unit["sku_id"] == sku}
        if len(explicit_names) > 1:
            diagnostics.append({"sku_id": sku, "reason": "conflicting_product_names"})
            continue
        if len(explicit_names) == 1:
            exact, = explicit_names
            if _norm(row["name"]) != _norm(exact):
                row["name"] = exact
                diagnostics.append({"sku_id": sku, "field": "name", "reason": "verified_source_name_used"})
        elif family and len(labels) == 1:
            label, = labels
            expected = f"{family[0]} {label}"
            opposing = {_norm(unit["text"]) for unit in variants if unit["sku_id"] != sku}
            normalized = _norm(row["name"])
            if (normalized in {_norm(label), _norm(family[0])} or normalized in
                    {_norm(family[0]) + other for other in opposing}):
                row["name"] = expected
                diagnostics.append({"sku_id": sku, "field": "name", "reason": "verified_source_name_used"})
        if not validators["name"].is_valid(row["name"]):
            diagnostics.append({"sku_id": sku, "reason": "invalid_product_name"})
            continue

        own_refs = {unit["source_ref"]["id"] for unit in direct_units}
        if not own_refs:
            own_refs = {unit["source_ref"]["id"] for unit in units if _sku_in(unit["text"], sku)}
        local = [unit for unit in units if unit["source_ref"]["id"] in own_refs and not unit.get("sku_id")]
        single = len(known_skus or by_sku) == 1
        price_candidates = [unit for unit in [*direct_units, *local]
                            if (unit.get("sku_id") == sku or single or _sku_in(unit["text"], sku))
                            and daily_price_evidence_unit(unit, allow_value_only=False)]
        explicit_missing_price = any(unit.get("excluded_reason") == "missing_daily_price"
                                     and unit.get("sku_id") in (None, sku) for unit in prepared.get("source_units", []))
        if not price_candidates and single and not explicit_missing_price:
            price_candidates = [unit for unit in local if daily_price_evidence_unit(unit, allow_value_only=True)]
        amounts = set()
        for unit in price_candidates:
            text = unit["text"].replace(sku, "")
            if (len(found := _price_amounts(text)) == 1 and not re.search(r"[-−]\s*\d", text)
                    and all(0 <= amount <= 10000000 for amount in found)):
                amounts.update(found)
        if len(amounts) == 1:
            amount, = amounts
            if "price" in row and Decimal(str(row["price"])) != amount:
                diagnostics.append({"sku_id": sku, "field": "price", "reason": "verified_source_price_used"})
            row["price"] = float(amount)
        elif len(amounts) > 1 or explicit_missing_price or _only_matches_promotion(row, sku, prepared, _price_amounts):
            row.pop("price", None)
            diagnostics.append({"sku_id": sku, "field": "price", "reason": "price_unresolved"})

        currencies = set().union(*(_explicit_currencies(unit["text"]) for unit in price_candidates))
        for unit in local:
            currencies.update(code for code in _explicit_currencies(unit["text"])
                              if _CURRENCY_DECLARATION.match(unit["text"])
                              or _matches({"currency": code}, "currency", unit))
        if len(currencies) == 1:
            code, = currencies
            if "currency" in row and row["currency"] != code:
                diagnostics.append({"sku_id": sku, "field": "currency", "reason": "verified_source_currency_used"})
            row["currency"] = code
        elif len(currencies) > 1:
            row.pop("currency", None)
            diagnostics.append({"sku_id": sku, "field": "currency", "reason": "currency_unresolved"})

        _preserve_scoped_specifications(row, direct_units, diagnostics)
        rows.append(row)

    missing_skus = known_skus - {row["sku_id"] for row in rows}
    meaningful, unretained = len(known_skus) or len(products), len(missing_skus) if known_skus else len(products)
    severe = 0 < len(known_skus) <= _MAX_PRODUCTS and len(missing_skus) * 10 >= len(known_skus) * 9
    if rows and not severe:
        measured, missing = _structured_fact_gap(rows, units)
        if measured >= 10 and missing * 10 >= measured * 9:
            meaningful, unretained, severe = measured, missing, True
    if not rows or severe and not allow_partial:
        _reject("从原文识别出的商品大多未能整理出可用结果。", major_extraction_failure=True,
                meaningful_units=meaningful, unretained_units=unretained)
    if severe:
        diagnostics.append({"reason": "partial_facts_retained", "meaningful_units": meaningful,
                            "unretained_units": unretained})
    diagnostics.extend({"sku_id": sku, "reason": "source_sku_omitted"} for sku in sorted(missing_skus))
    if questions:
        diagnostics.append({"reason": "model_questions_omitted", "count": len(questions) if isinstance(questions, list) else 1})
    report = {"summary": f"已整理 {len(rows)} 款商品", "fields": {"products": rows}, "evidence": [],
              "source_refs": prepared["sources"], "conflicts": []}
    return report, diagnostics


def _only_matches_promotion(row, sku, prepared, amount_parser):
    if "price" not in row:
        return False
    from .document_import import _session_table_cells

    amount = Decimal(str(row["price"]))
    locations = {item["location"]: item for item in prepared.get("locations", [])}
    units = [*prepared.get("source_units", []), *_session_table_cells(locations)]
    return any(unit.get("sku_id") and _sku_key(unit["sku_id"]) == _sku_key(sku)
               and _PROMOTION_PRICE_COLUMN.search(unit.get("column", ""))
               and amount_parser(unit["text"]) == {amount} for unit in units)


def _structured_fact_gap(rows, units):
    """Count only the distinct, explicitly labelled cells of confirmed SKU axes."""
    by_sku = {row["sku_id"]: row for row in rows}
    structured = {(unit["sku_id"], unit["column"], unit["text"]): unit for unit in units
                  if unit.get("sku_id") and unit.get("column") and _is_consumer_label(unit["column"])}
    supported = sum(any(_matches(by_sku.get(sku, {}), field, unit) for field in (
        "specifications", "knowledge", "knowledge_documents", "material", "ingredients", "usage_instructions",
        "return_policy", "shipping_origin", "shipping_time", "faq", "target_audience", "storage_conditions"))
        for (sku, _, _), unit in structured.items())
    return len(structured), len(structured) - supported


def _preserve_scoped_specifications(row, units, diagnostics):
    if not isinstance(row.get("specifications"), list):
        return
    values = defaultdict(set)
    for unit in units:
        if unit.get("column"):
            values[_norm(unit["column"])].add(unit["text"])
    specifications = []
    for spec in row["specifications"]:
        if (not isinstance(spec, dict) or not all(isinstance(spec.get(key), str) for key in ("name", "value"))
                or _PROMOTION_PRICE_COLUMN.search(spec["name"])):
            continue
        options = values[_norm(spec["name"])]
        if len(options) > 1:
            diagnostics.append({"sku_id": row["sku_id"], "field": "specifications", "reason": "source_spec_ambiguous"})
            continue
        if len(options) == 1:
            exact, = options
            if _norm(spec["value"]) != _norm(exact) and len(exact) <= 500:
                spec = {**spec, "value": exact}
                diagnostics.append({"sku_id": row["sku_id"], "field": "specifications", "reason": "verified_source_spec_used"})
        specifications.append(spec)
    row["specifications"] = specifications
