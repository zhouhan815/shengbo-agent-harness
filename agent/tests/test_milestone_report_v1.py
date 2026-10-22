"""Continuous timing must reconcile wall clock without counting SSE or overlaps twice."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
_spec = importlib.util.spec_from_file_location("report_milestones_v1", Path(__file__).resolve().parents[1] / "verification/report_milestones_v1.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


BASE = 1792242000


def at(seconds):
    return datetime.fromtimestamp(BASE + seconds, timezone.utc).isoformat()


def sample(tmp_path, monkeypatch, *, product_at=6, include_audio_terminal=True):
    def event(t, kind, **data):
        return {"timestamp": at(t), "type": kind, "run_id": "run1", "data": data,
            "sequence": int(t*10), "event_id": kind+str(t)}
    events = [event(2, "tool.completed", tool_name="dispatch_analysis_batch", status="SUCCEEDED"),
        event(2.1, "branch.started", role="audience_scenes"),
        event(5, "branch.prepared"),
        event(product_at, "tool.completed", tool_name="save_product", status="SUCCEEDED"),
        event(7, "decision.opened", question={"id": "q1", "kind": "playlist_order"}),
        event(9, "decision.answered", decision_id="q1"),
        event(16, "run.closed", status="COMPLETED")]
    events[1]["branch_task_id"] = "branch1"
    events[2]["branch_task_id"] = "branch1"
    events += [event(8.5, "model.response") for _ in range(10)]
    events.sort(key=lambda x: x["timestamp"])
    attempts = [{"id": "ordinary", "run_id": "run1", "role": "main", "purpose": "agent", "state": "succeeded",
        "sent_at": at(8), "completed_at": at(9), "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}},
        {"id": "summary", "run_id": "run1", "role": "main", "purpose": "summary", "state": "succeeded",
        "sent_at": at(5.5), "completed_at": at(6.5), "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}]
    jobs = [{"id": "scripts", "run_id": "run1", "kind": "scripts", "created_at": at(9), "status": "succeeded", "items": []},
        {"id": "audio", "run_id": "run1", "kind": "audio", "created_at": at(12), "status": "succeeded", "items": []}]
    e = SimpleNamespace(root=tmp_path, objects=[{"_kind": "run", "id": "run1", "status": "COMPLETED", "created_at": at(0)}],
        attempts=attempts, events=events, jobs=jobs, warnings=[], payload=lambda ref: {})
    monkeypatch.setattr(report, "voice_events", lambda root: [{"seq": 5, "event_type": "job.item_succeeded", "created_at": at(14),
        "payload": {"job_id": "audio", "status": "succeeded"}}] if include_audio_terminal else [])
    return e


def test_continuous_phases_cover_all_wall_and_split_crossing_summary(tmp_path, monkeypatch):
    data = report.milestone_report(sample(tmp_path, monkeypatch))
    assert data["phase_wall_sum_seconds"] == 16 and data["full_wall_covered"]
    assert [p["wall_seconds"] for p in data["phases"]] == [2, 3, 1, 3, 3, 2, 2]
    summary = [g for p in data["phases"] for g in p["model_categories"] if g["kind"] == "summary"]
    assert sum(g["duration_sum_seconds"] for g in summary) == 1
    assert sum(g["requests_started_in_phase"] for g in summary) == 1
    assert data["model_requests"]["actual_count"] == 2  # ten SSE observations are not ten new requests
    assert data["human_decisions"][0]["display_wait_seconds"] == 2
    assert data["human_decisions"][0]["blocking_seconds"] == 1  # second second overlaps a real request


def test_missing_terminal_does_not_fabricate_a_last_item_timestamp(tmp_path, monkeypatch):
    data = report.milestone_report(sample(tmp_path, monkeypatch, include_audio_terminal=False))
    assert "audio_terminal" in data["missing_milestones"]
    assert "audio_terminal" not in data["milestones"] and not data["full_wall_covered"]
    assert any("没有音频任务终态" in warning for warning in data["warnings"])


def test_out_of_order_milestones_keep_real_order_without_negative_phases(tmp_path, monkeypatch):
    data = report.milestone_report(sample(tmp_path, monkeypatch, product_at=4))
    assert data["chronological_order_differs_from_business_order"]
    assert all(p["wall_seconds"] >= 0 for p in data["phases"])
    assert data["phase_wall_sum_seconds"] == 16
    assert data["phases"][1]["end_milestone"] == "product_saved"
    assert data["phases"][2]["end_milestone"] == "analysis_prepared"


def test_clip_unions_are_additive_across_continuous_boundaries():
    intervals = [(1, 7), (3, 8), (10, 12)]
    assert report.seconds(report.clip(intervals, 0, 5)) + report.seconds(report.clip(intervals, 5, 15)) == report.seconds(intervals)


def test_optional_flow_and_business_event_supply_real_fallback_boundaries(tmp_path, monkeypatch):
    evidence = sample(tmp_path, monkeypatch)
    evidence.events[0].update(type="workflow.step.completed", data={"step": "dispatch_analysis_batch"})
    evidence.events = [e for e in evidence.events if e["data"].get("tool_name") != "save_product"]
    terminal = report.voice_events(tmp_path)
    monkeypatch.setattr(report, "voice_events", lambda root: [
        {"seq": 4, "event_type": "business.save_product", "created_at": at(6), "payload": {"run_id": "run1"}}, *terminal])
    data = report.milestone_report(evidence)
    assert data["full_wall_covered"] and data["phase_wall_sum_seconds"] == 16
    assert data["milestones"]["dispatch"]["event_type"] == "workflow.step.completed"
    assert data["milestones"]["product_saved"]["source"] == "voice.prelive_events"
