"""Public monitor aggregation counts attempts, never modules or missing data."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from shengbo_contracts.common import canonical_hash

SCRIPT = Path(__file__).resolve().parents[1] / "verification" / "report_priority_eight_monitors.py"
SPEC = importlib.util.spec_from_file_location("priority_eight_monitor_report", SCRIPT)
reporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reporter)


def event(kind, identity, data):
    return {"type": kind, "event_id": identity, "run_id": "r", "branch_task_id": "b", "sequence": 1,
            "timestamp": "2026-10-19T10:00:00+00:00", "data": data}


def case(tmp_path):
    source_ref = {"kind": "source_manifest", "id": "source-1", "revision": 1, "content_hash": canonical_hash("source")}
    objects = [{"_kind": "run", "id": "r", "conversation_id": "c"},
        {"_kind": "operation", "id": "private-operation", "args": {"content_base64": "MUST-NOT-APPEAR"}},
        {"_kind": "material", "id": "source-1", "conversation_id": "c", "artifact_ref": source_ref,
            "name": "商品.docx", "manifest": {"text": "PRIVATE-SOURCE-PROSE", "original_size": 1234,
                "coverage": {"processed_text_complete": True, "pending_visual": True, "business_understanding_complete": False},
                "parse_metrics": {"input_bytes": 1234, "total_parse_ms": 25.5, "text_characters": 500,
                    "stages": [{"stage": "ordered_word_parse", "duration_ms": 12.5}]}}},
        {"_kind": "document_read", "id": "read-1", "run_id": "r", "branch_task_id": "b",
            "artifact_ref": source_ref, "section": "paragraph:1", "read_count": 3, "duration_ms": 9.0, "returned_text_chars": 30}]
    events = [event("material.read.completed", "read-event-1", {"source_ref": source_ref, "section": "paragraph:1", "duration_ms": 2., "read_count": 1}),
        event("material.read.completed", "read-event-2", {"source_ref": source_ref, "section": "paragraph:1", "duration_ms": 7., "read_count": 2}),
        event("material.assessment.validation", "assessment-v", {"source_count": 1, "evidence_count": 5, "locator_reference_count": 5, "locator_count": 2, "validation_duration_ms": 0.25, "semantic_support_verified": False}),
        event("material.assessment.completed", "assessment-c", {"quality_counts": {"observations": 5}, "duration_ms": 1.5, "semantic_acceptance": False}),
        event("middleware.check", "m3", {"middleware": "M3 ContextProjection", "projection_duration_ms": 0.125, "projection_text_chars": 1234})]
    receipt = {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "secret": "MUST-NOT-APPEAR"},
        "content": "PRIVATE-RAW-MODEL-OUTPUT", "validation_product": {"image": "data:image/png;base64,MUST-NOT-APPEAR"},
        "timing": {"prompt_build_ms": 1.25, "provider_roundtrip_ms": 250.5, "request_bytes": 2000, "response_bytes": 1000},
        "context_compaction": {"before_bytes": 3000, "after_bytes": 2100, "reference_count": 1}}
    archived = {"job_id": "j", "item_id": "i", "attempt_id": "a", "response": receipt}
    response_ref = {"kind": "provider_response", "id": "response", "revision": 1, "content_hash": canonical_hash(archived)}
    attempt = {"attempt_id": "a", "purpose": "copywriting.whole_script", "state": "result_received", "validation_state": "accepted",
        "sent_at": "2026-10-19T10:00:00+00:00", "received_at": "2026-10-19T10:00:01+00:00",
        "input": {"module_keys": ["opening_long", "product_intro", "cta"]}, "provider_response_ref": response_ref,
        "usage": {"total_tokens": 150}, "usage_state": "known", "output": {
            "content": "PRIVATE-RAW-MODEL-OUTPUT", "validation_metrics": {"elapsed_ms": 0.5,
                "modules": [{"module_key": "cta", "elapsed_ms": 0.1, "sentence_count": 1, "spoken_characters": 20}]}}}
    metric = {"stage": "whole_module_commit", "elapsed_ms": 0.75, "recorded_at": "2026-10-19T10:00:01+00:00", "module_count": 3}
    jobs = [{"id": "j", "run_id": "r", "items": [{"item_id": "i", "module_results": {key: {} for key in ["opening_long", "product_intro", "cta"]},
        "provider_attempts": [attempt], "generation_metrics": [metric]}]}]
    voice_events = [{"type": "script.generation_stage", "event_id": "stage-event", "seq": 5,
                     "data": {"job_id": "j", "item_id": "i", **metric}}]
    evidence = SimpleNamespace(root=tmp_path, warnings=[], objects=objects, events=events, jobs=jobs)
    records = [{"kind": "provider_response", "id": "response", "revision": 1, "body": archived}]
    timing = {"run_ids": ["r"], "overall": {"wall_seconds": 42.0}, "stages": [{"stage": "scripts", "activity_union_seconds": 1.0}],
              "requests": {"model_actual_request_count": 5}}
    return evidence, records, voice_events, timing


def test_complete_report_is_traceable_and_does_not_double_count_modules_or_event_mirrors(tmp_path):
    evidence, records, events, timing = case(tmp_path)
    original = deepcopy((evidence.objects, evidence.events, evidence.jobs, records, events, timing))
    result = reporter.collect(tmp_path, evidence=evidence, records=records, voice_events=events, timing=timing)
    counts = result["copywriting_attempts"]
    assert counts["ledger_attempt_count"] == counts["whole_script_ledger_attempt_count"] == counts["confirmed_http_attempt_count"] == 1
    row = counts["rows"][0]
    assert row["usage"]["counts"]["total_tokens"] == 150
    assert row["provider_roundtrip_ms"]["value"] == 250.5
    assert row["ledger_sent_to_received_ms"]["value"] == 1000.0
    assert row["source"]["path"] == "/items/0/provider_attempts/0"
    assert len(result["generation_metrics"]["rows"]) == 1
    assert result["generation_metrics"]["rows"][0]["event_refs"][0]["event_id"] == "stage-event"
    assert result["sources"]["rows"][0]["coverage"]["value"]["business_understanding_complete"] is False
    assert result["sources"]["rows"][0]["parse_stages"][0]["duration_ms"]["value"] == 12.5
    assert result["material_assessments"]["rows"][0]["locator_reference_count"] == 5
    assert result["m3_projections"]["rows"][0]["projection_duration_ms"]["value"] == 0.125
    assert result["timing_reference"]["overall"]["wall_seconds"] == 42.0
    assert result["timing_reference"]["content_hash"] == canonical_hash(timing)
    encoded = json.dumps(result, ensure_ascii=False)
    for forbidden in ("MUST-NOT-APPEAR", "PRIVATE-SOURCE-PROSE", "PRIVATE-RAW-MODEL-OUTPUT", "data:image", "content_base64"):
        assert forbidden not in encoded
    assert original == (evidence.objects, evidence.events, evidence.jobs, records, events, timing)


def test_read_counts_are_cumulative_but_duration_is_not_multiplied_from_latest_sample(tmp_path):
    evidence, records, events, timing = case(tmp_path)
    result = reporter.collect(tmp_path, evidence=evidence, records=records, voice_events=events, timing=timing)
    read = result["document_reads"]["rows"][0]
    assert read["recorded_read_count"]["value"] == 3
    assert read["recorded_repeat_count"]["value"] == 2
    assert read["latest_duration_ms"]["value"] == 9.0
    assert read["event_duration_sum_ms"]["value"] == 9.0  # only the observed 2+7, not 3*9
    assert read["event_coverage"] == "partial_or_not_observed"
    assert read["unnecessary_confirmed"] is False


def test_missing_measurements_and_unknown_send_stay_unknown_not_zero(tmp_path):
    evidence, records, events, timing = case(tmp_path)
    attempt = evidence.jobs[0]["items"][0]["provider_attempts"][0]
    attempt.update(state="timeout_unknown", validation_state="unknown", usage={})
    attempt.pop("output"); attempt.pop("provider_response_ref"); attempt.pop("received_at")
    result = reporter.collect(tmp_path, evidence=evidence, records=[], voice_events=[], timing=timing)
    row = result["copywriting_attempts"]["rows"][0]
    assert row["request_observation"]["observed_http_attempt_count"] is None
    assert row["provider_roundtrip_ms"] == {"state": "not_observed", "value": None}
    assert row["usage"]["counts"] is None
    assert result["copywriting_attempts"]["send_or_outcome_unproven_count"] == 1


def test_same_attempt_id_is_deduplicated_and_bad_archive_cannot_prove_response(tmp_path):
    evidence, records, events, timing = case(tmp_path)
    attempts = evidence.jobs[0]["items"][0]["provider_attempts"]
    attempts.append(deepcopy(attempts[0]))
    records[0]["body"]["item_id"] = "other-item"
    result = reporter.collect(tmp_path, evidence=evidence, records=records, voice_events=events, timing=timing)
    assert result["copywriting_attempts"]["ledger_attempt_count"] == 1
    assert result["copywriting_attempts"]["rows"][0]["archive_association"] == "not_verified_or_missing"
    assert result["copywriting_attempts"]["rows"][0]["request_observation"]["observed_http_attempt_count"] is None
    assert result["warnings"]


def test_empty_evidence_is_explicit_and_does_not_run_timing_or_create_source_files(tmp_path):
    fake = SimpleNamespace(root=tmp_path, warnings=[], objects=[], events=[], jobs=[])
    before = list(tmp_path.iterdir())
    result = reporter.collect(tmp_path, evidence=fake, records=[], voice_events=[])
    assert result["sources"]["state"] == result["m3_projections"]["state"] == "not_observed"
    assert result["timing_reference"]["state"] == "not_observed"
    assert list(tmp_path.iterdir()) == before
