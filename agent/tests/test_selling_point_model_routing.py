"""Fast selling-point routing with actual DeepSeek adapter over an isolated transport."""
import asyncio
import json

import httpx
import pytest

from shengbo_contracts.business import AnalysisReport
from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import MessageRequest, RunScope
from shengbo_prelive.providers.deepseek import DeepSeekAgentModel
from shengbo_prelive.selling_point_extract import review_selling_points
from shengbo_prelive.store import TERMINAL
from test_runtime_business import Product, env, human_command, new_run


def _response(sequence, value):
    chunks = [
        {"id": f"selling-routing-{sequence}", "choices": [{"delta": {"content": json.dumps(value, ensure_ascii=False)}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 40, "total_tokens": 70}},
    ]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
        text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n")


def _use_wire(engine, responder):
    engine.model = DeepSeekAgentModel(model_name="deepseek-flash", async_transport=httpx.MockTransport(responder),
        key_reader=lambda: "offline-fixture", before_send=engine._before_send, observer=engine._model_event,
        on_request=engine._model_request, on_response=engine._model_response)


async def _seed_product(service):
    fields = Product(name="隔离保冷袋", price=69.9, sku_id="ROUTING-12",
        specifications=[{"name": "标称容量", "value": "12 升"}, {"name": "建议手提负重", "value": "不超过 5 千克"}]).model_dump()
    saved = await human_command(service, "save_product", {
        "fields": fields, "schema_hash": service.schema()["schema_hash"], "patches": []})
    ref = saved["data"]["product_ref"]
    research = AnalysisReport(summary="通勤者需要装餐盒后手提出行", product_ids=[ref["id"]],
        dependencies=[ref]).model_dump(mode="json")
    await human_command(service, "accept_analysis_report", {"role": "audience_scenes", "report": research,
        "report_hash": canonical_hash(research), "product_refs": [ref]})
    return ref


async def _submit(engine, ref):
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    reply = await engine.submit_message(conversation["id"], MessageRequest(
        text="为选中商品生成并保存核心卖点", intent_mode="new_task", client_request_id="selling-routing-fixture",
        observed_versions=[ref], selection={"target": {"object_ref": ref, "field_path": "/core_selling_points"}},
        view_context={"view": "products", "selling_points_action": "generate_and_save"}))
    identity = reply["run"]["id"]
    async with asyncio.timeout(10):
        while (run := engine.get_run(identity))["status"] not in TERMINAL:
            await asyncio.sleep(.025)
    return run


@pytest.mark.parametrize("case", ["normal", "targeted", "unsafe", "scoped_rewrite", "scoped_malformed", "style_fallback"])
async def test_fast_selling_points_routes_models_and_keeps_fact_gate(env, case):
    engine, service = env
    ref = await _seed_product(service)
    requests = []
    texts = ["容量清楚：标称容量 12 升，适合携带通勤餐盒。",
             "手提负重：建议手提不超过 5 千克。"]

    def respond(request):
        wire = json.loads(request.content)
        inputs = json.loads(wire["messages"][-1]["content"])
        requests.append((wire, inputs))
        assert wire["response_format"] == {"type": "json_object"}
        if len(requests) == 1:
            facts = {value["fact"]: identity for identity, value in inputs["evidence_catalog"].items()}
            draft = []
            for text, fact in zip(texts, ("12 升", "不超过 5 千克")):
                title, copy = text.split("：", 1)
                draft.append({"title": title, "copy": copy, "audience_need": "通勤带饭", "use_scene": "手提外出",
                    "evidence": [{"evidence_id": facts[fact]}]})
            return _response(1, {"items": draft})

        if len(requests) == 2:
            assert [row["index"] for row in inputs["candidates"]] == [0, 1]
            first_id, second_id = [row["point_id"] for row in inputs["candidates"]]
            second = {"point_id": second_id, "verdict": "keep"}
            if case == "scoped_rewrite":
                second.update(verdict="rewrite", reason="weak_value", title="手提有个量")
            elif case == "scoped_malformed":
                second.update(verdict="rewrite", reason="weak_value", title="手提有个量", copy=[])
            elif case == "style_fallback":
                second = {"point_id": second_id, "fact_verdict": "pass", "style_rewrite": {
                    "title": "轻松提起 10 千克", "copy": "外出轻松手提 10 千克。", "fact_verified": True}}
            elif case != "normal":
                second.update(verdict="uncertain", uncertainty_reason="需核对负重是否限于手提")
            return _response(2, {"items": [{"point_id": first_id, "verdict": "keep"}, second]})

        assert len(requests) == 3 and case != "normal"
        candidate, = inputs["candidates"]
        assert candidate["index"] == 1
        assert candidate["point_id"] == requests[1][1]["candidates"][1]["point_id"]
        assert candidate["facts"] == requests[1][1]["candidates"][1]["facts"]
        decision = {"point_id": candidate["point_id"], "verdict": "keep"}
        if case == "unsafe":
            decision.update(verdict="rewrite", unsupported_claims=["不超过 5 千克"],
                            selling_point="手提负重：轻松手提 10 千克。")
        if case in {"scoped_rewrite", "scoped_malformed"}:
            assert candidate["retry_reason"] == ("invalid_rewrite" if case == "scoped_rewrite" else "uncertain")
            decision.update(verdict="rewrite", reason="weak_value", title="手提有个量",
                            copy="通勤装好餐盒后手提外出，建议负重不超过 5 千克。")
        else:
            assert candidate["uncertainty_reason"]
        return _response(3, {"items": [decision]})

    await engine.start()
    _use_wire(engine, respond)
    run = await _submit(engine, ref)
    assert run["status"] == "COMPLETED", run.get("error")
    actual = [(wire["model"], wire["thinking"]["type"], wire.get("reasoning_effort")) for wire, _ in requests]
    expected = [("deepseek-flash", "disabled", None), ("deepseek-flash", "disabled", None)]
    if case not in {"normal", "style_fallback"}:
        expected.append(("deepseek-flash", "enabled", "low"))
    assert actual == expected
    saved = await service.query("read_product", {"product_id": ref["id"]})
    expected_texts = texts[:1] if case == "unsafe" else texts
    if case in {"scoped_rewrite", "scoped_malformed"}:
        expected_texts = [texts[0], "手提有个量：通勤装好餐盒后手提外出，建议负重不超过 5 千克。"]
    assert saved["body"]["core_selling_points"] == expected_texts
    assert saved["ref"]["revision"] == ref["revision"] + 1
    report, = run["selling_points_result"]["products"]
    assert (report["product_ref"], report["name"], report["items"]) == (saved["ref"], saved["body"]["name"], expected_texts)
    counts = {key: value for key, value in report["review"].items() if key != "decisions"}
    assert counts == {"candidate_count": 2, "saved_count": len(expected_texts), "rejected_count": 2 - len(expected_texts)}
    first_decision, second_decision = report["review"]["decisions"]
    assert first_decision["outcome"] == "saved" and first_decision["position"] == 1
    assert second_decision["position"] == 2 and second_decision["message"]
    if case == "style_fallback":
        assert second_decision["reason"] == "style_unusable" and not second_decision["rechecked"]
    if case == "unsafe":
        assert second_decision["outcome"] == "omitted" and second_decision["reason"] == "uncited_number"
    operations = [row for row in engine.store.list("operation") if row["run_id"] == run["id"]]
    assert [(row["key"], row["status"]) for row in operations] == [("save_product", "committed")]
    events = engine.events(run["id"], 0, 100)["events"]
    limits = [event["data"]["model_call_limit"] for event in events if event["type"] == "model.window_checked"]
    assert limits == ([3, 3] if case in {"normal", "style_fallback"} else [3, 3, 4])
    escalations = [event["data"]["item_count"] for event in events if event["type"] == "selling_points.review_escalated"]
    assert escalations == ([] if case in {"normal", "style_fallback"} else [1])
    omissions = [event["data"] for event in events if event["type"] == "selling_points.review_result"]
    if case == "unsafe":
        omission, = omissions
        assert omission["accepted_count"] == omission["rejected_count"] == 1
        assert omission["issues"][0]["reason"] == "uncited_number"
    else:
        assert not omissions


async def test_review_timeout_does_not_invent_a_provider_response_or_replay(env):
    engine, _ = env
    run = await new_run(engine, "复核卖点事实")
    engine.store.update("run", run["id"], phase="execute", status="RUNNING")
    sends = []

    async def wait_for_response(request):
        sends.append(request)
        await asyncio.sleep(10)

    _use_wire(engine, wait_for_response)
    with pytest.raises(ContractError) as timeout:
        await review_selling_points(engine, RunScope(run["id"], "fixture-review-timeout", "execute"),
            product={"name": "隔离保冷袋"}, evidence_catalog={"A": {"fact": "标称容量 12 升"}}, research={},
            items=[{"selling_point": "容量清楚：标称容量 12 升。", "evidence": [{"evidence_id": "A"}]}],
            timeout_seconds=0.3)
    assert timeout.value.code == "SELLING_POINTS_STAGE_TIMEOUT"
    assert timeout.value.details["timeout_kind"] == "total"
    assert len(sends) == 1
    with engine.store.tx() as db:
        stored, = db.execute("select body from attempts where run_id=?", (run["id"],)).fetchall()
    attempt = json.loads(stored[0])
    assert attempt["state"] == "outcome_unknown"
    assert attempt.get("provider_response_received") is False
    assert not attempt.get("usage")
