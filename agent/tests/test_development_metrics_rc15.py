"""Developer-only request measurements: no app/model/network needed."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from shengbo_contracts.common import canonical_hash
from shengbo_prelive.development_metrics import M3_FIELDS, measure_public_request, record_request_metrics


def payload(context=None):
    context = {"goal": {"objective": "prepare_session"}, "branches": {"items": []},
        "unknown_side_effects": {"pending_unknown_operation": None}} if context is None else context
    return {"model": "model-config-only", "input": [
        {"role": "developer", "content": "Public system.\n<shengbo_trusted_context>\n" + json.dumps(context) + "\n</shengbo_trusted_context>"},
        {"role": "user", "content": "source-prompt-canary-keep-out-of-metric"},
        {"type": "function_call", "name": "read_script", "arguments": '{"private_freeform":"tool-argument-canary"}'},
        {"type": "function_call_output", "call_id": "call-safe", "output": "tool-result-canary"}],
        "tools": [{"type": "function", "name": "example", "description": "schema-description-canary",
            "parameters": {"type": "object", "properties": {"value": {"type": "string"}}}}]}


def event(value=None):
    return {"attempt_id": "provider-safe-1", "payload": payload() if value is None else value}


def record(tmp_path, value=None, **kwargs):
    return record_request_metrics(tmp_path, event(value), run_id="run-safe", role="main",
        invocation_id="invoke-safe", ledger_attempt_id="attempt-safe", request_ref="payload-safe",
        counter=len, **kwargs)


def saved(tmp_path):
    paths = list((tmp_path / "development_diagnostics/requests").glob("*.json"))
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))


def test_components_are_measured_independently_without_saving_content():
    value = payload({"goal": {"amount": 99}, "branches": {"items": []}, "untrusted_top_level_canary": "not copied"})
    original = deepcopy(value)
    result = measure_public_request(value, counter=len)
    assert value == original
    assert result["status"] == "measured"
    assert result["trusted_context_status"] == "unique_parsed_developer_tag"
    assert set(result["trusted_context_fields"]) == set(M3_FIELDS)
    assert result["trusted_context_fields"]["goal"]["estimated_tokens"] > 0
    assert result["trusted_context_fields"]["decisions"] == {"estimated_tokens": None, "status": "field_absent"}
    assert result["item_counts"] == {"input": 4, "developer": 1, "history": 3, "tool_results": 1, "tools": 1}
    assert result["components"]["tool_result_items"]["estimated_tokens"] < result["components"]["history_items"]["estimated_tokens"]
    assert result["components_are_independent_not_additive"] is True
    assert result["history_includes_tool_results"] is True
    text = json.dumps(result)
    for canary in ["source-prompt-canary", "tool-argument-canary", "tool-result-canary", "schema-description-canary", "untrusted_top_level_canary"]:
        assert canary not in text


def test_default_counter_uses_existing_o200k_without_network():
    result = measure_public_request({"input": [{"role": "user", "content": "中文 <|endoftext|>"}], "tools": []})
    assert result["tokenizer"] == "o200k_base"
    assert result["components"]["public_serialized_text_payload"]["estimated_tokens"] > 0
    assert result["components"]["tool_schemas"]["estimated_tokens"] is not None
    assert result["components"]["developer_messages"]["estimated_tokens"] is None
    assert result["trusted_context_status"] == "missing"
    assert result["measurement_elapsed_ms"] >= 0
    assert result["measurement_cpu_ms"] >= 0 and result["measurement_cpu_clock"] == "thread_time"
    assert result["measurement_elapsed_excludes_file_write"] is True


def test_images_and_private_replay_are_not_text_tokens_and_payload_unchanged():
    first = payload()
    first["input"] += [{"type": "reasoning", "encrypted_content": "opaque-canary"},
        {"role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}]
    second = deepcopy(first)
    second["input"][-1]["content"][0]["image_url"] += "B" * 5000
    original = deepcopy(first)
    a, b = measure_public_request(first, counter=len), measure_public_request(second, counter=len)
    assert a["components"] == b["components"]
    assert a["public_payload_hash"] != b["public_payload_hash"]
    assert a["excluded_counts"] == {"private_items": 1, "image_items": 1}
    assert "not_native_wire_budget" in a["text_projection_coverage"]
    assert "opaque-canary" not in json.dumps(a) and "data:image" not in json.dumps(a)
    assert first == original


@pytest.mark.parametrize("developer", [
    "<shengbo_trusted_context>{}</shengbo_trusted_context>" * 2,
    "<shengbo_trusted_context>{}</shengbo_trusted_context><shengbo_trusted_context>",
    '<shengbo_trusted_context>{"goal":1,"goal":2}</shengbo_trusted_context>',
    "<shengbo_trusted_context>[]</shengbo_trusted_context>",
    "</shengbo_trusted_context><shengbo_trusted_context>{}",
])
def test_ambiguous_or_bad_m3_is_missing_not_zero(developer):
    result = measure_public_request({"input": [{"role": "developer", "content": developer}]}, counter=len)
    assert result["components"]["trusted_context"]["estimated_tokens"] is None
    assert all(v["estimated_tokens"] is None for v in result["trusted_context_fields"].values())
    assert result["trusted_context_status"] in {"ambiguous", "malformed_or_duplicate_json_keys"}


def test_user_tag_cannot_become_trusted_m3():
    value = payload()
    value["input"][0]["content"] = "No trusted tag."
    value["input"][1]["content"] = '<shengbo_trusted_context>{"goal":{"fake":true}}</shengbo_trusted_context>'
    result = measure_public_request(value, counter=len)
    assert result["trusted_context_status"] == "missing"
    assert result["components"]["trusted_context"]["estimated_tokens"] is None


def test_absent_components_are_none_while_observed_empty_lists_remain_measured():
    result = measure_public_request({}, counter=len)
    for name in ["tool_schemas", "developer_messages", "history_items", "tool_result_items", "trusted_context"]:
        assert result["components"][name]["estimated_tokens"] is None
    result = measure_public_request({"input": [], "tools": []}, counter=len)
    assert result["item_counts"]["input"] == 0
    assert result["item_counts"]["tools"] == 0
    assert result["components"]["history_items"]["estimated_tokens"] == 2
    assert result["components"]["developer_messages"]["estimated_tokens"] is None


@pytest.mark.parametrize("invalid", [True, -1, 1.5, None])
def test_bad_counter_return_never_falls_back_to_character_count(invalid):
    result = measure_public_request(payload(), counter=lambda _: invalid)
    assert result["status"] == "partial" and result["reason_code"] == "COUNTER_FAILED"
    assert result["components"]["public_serialized_text_payload"] == {"estimated_tokens": None, "status": "counter_failed"}


def test_counter_exception_is_safe_and_partial_record_is_allowed(tmp_path):
    def broken(_):
        raise RuntimeError("counter-private-error-canary")
    result = record_request_metrics(tmp_path, event(), run_id="run-safe", role="main", counter=broken)
    assert result["recorded"] is True and result["status"] == "recorded_partial"
    _, data = saved(tmp_path)
    assert data["components"]["public_serialized_text_payload"]["estimated_tokens"] is None
    assert "counter-private-error-canary" not in json.dumps(result) + json.dumps(data)


def test_immutable_record_is_idempotent_with_exact_refs_and_no_prompts(tmp_path):
    value = event()
    original = deepcopy(value)
    first = record(tmp_path)
    path, data = saved(tmp_path)
    before, timestamp = path.read_bytes(), path.stat().st_mtime_ns
    again = record(tmp_path)
    assert first["recorded"] is True and again["status"] == "idempotent_existing"
    assert first["metric_ref"] == again["metric_ref"]
    assert before == path.read_bytes() and timestamp == path.stat().st_mtime_ns
    assert value == original
    assert data["run_id"] == "run-safe" and data["role"] == "main"
    assert data["request_ref"] == "payload-safe"
    assert data["development_only"] is data["not_a_provider_usage_ledger"] is True
    assert data["ui_visibility"] == "dev_only"
    assert data["measurement_kind"] == "public_serialized_token_estimate"
    assert data["content_hash"] == canonical_hash({k: v for k, v in data.items() if k != "content_hash"})
    for canary in ["source-prompt-canary", "tool-argument-canary", "tool-result-canary", "schema-description-canary"]:
        assert canary not in path.read_text(encoding="utf-8")


def test_safe_sanitization_removes_credentials_and_private_values_from_record(tmp_path):
    value = event()
    value["payload"]["authorization"] = "Bearer fake-authorization-secret"
    value["payload"]["input"].append({"type": "reasoning", "encrypted_content": "private-replay-canary"})
    value["payload"]["input"].append({"role": "user", "content": "sk-abcdefghijklmno-supersecret"})
    original = deepcopy(value)
    result = record_request_metrics(tmp_path, value, run_id="run-safe", role="main", counter=len)
    path, _ = saved(tmp_path)
    assert result["recorded"] is True and value == original
    for canary in ["fake-authorization-secret", "private-replay-canary", "sk-abcdefghijklmno-supersecret"]:
        assert canary not in path.read_text(encoding="utf-8") + json.dumps(result)


def test_run_role_branch_identity_prevents_cross_subject_collision(tmp_path):
    subjects = [("run-a", "main", None), ("run-a", "product_facts", "branch-a"),
        ("run-a", "product_facts", "branch-b"), ("run-a", "audience_scenes", "branch-a"), ("run-b", "main", None)]
    refs = []
    for run_id, role, branch in subjects:
        result = record_request_metrics(tmp_path, event(), run_id=run_id, role=role,
            branch_task_id=branch, invocation_id="invoke-safe", ledger_attempt_id="attempt-safe", counter=len)
        assert result["recorded"] is True
        refs.append(result["metric_ref"]["id"])
    assert len(set(refs)) == len(subjects)
    assert len(list((tmp_path / "development_diagnostics/requests").glob("*.json"))) == len(subjects)


def test_existing_corrupt_record_is_not_overwritten(tmp_path):
    record(tmp_path)
    path, data = saved(tmp_path)
    data["role"] = "changed-without-content-hash"
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    result = record(tmp_path)
    assert result == {"recorded": False, "status": "unavailable", "reason_code": "EXISTING_METRIC_INVALID"}
    assert path.read_bytes() == before


def test_write_failure_and_invalid_metadata_are_fixed_safe_codes(tmp_path):
    (tmp_path / "development_diagnostics").write_text("file-not-directory", encoding="utf-8")
    result = record(tmp_path)
    assert result == {"recorded": False, "status": "unavailable", "reason_code": "DEVELOPMENT_METRIC_WRITE_FAILED"}
    assert str(tmp_path) not in json.dumps(result)
    result = record_request_metrics(tmp_path, event(), run_id="Bearer fake-secret", role="main", counter=len)
    assert result["reason_code"] == "METADATA_INVALID" and result["recorded"] is False


@pytest.mark.parametrize("bad_payload", [None, [], "prompt-not-a-payload"])
def test_invalid_payload_has_no_zero_estimate_or_unhandled_exception(tmp_path, bad_payload):
    result = record_request_metrics(tmp_path, {"payload": bad_payload}, run_id="run-safe", role="main", counter=len)
    assert result == {"recorded": False, "status": "unavailable", "reason_code": "PUBLIC_MEASUREMENT_FAILED"}
    assert not (tmp_path / "development_diagnostics").exists()


def test_exact_structured_request_ref_is_preserved_and_extra_text_rejected(tmp_path):
    ref = {"kind": "runtime_context", "id": "ctx-safe", "revision": 1, "content_hash": "sha256:" + "1" * 64}
    result = record_request_metrics(tmp_path, event(), run_id="run-safe", role="main", request_ref=ref, counter=len)
    _, data = saved(tmp_path)
    assert result["recorded"] is True and data["request_ref"] == ref
