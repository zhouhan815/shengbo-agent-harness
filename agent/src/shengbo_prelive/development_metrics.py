"""Best-effort developer measurements, never a budget or usage authority.

Only derived scalars, counters, hashes and exact trace references are persisted.
The caller supplies the provider's already-safe public on_request projection.
No original prompt, tool argument, image, credential or reasoning is written.
"""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import json
from pathlib import Path
import re
import time

from shengbo_contracts.common import canonical_hash, canonical_json
from .observability import sanitize


SCHEMA_VERSION = "development-request-metrics-v1"
M3_FIELDS = (
    "phase", "role", "original_user_request", "scope", "room_id", "goal", "selection",
    "selection_binding", "observed_versions", "allowed_effects", "task_assessment",
    "material_refs", "observed_refs", "business_receipts", "receipt_count", "branches",
    "workflow_progress", "decisions", "pending_questions", "read_skills", "retrieved_documents",
    "unknown_side_effects", "inherited_context", "constraints", "observed_business_workset",
    "tool_presentation",
)
_PRIVATE_TYPES = {"reasoning", "reasoning_summary", "reasoning_text", "thinking",
    "redacted_thinking", "private_reasoning"}
_IMAGE_TYPES = {"input_image", "image", "image_url"}
_M3_OPEN, _M3_CLOSE = "<shengbo_trusted_context>", "</shengbo_trusted_context>"
_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


@lru_cache(maxsize=1)
def _encoding():
    import tiktoken
    return tiktoken.get_encoding("o200k_base")


def _counter(text):
    return len(_encoding().encode(text, disallowed_special=()))


def _project(value, omitted):
    """Exclude images/private replay from textual estimation, not native wire."""
    if isinstance(value, dict):
        kind = value.get("type")
        if kind in _PRIVATE_TYPES:
            omitted["private_items"] += 1
            return None
        if kind in _IMAGE_TYPES:
            omitted["image_items"] += 1
            return None
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in {"encryptedcontent", "reasoningcontent", "chainofthought", "thinking"}:
                omitted["private_fields"] += 1
            elif key in {"image", "image_url"}:
                omitted["image_fields"] += 1
            else:
                part = _project(item, omitted)
                if part is not None or item is None:
                    result[key] = part
        return result
    if isinstance(value, list):
        return [part for item in value if (part := _project(item, omitted)) is not None or item is None]
    return value


