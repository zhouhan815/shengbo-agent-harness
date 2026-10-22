"""Reporting-only usage visibility: partial observation is never full usage."""
from copy import deepcopy
import unittest

from test_timing_report_v1 import evidence, event
from shengbo_prelive.timing_report import analyze, markdown


def attempt(identity, usage=None, *, state="succeeded", role="main", purpose="agent", **extra):
    value = {"id": identity, "run_id": "r", "provider_attempt_id": "provider-" + identity,
        "role": role, "purpose": purpose, "state": state, "sent_at": 10, "usage": usage}
    if state not in {"sent", "reserved"}:
        value["completed_at"] = 20
    return value | extra


class UsageClassificationTests(unittest.TestCase):
    def assert_partition(self, group):
        self.assertEqual(group["actual_request_count"], sum(group[key] for key in (
            "known_usage_count", "partial_usage_count", "pending_usage_count", "unknown_usage_count")))
        self.assertEqual(group["observed_usage_count"], group["known_usage_count"] + group["partial_usage_count"])

    def test_mixed_seven_requests_are_exclusive_and_partial_counts_remain_observed(self):
        attempts = [
            attempt("full-1", {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}),
            attempt("full-2", {"input_tokens": 200, "output_tokens": 30, "total_tokens": 230},
                    state="known_failed", content_status="rejected"),
            attempt("partial-1", {"input_tokens": 9}, state="outcome_unknown"),
            attempt("partial-2", {"input_tokens": 4}),
            attempt("pending", state="sent"),
            attempt("missing"), attempt("unknown", state="outcome_unknown"),
        ]
        data = evidence(attempts=attempts)
        original = deepcopy(attempts)
        report = analyze(data)
        self.assertEqual(report["schema_version"], "timing-v1.1")
        group = report["requests"]["by_role_and_purpose"][0]
        for key, count in {"known_usage_count": 2, "partial_usage_count": 2,
                           "pending_usage_count": 1, "unknown_usage_count": 2,
                           "observed_usage_count": 4, "actual_request_count": 7}.items():
            self.assertEqual(group[key], count, key)
        self.assert_partition(group)
        self.assertEqual(group["known_usage_totals"], {"input_tokens": 300, "output_tokens": 50, "total_tokens": 350})
        self.assertEqual(group["partial_usage_totals"], {"input_tokens": 13})
        self.assertEqual(group["observed_usage_totals"], {"input_tokens": 313, "output_tokens": 50, "total_tokens": 350})
        self.assertEqual(attempts, original)
        rejected = next(row for row in report["requests"]["model"] if row["id"] == "full-2")
        self.assertEqual(rejected["state"], "known_failed")
        self.assertEqual(rejected["usage_state"], "known")
        self.assertEqual(report["overall"]["wall_seconds"], 100)
        self.assertEqual(report["requests"]["model_actual_request_count"], 7)

    def test_usage_observation_precedes_local_state_completion(self):
        attempts = [attempt("full", {"input_tokens": 2, "output_tokens": 0, "total_tokens": 2}, state="sent"),
                    attempt("partial", {"input_tokens": 0}, state="sent")]
        report = analyze(evidence(attempts=attempts))
        self.assertEqual([row["usage_state"] for row in report["requests"]["model"]], ["known", "partial"])
        group = report["requests"]["by_role_and_purpose"][0]
        self.assertEqual(group["pending_usage_count"], 0)
        self.assertEqual(group["observed_usage_count"], 2)
        self.assert_partition(group)

    def test_terminal_provider_or_completed_timestamp_is_not_inflight_pending(self):
        for extra in ({"response_status": "completed"}, {"response_status": "failed"},
                      {"response_status": "incomplete"}, {"completed_at": 20}):
            with self.subTest(extra=extra):
                report = analyze(evidence(attempts=[attempt("terminal", state="sent", **extra)]))
                group = report["requests"]["by_role_and_purpose"][0]
                self.assertEqual(group["pending_usage_count"], 0)
                self.assertEqual(group["unknown_usage_count"], 1)
                self.assertEqual(group["known_usage_totals"], {})
                self.assert_partition(group)

    def test_invalid_values_never_count_as_observed_tokens_or_infer_missing_fields(self):
        cases = [(None, "unknown", {}), ({}, "unknown", {}),
            ({"input_tokens": None, "output_tokens": True, "total_tokens": -3}, "unknown", {}),
            ({"input_tokens": "9", "output_tokens": 1.0, "total_tokens": False}, "unknown", {}),
            ({"input_tokens": 9, "output_tokens": None, "total_tokens": "9"}, "partial", {"input_tokens": 9}),
            ({"total_tokens": 12}, "partial", {"total_tokens": 12}),
            ({"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, "known",
             {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}),
            ({"some_other_count": 20}, "unknown", {}),
            (["input_tokens", "output_tokens", "total_tokens"], "unknown", {})]
        for usage, expected_state, observed in cases:
            with self.subTest(usage=usage):
                report = analyze(evidence(attempts=[attempt("invalid", usage)]))
                self.assertEqual(report["requests"]["model"][0]["usage_state"], expected_state)
                group = report["requests"]["by_role_and_purpose"][0]
                self.assertEqual(group["observed_usage_totals"], observed)
                self.assertEqual(group["known_usage_totals"], observed if expected_state == "known" else {})
                self.assert_partition(group)

    def test_groups_and_final_snapshot_do_not_double_count_sse_observations(self):
        events = [event("model.response_observed", t, {"attempt_id": "provider-one", "usage": {"input_tokens": t}})
                  for t in range(11, 25)]
        attempts = [attempt("one", {"input_tokens": 24, "output_tokens": 3, "total_tokens": 27}),
                    attempt("summary", {"output_tokens": 4}, purpose="summary"),
                    attempt("audience", state="sent", role="audience_scenes")]
        report = analyze(evidence(events, attempts))
        groups = report["requests"]["by_role_and_purpose"]
        self.assertEqual(len(groups), 3)
        self.assertEqual(sum(row["actual_request_count"] for row in groups), 3)
        self.assertEqual(sum(row["known_usage_count"] for row in groups), 1)
        self.assertEqual(sum(row["partial_usage_count"] for row in groups), 1)
        self.assertEqual(sum(row["pending_usage_count"] for row in groups), 1)
        self.assertEqual(report["requests"]["model_actual_request_count"], 3)
        for group in groups:
            self.assert_partition(group)
        normal = next(row for row in groups if row["role"] == "main" and row["kind"] == "agent")
        self.assertEqual(normal["observed_usage_totals"]["total_tokens"], 27)

    def test_all_pending_or_all_partial_do_not_become_unknown_or_complete(self):
        for state, usage, category in (("sent", None, "pending"), ("outcome_unknown", {"input_tokens": 9}, "partial")):
            with self.subTest(category=category):
                report = analyze(evidence(attempts=[attempt("one", usage, state=state), attempt("two", usage, state=state)]))
                group = report["requests"]["by_role_and_purpose"][0]
                self.assertEqual(group[category + "_usage_count"], 2)
                self.assertEqual(group["known_usage_count"], 0)
                self.assertEqual(group["unknown_usage_count"], 0)
                self.assertEqual(group["known_usage_totals"], {})
                self.assert_partition(group)

    def test_reservations_not_sent_keep_separate_accounting(self):
        attempts = [{"id": "reserved", "run_id": "r", "state": "reserved", "reserved_at": 1},
                    {"id": "aborted", "run_id": "r", "state": "aborted_before_send", "reserved_at": 2,
                     "provider_attempt_id": "provider-not-sent"},
                    attempt("actual", state="sent")]
        report = analyze(evidence(attempts=attempts))
        self.assertEqual(report["requests"]["model_reserved_count"], 3)
        self.assertEqual(report["requests"]["model_reserved_not_sent_count"], 2)
        self.assertEqual(report["requests"]["model_actual_request_count"], 1)
        self.assertEqual(report["requests"]["by_role_and_purpose"][0]["pending_usage_count"], 1)

    def test_markdown_exposes_four_categories_and_observation_separately(self):
        report = analyze(evidence(attempts=[attempt("partial", {"input_tokens": 9}), attempt("pending", state="sent")]))
        text = markdown(report)
        for label in ("完整 known", "部分 partial", "在途 pending", "结束未知 unknown", "已观测 known+partial"):
            self.assertIn(label, text)
        self.assertIn("| main / agent | 2 | 0 | 1 | 1 | 0 | 1 |", text)
        self.assertIn("缺失字段不补零", text)


if __name__ == "__main__":
    unittest.main()
