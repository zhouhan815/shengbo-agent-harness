"""Read-only timing and request accounting from durable, public evidence.

Stage activity is a union of measured intervals, not a fictional fixed pipeline.
Stages may overlap. Only question wait not overlapped by real background work is
deducted from wall time. No model/provider calls or application writes occur here.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from statistics import median
from typing import Iterable

from .observability import sanitize

TERMINAL = {"COMPLETED", "READY_FOR_REVIEW", "FAILED", "BLOCKED", "CANCELLED", "SUPERSEDED"}
TOKEN_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")
STAGE_LABELS = {
    "materials_product": "资料整理与商品保存", "analysis": "顾客、商品与本场分析",
    "context_plan": "汇合上下文与话术计划", "scripts": "话术生成与局改",
    "playlist_review": "排序、开场与文本确认", "audio": "所需语音生成",
    "publication": "候选发布与结果核验", "planning_coordination": "主Agent规划、读取与协调",
    "human_decision": "人工决定", "unknown": "尚无足够阶段证据",
}
WORKFLOW_STAGE = {"wf_product_intake": "materials_product", "wf_analysis_batch": "analysis",
    "wf_bind_analysis": "analysis", "wf_context_plan": "context_plan", "wf_execute_script_plan": "scripts",
    "wf_prepare_review": "playlist_review", "wf_required_audio": "audio", "wf_finalize_preparation": "publication"}
WORKFLOW_STAGE.update({"wf_product_materials": "materials_product", "wf_analysis_coordination": "analysis",
    "wf_context_bundle": "context_plan", "wf_script_execution": "scripts", "wf_playback_review": "playlist_review"})


def stamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (ValueError, TypeError):
        return None


def union(intervals: Iterable[tuple[float, float]]):
    result = []
    for start, end in sorted((a, b) for a, b in intervals if a is not None and b is not None and b >= a):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def seconds(intervals):
    return sum(b - a for a, b in union(intervals))


def subtract(intervals, active):
    """Subtract union(active), preserving real blocking wait fragments."""
    result = []
    for start, end in union(intervals):
        fragments = [(start, end)]
        for a, b in union(active):
            fragments = [(x, y) for c, d in fragments
                         for x, y in ((c, min(d, a)), (max(c, b), d)) if y > x]
        result.extend(fragments)
    return union(result)


def _json(value, fallback=None):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _observed_tokens(usage):
    """Only public, observed counts; never fill a missing component with zero."""
    if not isinstance(usage, dict):
        return {}
    return {key: usage[key] for key in TOKEN_USAGE_KEYS
            if type(usage.get(key)) is int and usage[key] >= 0}


def _model_usage_state(attempt):
    observed = _observed_tokens(attempt.get("usage"))
    if len(observed) == len(TOKEN_USAGE_KEYS):
        return "known"
    if observed:
        return "partial"
    # A supplier terminal response ends the remote request even when local
    # parsing has not yet settled the attempt's 'sent' state.
    terminal_response = attempt.get("response_status") in {
        "completed", "failed", "incomplete", "cancelled", "canceled", "error"}
    inflight = attempt.get("state") in {"sent", "reserved"}
    ended = stamp(attempt.get("completed_at")) is not None or terminal_response
    return "pending" if inflight and not ended else "unknown"


def _usage_totals(rows):
    totals = {}
    for row in rows:
        for key, count in _observed_tokens(row.get("usage")).items():
            totals[key] = totals.get(key, 0) + count
    return totals


@contextmanager
def _connection(path):
    connection = sqlite3.connect("file:" + Path(path).resolve().as_posix() + "?mode=ro", uri=True, timeout=15)
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()


def _tables(path):
    if not Path(path).is_file():
        return []
    with _connection(path) as connection:
        return [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]


def _stage(name, args=None):
    args = args if isinstance(args, dict) else {}
    command = str(args.get("command") or name)
    if name in WORKFLOW_STAGE:
        return WORKFLOW_STAGE[name]
    if name.startswith("wf_"):
        for token, result in (("product", "materials_product"), ("analysis", "analysis"), ("context", "context_plan"),
                              ("script", "scripts"), ("review", "playlist_review"), ("audio", "audio"),
                              ("finalize", "publication"), ("publish", "publication")):
            if token in name:
                return result
    if command in {"save_product", "read_product", "read_source", "search_products", "search_catalog", "read_material", "extract_source",
                   "validate_product_assembly", "update_preparation_products"}:
        return "materials_product"
    if command in {"dispatch_analysis_batch", "read_analysis_batch", "accept_analysis", "accept_analysis_batch", "bind_analysis",
                   "submit_analysis_report", "read_analysis", "save_session_draft", "find_audience_research"} or command.startswith("submit_") and command.endswith("_report"):
        return "analysis"
    if command in {"create_context_bundle", "build_context_bundle", "save_context_bundle", "save_script_plan", "save_script_change_plan",
                   "assemble_context", "read_context", "read_recap", "import_recap", "link_recap_to_preparation"}:
        return "context_plan"
    if command in {"submit_script_change", "execute_script_plan", "retry_job_item", "read_script", "read_generation_result"}:
        return "scripts"
    if command in {"save_playlist", "compile_preparation", "compile_playback_plan", "request_text_review", "read_text_review",
                   "approve_text_for_audio", "preview_playlist_reorder", "save_asset_checklist", "read_asset_checklist"}:
        return "playlist_review"
    if command in {"prepare_required_audio", "read_audio_readiness", "read_audio_manifest"}:
        return "audio"
    if command in {"publish_preparation", "read_completion_observation", "read_publication", "verify_finish"}:
        return "publication"
    if command == "request_decision":
        return "human_decision"
    return "planning_coordination"


class Evidence:
    """Loads current snapshots and immutable events only; never loads credentials."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.warnings = []
        self.objects, self.attempts, self.events, self.jobs = [], [], [], []
        self._payload_files = {}
        agent = self.root / "agent" / "agent.sqlite3"
        if "objects" in _tables(agent):
            with _connection(agent) as connection:
                self.objects = [_json(row[1], {}) | {"_kind": row[0]} for row in connection.execute("SELECT kind,body FROM objects")]
                self.attempts = [_json(row[0], {}) for row in connection.execute("SELECT body FROM attempts")]
        eventdb = self.root / "agent" / "observability" / "observability.sqlite"
        if "events" in _tables(eventdb):
            with _connection(eventdb) as connection:
                self.events = [_json(row[0], {}) for row in connection.execute("SELECT envelope_json FROM events ORDER BY run_id,sequence")]
                self._payload_files = {row[0]: row[1] for row in connection.execute("SELECT payload_ref,filename FROM payloads")}
        elif (self.root / "events.json").exists():
            self.events = _json((self.root / "events.json").read_text(encoding="utf-8"), [])
            self.warnings.append("events.json may be a partial page; durable event store unavailable")
        voicedb = self.root / "voice" / "library.sqlite3"
        if "records" in _tables(voicedb):
            with _connection(voicedb) as connection:
                self.jobs = [_json(row[0], {}) for row in connection.execute("SELECT body FROM records WHERE kind='prelive_job'")]
        self.events.sort(key=lambda event: (stamp(event.get("timestamp")) or 0, event.get("sequence", 0)))

    def payload(self, ref):
        filename = self._payload_files.get(ref)
        if not filename:
            return {}
        directory = self.root / "agent" / "observability" / "payloads"
        path = (directory / filename).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file():
            return {}
        return _json(path.read_text(encoding="utf-8"), {})


