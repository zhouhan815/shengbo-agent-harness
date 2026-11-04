"""Rebind catalog references only when the frozen material proves the submitted value."""
from collections import defaultdict
from decimal import Decimal
import re
import unicodedata

from .catalog_coverage import (
    _ATTRIBUTE, _PRODUCT_NAME, _SHARED_PREFIX, _SHARED_VARIANT_SENTENCE, _SKU_HEADER, _declared_currency,
    _evidence_location, _fulfillment_destination, _is_consumer_label, _norm, _parts, _scoped_currency_units,
    _source_faq, daily_price_evidence_unit, source_family_anchor,
)


def _literal(text):
    # Keep signs, units, negation, and qualifiers. Normalize typography only.
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).strip("。；;！？!?")


def rebind_product_evidence(products, prepared, by_location, *, max_quote_chars, coverage):
    units = prepared.get("source_units", [])
    if not units or not all(isinstance(item, dict) and isinstance(item.get("fields"), dict)
                            and all(isinstance(item["fields"].get(key), str) and item["fields"][key].strip()
                                    for key in ("sku_id", "name")) for item in products):
        return {}
    candidates = {item["fields"]["sku_id"]: item["fields"] for item in products}
    if len(candidates) != len(products):
        return {}
    variants = [unit for unit in units if unit.get("sku_id") and not unit.get("excluded_reason")
                and unit.get("column") in {"款式", "规格", "版本", "型号"}]
    family = source_family_anchor(prepared["locations"], units, variants, candidates=candidates)
    source_skus = defaultdict(set)
    shared_blocks = set()
    for unit in units:
        if unit.get("sku_id"):
            source_skus[_evidence_location(unit)[:-1]].add(unit["sku_id"])
        elif not unit.get("excluded_reason") and _SHARED_VARIANT_SENTENCE.match(unit["text"]):
            shared_blocks.add(unit["location"])
    currency_units = _scoped_currency_units(prepared["locations"], {unit["id"]: unit for unit in units}, candidates)
    currency_proof = [unit for unit in units if unit["id"] in currency_units and _declared_currency(unit["text"])
                      and len(unit["text"]) <= max_quote_chars
                      and unit["text"] in by_location[unit["location"]]["text"]]
    output = {}
    for sku, fields in candidates.items():
        scoped = []
        for unit in units:
            source = by_location.get(unit["location"])
            if (unit.get("excluded_reason") or not source or source.get("kind") == "image"
                    or source["source_ref"] != unit["source_ref"] or source["locator"] != unit["locator"]
                    or not 0 < len(unit["text"]) <= max_quote_chars or unit["text"] not in source["text"]):
                continue
            if unit.get("sku_id") == sku:
                scoped.append(unit)
                continue
            if unit.get("sku_id"):
                continue
            source_id = _evidence_location(unit)[:-1]
            if (any(_norm(other) in _norm(unit["text"]) for other in candidates if other and other != sku)
                    or any(_norm(variant["text"]) in _norm(unit["text"]) for variant in variants
                           if variant["sku_id"] != sku)):
                continue
            single = len(candidates) == 1 and source_skus[source_id] == {sku}
            explicitly_shared = (unit.get("fixed_scope") == "all_skus" and _SHARED_PREFIX.match(unit["text"])
                                 or unit["location"] in shared_blocks)
            family_source = family and by_location[family[1]]["source_ref"] == unit["source_ref"]
            if single or explicitly_shared and family_source and source_skus[source_id] == candidates.keys():
                scoped.append(unit)
        proof = _proven_fields(fields, scoped, coverage if isinstance(coverage, dict) else {})
        if len(currency_proof) == 1 and "currency" not in proof:
            proof["currency"] = currency_proof
        if (family and "name" not in proof and len(family[2]) <= max_quote_chars
                and family[2] in by_location[family[1]]["text"]):
            options = [unit for unit in scoped if unit in variants and unit.get("sku_id") == sku
                       and _literal(f'{family[0]} {unit["text"]}') == _literal(fields["name"])]
            if len(options) == 1:
                proof["name"] = [{"location": family[1], "text": family[2]}, *options]
        output[sku] = proof
    return output


