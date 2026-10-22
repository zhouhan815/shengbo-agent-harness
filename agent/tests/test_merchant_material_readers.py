"""Meaningful offline coverage of heterogeneous merchant material boundaries."""
from io import BytesIO
import hashlib
from pathlib import Path
import zipfile

import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.materials import MaterialStore, max_bytes_for_extension
from shengbo_prelive.material_parsers import parse_rtf
from shengbo_prelive.store import RuntimeStore


@pytest.fixture
def materials(tmp_path):
    return MaterialStore(tmp_path, RuntimeStore(tmp_path / "runtime.sqlite"))


def image_bytes(color="red"):
    from PIL import Image
    output = BytesIO()
    Image.new("RGB", (20, 16), color).save(output, format="PNG")
    return output.getvalue()


def workbook_bytes(*, macro=False, dtd=False):
    output = BytesIO()
    relns = "http://schemas.openxmlformats.org/package/2006/relationships"
    sns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    dns = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
    ans = "http://schemas.openxmlformats.org/drawingml/2006/main"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{sns}" xmlns:r="{rns}"><sheets><sheet name="口播" sheetId="1" r:id="r1"/><sheet name="隐藏参数" sheetId="2" state="hidden" r:id="r2"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{relns}"><Relationship Id="r1" Target="worksheets/sheet1.xml"/><Relationship Id="r2" Target="worksheets/sheet2.xml"/></Relationships>')
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{sns}"><si><t>产品A</t></si><si><t>报价107新台币</t></si></sst>')
        sheet = f'<worksheet xmlns="{sns}" xmlns:r="{rns}"><dimension ref="A1"/><cols><col min="3" max="3" hidden="1"/></cols><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row><row r="9" hidden="1"><c r="C9" t="s"><v>1</v></c><c r="D9"><f>SUM(1,2)</f><v>3</v></c><c r="E9"><f>NOW()</f></c></row></sheetData><mergeCells><mergeCell ref="A1:B1"/></mergeCells><drawing r:id="d1"/></worksheet>'
        if dtd:
            sheet = '<!DOCTYPE x [<!ENTITY xxx "bad">]>' + sheet
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{sns}"><sheetData><row r="25"><c r="Z25" t="inlineStr"><is><t>隐藏规格500mAh</t></is></c></row></sheetData></worksheet>')
        archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", f'<Relationships xmlns="{relns}"><Relationship Id="d1" Target="../drawings/drawing1.xml"/></Relationships>')
        archive.writestr("xl/drawings/drawing1.xml", f'<d:wsDr xmlns:d="{dns}" xmlns:a="{ans}" xmlns:r="{rns}"><d:oneCellAnchor><d:from><d:col>1</d:col><d:row>8</d:row></d:from><d:pic><d:nvPicPr><d:cNvPr id="1" name="参数图"/></d:nvPicPr><a:blip r:embed="im1"/></d:pic></d:oneCellAnchor><d:oneCellAnchor><d:from><d:col>2</d:col><d:row>8</d:row></d:from><d:pic><a:blip r:embed="im2"/></d:pic></d:oneCellAnchor></d:wsDr>')
        archive.writestr("xl/drawings/_rels/drawing1.xml.rels", f'<Relationships xmlns="{relns}"><Relationship Id="im1" Target="../media/image1.png"/><Relationship Id="im2" Target="../media/image2.png"/><Relationship Id="external" Target="https://must-not-fetch.invalid/" TargetMode="External"/></Relationships>')
        archive.writestr("xl/media/image1.png", image_bytes())
        archive.writestr("xl/media/image2.png", image_bytes())
        archive.writestr("xl/media/orphan.png", image_bytes("blue"))
        if macro:
            archive.writestr("xl/vbaProject.bin", b"never-run")
    return output.getvalue()


def test_xlsx_actual_cells_hidden_sheets_formula_cache_and_visual_index(materials):
    data = workbook_bytes()
    accepted = materials.accept("c", "商品.xlsx", "application/octet-stream", data)
    assert accepted["status"] == "ready_with_visual_pending"
    full = materials.read(accepted["material_ref"], "c")
    manifest = full["manifest"]
    assert "报价107新台币" in full["text"]
    assert "隐藏规格500mAh" in full["text"]
    assert manifest["sheets"][0]["declared_dimension"] == "A1"
    assert manifest["sheets"][0]["cell_count"] == 4
    assert manifest["sheets"][1]["state"] == "hidden"
    assert manifest["coverage"]["source_media_count"] == 3
    assert manifest["coverage"]["embedded_image_count"] == 2  # same-image dedup
    assert manifest["coverage"]["orphan_image_count"] == 1
    assert manifest["coverage"]["business_understanding_complete"] is False
    formula = materials.read(accepted["material_ref"], "c", "sheet:1:cell:D9")["manifest"]["locators"][0]
    assert formula["text"] == "3" and formula["formula"] == "SUM(1,2)"
    assert formula["formula_executed"] is False
    missing_cache = materials.read(accepted["material_ref"], "c", "sheet:1:cell:E9")["manifest"]["locators"][0]
    assert missing_cache["cache_present"] is False and missing_cache["text"] == ""
    assert manifest["original_sha256"] == "sha256:" + hashlib.sha256(data).hexdigest()


