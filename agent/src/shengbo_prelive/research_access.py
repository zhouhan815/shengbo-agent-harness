"""Resolve a missing research observation through existing scoped query tools.

Discovery is not authorization: callers still read the exact formal artifact
and enforce its preparation, product, role and dependency boundaries. No user
message, historical pointer or model-authored reference grants a capability.
"""
from __future__ import annotations

from time import perf_counter

from pydantic import ValidationError

from shengbo_contracts.common import ArtifactRef, SCHEMA_VERSION, canonical_hash, same_artifact_ref

MAX_DISCOVERY_QUERIES = 6
MAX_PAGES_PER_PRODUCT = 3
PAGE_SIZE = 100


def _product_ref(value):
    try:
        reference = ArtifactRef.model_validate(value, strict=True)
    except (ValidationError, TypeError, ValueError):
        return None
    if reference.kind != "product" or reference.schema_version != SCHEMA_VERSION:
        return None
    # Products are RecordRef on the wire. Injecting ArtifactRef's default
    # schema_version changes their exact backend contract and is rejected.
    return dict(value)


def _frozen_product_refs(run):
    """Use server-owned frozen scope; observed refs authorize only new receipts."""
    frozen = run.get("goal") or {}
    references = [*frozen.get("action_target_refs", run.get("action_target_refs", [])),
                  *frozen.get("context_product_refs", run.get("context_product_refs", []))]
    created = set(run.get("created_product_ids", []))
    references.extend(ref for ref in run.get("observed_refs", [])
                      if isinstance(ref, dict) and ref.get("id") in created)
    result = {}
    for raw in references:
        reference = _product_ref(raw)
        if reference:
            result[canonical_hash(reference)] = reference
    return list(result.values())


def _evidence_revision(engine, run):
    # An intervening explicit preparation/research query can expose a new
    # legitimate candidate. Do not negatively cache across that observation.
    return canonical_hash([entry["id"] for entry in engine.store.list("query_evidence")
        if entry.get("run_id") == run["id"] and entry.get("scope") == run["scope"]
        and entry.get("key") in {"read_preparation", "find_audience_research"}])


def _candidate_matches(item, reference, product_refs):
    if (not isinstance(item, dict) or not same_artifact_ref(item.get("research_ref"), reference)
            or item.get("compatibility") != "exact"):
        return False
    report = item.get("report") or {}
    dependencies = [*item.get("product_refs", []), *report.get("dependencies", [])]
    report_products = [raw for raw in dependencies if isinstance(raw, dict) and raw.get("kind") == "product"]
    if not report_products or any(not any(same_artifact_ref(raw, permitted) for permitted in product_refs)
                                  for raw in report_products):
        return False
    return not (set(report.get("product_ids", [])) - {raw["id"] for raw in product_refs})


async def resolve_analysis_observation(engine, scope, run, reference) -> bool:
    """Bounded exact discovery only; does not accept or adopt a report.

    A positive result means the reference was found through a legitimate
    current-scope query. The original reader MUST still verify full artifact
    identity, preparation ownership and product dependencies before returning
    any content. A negative result remains distinct from actual permission
    denial, so the caller may return a correctable discovery error.
    """
    if scope.role != "main" or scope.phase != "execute" or scope.branch_task_id:
        return False
    try:
        requested = ArtifactRef.model_validate(reference, strict=True)
    except (ValidationError, TypeError, ValueError):
        return False
    if requested.kind != "analysis" or requested.schema_version != SCHEMA_VERSION or not run.get("goal"):
        return False
    product_refs = _frozen_product_refs(run)
    if not product_refs:
        return False
    started = perf_counter()
    preparation_id = run["scope"].get("preparation_id")
    cache_id = canonical_hash({"run_id": run["id"], "reference": requested.model_dump(mode="json"),
                               "product_refs": product_refs, "preparation_id": preparation_id})
    prior = engine.store.get("analysis_resolution", cache_id, required=False)
    evidence_revision = _evidence_revision(engine, run)
    if prior and prior.get("evidence_revision") == evidence_revision:
        engine._emit(scope.run_id, "analysis.reference_resolution", {
            "title": "研究精确引用检索", "status": "RESOLVED" if prior["resolved"] else "UNRESOLVED",
            "cache_hit": True, "query_count": 0, "duration_ms": round((perf_counter() - started) * 1000, 3),
            "reference_id": requested.id, "discovery_is_authorization": False}, scope)
        return bool(prior["resolved"])

    query_count, resolved, source = 0, False, None
    if preparation_id:
        query_count += 1
        preparation = await engine.business_query(scope, "read_preparation", {"preparation_id": preparation_id})
        # Only actual backend-owned preparation inputs, not an Agent preview.
        inputs = preparation.get("context_inputs") or {}
        same_prep = preparation.get("ref", {}).get("id") == preparation_id
        refs = [*(preparation.get("body") or {}).get("analysis_refs", []), *inputs.get("analysis_refs", [])]
        if same_prep and any(same_artifact_ref(value, reference) for value in refs):
            resolved, source = True, "current_preparation"

    if not resolved:
        for product_ref in product_refs:
            cursor = 0
            for _ in range(MAX_PAGES_PER_PRODUCT):
                if query_count >= MAX_DISCOVERY_QUERIES:
                    break
                query_count += 1
                result = await engine.business_query(scope, "find_audience_research", {
                    "ref": product_ref, "product_id": product_ref["id"],
                    "cursor": cursor, "limit": PAGE_SIZE})
                if any(_candidate_matches(item, reference, product_refs) for item in result.get("items", [])):
                    resolved, source = True, "exact_product_research_index"
                    break
                next_cursor = result.get("next_cursor")
                if not isinstance(next_cursor, int) or next_cursor <= cursor:
                    break
                cursor = next_cursor
            if resolved or query_count >= MAX_DISCOVERY_QUERIES:
                break

    engine.store.put("analysis_resolution", {"id": cache_id, "run_id": run["id"],
        "reference": requested.model_dump(mode="json"), "resolved": resolved,
        "evidence_revision": _evidence_revision(engine, run), "source": source})
    engine._emit(scope.run_id, "analysis.reference_resolution", {
        "title": "研究精确引用检索", "status": "RESOLVED" if resolved else "UNRESOLVED",
        "cache_hit": False, "query_count": query_count, "duration_ms": round((perf_counter() - started) * 1000, 3),
        "reference_id": requested.id, "resolution_source": source, "discovery_is_authorization": False}, scope)
    return resolved
