"""Network-free provider contract tests. All strings labelled fake are fake."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
import unittest

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from shengbo_prelive.providers.chatgpt import build_chatgpt_model, ModelProviderError
from shengbo_prelive.providers.credentials import CredentialManager, CredentialError, PLAN_SCOPE


class FakeCredentials:
    def get_access_token(self, force_refresh=False):
        return "fake-access-credential"


def sse_response(output=None, *, terminal="response.completed", status="completed", usage=None):
    output = output if output is not None else [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "已完成"}]}]
    events = [{"type": "response.output_text.delta", "delta": "provisional"}]
    if terminal:
        events.append({"type": terminal, "response": {"status": status, "output": output, "usage": usage or {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}}})
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                          text="".join("data: " + json.dumps(e) + "\n\n" for e in events))


class ProviderContractTests(unittest.TestCase):
    def model(self, handler=None, **kwargs):
        self.requests = []
        def respond(request):
            self.requests.append(request)
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"models": [{"slug": "gpt-6.1-sol", "visibility": "list"}]})
            return handler(request) if handler else sse_response()
        return build_chatgpt_model(credentials=FakeCredentials(), transport=httpx.MockTransport(respond), **kwargs)

    def test_restricted_payload_preserves_all_system_instructions_and_full_history(self):
        model = self.model()
        result = model.invoke([SystemMessage("business rules"), SystemMessage("framework skills"), HumanMessage("hello")])
        payload = json.loads(self.requests[-1].content)
        self.assertEqual(payload["input"][0]["role"], "developer")
        self.assertEqual([x["content"] for x in payload["input"][:2]], ["business rules", "framework skills"])
        self.assertIs(payload["store"], False)
        self.assertIs(payload["stream"], True)
        self.assertEqual(result.content, "已完成")
        self.assertTrue(result.response_metadata["response_completed"])
        self.assertFalse({"temperature", "metadata", "previous_response_id", "max_output_tokens"} & set(payload))

    def test_namespace_tool_roundtrip_ids_and_json_arguments(self):
        @tool
        def multiply(a: int, b: int) -> int:
            """Multiply two integers."""
            return a * b
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                return sse_response([{"type": "function_call", "namespace": "functions", "name": "multiply", "call_id": "call_test", "arguments": '{"a":6,"b":7}'}])
            payload = json.loads(request.content)
            self.assertEqual(payload["input"][-1], {"type": "function_call_output", "call_id": "call_test", "output": "42"})
            self.assertEqual(payload["input"][-2]["call_id"], "call_test")
            return sse_response()
        model = self.model(handler).bind_tools([multiply])
        first = model.invoke([HumanMessage("calculate")])
        self.assertEqual(first.tool_calls[0]["args"], {"a": 6, "b": 7})
        result = model.invoke([HumanMessage("calculate"), first, ToolMessage("42", tool_call_id="call_test")])
        self.assertTrue(result.response_metadata["response_completed"])
        body = json.loads(self.requests[-1].content)
        self.assertEqual(body["tools"][0]["type"], "namespace")
        self.assertEqual(body["tools"][0]["tools"][0]["name"], "multiply")

    def test_uncompleted_stream_never_emits_success_or_chunk(self):
        for terminal, status in [(None, "completed"), ("response.incomplete", "incomplete"), ("response.failed", "failed"), ("response.completed", "incomplete")]:
            events = []
            model = self.model(lambda _: sse_response(terminal=terminal, status=status), observer=events.append)
            with self.assertRaises(ModelProviderError):
                list(model.stream("test"))
            self.assertNotIn("model.completed", [e["type"] for e in events])

    def test_empty_completed_output_reconstructed_from_done_events(self):
        def wire(events):
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  text="".join("data: " + json.dumps(e) + "\n\n" for e in events))
        cases = [
            ([{"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "实际文本"},
              {"type": "response.completed", "response": {"status": "completed", "output": []}}], "实际文本"),
            ([{"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "namespace": "functions", "name": "local_tool", "call_id": "call_regression", "arguments": ""}},
              {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"value":7}'},
              {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": '{"value":7}'},
              {"type": "response.completed", "response": {"status": "completed", "output": []}}], None)]
        for events, expected_text in cases:
            result = self.model(lambda _, events=events: wire(events)).invoke("test")
            self.assertTrue(result.response_metadata["response_completed"])
            if expected_text:
                self.assertEqual(result.content, expected_text)
            else:
                self.assertEqual(result.tool_calls[0]["args"], {"value": 7})
                self.assertEqual(result.tool_calls[0]["id"], "call_regression")

    def test_async_and_streaming_paths(self):
        async def run():
            model = self.model()
            result = await model.ainvoke("test")
            self.assertEqual(result.content, "已完成")
            chunks = [c async for c in model.astream("test")]
            self.assertTrue(chunks[0].response_metadata["response_completed"])
        asyncio.run(run())
        self.assertEqual("".join(c.content for c in self.model().stream("test")), "已完成")

    def test_credentials_are_private_and_observer_never_receives_headers(self):
        events, requests, responses = [], [], []
        model = self.model(observer=events.append, on_request=requests.append, on_response=responses.append)
        model.invoke("hello")
        recorded = json.dumps([model.model_dump(), events, requests, responses], default=str)
        self.assertNotIn("fake-access-credential", recorded)
        self.assertNotIn("Authorization", recorded)

    def test_budget_gate_prevents_all_network_and_unsupported_options_fail_closed(self):
        def deny(payload):
            self.assertTrue(payload["input"])
            raise RuntimeError("budget_closed")
        model = self.model(before_send=deny)
        with self.assertRaisesRegex(RuntimeError, "budget_closed"):
            model.invoke("test")
        self.assertEqual(len(self.requests), 0)
        with self.assertRaises(ModelProviderError):
            self.model().invoke("test", temperature=0.2)

    def test_catalog_missing_model_does_not_fallback(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"models": [{"slug": "other-model"}]})
        model = build_chatgpt_model(credentials=FakeCredentials(), transport=httpx.MockTransport(handler))
        with self.assertRaisesRegex(ModelProviderError, "requested_model_not_authorized"):
            model.invoke("hello")
        self.assertEqual(len(requests), 1)

    def test_http_error_and_network_error_never_leak_body_or_token(self):
        model = self.model(lambda _: httpx.Response(401, json={"error": "fake-access-credential"}))
        with self.assertRaises(ModelProviderError) as context:
            model.invoke("hi")
        self.assertNotIn("fake-access-credential", str(context.exception))
        self.assertEqual(context.exception.http_status, 401)

    def test_invalid_tool_args_blocked(self):
        model = self.model(lambda _: sse_response([{"type": "function_call", "namespace": "functions", "name": "x", "call_id": "call_bad", "arguments": "[]"}]))
        with self.assertRaisesRegex(ModelProviderError, "model_tool_call_invalid"):
            model.invoke("hi")

    def test_image_material_data_url_conversion_and_remote_path_rejection(self):
        # Synthetic signature payload is sufficient for wire conversion testing.
        data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
        model = self.model()
        model.invoke([HumanMessage(content=[{"type": "text", "text": "识别image1商品事实"},
                                          {"type": "image_url", "image_url": {"url": data_url}}])])
        content = json.loads(self.requests[-1].content)["input"][0]["content"]
        self.assertEqual(content[1], {"type": "input_image", "image_url": data_url})
        for unsafe in ["C:/Users/example/file.png", "https://example.test/img?access_token=fake", "file:///tmp/pic.png", "data:image/svg+xml;base64,PHN2Zz4="]:
            with self.assertRaises(ModelProviderError):
                model.invoke([HumanMessage(content=[{"type": "image_url", "image_url": {"url": unsafe}}])])

    def test_image_bytes_not_mistaken_for_text_budget(self):
        data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 600000).decode()
        model = self.model(max_input_chars=1000)
        result = model.invoke([HumanMessage(content=[{"type": "image_url", "image_url": {"url": data_url}}])])
        self.assertTrue(result.response_metadata["response_completed"])

    def test_minimal_deep_agent_mock_executes_real_local_tool(self):
        from deepagents import create_deep_agent
        calls = []
        @tool
        def verified_sum(a: int, b: int) -> int:
            """Compute a verified sum."""
            calls.append((a, b))
            return a + b
        def respond(request):
            body = json.loads(request.content)
            if any(x.get("type") == "function_call_output" for x in body["input"]):
                return sse_response()
            return sse_response([{"type": "function_call", "namespace": "functions", "name": "verified_sum", "call_id": "call_graph", "arguments": '{"a":2,"b":3}'}])
        agent = create_deep_agent(model=self.model(respond), tools=[verified_sum], system_prompt="Use verified_sum.")
        result = agent.invoke({"messages": [{"role": "user", "content": "2+3"}]})
        self.assertEqual(calls, [(2, 3)])
        self.assertEqual(result["messages"][-1].content, "已完成")


class FakeVault:
    def __init__(self):
        self.value = {"subject": "fake-subject", "client_id": "oaiapp_fake",
                      "access_token": "fake-old-access", "refresh_token": "fake-old-refresh",
                      "expires_at": 0, "scopes": [PLAN_SCOPE]}
        self.saves = 0
    def load(self):
        return dict(self.value)
    def save(self, value):
        self.value = dict(value)
        self.saves += 1


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FakeVault()
    def tearDown(self):
        self.temporary.cleanup()
    def manager(self, handler):
        return CredentialManager(self.root, vault=self.vault, transport=httpx.MockTransport(handler), clock=lambda: 1000)
    def test_valid_record_is_read_without_refresh(self):
        self.vault.value["expires_at"] = 2000
        manager = self.manager(lambda _: self.fail("unexpected request"))
        self.assertEqual(manager.get_access_token(), "fake-old-access")
        self.assertNotIn("fake-old", repr(manager))
    def test_thread_competition_reads_rotated_record_inside_lock(self):
        posts = []
        def handler(request):
            posts.append(request)
            time.sleep(0.05)
            return httpx.Response(200, json={"access_token": "fake-new-access", "refresh_token": "fake-new-refresh", "expires_in": 3600})
        managers = [self.manager(handler) for _ in range(6)]
        with ThreadPoolExecutor(max_workers=6) as executor:
            values = list(executor.map(lambda m: m.get_access_token(), managers))
        self.assertEqual(values, ["fake-new-access"] * 6)
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.vault.value["refresh_token"], "fake-new-refresh")
        self.assertEqual(self.vault.saves, 2)  # durable send marker + rotation
    def test_rotation_network_unknown_preserves_original_and_no_retry(self):
        calls = []
        def handler(request):
            calls.append(1)
            raise httpx.ReadTimeout("secret fake-old-refresh", request=request)
        with self.assertRaisesRegex(CredentialError, "credential_refresh_outcome_unknown"):
            self.manager(handler).get_access_token()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.vault.saves, 1)
        self.assertTrue(self.vault.value["refresh_in_flight"])
        with self.assertRaisesRegex(CredentialError, "credential_refresh_reconciliation_required"):
            self.manager(handler).get_access_token()
        self.assertEqual(len(calls), 1)
    def test_invalid_grant_marked_terminal_without_clearing_identity(self):
        manager = self.manager(lambda _: httpx.Response(400, json={"error": "invalid_grant"}))
        with self.assertRaises(CredentialError):
            manager.get_access_token()
        self.assertTrue(self.vault.value["terminal_auth_error"])
        self.assertEqual(self.vault.value["subject"], "fake-subject")
        with self.assertRaisesRegex(CredentialError, "chatgpt_reauthorization_required"):
            manager.get_access_token()
    def test_permission_loss_saves_rotated_token_and_blocks(self):
        manager = self.manager(lambda _: httpx.Response(200, json={"access_token": "fake-new", "refresh_token": "fake-rotated", "expires_in": 3600, "scope": "openid"}))
        with self.assertRaisesRegex(CredentialError, "chatgpt_plan_permission_missing"):
            manager.get_access_token()
        self.assertEqual(self.vault.value["refresh_token"], "fake-rotated")
    def test_bounded_definite_transient_retry(self):
        posts = []
        def handler(request):
            posts.append(1)
            return httpx.Response(503, json={})
        with self.assertRaisesRegex(CredentialError, "credential_refresh_temporarily_unavailable"):
            self.manager(handler).get_access_token()
        self.assertEqual(len(posts), 2)
        self.assertEqual(self.vault.saves, 2)


class FileFakeVault:
    """Plaintext is confined to test tempdir and contains fake values only."""
    def __init__(self, root):
        self.path = Path(root) / "fake-record.json"
    def load(self):
        return json.loads(self.path.read_text())
    def save(self, record):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record))
        temporary.replace(self.path)


def _cross_process_refresh(root, results):
    def handler(request):
        with (Path(root) / "fake-network-count.txt").open("a") as count:
            count.write("post\n")
        time.sleep(0.1)
        return httpx.Response(200, json={"access_token": "fake-new", "refresh_token": "fake-rotated", "expires_in": 3600})
    manager = CredentialManager(root, vault=FileFakeVault(root), transport=httpx.MockTransport(handler), clock=lambda: 1000)
    try:
        results.put(manager.get_access_token() == "fake-new")
    except Exception:
        results.put(False)


class ProcessLockTests(unittest.TestCase):
    def test_cross_process_refresh_is_serialized(self):
        with tempfile.TemporaryDirectory() as root:
            FileFakeVault(root).save(FakeVault().load())
            context = multiprocessing.get_context("spawn")
            results = context.Queue()
            processes = [context.Process(target=_cross_process_refresh, args=(root, results)) for _ in range(3)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(20)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            self.assertEqual([results.get(timeout=1) for _ in processes], [True] * 3)
            self.assertEqual((Path(root) / "fake-network-count.txt").read_text().splitlines(), ["post"])


if __name__ == "__main__":
    unittest.main()
