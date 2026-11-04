"""Assign selectable evidence IDs to facts from an exact product revision."""

from shengbo_contracts.common import canonical_hash, canonical_json


# Keep the legacy exact-quote validator and the evidence catalog on one policy.
FACT_FIELDS = frozenset({
    "material", "ingredients", "specifications", "usage_instructions",
    "storage_conditions", "shelf_life_value", "shelf_life_unit", "shipping_origin",
    "shipping_time", "return_policy", "target_audience", "knowledge", "knowledge_documents",
})


def build_evidence_catalog(product_ref, body):
    """Map IDs to original values. Labels help selection but are never saved as facts."""
    catalog = {}
    hidden = set(body.get("hidden_optional_fields") or [])
    version = [product_ref.get(key) for key in ("id", "revision", "content_hash")]

    def add(path, label, value):
        if isinstance(value, str):
            if not value.strip():
                return
            fact = value
        elif type(value) in (int, float):
            fact = canonical_json(value)
        else:
            return
        evidence_id = "F-" + canonical_hash([version, path, fact]).split(":", 1)[1][:16]
        catalog[evidence_id] = {"product_field": path, "label": label, "fact": fact}

    for field in sorted(FACT_FIELDS):
        group = "shelf_life" if field.startswith("shelf_life_") else field
        if group in hidden or (group == "shelf_life" and body.get("shelf_life_value") is None):
            continue
        value = body.get(field)
        if field == "specifications" and isinstance(value, list):
            for index, spec in enumerate(value):
                if isinstance(spec, dict):
                    add(f"/specifications/{index}/value", spec.get("name") or field, spec.get("value"))
        elif field == "knowledge_documents" and isinstance(value, list):
            for index, doc in enumerate(value):
                if isinstance(doc, dict):
                    add(f"/knowledge_documents/{index}/content", doc.get("title") or field, doc.get("content"))
        elif field not in {"specifications", "knowledge_documents"}:
            add(f"/{field}", field, value)
    return catalog
