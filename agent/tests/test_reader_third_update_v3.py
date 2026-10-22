"""Third-update reader fixes: bounded OOXML and fake OCR, no model inference."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import threading
import time
import zipfile
from xml.etree import ElementTree as ET

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from PIL import Image
import pytest

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive import material_parsers as parsers
from shengbo_prelive import material_pdf_reader as pdf
from shengbo_prelive import material_visual_tools as visual
from shengbo_prelive.materials import MaterialStore
from shengbo_prelive.store import RuntimeStore


def _workbook_bytes(workbook):
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _drawing_xlsx(*, picture=False, grouped=False, text=True):
    workbook = Workbook()
    workbook.active["A1"] = "Price 29 yuan"
    with zipfile.ZipFile(BytesIO(_workbook_bytes(workbook))) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    ns = parsers.NS
    sheet = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
    ET.SubElement(sheet, "{" + ns["s"] + "}drawing", {"{" + ns["r"] + "}id": "rIdTerms"})
    parts["xl/worksheets/sheet1.xml"] = ET.tostring(sheet)
    content_types = ET.fromstring(parts["[Content_Types].xml"])
    ct = "http://schemas.openxmlformats.org/package/2006/content-types"
    ET.SubElement(content_types, "{" + ct + "}Override", {
        "PartName": "/xl/drawings/drawing1.xml", "ContentType": "application/vnd.openxmlformats-officedocument.drawing+xml"})
    if picture:
        ET.SubElement(content_types, "{" + ct + "}Default", {"Extension": "png", "ContentType": "image/png"})
    parts["[Content_Types].xml"] = ET.tostring(content_types)
    parts["xl/worksheets/_rels/sheet1.xml.rels"] = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdTerms" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/></Relationships>'
    shape = ('<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="42" name="Excluded delivery terms"/><xdr:cNvSpPr txBox="1"/></xdr:nvSpPr>'
             '<xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>'
             '<xdr:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Shipping is NOT included</a:t></a:r><a:br/>'
             '<a:r><a:t>No free gift</a:t></a:r></a:p><a:p><a:r><a:t>Same source conditions</a:t></a:r></a:p>'
             '</xdr:txBody></xdr:sp>') if text else ''
    if grouped:
        shape = '<xdr:grpSp><xdr:nvGrpSpPr><xdr:cNvPr id="40" name="Grouped terms"/><xdr:cNvGrpSpPr/></xdr:nvGrpSpPr><xdr:grpSpPr/>' + shape + '</xdr:grpSp>'
    if picture:
        stream = BytesIO()
        Image.new("RGB", (20, 20), "red").save(stream, format="PNG")
        parts["xl/media/image1.png"] = stream.getvalue()
        parts["xl/drawings/_rels/drawing1.xml.rels"] = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdPic" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image1.png"/></Relationships>'
        shape += '<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="43" name="Product image"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rIdPic"/></xdr:blipFill><xdr:spPr/></xdr:pic>'
    drawing = ('<xdr:wsDr xmlns:xdr="' + ns["d"] + '" xmlns:a="' + ns["a"] + '" xmlns:r="' + ns["r"] + '">'
               '<xdr:twoCellAnchor><xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>'
               '<xdr:to><xdr:col>4</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>'
               + shape + '<xdr:clientData/></xdr:twoCellAnchor></xdr:wsDr>')
    parts["xl/drawings/drawing1.xml"] = drawing.encode()
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return output.getvalue()


@pytest.mark.parametrize("grouped", [False, True])
def test_drawing_text_is_retained_with_real_anchor_without_fake_raster(grouped):
    result = parsers.parse_xlsx(_drawing_xlsx(grouped=grouped))
    texts = [item for item in result["locators"] if item["kind"] == "drawing_text"]
    assert len(texts) == 1
    assert texts[0]["text"] == "Shipping is NOT included\nNo free gift\nSame source conditions"
    assert texts[0]["source_shape_id"] == "42"
    assert texts[0]["sheet_id"] == "sheet:1"
    assert texts[0]["from_zero_based"]["col"] == "1"
    assert bool(texts[0]["group_path"]) == grouped
    assert result["text"].count("Shipping is NOT included") == 1
    assert result["embedded_images"] == [] and result["unsupported_visual_regions"] == []
    assert result["coverage"]["processed_text_complete"] is True
    assert result["coverage"]["drawing_text_locator_count"] == 1


def test_textbox_and_existing_picture_keep_image_id_bytes_and_cell_locator():
    baseline = parsers.parse_xlsx(_drawing_xlsx(picture=True, text=False))
    updated = parsers.parse_xlsx(_drawing_xlsx(picture=True))
    assert updated["embedded_images"][0]["id"] == baseline["embedded_images"][0]["id"] == "image:1"
    assert updated["embedded_images"][0]["sha256"] == baseline["embedded_images"][0]["sha256"]
    assert updated["embedded_images"][0]["anchors"][0]["sheet_id"] == "sheet:1"
    assert any(item["id"] == "sheet:1:cell:A1" for item in updated["locators"])
    assert updated["coverage"]["pending_visual"] is True
    assert "No free gift" in updated["text"]


def _chart_xlsx():
    workbook = Workbook()
    workbook.active.append(["Price", 29])
    workbook.active.append(["Delivery fee", 9])
    chart = BarChart()
    chart.add_data(Reference(workbook.active, min_col=2, min_row=1, max_row=2))
    workbook.active.add_chart(chart, "D4")
    return _workbook_bytes(workbook)


@pytest.fixture
def materials(tmp_path):
    return MaterialStore(tmp_path, RuntimeStore(tmp_path / "reader.sqlite3"))


def test_unrendered_chart_is_discoverable_without_ocr_or_fabricated_image(materials, monkeypatch):
    monkeypatch.setattr(pdf, "ocr_image", lambda *args: pytest.fail("unrendered object cannot enter OCR"))
    raw = _chart_xlsx()
    accepted = materials.accept("c", "chart.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", raw)
    ref = accepted["material_ref"]
    manifest = deepcopy(materials._record(ref, "c")[0]["manifest"])
    assert accepted["coverage"]["pending_visual"] is True
    assert accepted["coverage"]["processed_text_complete"] is False
    assert manifest["embedded_images"] == []
    region = manifest["unsupported_visual_regions"][0]
    assert region["object_type"] == "graphicFrame" and not region["id"].startswith("image:")
    workset = materials.read(ref, "c", "visual_workset")["manifest"]["visual_workset"]
    assert workset["total"] == 1
    assert workset["items"][0]["render_available"] is False
    assert workset["items"][0]["ocr_read_args"] is None
    precise = materials.read(ref, "c", region["id"])
    assert precise["manifest"]["locators"][0]["id"] == region["id"]
    with pytest.raises(ContractError, match="MATERIAL_VISUAL_LOCATOR_INVALID"):
        materials.record_visual_delivery(ref, "c", locator=region["id"], run_id="r")
    with pytest.raises(ContractError, match="MATERIAL_VISUAL_LOCATOR_INVALID"):
        materials.read(ref, "c", "ocr:" + region["id"])
    assert canonical_hash(materials._record(ref, "c")[0]["manifest"]) == ref["content_hash"]
    assert materials.blob_bytes(ref, "c") == raw


def test_drawing_object_limit_is_preserved(monkeypatch):
    monkeypatch.setattr(parsers, "MAX_DRAWING_OBJECTS", 1)
    with pytest.raises(ContractError, match="MATERIAL_XLSX_DRAWING_OBJECT_LIMIT"):
        parsers.parse_xlsx(_drawing_xlsx(picture=True))


def test_nested_drawing_groups_have_explicit_depth_boundary():
    with zipfile.ZipFile(BytesIO(_drawing_xlsx())) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    root = ET.fromstring(parts["xl/drawings/drawing1.xml"])
    anchor = root[0]
    shape = anchor.find("d:sp", parsers.NS)
    anchor.remove(shape)
    for _ in range(70):
        group = ET.Element("{" + parsers.NS["d"] + "}grpSp")
        group.append(shape)
        shape = group
    anchor.append(shape)
    parts["xl/drawings/drawing1.xml"] = ET.tostring(root)
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    with pytest.raises(ContractError, match="MATERIAL_XLSX_DRAWING_DEPTH_LIMIT"):
        parsers.parse_xlsx(output.getvalue())


@pytest.mark.parametrize("number,number_format,display,supported", [
    (0.25, "0.00%", "25.00%", True), (29, '"NT$"#,##0.00', "NT$29.00", True),
    (0.25, '0.00"%"', "0.25", False), (0.25, r"0.00\%", "0.25", False),
    (0, '0.00;-0.00;"NOT_APPLICABLE"', "0", False),
    (0.25, "[>1]0%;0.00", "0.25", False),
])
def test_number_formats_are_correct_or_explicitly_preserved_raw(number, number_format, display, supported):
    workbook = Workbook()
    workbook.active["A1"] = number
    workbook.active["A1"].number_format = number_format
    manifest = parsers.parse_xlsx(_workbook_bytes(workbook))
    cell = manifest["locators"][0]
    assert cell["display_text"] == cell["text"] == display
    assert cell["number_format"] == number_format
    assert cell["format_supported"] is supported
    assert manifest["coverage"]["unsupported_number_format_count"] == int(not supported)
    if not supported:
        assert cell["text"] == cell["raw_value"]
        assert "some_cell_number_formats_preserved_raw_not_fully_rendered" in manifest["unknowns"]


@pytest.fixture
def fake_pdf_ocr(materials, monkeypatch):
    def fake_parse(data):
        pages = [{"number": page, "text": "Source text"} for page in (1, 2)]
        images = [{"id": f"image:{page}", "kind": "image", "page": page, "source": "pdf_page_render",
                   "visual_status": "pending_visual_read", "anchors": [{"locator_id": f"page:{page}"}]} for page in (1, 2)]
        return {"kind": "document", "extractor": "fixture", "text": "Source text", "pages": pages,
                "locators": [{"id": f"page:{page}", "kind": "page", "text": "Source text"} for page in (1, 2)] + images,
                "embedded_images": images, "coverage": {"processed_text_complete": True, "pending_visual": True}}
    capabilities = {"engine": "pdf-reader-sdk-v1", "ocr_backend": "rapidocr",
                    "ocr_language": "chi_sim+chi_tra+eng", "ocr_model_fingerprint": "model-a"}
    calls = []
    def fake_ocr(data, number):
        calls.append(number)
        return {"text": "Shipping is NOT included", "page": number,
                "locators": [{"id": f"page:{number}:ocr:1", "page": number, "text": "Shipping is NOT included"}],
                "parse_metrics": {"ocr_engine": "fake-no-inference"}}
    monkeypatch.setattr(pdf, "parse_pdf", fake_parse)
    monkeypatch.setattr(pdf, "pdf_reader_capabilities", lambda: capabilities)
    monkeypatch.setattr(pdf, "ocr_pdf_page", fake_ocr)
    ref = materials.accept("c", "sample.pdf", "application/pdf", b"%PDF-1.7\nfixture-a")["material_ref"]
    return ref, capabilities, calls


def test_pdf_aliases_share_computation_but_keep_separate_delivery_receipts(materials, fake_pdf_ocr):
    ref, _, calls = fake_pdf_ocr
    first = materials.read(ref, "c", "ocr:page:1")["ocr_result"]
    second = materials.read(ref, "c", "ocr:image:1")["ocr_result"]
    assert calls == [1]
    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert second["computation_target"] == {"kind": "pdf_page", "page": 1}
    assert first["source_locator"] == "page:1" and second["source_locator"] == "image:1"
    assert second["locators"][0]["id"] == "image:1:ocr:1"
    assert materials.list_visual_receipts(ref, "c") == []
    page = materials.record_visual_delivery(ref, "c", locator="page:1", run_id="r", method="ocr")
    image = materials.record_visual_delivery(ref, "c", locator="image:1", run_id="r", method="ocr")
    assert page["id"] != image["id"]
    item = materials.read(ref, "c", "visual_workset")["manifest"]["visual_workset"]["items"][0]
    assert item["delivery_receipt_ids"] == [image["id"]]
    assert item["review_locator_choices"][1]["read_receipt_ids"] == [page["id"]]


def test_ocr_page_source_and_model_reader_changes_cannot_hit_same_cache(materials, fake_pdf_ocr):
    ref, capabilities, calls = fake_pdf_ocr
    materials.read(ref, "c", "ocr:page:1")
    assert materials.read(ref, "c", "ocr:page:2")["ocr_result"]["cache_hit"] is False
    other = materials.accept("c", "other.pdf", "application/pdf", b"%PDF-1.7\nfixture-b")["material_ref"]
    assert materials.read(other, "c", "ocr:page:1")["ocr_result"]["cache_hit"] is False
    capabilities["ocr_model_fingerprint"] = "model-b"
    assert materials.read(ref, "c", "ocr:page:1")["ocr_result"]["cache_hit"] is False
    capabilities["engine"] = "pdf-reader-next"
    assert materials.read(ref, "c", "ocr:page:1")["ocr_result"]["cache_hit"] is False
    assert calls == [1, 2, 1, 1, 1]


def test_concurrent_pdf_aliases_do_not_duplicate_same_process_ocr(materials, fake_pdf_ocr, monkeypatch):
    ref, _, calls = fake_pdf_ocr
    original = pdf.ocr_pdf_page
    start = threading.Barrier(3)
    def slow_fake(data, page):
        time.sleep(0.08)  # deterministic overlap; no OCR or provider is invoked
        return original(data, page)
    monkeypatch.setattr(pdf, "ocr_pdf_page", slow_fake)
    def read(section):
        start.wait(timeout=2)
        return materials.read(ref, "c", section)["ocr_result"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        page = executor.submit(read, "ocr:page:1")
        image = executor.submit(read, "ocr:image:1")
        start.wait(timeout=2)
        results = [page.result(timeout=3), image.result(timeout=3)]
    assert calls == [1]
    assert sorted(result["cache_hit"] for result in results) == [False, True]
    assert {result["source_locator"] for result in results} == {"page:1", "image:1"}


def test_legacy_ocr_cache_is_retained_but_not_promoted_without_new_fingerprint(materials, fake_pdf_ocr):
    ref, capabilities, calls = fake_pdf_ocr
    identity = "material_ocr_" + canonical_hash([ref, "page:1", capabilities, capabilities["ocr_model_fingerprint"]])[7:]
    old = {"id": identity, "conversation_id": "c", "source_ref": ref, "locator": "page:1",
           "result": {"text": "Old cache text"}, "result_hash": canonical_hash({"text": "Old cache text"})}
    old = materials.store.put("material_ocr_result", old)
    result = materials.read(ref, "c", "ocr:page:1")["ocr_result"]
    assert result["legacy_cache_ignored"] is True and result["cache_hit"] is False
    assert result["text"] == "Shipping is NOT included" and calls == [1]
    assert materials.read(ref, "c", "ocr:image:1")["ocr_result"]["cache_hit"] is True
    assert materials.store.get("material_ocr_result", identity) == old


def test_canonical_ocr_cache_still_rejects_integrity_failure(materials, fake_pdf_ocr):
    ref, _, _ = fake_pdf_ocr
    materials.read(ref, "c", "ocr:page:1")
    row = materials.store.list("material_ocr_result")[0]
    row["result"]["text"] = "Incorrect cache content"
    materials.store.put("material_ocr_result", row)
    with pytest.raises(ContractError, match="MATERIAL_BLOB_INTEGRITY_FAILED"):
        materials.read(ref, "c", "ocr:image:1")


def test_model_file_bytes_participate_in_fingerprint(tmp_path):
    model = tmp_path / "fake.onnx"
    model.write_bytes(b"model-a")
    capabilities = {"ocr_backend": "rapidocr", "ocr_backend_details": {"rapidocr": {
        "rapidocr_version": "3.10.0", "onnxruntime_version": "1.31.0",
        "models": [{"name": "fake.onnx", "path": str(model)}]}}}
    before = visual._ocr_fingerprint(capabilities)
    model.write_bytes(b"model-b")  # same byte count, different actual model
    assert visual._ocr_fingerprint(capabilities) != before