def _message_text(item):
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part["text"] for part in content if isinstance(part, dict)
            and part.get("type") in {"text", "input_text", "output_text"} and isinstance(part.get("text"), str))
    return ""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def measure_public_request(payload, *, counter=None):
    """Measure only safe public text. Counter failure is missing, never chars."""
    started = time.perf_counter()
    cpu_started = time.thread_time()
    result = {"schema_version": SCHEMA_VERSION, "development_only": True,
        "ui_visibility": "dev_only", "measurement_kind": "public_serialized_token_estimate",
        "not_a_provider_usage_ledger": True, "does_not_change_budget_or_control": True,
        "tokenizer": "o200k_base" if counter is None else "injected_counter",
        "status": "measured", "reason_code": None, "public_payload_hash": None,
        "components": {}, "trusted_context_fields": {}, "excluded_counts": {},
        "components_are_independent_not_additive": True,
        "history_includes_tool_results": True, "measurement_elapsed_excludes_file_write": True}
    try:
        if not isinstance(payload, dict):
            raise ValueError("invalid")
        safe = sanitize(payload)
        result["public_payload_hash"] = canonical_hash(safe)
        omitted = Counter()
        projected = _project(safe, omitted)
        result["excluded_counts"] = dict(omitted)
        count = counter or _counter
        def segment(value, missing="missing"):
            if value is None:
                return {"estimated_tokens": None, "status": missing}
            try:
                value = count(canonical_json(value))
                if type(value) is not int or value < 0:
                    raise ValueError("invalid counter")
                return {"estimated_tokens": value, "status": "measured"}
            except Exception:
                result.update(status="partial", reason_code="COUNTER_FAILED")
                return {"estimated_tokens": None, "status": "counter_failed"}
        items = projected.get("input")
        items = items if isinstance(items, list) else None
        developer = [i for i in items if isinstance(i, dict) and i.get("role") == "developer"] if items is not None else None
        history = [i for i in items if not isinstance(i, dict) or i.get("role") != "developer"] if items is not None else None
        tool_results = [i for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"] if items is not None else None
        result["components"] = {
            "public_serialized_text_payload": segment(projected),
            "tool_schemas": segment(projected.get("tools") if isinstance(projected.get("tools"), list) else None),
            "developer_messages": segment(developer if developer else None),
            "history_items": segment(history), "tool_result_items": segment(tool_results),
        }
        result["item_counts"] = {"input": len(items) if items is not None else None,
            "developer": len(developer) if developer is not None else None,
            "history": len(history) if history is not None else None,
            "tool_results": len(tool_results) if tool_results is not None else None,
            "tools": len(projected["tools"]) if isinstance(projected.get("tools"), list) else None}
        text = "\n".join(_message_text(i) for i in developer or [])
        m3 = None
        if text.count(_M3_OPEN) != 1 or text.count(_M3_CLOSE) != 1:
            m3_status = "missing" if _M3_OPEN not in text and _M3_CLOSE not in text else "ambiguous"
        else:
            try:
                opening = text.index(_M3_OPEN) + len(_M3_OPEN)
                closing = text.index(_M3_CLOSE)
                if closing < opening:
                    raise ValueError("order")
                m3 = json.loads(text[opening:closing], object_pairs_hook=_pairs)
                if not isinstance(m3, dict):
                    raise ValueError("object")
                m3_status = "unique_parsed_developer_tag"
            except Exception:
                m3, m3_status = None, "malformed_or_duplicate_json_keys"
        result["trusted_context_status"] = m3_status
        result["components"]["trusted_context"] = segment(m3, m3_status)
        result["trusted_context_fields"] = {key: segment({key: m3[key]}) if m3 is not None and key in m3
            else {"estimated_tokens": None, "status": "field_absent" if m3 is not None else m3_status}
            for key in M3_FIELDS}
        if omitted:
            result["text_projection_coverage"] = "images_or_private_replay_excluded_not_native_wire_budget"
        else:
            result["text_projection_coverage"] = "public_text_projection_only_not_native_wire_budget"
    except Exception:
        result.update(status="unavailable", reason_code="PUBLIC_MEASUREMENT_FAILED")
    result["measurement_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    result["measurement_cpu_ms"] = round((time.thread_time() - cpu_started) * 1000, 3)
    result["measurement_cpu_clock"] = "thread_time"
    return result


def _identifier(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) and sanitize(value) == value else None


def _request_ref(value):
    if isinstance(value, str):
        return _identifier(value)
    if not isinstance(value, dict) or set(value) - {"kind", "id", "revision", "content_hash", "schema_version"}:
        return None
    if (not _identifier(value.get("kind")) or not _identifier(value.get("id"))
        or type(value.get("revision")) is not int or value["revision"] < 1
        or not isinstance(value.get("content_hash"), str) or not _HASH.fullmatch(value["content_hash"])):
        return None
    if "schema_version" in value and not _identifier(value["schema_version"]):
        return None
    return dict(value)


def record_request_metrics(root, event, *, run_id, role, branch_task_id=None,
        invocation_id=None, ledger_attempt_id=None, request_ref=None, counter=None):
    """Exclusive immutable diagnostics; all failures are non-authoritative."""
    try:
        if not _identifier(run_id) or role not in {"main", "product_facts", "audience_scenes", "session_config"}:
            return {"recorded": False, "status": "unavailable", "reason_code": "METADATA_INVALID"}
        metrics = measure_public_request(event.get("payload") if isinstance(event, dict) else None, counter=counter)
        if metrics["status"] == "unavailable" or not metrics["public_payload_hash"]:
            return {"recorded": False, "status": "unavailable", "reason_code": "PUBLIC_MEASUREMENT_FAILED"}
        identity = {"run_id": run_id, "role": role, "branch_task_id": _identifier(branch_task_id),
            "invocation_id": _identifier(invocation_id), "ledger_attempt_id": _identifier(ledger_attempt_id),
            "provider_attempt_id": _identifier(event.get("attempt_id")), "request_ref": _request_ref(request_ref),
            "public_payload_hash": metrics["public_payload_hash"]}
        identity_hash = canonical_hash(identity)
        base = Path(root).resolve()
        folder = base / "development_diagnostics/requests"
        folder.mkdir(parents=True, exist_ok=True)
        if not folder.resolve().is_relative_to(base):
            raise ValueError("outside")
        path = folder / (identity_hash[7:] + ".json")
        document = {**metrics, **identity, "input_identity_hash": identity_hash,
            "metadata_missing": [key for key, value in identity.items() if value is None]}
        document["content_hash"] = canonical_hash(document)
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(canonical_json(document))
            status = "recorded_partial" if metrics["status"] == "partial" else "recorded"
        except FileExistsError:
            existing = json.loads(path.read_text(encoding="utf-8"))
            expected = existing.get("content_hash")
            if (existing.get("schema_version") != SCHEMA_VERSION or existing.get("input_identity_hash") != identity_hash
                or expected != canonical_hash({k: v for k, v in existing.items() if k != "content_hash"})):
                return {"recorded": False, "status": "unavailable", "reason_code": "EXISTING_METRIC_INVALID"}
            document, status = existing, "idempotent_existing"
        return {"recorded": True, "status": status, "reason_code": document.get("reason_code"),
            "metric_ref": {"kind": "development_request_metric", "id": identity_hash,
                "revision": 1, "content_hash": document["content_hash"]},
            "relative_path": "development_diagnostics/requests/" + path.name}
    except Exception:
        return {"recorded": False, "status": "unavailable", "reason_code": "DEVELOPMENT_METRIC_WRITE_FAILED"}
