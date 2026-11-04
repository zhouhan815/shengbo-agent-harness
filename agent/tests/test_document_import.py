"""Agent/Voice integration for the explicit document import boundaries."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.catalog_extract import TOOL_NAME
from shengbo_prelive.catalog_intake import BASIC_CREATE_REQUEST, _source_fingerprint
from shengbo_prelive.document_import import ROOM_DEFAULT_REQUEST, checked_session
from shengbo_prelive.models import AnswerRequest, MessageRequest
from shengbo_prelive.store import TERMINAL
from test_catalog_response_recovery import SOURCE_TEXT, report_json
from test_runtime_business import env, human_command


async def _run(engine, source, *, room=None, prep=None, answers=(), filename="source.txt"):
    if prep:
        conversation = engine.create_conversation({"scope": {"kind": "preparation", "preparation_id": prep},
            "room_id": room})
        view = {"view": "session", "room_id": room, "preparation_id": prep, "room_material_action": "import"}
    else:
        conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
        view = {"view": "products", "product_material_action": "create", "catalog_create_room": True}
    ref = engine.accept_material(conversation["id"], filename, "text/plain", source.encode())["material_ref"]
    run = await engine.submit_message(conversation["id"], MessageRequest(
        text=ROOM_DEFAULT_REQUEST if room else BASIC_CREATE_REQUEST, intent_mode="new_task",
        client_request_id=conversation["id"], material_refs=[ref], view_context=view))
    seen = set()
    async with asyncio.timeout(15):
        while (current := engine.get_run(run["run"]["id"]))["status"] not in TERMINAL:
            for question in current["questions"]:
                kind = question["kind"]
                if question["status"] != "OPEN" or question["id"] in seen:
                    continue
                assert kind in answers, question
                seen.add(question["id"])
                await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
                    answer=answers[kind], client_request_id=question["id"]))
            await asyncio.sleep(.05)
    assert current["status"] == "COMPLETED", current.get("error")
    return current["document_import_result"]


async def _draft(service, result):
    return (await service.query("read_context", {"ref": result["draft_ref"], "sections": ["draft"]}))["body"]["draft"]


class DocumentModel:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        assert [tool["function"]["name"] for tool in tools] == [TOOL_NAME]
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        return SimpleNamespace(tool_calls=[{"name": TOOL_NAME, "args": self.response(json.loads(messages[-1].content))}])


@pytest.mark.asyncio
async def test_catalog_without_activity_creates_one_room_and_reuses_exact_import(env):
    engine, service = env
    await engine.start()
    engine.model = DocumentModel(lambda inputs: {**report_json(inputs), "session": None})
    result = await _run(engine, SOURCE_TEXT, answers={"catalog_intake_approval": "save_all"})
    assert not result["activity_found"] and len(result["product_ids"]) == 1
    assert not (await _draft(service, result))["commerce"]["promotion"]
    duplicate = await _run(engine, SOURCE_TEXT)
    assert duplicate["room_id"] == result["room_id"]
    assert duplicate["draft_ref"] == result["draft_ref"]
    room = (await human_command(service, "create_room", {"name": "另一直播间", "initial_config": {}}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"],
        "room_revision": room["revision"], "goal": "沿用现成商品", "source_refs": []}))["data"]["preparation_ref"]
    attached = await _run(engine, SOURCE_TEXT, room=room["id"], prep=prep["id"])
    assert attached["product_ids"] == result["product_ids"]
    assert attached["room_id"] == room["id"] and not attached["activity_found"]
    assert engine.model.calls == 1


@pytest.mark.asyncio
async def test_session_only_preserves_current_room_and_groups_conflicting_campaign(env):
    engine, service = env
    room = (await human_command(service, "create_room", {"name": "已存在直播间", "initial_config": {}}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"],
        "room_revision": room["revision"], "goal": "本场", "source_refs": []}))["data"]["preparation_ref"]
    await human_command(service, "save_session_draft", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "draft": {"commerce": {"promotion": "人工已设活动"}, "objective": "保留"}})
    await engine.start()
    heading = "直播活动"
    rule = "2026 年 11 月 11 日 20:00 至 23:59，北京时间。超过截止时间按结算页执行。"
    def response(inputs):
        sources = {quote: next(item["location"] for item in inputs["source_material"] if quote in item["text"])
                   for quote in (heading, rule)}
        return {"products": [], "questions": [], "coverage": {}, "session": {
            "theme": {"quote": heading, "location": sources[heading]},
            "window": {"quote": rule, "location": sources[rule],
                "start": "2026-11-11T20:00:00+08:00", "end": "2026-11-11T23:59:00+08:00"},
            "rules": [{"quote": rule, "location": sources[rule]}], "scene": None}}
    engine.model = DocumentModel(response)
    result = await _run(engine, heading+"\n"+rule, room=room["id"], prep=prep["id"],
        answers={"document_import_conflict": "keep"})
    assert result["room_id"] == room["id"] and result["preparation_id"] == prep["id"]
    assert not result["product_ids"] and not (await service.query("search_products", {}))["items"]
    assert result["conflicts"][0]["field"] == "活动说明"
    saved = await _draft(service, result)
    assert saved["theme"] == heading and saved["objective"] == "保留"
    assert saved["commerce"]["promotion"] == "人工已设活动" and "campaign_starts_at" not in saved


def test_same_document_fingerprint_ignores_ocr_variations_but_checks_original():
    a = {"locations": [{"kind": "image", "text": "first OCR", "ocr_regions": [], "original_sha256": "x"}]}
    b = {"locations": [{"kind": "image", "text": "second OCR", "ocr_regions": [1], "original_sha256": "x"}]}
    assert _source_fingerprint(a) == _source_fingerprint(b)
    b["locations"][0]["original_sha256"] = "y"
    assert _source_fingerprint(a) != _source_fingerprint(b)


def test_session_reports_only_invalid_fields_without_mutating_the_candidate():
    rows = [{"kind": "paragraph", "location": f"L{i}", "locator": f"paragraph:{i}",
             "source_ref": {"id": "activity" if i != 5 else "separate"}, "text": text}
            for i, text in enumerate(("双十一直播", "2026 年 11 月 11 日 20:00 至 21:00，北京时间。",
                "A-1 每件 59.90 元", "满 129 元减 15 元券", "商品以人民币计价"), 1)]
    raw = {"theme": {"location": "L1", "quote": rows[0]["text"]},
           "window": {"location": "L2", "quote": rows[1]["text"],
                      "start": "2026-11-11T20:00:00+09:00", "end": "2026-11-11T21:00:00+08:00"},
           "prices": [{"sku": "A-1", "amount": 69.9, "location": "L3", "quote": rows[2]["text"]}],
           "coupon": {"amount": 129, "minimum_spend": 15, "currency": "CNY",
                      "location": "L4", "quote": rows[3]["text"]}}
    unchanged = json.dumps(raw)
    with pytest.raises(ContractError, match="DOCUMENT_SESSION_EVIDENCE_INVALID") as caught:
        checked_session(raw, {"locations": rows})
    assert json.dumps(raw) == unchanged
    details = caught.value.details
    assert details["repair_fields"] == ["coupon", "prices", "window"]
    assert {(tuple(issue["path"]), issue["reason"], issue["location"]) for issue in details["issues"]} == {
        (("window", "start"), "time_not_grounded", "L2"),
        (("prices", 0, "amount"), "sku_amount_not_grounded", "L3"),
        (("coupon", "currency"), "coupon_currency_not_grounded", "L4"),
        (("coupon", "minimum_spend"), "coupon_threshold_not_grounded", "L4"),
        (("coupon", "amount"), "coupon_discount_not_grounded", "L4"),
    }

    raw["window"]["start"] = "2026-11-11T20:00:00+08:00"
    raw["prices"][0]["amount"] = 59.9
    raw["coupon"].update(amount=15, minimum_spend=129)
    rows[3]["text"] += "，人民币。"
    checked = checked_session(raw, {"locations": rows})
    assert checked["theme"] == raw["theme"] and checked["coupon"]["amount"] == 15


@pytest.mark.parametrize("raw,expected_fields,expected_path", [
    ({"rules": [{"location": "L1", "quote": "库存为开播前快照"}, {"location": "L2"}]}, ["rules"], ["rules", 1, "quote"]),
    ({"unsupported": "fixture"}, [], ["unsupported"]),
    ("not a session object", [], []),
])
def test_session_schema_errors_preserve_original_paths(raw, expected_fields, expected_path):
    with pytest.raises(ContractError, match="DOCUMENT_SESSION_EVIDENCE_INVALID") as caught:
        checked_session(raw, {"locations": []})
    details = caught.value.details
    assert details["repair_fields"] == expected_fields and details["issue_count"] == 1
    assert details["issues"][0]["path"] == expected_path
    assert details["issues"][0]["reason"].startswith("schema_")


def test_explicit_session_labels_prevent_omission_and_swapped_time_roles():
    rows = [{"kind": "paragraph", "location": f"L{i}", "locator": f"paragraph:{i}",
             "source_ref": {"id": "activity"}, "text": text}
            for i, text in enumerate(("本场主题：桌面清洁", "计划直播排期　2026 年 11 月 11 日 20:00 至 21:00，北京时间。",
                "活动时间与入口　2026 年 11 月 11 日 19:30 至 22:00，北京时间。"), 1)]
    prepared = {"locations": rows}
    with pytest.raises(ContractError) as missing:
        checked_session(None, prepared)
    assert missing.value.details["repair_fields"] == ["schedule", "theme", "window"]
    assert {issue["location"] for issue in missing.value.details["issues"]} == {"L1", "L2", "L3"}
    assert checked_session(None, prepared, require_complete=False) is None

    raw = {"theme": {"location": "L1", "quote": rows[0]["text"]},
           "schedule": {"location": "L3", "quote": rows[2]["text"],
                        "start": "2026-11-11T19:30:00+08:00", "end": "2026-11-11T22:00:00+08:00"},
           "window": {"location": "L2", "quote": rows[1]["text"],
                      "start": "2026-11-11T20:00:00+08:00", "end": "2026-11-11T21:00:00+08:00"}}
    with pytest.raises(ContractError) as swapped:
        checked_session(raw, prepared, require_complete=False)
    assert {tuple(issue["path"]) for issue in swapped.value.details["issues"]} == {
        ("schedule", "location"), ("window", "location")}
    raw["schedule"], raw["window"] = raw["window"], raw["schedule"]
    assert checked_session(raw, prepared)["schedule"]["location"] == "L2"


def test_coupon_scope_checks_only_an_explicit_sku_list():
    source = {"kind": "paragraph", "location": "L1", "locator": "paragraph:1",
              "source_ref": {"id": "activity"}, "text": "人民币满 129 元减 15 元券。仅适用 A-1、A-2。"}
    coupon = {"location": "L1", "quote": "人民币满 129 元减 15 元券", "currency": "CNY",
              "amount": 15, "minimum_spend": 129, "skus": []}
    with pytest.raises(ContractError) as not_all:
        checked_session({"coupon": coupon}, {"locations": [source]})
    assert not_all.value.details["issues"][0]["reason"] == "coupon_sku_scope_not_grounded"
    coupon["skus"] = ["a-2", "A-1"]
    assert checked_session({"coupon": coupon}, {"locations": [source]})["coupon"]["skus"] == ["a-2", "A-1"]
    source["text"] = source["text"].replace("仅适用 A-1、A-2", "仅适用商品卡")
    coupon["skus"] = []
    assert checked_session({"coupon": coupon}, {"locations": [source]})["coupon"]["skus"] == []
    source["text"] = "商品活动价仅适用 B-1。人民币满 129 元减 15 元券。会员折扣仅适用 B-1。"
    assert checked_session({"coupon": coupon}, {"locations": [source]})["coupon"]["skus"] == []


def test_coupon_stacking_uses_verified_scope_and_keeps_other_coupon_exclusion():
    from shengbo_prelive.document_import import checked_session, _configuration
    loc = [{"kind": "paragraph", "location": f"L{i}", "locator": f"paragraph:{i}",
            "source_ref": {"id": "document"}, "text": value}
           for i, value in enumerate(("商品以人民币计价", "A-1 每件 59.90 元",
                "直播间满 129 元减 15 元券。可与本次商品活动价一起使用，不能再叠加店铺会员券。"), 1)]
    raw = {"coupon": {"amount": 15, "minimum_spend": 129, "currency": "CNY", "skus": ["A-1"],
                      "location": "L3", "quote": "直播间满 129 元减 15 元券"},
           "prices": [{"sku": "A-1", "amount": 59.9, "location": "L2", "quote": loc[1]["text"]}],
           "rules": [{"location": "L3", "quote": loc[2]["text"]}], "stacks_with_coupon": False}
    def result():
        session = checked_session(raw, {"locations": loc})
        return _configuration(session, {"a-1": "product-1"})[0]["commerce"]
    commerce = result()
    assert not commerce["product_overrides"]["product-1"]["exclusive"]
    assert "不能再叠加店铺会员券" in commerce["promotion"]
    loc[2]["text"] = loc[2]["text"].replace("可与", "不可与")
    raw["rules"][0]["quote"] = loc[2]["text"]
    assert result()["product_overrides"]["product-1"]["exclusive"]



@pytest.mark.parametrize("declared_currency,expected", [("人民币", [46, 61]), ("", [])])
def test_checked_session_recovers_explicit_table_prices_without_model(declared_currency, expected):
    from shengbo_prelive.document_import import checked_session

    source = {"id": "document"}
    rows = [
        {"kind": "paragraph", "location": "L1", "locator": "paragraph:1",
         "source_ref": source, "text": declared_currency},
        {"kind": "table_row", "location": "L2", "locator": "table:1:row:2",
         "source_ref": source, "text": "WL-C01 | 58.00 元 | 46.00 元 | 65 件"},
        {"kind": "table_row", "location": "L3", "locator": "table:1:row:3",
         "source_ref": source, "text": "WL-C02 | 76.00 元 | 61.00 元 | 28 件"},
    ]
    units = [{"location": location, "sku_id": sku, "column": label, "text": value}
             for location, sku, values in [("L2", "WL-C01", ("58.00 元", "46.00 元", "65 件")),
                                           ("L3", "WL-C02", ("76.00 元", "61.00 元", "28 件"))]
             for label, value in zip(("日常售价", "直播售价", "可售库存"), values)]
    prepared = {"locations": rows, "source_units": units}
    session = checked_session(None, prepared)
    model_gap = "活动价格缺少币种"
    checked = checked_session({"gaps": [model_gap]}, prepared)
    assert (model_gap in checked["gaps"]) == (not declared_currency)
    if declared_currency:
        checked = checked_session({"prices": [
            {"sku": "WL-C01", "amount": 46, "location": "L2", "quote": "直播售价 | 46.00 元", "currency": "CNY"},
            {"sku": "WL-C02", "amount": 61, "location": "L3", "quote": "直播售价 | 61.00 元", "currency": "CNY"},
        ], "rules": [{"location": "L1", "quote": "人民币", "fields": []}]}, prepared)
        assert [row["amount"] for row in checked["prices"]] == expected
    assert [row["amount"] for row in session["prices"]] == expected
    assert list(session["sources"]) == (["L2", "L3"] if expected else [])
    assert not session["window"] and not session["schedule"]
    assert session["gaps"]

def test_checked_session_requires_both_explicit_time_bounds():
    from shengbo_prelive.document_import import checked_session
    from shengbo_contracts.common import ContractError

    text = "直播排期：2026 年 11 月 12 日 19:00 至 21:30，北京时间。"
    prepared = {"locations": [{"location": "L1", "locator": "paragraph:1", "kind": "paragraph",
                               "text": text, "source_ref": {"id": "one"}}]}
    raw = {"schedule": {"location": "L1", "quote": text, "start": "2026-11-12T19:00:00+08:00"}}
    with pytest.raises(ContractError) as result:
        checked_session(raw, prepared)
    assert result.value.details["issues"] == [
        {"path": ["schedule", "end"], "field": "schedule", "reason": "time_not_grounded", "location": "L1"}]
    assert checked_session(raw, prepared, require_complete=False)["schedule"]["end"] is None
