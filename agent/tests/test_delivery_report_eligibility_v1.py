"""Distinct evidence, build and timing denominators; no real runtime calls."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("delivery_report", Path(__file__).resolve().parents[1] / "verification/build_efficiency_delivery_report.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


def successful(status="COMPLETED"):
    return {"status": status, "acceptance": {"ok": True, "objective": "prepare_session",
        "observation": {"candidate_ready": True}}, "adopted": False, "started_live": False}


def register(root, version="RC6", *, suite=None, rounds=3, **overrides):
    data = {"version": version, "benchmark_root": str(suite or root / "bench"), "rounds": rounds,
        "files": [{"project": "agent", "path": "src/example.py", "sha256": "a" * 64, "benchmark_source": True}], **overrides}
    path = root / (version.lower() + "-source-manifest.json")
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def sample(root, index, result=None, wall=100, *, timing=True, timing_changes=None, run_id=None):
    folder = root / "bench" / ("round-%02d" % index)
    folder.mkdir(parents=True, exist_ok=True)
    result = deepcopy(successful() if result is None else result)
    result.setdefault("run_id", run_id or "run-%02d" % index)
    (folder / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if timing:
        rid = result["run_id"]
        data = {"run_ids": [rid] if rid else [], "statuses": {rid: result["status"]} if rid else {},
            "overall": {"wall_seconds": wall, "human_blocking_wait_seconds": 5,
                "machine_elapsed_seconds": max(0, wall - 5) if type(wall) in (int, float) else None},
            "requests": {"by_role_and_purpose": [], "model_actual_request_count": 1,
                "voice_by_purpose": {"copywriting.generate": 2, "tts": 3}}, **(timing_changes or {})}
        (folder / "timing.json").write_text(json.dumps(data), encoding="utf-8")
    return folder


def collect(root, version="RC6", folders=None):
    return report.collect_benchmarks(root, [(version, path) for path in (folders or [root / "bench"])])


@pytest.mark.parametrize("status", ["COMPLETED", "READY_FOR_REVIEW"])
def test_both_successful_terminal_outcomes_need_exact_goal_acceptance(status):
    assert report.is_full_candidate(successful(status))


@pytest.mark.parametrize("status", ["BLOCKED", "FAILED", "CANCELLED", "SUPERSEDED", "INCOMPLETE_TIMEOUT", "INCOMPLETE_ERROR", "RUNNING"])
def test_failure_or_incomplete_cannot_pass_even_with_ready_old_candidate(status):
    assert not report.is_full_candidate(successful(status))


@pytest.mark.parametrize("path,value", [
    (("acceptance", "objective"), "read_explain"), (("acceptance", "ok"), False), (("acceptance", "ok"), 1),
    (("acceptance", "observation", "candidate_ready"), False), (("acceptance", "observation", "candidate_ready"), 1),
    (("adopted",), True), (("adopted",), 0), (("started_live",), True), (("started_live",), 0),
    (("benchmark_eligible",), False),
])
def test_each_required_evidence_is_strict_and_read_only_task_is_excluded(path, value):
    result = successful(); obj = result
    for key in path[:-1]: obj = obj[key]
    obj[path[-1]] = value
    assert not report.is_full_candidate(result)


@pytest.mark.parametrize("path", [("status",), ("acceptance",), ("acceptance", "ok"), ("acceptance", "objective"),
    ("acceptance", "observation"), ("acceptance", "observation", "candidate_ready"), ("adopted",), ("started_live",)])
def test_missing_evidence_is_not_inferred_as_success(path):
    result = successful(); obj = result
    for key in path[:-1]: obj = obj[key]
    del obj[path[-1]]
    assert not report.is_full_candidate(result)


def test_failure_readonly_and_missing_flags_stay_out_of_success_median(tmp_path):
    register(tmp_path)
    cases = [(successful("READY_FOR_REVIEW"), 100), (successful(), 200), (successful(), 300),
        (successful("BLOCKED"), 1), (successful("INCOMPLETE_TIMEOUT"), 2)]
    readonly = successful(); readonly["acceptance"]["objective"] = "read_explain"
    missing = successful(); del missing["adopted"]
    denied = successful(); denied["benchmark_eligible"] = False
    cases.extend([(readonly, 3), (missing, 4), (denied, 5)])
    for i, (result, wall) in enumerate(cases, 1): sample(tmp_path, i, result, wall)
    gathered = collect(tmp_path)
    assert gathered["stats"]["RC6"]["functional_count"] == 3
    assert gathered["stats"]["RC6"]["timed_walls"] == [100, 200, 300]
    html = report.build(tmp_path, [("RC6", tmp_path / "bench")])
    assert "中位 3分20.0秒" in html and "最长 5分00.0秒" in html
    assert "BLOCKED" in html and "INCOMPLETE_TIMEOUT" in html


@pytest.mark.parametrize("wall", [True, False, -1, float("nan"), float("inf"), -float("inf"), "120", None])
def test_functional_success_and_valid_timing_denominators_are_different(tmp_path, wall):
    register(tmp_path)
    for i, seconds in enumerate([100, 200, wall], 1): sample(tmp_path, i, wall=seconds)
    stats = collect(tmp_path)["stats"]["RC6"]
    assert stats["functional_count"] == stats["verified_functional_count"] == 3
    assert stats["timed_walls"] == [100, 200]
    html = report.build(tmp_path, [("RC6", tmp_path / "bench")])
    assert "功能完整 3 个" in html and "有效计时 2 个" in html
    assert "不报告三轮中位数" in html


def test_startup_failure_without_timing_uses_neither_zero_nor_cleanup_time(tmp_path):
    register(tmp_path)
    folder = sample(tmp_path, 1, {"status": "INCOMPLETE_ERROR", "run_id": None, "benchmark_eligible": False}, timing=False)
    (folder / "timing-after-cleanup.json").write_text(json.dumps({"overall": {"wall_seconds": 999}, "requests": {"model_actual_request_count": 42}}), encoding="utf-8")
    row = collect(tmp_path)["samples"][0]
    assert row["status"] == "INCOMPLETE_ERROR" and row["wall"] is None and row["gpt"] is None
    assert collect(tmp_path)["stats"]["RC6"]["accepted_runs"] == 0
    html = report.build(tmp_path, [("RC6", tmp_path / "bench")])
    assert "16分39.0秒" not in html and "没有完成候选准备" in html


def test_missing_result_and_nonterminal_round_are_visible_not_silent(tmp_path):
    register(tmp_path)
    folder = sample(tmp_path, 1, successful("RUNNING"))
    (folder / "result.json").unlink()
    (folder / "ids.json").write_text(json.dumps({"run_id": "run-01"}), encoding="utf-8")
    second = tmp_path / "bench" / "round-02"; second.mkdir()
    gathered = collect(tmp_path)
    assert [row["status"] for row in gathered["samples"]] == ["RUNNING", "RESULT_MISSING"]
    assert gathered["stats"]["RC6"]["accepted_runs"] == 1
    assert gathered["stats"]["RC6"]["terminal_runs"] == 0
    assert gathered["stats"]["RC6"]["functional_count"] == 0


def test_no_round_directory_still_reports_unstarted_suite(tmp_path):
    register(tmp_path)
    gathered = collect(tmp_path)
    assert len(gathered["samples"]) == 1 and gathered["samples"][0]["status"] == "RESULT_MISSING"
    assert gathered["stats"]["RC6"]["accepted_runs"] == 0


@pytest.mark.parametrize("mutation", ["wrong_version", "wrong_root", "bad_sha", "duplicate_file", "absent"])
def test_unverified_or_conflicting_build_never_enters_version_timing(tmp_path, mutation):
    path = register(tmp_path)
    data = json.loads(path.read_text())
    if mutation == "wrong_version": data["version"] = "RC5"
    elif mutation == "wrong_root": data["benchmark_root"] = str(tmp_path / "other")
    elif mutation == "bad_sha": data["files"][0]["sha256"] = "not-a-hash"
    elif mutation == "duplicate_file": data["files"].append(deepcopy(data["files"][0]))
    if mutation == "absent": path.unlink()
    else: path.write_text(json.dumps(data), encoding="utf-8")
    sample(tmp_path, 1)
    stats = collect(tmp_path)["stats"]["RC6"]
    assert stats["functional_count"] == 1
    assert stats["verified_functional_count"] == 0 and stats["timed_walls"] == []


def test_round_path_and_accepted_run_are_deduplicated_even_across_labels(tmp_path):
    register(tmp_path)
    first = sample(tmp_path, 1)
    second = sample(tmp_path, 2, run_id="run-01")
    gathered = report.collect_benchmarks(tmp_path, [("RC6", tmp_path / "bench"), ("RC6", first), ("WRONG", second)])
    assert len(gathered["samples"]) == 1
    assert gathered["stats"]["RC6"]["timed_walls"] == [100]
    assert any("run_id" in text for text in gathered["warnings"])
    assert any("轮次目录" in text for text in gathered["warnings"])


def test_timing_from_other_run_or_status_cannot_enter_denominator(tmp_path):
    register(tmp_path)
    sample(tmp_path, 1, timing_changes={"run_ids": ["foreign-run"]})
    sample(tmp_path, 2, timing_changes={"statuses": {"run-02": "RUNNING"}})
    stats = collect(tmp_path)["stats"]["RC6"]
    assert stats["functional_count"] == 2 and stats["timed_walls"] == []


def test_phase_selection_requires_both_exact_source_root_and_run(tmp_path):
    register(tmp_path)
    folder = sample(tmp_path, 1)
    phase = {"source_root": str(folder), "run_id": "run-01", "status": "COMPLETED", "overall": {"wall_seconds": 100},
        "full_wall_covered": True, "phases": [{"label": "SELECTED_PHASE", "wall_seconds": 100, "human_blocking_seconds": 5}]}
    (tmp_path / "phase-report-current.json").write_text(json.dumps(phase), encoding="utf-8")
    for name, changed in [("foreign-root", {"source_root": str(tmp_path / "old")}), ("foreign-run", {"run_id": "old-run"})]:
        old = {**phase, **changed, "phases": [{"label": "MUST_NOT_APPEAR", "wall_seconds": 9}]}
        (tmp_path / ("phase-report-" + name + ".json")).write_text(json.dumps(old), encoding="utf-8")
    html = report.build(tmp_path, [("RC6", tmp_path / "bench")])
    assert "SELECTED_PHASE" in html and "MUST_NOT_APPEAR" not in html
    assert "完整连续计时（不等于功能成功）" in html


def test_dynamic_links_use_output_parent_and_never_hardcode_old_audits(tmp_path):
    register(tmp_path)
    sample(tmp_path, 1)
    audit = tmp_path / "rc6-content-round-01.md"; audit.write_text("current audit", encoding="utf-8")
    (tmp_path / "stable-content-audit.md").write_text("old audit", encoding="utf-8")
    output = tmp_path / "nested" / "report.html"
    html = report.build(tmp_path, [("RC6", tmp_path / "bench")], output_path=output,
        evidence_links=[("pending custom evidence", tmp_path / "not-created.json")])
    assert "href='../rc6-content-round-01.md'" in html
    assert "href='../rc6-source-manifest.json'" in html
    assert "stable-content-audit.md" not in html and "rc3-source-manifest.json" not in html
    assert "href='../not-created.json'" not in html and "补充证据尚不存在" in html


def test_failed_rounds_and_measured_zero_are_not_confused(tmp_path):
    register(tmp_path)
    sample(tmp_path, 1, wall=0)
    for i, status in enumerate(["FAILED", "BLOCKED", "INCOMPLETE_TIMEOUT"], 2): sample(tmp_path, i, {"status": status}, wall=55)
    gathered = collect(tmp_path)
    assert gathered["stats"]["RC6"]["functional_count"] == 1
    assert gathered["stats"]["RC6"]["timed_walls"] == [0]
    assert [row["wall"] for row in gathered["samples"]] == [0, 55, 55, 55]
    html = report.build(tmp_path, [("RC6", tmp_path / "bench")])
    assert "0分00.0秒" in html and "中位 " not in html