def analyze(evidence: Evidence, run_ids=None, *, observed_end=None):
    runs = [row for row in evidence.objects if row.get("_kind") == "run"]
    selected = set(run_ids or [row["id"] for row in runs])
    runs = [row for row in runs if row.get("id") in selected]
    events = [row for row in evidence.events if row.get("run_id") in selected]
    attempts = [row for row in evidence.attempts if row.get("run_id") in selected]
    jobs = [row for row in evidence.jobs if row.get("run_id") in selected]
    starts = [stamp(row.get("created_at")) for row in runs] + [stamp(row.get("timestamp")) for row in events]
    starts = [value for value in starts if value is not None]
    if not starts:
        return {"schema_version": "timing-v1.1", "complete": False, "run_ids": sorted(selected),
                "warnings": [*evidence.warnings, "No run timestamps found"], "stages": [], "requests": {}}
    start = min(starts)
    complete = bool(runs) and all(row.get("status") in TERMINAL for row in runs)
    closes = [stamp(row.get("closed_at")) for row in runs]
    closes = [value for value in closes if value is not None]
    last_event = max((stamp(row.get("timestamp")) or start for row in events), default=start)
    end = max(closes + [last_event]) if complete else max(stamp(observed_end) or datetime.now(timezone.utc).timestamp(), last_event)
    activity, stages = [], defaultdict(list)
    tool_pending, branch_pending, flow_pending, step_pending = {}, {}, {}, {}
    tool_rows, step_rows, model_rows, provider_rows, flow_rows, branch_rows = [], [], [], [], [], []
    questions, answers, request_kinds = {}, {}, {}
    for event in events:
        kind, data = event.get("type", ""), event.get("data", {})
        at = stamp(event.get("timestamp"))
        if at is None:
            continue
        invocation = (event.get("run_id"), event.get("invocation_id"), event.get("branch_task_id"))
        if kind == "model.request":
            request = evidence.payload(data.get("payload_ref"))
            payload = request.get("payload", {})
            # This SDK sends the dedicated summary prompt as a single user
            # message, not a developer message. Ordinary Agent requests retain
            # their tool schema. Explicit attempt.purpose takes precedence.
            inputs = payload.get("input", [])
            is_summary = not payload.get("tools") and len(inputs) == 1 and (
                "<shengbo_summary_rules>" in str(inputs[0].get("content", "")))
            if request.get("attempt_id"):
                request_kinds[request["attempt_id"]] = "summary" if is_summary else "agent"
        if kind == "tool.started":
            key = (*invocation, data.get("tool_call_id") or data.get("tool_name"))
            tool_pending[key] = (at, data)
        elif kind in {"tool.completed", "tool.failed", "tool.error"}:
            candidates = [key for key in tool_pending if key[:3] == invocation
                          and tool_pending[key][1].get("tool_name") == data.get("tool_name")]
            if candidates:
                # M9 completion can omit tool_call_id. Match FIFO within the
                # same invocation/name; preserve this evidence limitation.
                key = min(candidates, key=lambda item: tool_pending[item][0])
                began, prior = tool_pending.pop(key)
                name, args = prior.get("tool_name", "unknown"), prior.get("arguments", {})
                stage = _stage(name, args)
                tool_rows.append({"name": name, "stage": stage, "started_at": began, "ended_at": at,
                    "duration_seconds": round(at - began, 6), "branch_task_id": invocation[2],
                    "argument_hash": hashlib.sha256(json.dumps(sanitize(args), sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                    "status": data.get("status", kind.rsplit(".", 1)[-1])})
                stages[stage].append((began, at))
                if stage != "human_decision":
                    activity.append((began, at))
        if kind == "branch.started":
            branch_pending[event.get("branch_task_id")] = (at, data.get("role", "unknown"))
        elif kind in {"branch.prepared", "branch.failed", "branch.cancelled"}:
            branch = event.get("branch_task_id")
            if branch in branch_pending:
                began, role = branch_pending.pop(branch)
                branch_rows.append({"branch_task_id": branch, "role": role, "started_at": began, "ended_at": at,
                                    "duration_seconds": round(at - began, 6), "terminal": kind})
                activity.append((began, at)); stages["analysis"].append((began, at))
        if kind == "decision.opened":
            question = data.get("question", {})
            questions[question.get("id") or question.get("decision_id")] = (at, question.get("kind"))
        elif kind == "decision.answered":
            answers[data.get("decision_id")] = at
        if kind == "workflow.started":
            flow_pending[data.get("flow_id")] = (at, data)
        elif kind in {"workflow.completed", "workflow.failed"} and data.get("flow_id") in flow_pending:
            began, prior = flow_pending.pop(data["flow_id"])
            workflow = prior.get("workflow", "unknown")
            flow_rows.append({"flow_id": data["flow_id"], "workflow": workflow, "started_at": began, "ended_at": at,
                              "duration_seconds": round(at - began, 6), "status": data.get("status")})
            stages[_stage(workflow)].append((began, at))
        if kind == "workflow.step.started":
            step_pending[(data.get("flow_id"), data.get("step"))] = (at, data)
        elif kind in {"workflow.step.completed", "workflow.step.failed"}:
            key = (data.get("flow_id"), data.get("step"))
            if key in step_pending:
                began, prior = step_pending.pop(key)
                step_rows.append({"flow_id": key[0], "step": key[1], "workflow": prior.get("workflow"),
                                  "started_at": began, "ended_at": at, "duration_seconds": round(at - began, 6),
                                  "status": data.get("status")})
    # Incomplete active spans must also count against blocking-wait deduction.
    for began, _ in branch_pending.values():
        activity.append((began, end)); stages["analysis"].append((began, end))
    for key, (began, data) in tool_pending.items():
        if _stage(data.get("tool_name", ""), data.get("arguments")) != "human_decision":
            activity.append((began, end))
    for attempt in attempts:
        sent = stamp(attempt.get("sent_at"))
        fallback = False
        if sent is None and attempt.get("provider_attempt_id") and attempt.get("state") not in {"reserved", "aborted_before_send"}:
            sent, fallback = stamp(attempt.get("reserved_at")), True
        finished = stamp(attempt.get("completed_at") or (
            attempt.get("observed_at") if attempt.get("state") not in {"sent", "reserved"} else None))
        if sent is None:
            continue  # reservation is not proof of a real model request
        finished = max(sent, finished if finished is not None else end)
        purpose = attempt.get("purpose") or request_kinds.get(attempt.get("provider_attempt_id"), "agent")
        row = {key: attempt.get(key) for key in ("id", "provider_attempt_id", "role", "state", "usage", "failure_code", "response_status")}
        row.update(kind=purpose, started_at=sent, ended_at=finished, duration_seconds=round(finished - sent, 6),
                   historical_reservation_start_estimate=fallback,
                   usage_state=_model_usage_state(attempt))
        model_rows.append(row); activity.append((sent, finished))
        stages["analysis" if attempt.get("role") != "main" else "planning_coordination"].append((sent, finished))
    for job in jobs:
        stage = "scripts" if job.get("kind") == "scripts" else "audio"
        for item in job.get("items", []):
            for attempt in item.get("provider_attempts", []):
                sent = stamp(attempt.get("sent_at"))
                received = stamp(attempt.get("received_at") or attempt.get("completed_at") or item.get("completed_at"))
                if sent is None:
                    continue
                finished = max(sent, received if received is not None else end)
                output = attempt.get("output") or {}
                payload = output.get("sentences", output) if isinstance(output, dict) else {}
                usage = attempt.get("usage") or (payload.get("usage") if isinstance(payload, dict) else None)
                purpose = attempt.get("purpose", "")
                selection = (attempt.get("input") or {}).get("voice", {})
                provider = "system" if purpose == "tts" and isinstance(selection, dict) and selection.get("provider") == "system" else (
                    payload.get("provider") if isinstance(payload, dict) else None)
                provider_rows.append({"job_id": job.get("id"), "item_id": item.get("item_id"),
                    "attempt_id": attempt.get("attempt_id"), "purpose": purpose, "state": attempt.get("state"),
                    "provider": provider, "stage": stage, "usage": usage,
                    "usage_state": "local_not_billed" if provider == "system" else "known" if usage else "unknown",
                    "started_at": sent, "ended_at": finished, "duration_seconds": round(finished - sent, 6),
                    "completion_time_basis": "provider_received" if stamp(attempt.get("received_at")) is not None else "item_end_or_observation"})
                activity.append((sent, finished)); stages[stage].append((sent, finished))
    # Awaited question intervals only: question display can overlap child/model/job work.
    waits = [(began, min(answers.get(identity, end), end)) for identity, (began, _) in questions.items()]
    blocking = subtract(waits, activity)
    stage_rows = []
    for stage, intervals in sorted(stages.items()):
        valid = union(intervals)
        stage_rows.append({"stage": stage, "label": STAGE_LABELS.get(stage, stage),
            "activity_union_seconds": round(seconds(valid), 6),
            "first_to_last_span_seconds": round(valid[-1][1] - valid[0][0], 6) if valid else 0,
            "interval_count": len(valid), "overlap_warning": "Different stages can overlap; do not sum stage durations."})
    duplicate_groups = Counter((row["name"], row["argument_hash"], row.get("branch_task_id")) for row in tool_rows)
    duplicates = [{"tool": key[0], "argument_hash": key[1], "branch_task_id": key[2], "count": count,
                   "duplicate_occurrences": count - 1, "unnecessary_confirmed": False}
                  for key, count in duplicate_groups.items() if count > 1]
    groups = defaultdict(list)
    for row in model_rows:
        groups[(row.get("role"), row["kind"])].append(row)
    usage_groups = []
    for (role, kind), rows in sorted(groups.items(), key=lambda item: str(item[0])):
        states = Counter(row["usage_state"] for row in rows)
        known = [row for row in rows if row["usage_state"] == "known"]
        partial = [row for row in rows if row["usage_state"] == "partial"]
        usage_groups.append({"role": role, "kind": kind, "actual_request_count": len(rows),
            "known_usage_count": states["known"], "partial_usage_count": states["partial"],
            "pending_usage_count": states["pending"], "unknown_usage_count": states["unknown"],
            "observed_usage_count": len(known) + len(partial),
            "known_usage_totals": _usage_totals(known), "partial_usage_totals": _usage_totals(partial),
            "observed_usage_totals": _usage_totals(known + partial),
            "request_duration_sum_seconds": round(sum(row["duration_seconds"] for row in rows), 6)})
    branch_intervals = [(row["started_at"], row["ended_at"]) for row in branch_rows]
    observed_branch_intervals = branch_intervals + [(began, end) for began, _ in branch_pending.values()]
    result = {"schema_version": "timing-v1.1", "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(evidence.root), "run_ids": sorted(selected), "complete": complete,
        "statuses": {row["id"]: row.get("status") for row in runs},
        "overall": {"start_timestamp": start, "end_timestamp": end, "wall_seconds": round(end - start, 6),
            "question_display_wait_seconds": round(seconds(waits), 6),
            "human_blocking_wait_seconds": round(seconds(blocking), 6),
            "machine_elapsed_seconds": round(max(0, end - start - seconds(blocking)), 6),
            "active_interval_union_seconds": round(seconds(activity), 6),
            "target_300_600_seconds": "target_only_not_a_quality_gate"},
        "analysis_parallel": {"completed_branch_count": len(branch_rows),
            "inflight_branch_count": len(branch_pending),
            "observed_elapsed_union_seconds": round(seconds(observed_branch_intervals), 6),
            "elapsed_union_seconds": round(seconds(branch_intervals), 6),
            "sum_branch_seconds": round(sum(row["duration_seconds"] for row in branch_rows), 6),
            "overlap_saved_seconds": round(sum(row["duration_seconds"] for row in branch_rows) - seconds(branch_intervals), 6)},
        "requests": {"model_reserved_count": len(attempts), "model_actual_request_count": len(model_rows),
            "model_reserved_not_sent_count": sum(row.get("state") in {"reserved", "aborted_before_send"} for row in attempts),
            "by_role_and_purpose": usage_groups, "voice_actual_request_count": len(provider_rows),
            "voice_by_purpose": dict(Counter(row["purpose"] for row in provider_rows)),
            "model": model_rows, "voice": provider_rows},
        "tools": {"outer_started_count": sum(row.get("type") == "tool.started" for row in events),
            "completed_count": len(tool_rows), "by_name": dict(Counter(row["name"] for row in tool_rows)),
            "same_argument_repeat_count": sum(row["duplicate_occurrences"] for row in duplicates),
            "repeat_groups": duplicates, "rows": tool_rows},
        "workflows": {"outer_completed_count": len(flow_rows), "internal_step_count": len(step_rows),
            "flows": flow_rows, "steps": step_rows, "unfinished_flows": len(flow_pending), "unfinished_steps": len(step_pending)},
        "stages": stage_rows, "branches": branch_rows,
        "warnings": [*evidence.warnings,
            "Repeated arguments are candidates for review, not proof of unnecessary work.",
            "Stage unions and model/provider sums overlap; only overall wall time is end-to-end.",
            "Historical rows without sent_at use reservation time as a labelled estimate.",
            "A terminal FAILED/BLOCKED run is complete as an observation, not a successful candidate.",
            "Local TTS time is synthesis time, not speech playback duration; fee invoices are not inferred."],
    }
    return sanitize(result)


def compare(reports):
    eligible = [report for report in reports if report.get("complete")
                and report.get("statuses") and all(value in {"COMPLETED", "READY_FOR_REVIEW"}
                    for value in report["statuses"].values())]
    values = [report["overall"]["machine_elapsed_seconds"] for report in eligible]
    return {"schema_version": "timing-comparison-v1", "round_count": len(reports), "successful_terminal_rounds": len(values),
            "machine_seconds": {"min": min(values), "median": median(values), "max": max(values)} if values else None,
            "p95": None, "note": "少量轮次只报告min/median/max，不宣称稳定p95；失败轮另列。",
            "rounds": [{"source_root": report.get("source_root"), "statuses": report.get("statuses"),
                        "complete": report.get("complete"), "overall": report.get("overall"),
                        "requests": {key: report.get("requests", {}).get(key) for key in
                                     ("model_actual_request_count", "voice_actual_request_count")}}
                       for report in reports]}


def markdown(report):
    total = report.get("overall", {})
    lines = ["# 播前 Agent 真实计时", "", f"完整观测：{report.get('complete')}；任务：{', '.join(report.get('run_ids', []))}", "",
        "| 口径 | 秒 |", "| --- | ---: |",
        *[f"| {label} | {total.get(key, '未知')} |" for key, label in (
            ("wall_seconds", "用户经历总时长"), ("human_blocking_wait_seconds", "真正阻塞的人工等待"),
            ("machine_elapsed_seconds", "扣除真正人工等待后"))], "",
        "| 阶段活动 | 活动时间并集（秒） | 首末跨度（秒） |", "| --- | ---: | ---: |",
        *[f"| {row['label']} | {row['activity_union_seconds']} | {row['first_to_last_span_seconds']} |" for row in report.get("stages", [])],
        "", "各阶段可并行，不能相加当成总时长。阶段跨度包含其中等待；活动并集不含未观察活动的间隙。", "",
        "| 请求类型 | 次数 | 完整 known | 部分 partial | 在途 pending | 结束未知 unknown | 已观测 known+partial |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        *[f"| {row['role']} / {row['kind']} | {row['actual_request_count']} | {row.get('known_usage_count', '未知')} | {row.get('partial_usage_count', '未知')} | {row.get('pending_usage_count', '未知')} | {row['unknown_usage_count']} | {row.get('observed_usage_count', '未知')} |"
          for row in report.get("requests", {}).get("by_role_and_purpose", [])],
        f"| 文案与TTS实际请求 | {report.get('requests', {}).get('voice_actual_request_count', 0)} | 另见JSON供应商口径 | — | — | — | — |", "",
        "known/partial/pending/unknown互斥；已观测为known+partial。known_usage_totals仅含完整用量；observed_usage_totals保留部分观测，缺失字段不补零。本机TTS仍按local_not_billed单列。", "",
        "| 工具与工具流 | 数量 |", "| --- | ---: |",
        f"| 外层工具启动 | {report.get('tools', {}).get('outer_started_count', 0)} |",
        f"| 相同参数重复（不等于确认冗余） | {report.get('tools', {}).get('same_argument_repeat_count', 0)} |",
        f"| 工具流内部步骤 | {report.get('workflows', {}).get('internal_step_count', 0)} |", "",
        *[f"- {text}" for text in report.get("warnings", [])]]
    return "\n".join(lines) + "\n"
