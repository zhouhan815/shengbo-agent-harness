"""2026-10-22: formal script generation streams so a long thinking call is not cut at ~60 s."""
from __future__ import annotations

import json
import unittest

import httpx

from app.prelive.deepseek_options import STREAM_FIELDS, read_provider_body, streamed


def _sse(chunks):
    text = "".join(": keep-alive\n\n" if item is None else "data: " + (item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)) + "\n\n"
                   for item in chunks)
    return text.encode("utf-8")


CHUNKS = [
    {"id": "r1", "choices": [{"index": 0, "delta": {"reasoning_content": "想" * 5000}}]},
    None,
    {"id": "r1", "choices": [{"index": 0, "delta": {"content": "{\"modules\": "}}]},
    {"id": "r1", "choices": [{"index": 0, "delta": {"content": "[]}"}}]},
    {"id": "r1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    {"id": "r1", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    "[DONE]",
]


async def _read(content, headers, max_bytes=1000):
    async def handler(request):
        return httpx.Response(200, headers=headers, content=content)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with client.stream("POST", "https://example.test") as response:
            return await read_provider_body(response, max_bytes, lambda: ValueError("too_large"))


class StreamedScriptResponseTest(unittest.IsolatedAsyncioTestCase):
    def test_hashed_body_is_unchanged(self):
        body = {"model": "deepseek-v4-pro", "stream": False}
        sent = streamed(body)
        self.assertEqual(body, {"model": "deepseek-v4-pro", "stream": False})
        self.assertEqual(sent["stream"], True)
        self.assertEqual(sent["stream_options"], STREAM_FIELDS["stream_options"])

    async def test_sse_folds_to_the_non_streaming_shape_and_counts_answer_only(self):
        result, size = await _read(_sse(CHUNKS), {"content-type": "text/event-stream"}, max_bytes=100)
        self.assertEqual(result["choices"][0]["message"]["content"], "{\"modules\": []}")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(result["usage"]["total_tokens"], 15)
        self.assertEqual(result["id"], "r1")
        # 5000 reasoning characters and SSE framing do not count against the answer limit.
        self.assertEqual(size, len("{\"modules\": []}".encode("utf-8")))

    async def test_stream_without_finish_has_no_choices(self):
        result, _ = await _read(_sse(CHUNKS[:4]), {"content-type": "text/event-stream"})
        self.assertEqual(result["choices"], [])

    async def test_answer_limit_still_applies(self):
        with self.assertRaises(ValueError):
            await _read(_sse(CHUNKS), {"content-type": "text/event-stream"}, max_bytes=5)

    async def test_plain_json_response_still_parses(self):
        body = {"id": "r2", "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}
        result, size = await _read(json.dumps(body).encode(), {"content-type": "application/json"})
        self.assertEqual(result, body)
        self.assertEqual(size, len(json.dumps(body).encode()))


if __name__ == "__main__":
    unittest.main()
