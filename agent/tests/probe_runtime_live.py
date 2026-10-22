"""Explicit bounded live RuntimeEngine integration, using an empty fake business.

No existing user products, documents, speech APIs or live rooms are accessed.
Outputs and diagnostics contain status/schema counts only, never request bodies,
model prose, credentials, account/installation identifiers, or provider headers.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import tempfile

from shengbo_contracts.common import ContractError
from shengbo_prelive.models import MessageRequest
from shengbo_prelive.providers import build_chatgpt_model
from shengbo_prelive.runtime import RuntimeEngine
from shengbo_prelive.store import TERMINAL


class EmptyReadOnlyBusiness:
    def __init__(self):
        self.query_names = []
    async def query(self, key, args=None, **kwargs):
        self.query_names.append(key)
        if key == "read_capabilities":
            return {"contract_version": "PRELIVE-2.3", "adapter_configured": True, "live_agent_control": False,
                    "capabilities": ["整理商品资料与卖点", "分析顾客与使用场景", "配置本场主题及优惠", "准备模块化话术", "人工确认后生成语音", "素材需求清单"],
                    "human_only": ["approve_text_for_audio", "adopt_snapshot", "start_prepared_room"],
                    "note": "此验收业务库为空，只允许解释，不存在任何真实商品或本场。"}
        if key == "read_completion_observation":
            return {"pending_job_ids": [], "jobs": [], "candidate_ready": False, "products": [], "preparation": {}}
        if key in {"search_products", "read_session_history", "get_workspace_snapshot"}:
            return {"items": []}
        return {}
    async def command(self, *args, **kwargs):
        raise ContractError("PROBE_WRITE_FORBIDDEN", "只读验收不允许业务副作用", status=403)
    async def close(self):
        pass


async def probe(max_calls=8):
    calls = {"admitted": 0, "sent": 0}
    model_events = []
    def factory(**kwargs):
        trusted_gate, trusted_observer = kwargs.pop("before_send"), kwargs.pop("observer")
        def gate(payload):
            if calls["admitted"] >= max_calls:
                raise ContractError("PROBE_MODEL_CALL_LIMIT", "验收模型调用硬上限", status=429)
            calls["admitted"] += 1
            trusted_gate(payload)
        def observer(event):
            if event.get("type") == "model.sent":
                calls["sent"] += 1
            model_events.append({"type": event.get("type"), "code": event.get("code"), "http_status": event.get("http_status")})
            trusted_observer(event)
        return build_chatgpt_model(**kwargs, before_send=gate, observer=observer, timeout=100)
    with tempfile.TemporaryDirectory(prefix="shengbo-live-runtime-probe-") as directory:
        engine = RuntimeEngine(directory, EmptyReadOnlyBusiness(), model_factory=factory)
        try:
            await engine.start()
            conversation = engine.create_conversation({"title": "隔离只读验收"})
            result = await engine.submit_message(conversation["id"], MessageRequest(text="只解释播前助手能做什么。不要更新资料，不要调研，不生成稿件或语音。", client_request_id="isolated-runtime-probe"))
            run_id = result["run"]["id"]
            async def wait():
                while True:
                    run = engine.get_run(run_id)
                    if run["status"] in TERMINAL | {"WAITING_USER"}:
                        return run
                    await asyncio.sleep(.3)
            run = await asyncio.wait_for(wait(), timeout=150)
            events = []
            cursor = 0
            while True:
                page = engine.events(run_id, cursor, 200)
                events.extend(page["events"])
                if page["next_cursor"] == cursor:
                    break
                cursor = page["next_cursor"]
            counts = dict(Counter(e["type"] for e in events))
            tool_names = [e["data"].get("tool_name") for e in events if e["type"] == "tool.started"]
            with engine.store.tx() as connection:
                attempts = [json.loads(row[0])["state"] for row in connection.execute("SELECT body FROM attempts WHERE run_id=?", (run_id,))]
            return {"ok": run["status"] == "COMPLETED", "status": run["status"], "model_calls": calls["sent"],
                    "admitted_calls": calls["admitted"], "framework": "Deep Agents RuntimeEngine", "event_type_counts": counts,
                    "attempt_states": attempts, "query_names": engine.client.query_names,
                    "tool_names": tool_names,
                    "error_code": run.get("error", {}).get("code"), "acceptance_ok": run.get("acceptance", {}).get("ok"),
                    "trace_payloads_recorded": counts.get("model.request", 0) > 0 and counts.get("model.response", 0) > 0}
        except BaseException as error:
            return {"ok": False, "error_code": getattr(error, "code", type(error).__name__), "model_calls": calls["sent"],
                    "admitted_calls": calls["admitted"], "provider_event_types": [item["type"] for item in model_events],
                    "provider_error_codes": [item["code"] for item in model_events if item["code"]]}
        finally:
            await engine.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--max-calls", type=int, choices=range(1, 13), default=8)
    args = parser.parse_args()
    if not args.confirm_live:
        print(json.dumps({"ok": False, "code": "explicit_live_flag_required"}))
        return 2
    result = asyncio.run(probe(args.max_calls))
    destination = Path(__file__).resolve().parents[1] / ".runtime" / "runtime_live_probe_result.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