def test_overview_and_precise_read_dont_attach_all_embedded_images(materials):
    accepted = materials.accept("c", "商品.xlsx", "application/octet-stream", workbook_bytes())
    ref = accepted["material_ref"]
    overview = materials.read(ref, "c", "overview")
    assert overview["view"] == "overview"
    assert overview["manifest"]["sections"][0]["read_args"]["section"] == "sheet:1"
    assert overview["manifest"]["embedded_images"][0]["read_args"]["section"] == "image:1"
    assert materials.image_messages(ref, "c") == []
    assert materials.image_messages(ref, "c", "full") == []
    blocks = materials.image_messages(ref, "c", "image:1")
    assert len(blocks) == 2 and "image:1" in blocks[0]["text"]
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "报价107新台币" in materials.read(ref, "c", "sheet:1")["text"]
    assert "隐藏规格" not in materials.read(ref, "c", "sheet:1")["text"]
    with pytest.raises(ContractError):
        materials.read(ref, "other-conversation", "image:1")


def test_docx_preserves_paragraph_table_order_and_image_anchor(materials):
    from docx import Document
    doc = Document()
    doc.add_paragraph("第一段商品名称")
    doc.add_table(rows=1, cols=2).cell(0, 0).text = "中间参数"
    doc.add_paragraph("最后一段活动")
    doc.add_picture(BytesIO(image_bytes()))
    buffer = BytesIO()
    doc.save(buffer)
    accepted = materials.accept("c", "资料.docx", "application/octet-stream", buffer.getvalue())
    ref = accepted["material_ref"]
    full = materials.read(ref, "c")
    assert full["text"].index("第一段") < full["text"].index("中间参数") < full["text"].index("最后一段")
    image = full["manifest"]["embedded_images"][0]
    assert image["anchors"][0]["locator_id"] == "paragraph:3"
    assert len(materials.image_messages(ref, "c", "paragraph:3")) == 2
    assert materials.read(ref, "c", "table:1:row:1:cell:1")["text"] == "中间参数"


def test_rtf_visible_text_multibyte_unicode_fields_and_image(materials):
    chinese = "商品名：中文杯".encode("gbk")
    escaped = b"".join(b"\\'" + bytes(f"{byte:02x}", "ascii") for byte in chinese)
    raw_image = image_bytes()
    data = (b"{\\rtf1\\ansi\\ansicpg936{\\fonttbl{\\f0 SECRET_FONT;}}{\\listtext garbage-style}" + escaped +
            b"\\par Unicode\\u20013?\\u25991? {\\field{\\*\\fldinst INCLUDETEXT dangerous.invalid}{\\fldrslt visible result}}" +
            b"{\\pict\\pngblip " + raw_image.hex().encode() + b"}}")
    accepted = materials.accept("c", "商品.rtf", "text/rtf", data)
    ref = accepted["material_ref"]
    text = materials.read(ref, "c")["text"]
    assert "商品名：中文杯" in text and "Unicode中文" in text
    assert "visible result" in text
    assert "SECRET_FONT" not in text and "dangerous.invalid" not in text
    assert "garbage-style" not in text
    assert materials.image_messages(ref, "c") == []
    assert len(materials.image_messages(ref, "c", "image:1")) == 2


@pytest.mark.parametrize("kwargs", [{"macro": True}, {"dtd": True}])
def test_untrusted_office_active_content_and_dtd_rejected(materials, kwargs):
    with pytest.raises(ContractError):
        materials.accept("c", "不可信.xlsx", "application/octet-stream", workbook_bytes(**kwargs))


def test_format_specific_upload_budgets_do_not_relax_text_or_image():
    assert max_bytes_for_extension(".xlsx") == 128 * 1024 * 1024
    for ext in [".txt", ".png", ".docx", ".rtf", ".doc", ".pdf"]:
        assert max_bytes_for_extension(ext) == 10 * 1024 * 1024


def test_rtf_unclosed_group_rejected_and_ole_is_explicitly_partial(materials):
    with pytest.raises(ContractError):
        parse_rtf(b"{\\rtf1 unclosed")
    accepted = materials.accept("c", "object.rtf", "application/octet-stream", b"{\\rtf1 Text{\\object{\\objdata BAD}}}")
    assert accepted["status"] == "partially_extracted"
    full = materials.read(accepted["material_ref"], "c")
    assert "BAD" not in full["text"]
    assert full["manifest"]["coverage"]["processed_text_complete"] is False


def test_rtf_word_nonbreaking_hyphen_does_not_disappear_from_specs():
    parsed = parse_rtf(b"{\\rtf1\\ansi Type\\'1eC}")
    assert parsed["text"] == "Type\u2011C"


def test_legacy_doc_missing_converter_fails_explicitly_not_ready(materials, monkeypatch):
    import shengbo_prelive.material_parsers as parser
    from types import SimpleNamespace
    monkeypatch.setattr(parser.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=2))
    with pytest.raises(ContractError) as caught:
        materials.accept("c", "legacy.doc", "application/msword", bytes.fromhex("d0cf11e0a1b11ae1") + b"not-a-real-doc")
    assert caught.value.code == "MATERIAL_DOC_CONVERTER_UNAVAILABLE"
    assert materials.store.list("material") == []
