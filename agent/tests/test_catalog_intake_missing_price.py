"""A durable user price supplements source evidence without approving a write."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.catalog_extract import TOOL_NAME, _catalog_report
from shengbo_prelive.models import AnswerRequest
from shengbo_prelive.store import TERMINAL
from test_catalog_response_recovery import FIELDS, SOURCE_TEXT, report_json, submit_and_wait
from test_catalog_coverage import SOURCE, prepared
from test_runtime_business import env


@pytest.mark.asyncio
@pytest.mark.parametrize(("defer", "placeholder"), [(False, ""), (True, "待确认")])
async def test_missing_price_is_human_input_then_separate_intake_approval(env, defer, placeholder):
    engine, service = env
    await engine.start()
    calls = []

    class Model:
        def bind_tools(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, messages):
            calls.append(1)
            inputs = json.loads(messages[-1].content)
            assert not any("日常售价" in unit["text"] for unit in inputs["source_units"])
            args = report_json(inputs)
            args["products"][0]["fields"] = {key: value for key, value in FIELDS.items() if key != "price"}
            return SimpleNamespace(tool_calls=[{"name": TOOL_NAME, "args": args}])

    async def next_question(identity, previous):
        async with asyncio.timeout(8):
            while True:
                run = engine.get_run(identity)
                assert run["status"] not in TERMINAL, run
                for question in run["questions"]:
                    if question["id"] != previous and question["status"] == "OPEN":
                        return question
                await asyncio.sleep(.025)

    async def answer(question, value, request_id):
        return await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
            answer=value, client_request_id=request_id))

    engine.model = Model()
    identity, current = await submit_and_wait(engine, SOURCE_TEXT.replace("29.9", "日常售价：" + placeholder))
    first, = current["questions"]
    assert first["kind"] == "catalog_intake_price"
    assert first["options"] == [{"value": "later", "label": "稍后补价格（暂不入库）"}]
    assert first["preview"]["sku_id"] == FIELDS["sku_id"] and first["preview"]["currency"] == FIELDS["currency"]
    with pytest.raises(ContractError, match="CATALOG_PRICE_AMOUNT_INVALID"):
        await answer(first, {"text": "29 到 39"}, "ambiguous-price")
    current_question = first
    if defer:
        await answer(first, "later", "later-price")
        current_question = await next_question(identity, first["id"])
        assert current_question["kind"] == "catalog_intake_price"
        assert not current_question["options"] and current_question["allow_text"]
    assert not (await service.query("search_products", {}))["items"]
    await answer(current_question, {"text": "３１．８０"}, "manual-price")
    approval = await next_question(identity, current_question["id"])
    assert approval["kind"] == "catalog_intake_approval"
    assert approval["preview"]["items"][0]["price"] == 31.8
    assert not (await service.query("search_products", {}))["items"]
    await answer(approval, "save_all", "approve-priced-product")
    async with asyncio.timeout(8):
        while (result := engine.get_run(identity))["status"] not in TERMINAL:
            await asyncio.sleep(.025)
    assert result["status"] == "COMPLETED", result
    products = (await service.query("search_products", {}))["items"]
    assert [(product["price"], product["ref"]["kind"]) for product in products] == [(31.8, "product")]
    branch, = [branch for branch in engine.store.list("branch") if branch["run_id"] == identity]
    proof, = [item for item in branch["report"]["insights"] if item["kind"] == "catalog_human_daily_price"]
    assert proof["question_id"] == current_question["id"] and proof["price"] == 31.8
    assert all("price" not in item["fields"] for item in branch["report"]["evidence"])
    assert len(calls) == 1


@pytest.mark.parametrize("price_quote", ["最大承重29.9千克", "可售库存29.9件", "直播售价29.9元", "29.9"])
def test_missing_price_cannot_be_inferred_from_an_unrelated_amount(price_quote):
    source = prepared(("paragraph:1", FIELDS["name"]), ("paragraph:2", FIELDS["sku_id"]),
                      ("paragraph:3", price_quote), ("paragraph:4", "人民币"),
                      ("paragraph:5", "日常售价：待确认"))
    source.update(sources=[SOURCE])
    args = {"products": [{"fields": FIELDS, "evidence": [
        {"location": f"L{i}", "fields": [field]} for i, field in
        enumerate(("name", "sku_id", "price", "currency"), 1)]}], "questions": [], "coverage": {}}
    with pytest.raises(ContractError, match="CATALOG_FACTS_INVALID") as invalid:
        _catalog_report(args, source)
    assert invalid.value.details["fields"] == ["price"]


def test_explicit_daily_price_can_be_verified_but_not_omitted():
    source = prepared(("paragraph:1", FIELDS["name"]), ("paragraph:2", FIELDS["sku_id"]),
                      ("paragraph:3", "日常售价：29.9元"), ("paragraph:4", "人民币"))
    source.update(sources=[SOURCE])
    args = {"products": [{"fields": FIELDS, "evidence": [
        {"location": f"L{i}", "fields": [field]} for i, field in
        enumerate(("name", "sku_id", "price", "currency"), 1)]}], "questions": [],
        "coverage": {"U1": "*|name", "U2": "*|sku_id", "U3": "*|price", "U4": "*|currency"}}
    assert _catalog_report(args, source)["fields"]["products"][0]["price"] == 29.9
    candidate = args["products"][0]
    candidate["fields"] = {key: value for key, value in FIELDS.items() if key != "price"}
    candidate["evidence"].pop(2)
    with pytest.raises(ContractError, match="CATALOG_FACTS_INVALID") as invalid:
        _catalog_report(args, source)
    assert invalid.value.details["fields"] == ["price"]
