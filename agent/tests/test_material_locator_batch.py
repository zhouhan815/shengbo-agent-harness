"""Targeted integration fixes: batched evidence positions and explicit images."""
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from xml.etree import ElementTree as ET
import zipfile

import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.models import RunScope
from shengbo_prelive.tools import build_tools
from test_merchant_material_readers import materials, workbook_bytes, image_bytes
from test_priority_eight_runtime import report_branch, report_for
from test_runtime_business import env


def test_batch_locator_validation_checks_record_once_and_keeps_exact_rules(materials, monkeypatch):
    ref = materials.accept("c", "产品.xlsx", "application/octet-stream", workbook_bytes())["material_ref"]
    count = []
    original = materials._record
    def counted(*args):
        count.append(1)
        return original(*args)
    monkeypatch.setattr(materials, "_record", counted)
    result = materials.validate_locators(ref, "c", ["sheet:1", "sheet:1:cell:C9", "sheet:1:cell:C9", "image:1"])
    assert len(count) == 1
    assert result["locator_count"] == 3 and result["requested_locator_count"] == 4
    assert result["semantic_support_verified"] is False
    with pytest.raises(ContractError):
        materials.validate_locators(ref, "foreign-conversation", ["sheet:1"])
    with pytest.raises(ContractError):
        materials.validate_locators(ref, "c", ["sheet:99"])


@pytest.mark.parametrize("reserved", ["overview", "full", "image_index:1", "locator_index:1"])
def test_control_views_are_not_valid_source_evidence(materials, reserved):
    ref = materials.accept("c", "资料.txt", "text/plain", b"source")["material_ref"]
    with pytest.raises(ContractError) as caught:
        materials.validate_locators(ref, "c", [reserved])
    assert caught.value.code == "MATERIAL_SECTION_NOT_FOUND"


@pytest.mark.asyncio
async def test_runtime_groups_repeated_evidence_by_exact_source_and_emits_cost(env, monkeypatch):
    engine, _ = env
    run, source, scope = await report_branch(engine)
    second = engine.materials.accept(run["conversation_id"], "second.md", "text/markdown", "第二资料".encode())["material_ref"]
    branch = engine.store.get("branch", scope.branch_task_id)
    engine.store.update("branch", scope.branch_task_id, task={**branch["task"], "source_refs": [source, second]})
    report = report_for(source)
    report["evidence"] += [{"id": key, "source_ref": source if key != "e4" else second,
                             "locator": "text:1"} for key in ("e2", "e3", "e4")]
    report["material_assessment"]["product_candidates"][0]["evidence_ids"] = ["e1", "e2", "e3", "e4"]
    calls = []
    original = engine.materials._record
    def counted(*args):
        calls.append(args[0]["id"])
        return original(*args)
    monkeypatch.setattr(engine.materials, "_record", counted)
    monkeypatch.setattr(engine.materials, "read", lambda *args, **kwargs: pytest.fail("evidence validation must not reread/copy source bodies"))
    result = engine._validated_material_assessment(scope, report)
    assert result.product_candidates[0].name == "测试商品"
    assert sorted(calls) == sorted([source["id"], second["id"]])
    events = [e["data"] for e in engine.trace.events(run["id"])["events"] if e["type"] == "material.assessment.validation"]
    assert len(events) == 1
    assert events[0]["status"] == "valid"
    assert events[0]["source_count"] == 2 and events[0]["evidence_count"] == 4
    assert events[0]["locator_count"] == 2 and events[0]["locator_reference_count"] == 4
    assert events[0]["validation_duration_ms"] >= 0


def many_image_workbook():
    source = zipfile.ZipFile(BytesIO(workbook_bytes()))
    ns = {"d": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
          "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    drawing = ET.fromstring(source.read("xl/drawings/drawing1.xml"))
    relationships = ET.fromstring(source.read("xl/drawings/_rels/drawing1.xml.rels"))
    for index, color in enumerate(["green", "yellow", "black", "purple"], 4):
        anchor = deepcopy(drawing[0])
        anchor.find(".//a:blip", ns).set("{" + ns["r"] + "}embed", f"im{index}")
        drawing.append(anchor)
        ET.SubElement(relationships, "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship",
                      {"Id": f"im{index}", "Target": f"../media/image{index}.png"})
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in source.namelist():
            value = ET.tostring(drawing) if name == "xl/drawings/drawing1.xml" else ET.tostring(relationships) if name == "xl/drawings/_rels/drawing1.xml.rels" else source.read(name)
            archive.writestr(name, value)
        for index, color in enumerate(["green", "yellow", "black", "purple"], 4):
            archive.writestr(f"xl/media/image{index}.png", image_bytes(color))
    return output.getvalue()


@pytest.mark.asyncio
async def test_multi_image_sheet_is_text_only_until_explicit_image_tool_read(env, monkeypatch):
    engine, _ = env
    run, _, _ = await report_branch(engine)
    ref = engine.materials.accept(run["conversation_id"], "多图商品.xlsx", "application/octet-stream", many_image_workbook())["material_ref"]
    engine.store.update("run", run["id"], input={**run["input"], "material_refs": [ref]})
    scope = RunScope(run["id"], "source-tool", "execute")
    tool = next(t for t in build_tools(engine, role="product_facts") if t.name == "read_source")
    calls = []
    original = engine.materials.image_messages
    def counted(*args, **kwargs):
        calls.append(kwargs.get("section"))
        return original(*args, **kwargs)
    monkeypatch.setattr(engine.materials, "image_messages", counted)
    # There really are >4 distinct images anchored on this sheet.
    with pytest.raises(ContractError, match="MATERIAL_VISUAL_SELECTION_REQUIRED"):
        original(ref, run["conversation_id"], section="sheet:1")
    runtime = SimpleNamespace(context=scope)
    text_result = await tool.coroutine(runtime=runtime, ref=ref, section="sheet:1")
    assert isinstance(text_result, dict) and "报价107新台币" in text_result["text"]
    assert calls == []
    index_result = await tool.coroutine(runtime=runtime, ref=ref, section="image_index:1")
    assert isinstance(index_result, dict) and calls == []
    image_result = await tool.coroutine(runtime=runtime, ref=ref, section="image:1")
    assert calls == ["image:1"]
    assert isinstance(image_result, list) and any(x["type"] == "image_url" for x in image_result)