def _proven_fields(fields, scoped, coverage):
    from .catalog_extract import _explicit_currencies, _price_amounts

    sku = fields["sku_id"]
    proof = {}

    def unique(field, matches):
        if len(matches) == 1:
            proof[field] = matches

    unique("sku_id", [unit for unit in scoped if unit.get("sku_id") == sku and unit["text"] == sku
                      and _SKU_HEADER.fullmatch(unit.get("column", ""))])
    name = fields.get("name")
    if isinstance(name, str):
        unique("name", [unit for unit in scoped if
                       unit.get("sku_id") == sku and unit.get("column") in {"名称", "商品名称", "产品名称", "品名", "款式", "版本", "型号"}
                       and _literal(unit["text"]) == _literal(name) or
                       (match := _PRODUCT_NAME.fullmatch(unit["text"])) and _literal(match[1]) == _literal(name)])
    price = fields.get("price")
    price_units = [unit for unit in scoped if unit.get("sku_id") == sku
                   and daily_price_evidence_unit(unit, allow_value_only=False)]
    if (len(price_units) == 1 and type(price) in {int, float} and 0 <= price <= 10000000
            and _price_amounts(price_units[0]["text"]) == {Decimal(str(price))}):
        proof["price"] = price_units
        if isinstance(fields.get("currency"), str) and _explicit_currencies(price_units[0]["text"]) == {fields["currency"]}:
            proof["currency"] = price_units

    # Match every part of a field, so evidence for one correct clause cannot hide an added unsupported claim.
    specifications = fields.get("specifications")
    if isinstance(specifications, list) and specifications:
        matches = []
        for spec in specifications:
            if not isinstance(spec, dict) or not all(isinstance(spec.get(key), str) for key in ("name", "value")):
                break
            label, value = _literal(spec["name"]), _literal(spec["value"])
            options = [unit for unit in scoped if _spec_pair(unit) == (label, value)]
            if len(options) != 1:
                break
            matches.extend(options)
        else:
            proof["specifications"] = matches

    for field in ("shipping_origin", "shipping_time", "faq"):
        value = fields.get(field)
        if not isinstance(value, str) or not value:
            continue
        if field == "faq":
            pair = _source_faq(value.replace("\n", ""))
            if pair:
                unique(field, [unit for unit in scoped if (original := _source_faq(unit["text"]))
                               and tuple(map(_literal, pair)) == tuple(map(_literal, original))])
        else:
            unique(field, [unit for unit in scoped if
                           (destination := _fulfillment_destination(unit["text"], {field}))
                           and _literal(destination[1]) == _literal(value)])
    for field in ("usage_instructions", "return_policy", "target_audience", "material", "core_selling_points"):
        value = fields.get(field)
        if not isinstance(value, str) or not value:
            continue
        parts = [_literal(part) for part in _parts(value)]
        options = [unit for unit in scoped if coverage.get(unit["id"]) in (f"{sku}|{field}", f"*|{field}")]
        if parts and parts == [_literal(unit["text"]) for unit in options]:
            proof[field] = options
    return proof


def _spec_pair(unit):
    label = unit.get("column", "")
    if label and _is_consumer_label(label):
        return _literal(label), _literal(unit["text"])
    match = _ATTRIBUTE.fullmatch(_SHARED_PREFIX.sub("", unit["text"]))
    if match and _is_consumer_label(match[1]):
        return tuple(map(_literal, match.groups()))
    return None


def repair_product_links(links, proof, by_location, *, max_links, known_skus, sku, shared_scene):
    """Replace weak text references; image associations keep their separate validator."""
    replace = {}
    for field, units in proof.items():
        field_links = [link for link in links if field in link["fields"]]
        if any(by_location.get(link["location"], {}).get("kind") == "image" for link in field_links):
            continue
        if field == "sku_id" and any(isinstance(link.get("quote"), str) and any(
                re.search(r"(?<![\w-])" + re.escape(other) + r"(?![\w-])", link["quote"])
                for other in known_skus - {sku}) for link in field_links):
            continue  # Treat an explicit opposing identity as a genuine conflict.
        if field_links and all(any(link["location"] == unit["location"]
                               and isinstance(quote := link.get("quote", by_location[unit["location"]]["text"]), str)
                               and unit["text"] in quote and quote in by_location[unit["location"]]["text"]
                               for unit in units) for link in field_links):
            continue
        replace[field] = units
    if not replace:
        return links
    repaired = []
    for link in links:
        fields = [field for field in link["fields"] if field not in replace]
        if (shared_scene and link["location"] == shared_scene["location"]
                and not any(field in link["fields"] for field in ("sku_id", "price", "currency"))
                and isinstance(quote := link.get("quote", by_location[link["location"]]["text"]), str)
                and shared_scene["text"] in quote and quote in by_location[link["location"]]["text"]):
            fields = link["fields"]  # This validated shared quote also supports the existing scene reconciliation.
        if fields:
            repaired.append({**link, "fields": fields})
    grouped = defaultdict(list)
    for field, units in replace.items():
        for unit in units:
            key = unit["location"], unit["text"]
            if field not in grouped[key]:
                grouped[key].append(field)
    repaired.extend({"location": location, "quote": text, "fields": fields}
                    for (location, text), fields in grouped.items())
    return repaired if len(repaired) <= max_links else links
