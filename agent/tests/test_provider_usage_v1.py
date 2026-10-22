"""S05 network-free regression: response observation precedes local acceptance."""
import asyncio
import json
import unittest

import httpx

from shengbo_prelive.providers.chatgpt import build_chatgpt_model, ModelProviderError


class Credentials:
    def get_access_token(self):
        return "fake-test-token-never-record"


def stream(events):
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                          text="".join("data: " + json.dumps(e) + "\n\n" for e in events))


class UsageObservationTests(unittest.TestCase):
    def model(self, events):
        self.events, self.responses = [], []
        return build_chatgpt_model(credentials=Credentials(), verify_catalog=False,
            observer=self.events.append, on_response=self.responses.append,
            transport=httpx.MockTransport(lambda _: stream(events)))

    def terminal(self, output, status="completed", usage=None):
        return [{"type": "response." + status, "response": {"id": "resp_fixture", "status": status,
            "output": output, "usage": usage if usage is not None else {"input_tokens": 13, "output_tokens": 7, "total_tokens": 20}}}]

    def test_usage_and_public_output_saved_before_invalid_call_acceptance(self):
        output = [{"type": "reasoning", "encrypted_content": "private-fixture", "summary": [{"text": "private-fixture"}]},
                  {"type": "function_call", "name": "fixture", "namespace": "functions", "call_id": "call_fixture", "arguments": "[]"}]
        model = self.model(self.terminal(output))
        with self.assertRaisesRegex(ModelProviderError, "model_tool_call_invalid"):
            model.invoke("test")
        self.assertEqual(self.events[-1]["usage"]["total_tokens"], 20)
        self.assertFalse(self.events[-1]["outcome_unknown"])
        self.assertIsNone(self.responses[0]["output"][0]["arguments"])
        self.assertTrue(self.responses[0]["output"][0]["arguments_diagnostic"]["omitted"])
        self.assertNotIn("private-fixture", json.dumps([self.events, self.responses]))
        self.assertEqual(self.events[-1]["content_status"], "rejected")

    def test_empty_completed_response_usage_recorded(self):
        model = self.model(self.terminal([]))
        with self.assertRaisesRegex(ModelProviderError, "model_empty_completed_response"):
            model.invoke("test")
        self.assertEqual(self.responses[0]["usage"]["total_tokens"], 20)
        self.assertEqual(self.events[-1]["usage"]["total_tokens"], 20)

    def test_usage_seen_before_disconnection_retained_and_outcome_still_unknown(self):
        model = self.model([{"type": "response.usage", "usage": {"input_tokens": 9}}])
        with self.assertRaisesRegex(ModelProviderError, "model_stream_interrupted"):
            model.invoke("test")
        self.assertEqual(self.events[-1]["usage"], {"input_tokens": 9})
        self.assertTrue(self.events[-1]["outcome_unknown"])

    def test_failed_terminal_has_usage_and_no_false_success(self):
        model = self.model(self.terminal([], "failed"))
        with self.assertRaises(ModelProviderError):
            model.invoke("test")
        self.assertEqual(self.events[-1]["usage"]["total_tokens"], 20)
        self.assertFalse(self.events[-1]["outcome_unknown"])
        self.assertNotIn("model.completed", [x["type"] for x in self.events])

    def test_missing_or_partial_usage_not_filled_with_zero(self):
        output = [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]
        for usage in ({}, {"input_tokens": 9}, {"input_tokens": True, "output_tokens": -1}):
            result = self.model(self.terminal(output, usage=usage)).invoke("test")
            self.assertIsNone(result.usage_metadata)
            self.assertNotIn("total_tokens", self.responses[0].get("usage") or {})

    def test_async_rejection_retains_usage(self):
        async def run():
            model = self.model(self.terminal([{"type": "function_call", "arguments": "invalid"}]))
            with self.assertRaises(ModelProviderError):
                await model.ainvoke("test")
            self.assertEqual(self.events[-1]["usage"]["total_tokens"], 20)
        asyncio.run(run())

    def test_invalid_argument_diagnostics_never_keep_arbitrary_raw_string(self):
        secret = "unstructured-private-diagnostic-text-not-a-known-token-shape"
        model = self.model(self.terminal([{"type": "function_call", "name": "fixture", "namespace": "functions",
            "call_id": "fixture", "arguments": secret}]))
        with self.assertRaisesRegex(ModelProviderError, "model_tool_call_invalid"):
            model.invoke("test")
        public = json.dumps([self.events, self.responses], ensure_ascii=False)
        self.assertNotIn(secret, public)
        diagnostic = self.responses[0]["output"][0]["arguments_diagnostic"]
        self.assertEqual(diagnostic["characters"], len(secret))
        self.assertEqual(len(diagnostic["sha256"]), 64)
        self.assertEqual(self.events[-1]["usage"]["total_tokens"], 20)

    def test_response_observation_happens_before_result_hook_and_acceptance(self):
        order = []
        model = self.model(self.terminal([{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]))
        model._observer = lambda event: order.append(event["type"])
        model._on_response = lambda event: order.append("result_hook")
        model.invoke("test")
        self.assertLess(order.index("model.response_observed"), order.index("result_hook"))
        self.assertLess(order.index("result_hook"), order.index("model.completed"))


if __name__ == "__main__":
    unittest.main()
