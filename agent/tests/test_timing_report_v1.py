"""Meaningful offline accounting tests with overlapping human/background work."""
from pathlib import Path
from types import SimpleNamespace
import unittest

from shengbo_prelive.timing_report import analyze, compare, subtract, union, seconds


def event(kind, at, data=None, branch=None, invocation="i"):
    return {"type": kind, "timestamp": at, "run_id": "r", "invocation_id": invocation,
            "branch_task_id": branch, "data": data or {}}


def evidence(events=None, attempts=None, jobs=None, status="COMPLETED"):
    return SimpleNamespace(root=Path("fake-only"), warnings=[],
        objects=[{"_kind": "run", "id": "r", "created_at": 0, "closed_at": 100, "status": status}],
        attempts=attempts or [], events=events or [], jobs=jobs or [], payload=lambda ref: {})


class TimingReportTests(unittest.TestCase):
    def test_interval_unions_and_wait_subtraction(self):
        self.assertEqual(union([(0, 20), (10, 30), (30, 40)]), [(0, 40)])
        self.assertEqual(subtract([(0, 20)], [(3, 5), (7, 10), (40, 50)]), [(0, 3), (5, 7), (10, 20)])

    def test_question_display_overlap_is_not_falsely_subtracted(self):
        events = [event("decision.opened", 10, {"question": {"id": "q", "kind": "approve"}}),
                  event("branch.started", 15, {"role": "audience_scenes"}, branch="b"),
                  event("branch.prepared", 30, {"role": "audience_scenes"}, branch="b"),
                  event("decision.answered", 40, {"decision_id": "q"})]
        report = analyze(evidence(events))
        self.assertEqual(report["overall"]["wall_seconds"], 100)
        self.assertEqual(report["overall"]["question_display_wait_seconds"], 30)
        self.assertEqual(report["overall"]["human_blocking_wait_seconds"], 15)
        self.assertEqual(report["overall"]["machine_elapsed_seconds"], 85)

    def test_new_and_legacy_graph_pause_end_the_tool_span(self):
        for kind in ("tool.interrupted", "tool.failed"):
            with self.subTest(kind=kind):
                events = [event("tool.started", 8, {"tool_name": "request_user_decision"}),
                          event("decision.opened", 10, {"question": {"id": "q", "kind": "catalog_confirm"}}),
                          event(kind, 11, {"tool_name": "request_user_decision", "status": "INTERRUPTED"}),
                          event("decision.answered", 40, {"decision_id": "q"})]
                report = analyze(evidence(events))
                self.assertEqual(report["tools"]["rows"][0]["status"], "INTERRUPTED")
                self.assertEqual(report["tools"]["rows"][0]["stage"], "human_decision")
                self.assertEqual(report["tools"]["rows"][0]["duration_seconds"], 3)
                self.assertEqual(report["overall"]["human_blocking_wait_seconds"], 30)

    def test_parallel_analysis_union_not_sum(self):
        events = [event("branch.started", 10, {"role": "product_facts"}, branch="a"),
                  event("branch.started", 12, {"role": "audience_scenes"}, branch="b"),
                  event("branch.prepared", 30, {}, branch="a"), event("branch.prepared", 40, {}, branch="b")]
        report = analyze(evidence(events))
        self.assertEqual(report["analysis_parallel"]["elapsed_union_seconds"], 30)
        self.assertEqual(report["analysis_parallel"]["sum_branch_seconds"], 48)
        self.assertEqual(report["analysis_parallel"]["overlap_saved_seconds"], 18)

    def test_reservation_not_counted_as_remote_request_and_unknown_not_zero(self):
        attempts = [{"id": "reserved", "run_id": "r", "state": "aborted_before_send", "role": "main", "reserved_at": 1},
                    {"id": "actual", "run_id": "r", "state": "known_failed", "role": "main", "sent_at": 10,
                     "completed_at": 20, "provider_attempt_id": "p", "usage": {"input_tokens": 7}}]
        report = analyze(evidence(attempts=attempts))
        self.assertEqual(report["requests"]["model_reserved_count"], 2)
        self.assertEqual(report["requests"]["model_actual_request_count"], 1)
        grouped = report["requests"]["by_role_and_purpose"][0]
        self.assertEqual(grouped["known_usage_totals"], {})
        self.assertEqual(grouped["observed_usage_totals"], {"input_tokens": 7})
        self.assertEqual(grouped["partial_usage_count"], 1)

    def test_macro_steps_are_counted_separately_and_repeats_not_declared_useless(self):
        events = [event("workflow.started", 1, {"flow_id": "f", "workflow": "wf_finalize_preparation"}),
                  event("workflow.step.started", 2, {"flow_id": "f", "step": "readiness"}),
                  event("workflow.step.completed", 3, {"flow_id": "f", "step": "readiness"}),
                  event("workflow.completed", 4, {"flow_id": "f", "workflow": "wf_finalize_preparation"})]
        for at in (10, 20):
            events.extend([event("tool.started", at, {"tool_name": "read_context", "arguments": {"ref": "x"}}),
                           event("tool.completed", at + 2, {"tool_name": "read_context", "status": "SUCCEEDED"})])
        report = analyze(evidence(events))
        self.assertEqual(report["workflows"]["internal_step_count"], 1)
        self.assertEqual(report["tools"]["same_argument_repeat_count"], 1)
        self.assertIs(report["tools"]["repeat_groups"][0]["unnecessary_confirmed"], False)

    def test_failed_round_preserved_not_in_success_median(self):
        good = analyze(evidence())
        bad = analyze(evidence(status="FAILED"))
        result = compare([good, bad])
        self.assertEqual(result["round_count"], 2)
        self.assertEqual(result["successful_terminal_rounds"], 1)
        self.assertEqual(result["machine_seconds"]["median"], 100)
        self.assertIsNone(result["p95"])

    def test_inflight_sent_time_is_not_reported_as_zero_duration_or_final_unknown(self):
        pending = {"id": "pending", "run_id": "r", "role": "main", "state": "sent", "sent_at": 10,
                   "observed_at": 10, "provider_attempt_id": "p", "usage": None}
        data = evidence(attempts=[pending], status="RUNNING")
        data.objects[0].pop("closed_at")
        report = analyze(data, observed_end=50)
        row = report["requests"]["model"][0]
        self.assertEqual(row["duration_seconds"], 40)
        self.assertEqual(row["usage_state"], "pending")
        self.assertFalse(report["complete"])

    def test_multiple_sse_observations_do_not_become_multiple_model_requests(self):
        events = [event("model.response_observed", at, {"attempt_id": "provider-one", "usage": {"input_tokens": at}})
                  for at in range(11, 25)]
        attempts = [{"id": "actual-one", "run_id": "r", "role": "main", "state": "succeeded", "sent_at": 10,
                     "completed_at": 25, "provider_attempt_id": "provider-one",
                     "usage": {"input_tokens": 24, "output_tokens": 3, "total_tokens": 27}}]
        report = analyze(evidence(events, attempts))
        self.assertEqual(report["requests"]["model_actual_request_count"], 1)
        self.assertEqual(report["requests"]["by_role_and_purpose"][0]["known_usage_totals"]["total_tokens"], 27)


if __name__ == "__main__":
    unittest.main()
