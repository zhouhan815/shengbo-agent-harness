"""SDK integration preserves source facts, sparse IO and honest read coverage."""
from datetime import datetime
from io import BytesIO
import zipfile

from docx import Document
from docx.oxml import OxmlElement
from openpyxl import Workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904
import pytest

from shengbo_prelive.material_parsers import parse_docx, parse_rtf, parse_xlsx


def _bytes(document):
    result = BytesIO()
    document.save(result)
    return result.getvalue()


def _replace_member(data, member, transform):
    result = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            target.writestr(item.filename, transform(payload) if item.filename == member else payload)
    return result.getvalue()


def test_docx_sdk_physical_merged_cells_nested_content_and_story_supplement():
    doc = Document()
    doc.add_paragraph("First paragraph")
    table = doc.add_table(rows=2, cols=3)
    table.cell(0, 0).text = "Merged specification"
    table.cell(0, 0).merge(table.cell(1, 1))
    table.cell(0, 2).text = "Tail column"
    nested = table.cell(1, 2).add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "Nested specification"
    # python-docx Paragraph.text skips content controls; the supplemental
    # source traversal must retain this visible text in its original position.
    paragraph = doc.add_paragraph("Last paragraph ")
    sdt, content, run, text = (OxmlElement(name) for name in ("w:sdt", "w:sdtContent", "w:r", "w:t"))
    text.text = "content-control fact"
    run.append(text)
    content.append(run)
    sdt.append(content)
    paragraph._p.append(sdt)
    doc.sections[0].header.paragraphs[0].text = "Header specification"
    parsed = parse_docx(_bytes(doc))
    cells = [x for x in parsed["locators"] if x["kind"] == "table_cell"]
    assert len(cells) == 4  # the SDK rectangular grid would return six cells
    assert sum("Merged specification" in x["text"] for x in cells) == 1
    assert parsed["text"].count("Merged specification") == 1
    assert parsed["text"].count("Nested specification") == 1
    assert "content-control fact" in parsed["text"] and "Header specification" in parsed["text"]
    assert parsed["coverage"]["sdk_text_locator_count"] > 0
    assert parsed["coverage"]["supplemented_text_locator_count"] > 0
    assert cells[0]["column_span"] == 2 and cells[2]["vertical_merge"] == "continue"
    assert parsed["parser"]["libraries"][0] == {"name": "python-docx", "version": "1.2.0"}


@pytest.mark.parametrize("date1904", [False, True])
def test_xlsx_sdk_dates_percent_currency_and_original_formula_cache(date1904, monkeypatch):
    import openpyxl
    # Production must not load the entire media container or trust dimensions.
    monkeypatch.setattr(openpyxl, "load_workbook", lambda *a, **k: pytest.fail("whole-workbook loading"))
    wb = Workbook()
    if date1904:
        wb.epoch = CALENDAR_MAC_1904
    ws = wb.active
    ws.title = "Visible"
    ws["A1"], ws["B1"], ws["C1"], ws["D1"] = 0.125, 1234.5, datetime(2026, 10, 19), "=A1*2"
    ws["A1"].number_format = "0.0%"
    ws["B1"].number_format = '"NT$"#,##0.00'
    ws["C1"].number_format = "yyyy-mm-dd"
    ws["D1"].number_format = "0.0%"
    ws["XFD1048576"] = "Sparse tail specification"
    hidden = wb.create_sheet("Hidden")
    hidden.sheet_state = "hidden"
    hidden["Z99"] = "Hidden fact"
    data = _bytes(wb)
    # A cached formula result is a source value and must never be recalculated.
    data = _replace_member(data, "xl/worksheets/sheet1.xml", lambda raw: raw.replace(b"<f>A1*2</f><v></v>", b"<f>A1*2</f><v>0.25</v>").replace(b'<dimension ref="A1:XFD1048576"/>', b'<dimension ref="A1"/>'))
    parsed = parse_xlsx(data)
    cells = {x["cell"]: x for x in parsed["locators"] if x["kind"] == "spreadsheet_cell" and x["sheet_id"] == "sheet:1"}
    assert len(cells) == 5
    assert cells["A1"]["text"] == "12.5%" and cells["A1"]["raw_value"] == "0.125"
    assert cells["B1"]["text"] == "NT$1,234.50"
    assert cells["C1"]["text"].startswith("2026-10-19")
    assert cells["C1"]["date_system"] == ("1904" if date1904 else "1900")
    assert cells["D1"]["text"] == "25.0%" and cells["D1"]["cached_value"] == "0.25"
    assert cells["D1"]["formula"] == "A1*2" and cells["D1"]["formula_executed"] is False
    assert "Hidden fact" in parsed["text"]
    assert parsed["sheets"][0]["declared_dimension"] == "A1"
    assert parsed["coverage"]["full_workbook_loaded"] is False


def test_xlsx_unrendered_number_formats_are_explicit_and_raw_retained():
    wb = Workbook()
    wb.active["A1"] = 0.5
    wb.active["A1"].number_format = "# ?/?"
    parsed = parse_xlsx(_bytes(wb))
    cell = parsed["locators"][0]
    assert cell["text"] == "0.5" and cell["raw_value"] == "0.5"
    assert cell["format_supported"] is False
    assert "some_cell_number_formats_preserved_raw_not_fully_rendered" in parsed["unknowns"]


def test_rtf_sdk_font_codepages_story_text_and_binary_picture_source():
    chinese = "繁體商品".encode("cp950")
    escaped = b"".join(b"\\'" + format(x, "02x").encode() for x in chinese)
    # The declared global codepage differs from the selected font. striprtf's
    # per-font charset decoder is needed; the old global decoder lost these.
    payload = (b"{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0\\fcharset136 Ming;}}\\f0 " + escaped +
               b"\\par{\\header Header fact}{\\shptxt Textbox fact} \\u-10179?\\u-8704?}")
    parsed = parse_rtf(payload)
    assert "繁體商品" in parsed["text"] and "😀" in parsed["text"]
    assert "Header fact" in parsed["text"] and "Textbox fact" in parsed["text"]
    assert "Ming;" not in parsed["text"]
    assert parsed["parser"]["libraries"][0]["name"] == "striprtf"
    raw_image = b"\x89PNG\r\n\x1a\n" + b"{\\}\\'" * 9
    binary = b"{\\rtf1 before{\\pict\\pngblip\\bin" + str(len(raw_image)).encode() + b" " + raw_image + b"}after}"
    image_parse = parse_rtf(binary)
    assert image_parse["text"] == "beforeafter"
    assert image_parse["embedded_images"][0]["bytes"] == len(raw_image)
