"""Small synthetic PDF corpus; no merchant original, model or paid service."""
from io import BytesIO
import importlib.util
import subprocess
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive import material_pdf_reader as pdf
from shengbo_prelive import material_chinese_ocr as chinese_ocr


def _text_stream(lines):
    commands = ["BT /F1 12 Tf"]
    for text, x, y in lines:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(f"1 0 0 1 {x} {y} Tm ({escaped}) Tj")
    commands.append("ET")
    return "\n".join(commands).encode("ascii")


def make_pdf(kind="text", pages=1):
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                               NameObject("/Subtype"): NameObject("/Type1"),
                                               NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for number in range(pages):
        page = writer.add_blank_page(width=595, height=842)
        resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        commands = b""
        if kind in {"text", "mixed"}:
            count = 24 if kind == "text" else 1
            commands = _text_stream([(f"Product material page {number + 1} line {row}: natural cotton towel price 29 yuan.",
                                      36, 790 - row * 22) for row in range(count)])
        if kind in {"scan", "mixed", "chinese_scan"}:
            bitmap = Image.new("RGB", (1000, 600), "white")
            painter = ImageDraw.Draw(bitmap)
            try:
                font_path = "C:/Windows/Fonts/msyh.ttc" if kind == "chinese_scan" else "C:/Windows/Fonts/arial.ttf"
                image_font = ImageFont.truetype(font_path, 56)
            except OSError:
                image_font = ImageFont.load_default(size=42)
            painter.text((55, 70), "纯棉毛巾 价格29元" if kind == "chinese_scan" else "Product image price 29 yuan", fill="black", font=image_font)
            painter.text((55, 170), "净含量500克" if kind == "chinese_scan" else "Cotton towel weight 500 g", fill="black", font=image_font)
            stream = DecodedStreamObject()
            stream.set_data(bitmap.tobytes())
            stream.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                           NameObject("/Width"): NumberObject(bitmap.width), NameObject("/Height"): NumberObject(bitmap.height),
                           NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
            resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Im0"): writer._add_object(stream.flate_encode())})
            commands += b"\nq 520 0 0 312 36 360 cm /Im0 Do Q\n"
        if kind == "table":
            commands = _text_stream([("Product", 45, 750), ("Price", 260, 750), ("Weight", 400, 750),
                                     ("Cotton towel", 45, 710), ("29 yuan", 260, 710), ("500 g", 400, 710),
                                     ("Linen towel", 45, 670), ("39 yuan", 260, 670), ("600 g", 400, 670)])
            commands += b"\n0.5 w 36 780 m 540 780 l S 36 735 m 540 735 l S 36 695 m 540 695 l S 36 650 m 540 650 l S\n"
            commands += b"36 650 m 36 780 l S 240 650 m 240 780 l S 380 650 m 380 780 l S 540 650 m 540 780 l S\n"
        page[NameObject("/Resources")] = resources
        stream = DecodedStreamObject()
        stream.set_data(commands)
        page[NameObject("/Contents")] = writer._add_object(stream)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


requires_sdk = pytest.mark.skipif(importlib.util.find_spec("liteparse") is None, reason="LiteParse SDK is not installed")


@requires_sdk
def test_native_text_uses_sdk_blocks_and_page_locators_without_ocr():
    manifest = pdf.parse_pdf(make_pdf("text", pages=2))
    assert manifest["extractor"].startswith("liteparse:")
    assert manifest["parse_metrics"]["ocr_performed"] is False
    assert manifest["parse_metrics"]["model_calls"] == 0
    assert manifest["parse_metrics"]["isolated_subprocess"] is True
    assert manifest["parse_metrics"]["block_count"] > 0
    assert manifest["coverage"]["page_count"] == 2
    assert "Product material page 2" in manifest["text"]
    assert {"page:1", "page:2"} <= {item["id"] for item in manifest["locators"]}
    assert manifest["pages"][0]["complexity"] is not None


@requires_sdk
@pytest.mark.parametrize("kind", ["scan", "mixed"])
def test_scan_and_mixed_text_image_pages_remain_visual_pending(kind):
    manifest = pdf.parse_pdf(make_pdf(kind))
    assert manifest["coverage"]["pending_visual"] is True
    assert manifest["coverage"]["pages_pending_visual"] == [1]
    image = manifest["embedded_images"][0]
    assert image["id"] == "image:1"
    assert image["source"] == "pdf_page_render"
    assert image["anchors"][0]["locator_id"] == "page:1"
    assert image["visual_status"] == "pending_visual_read"
    assert "png" not in image and "sha256" not in image  # rendered only on demand
    assert manifest["parse_metrics"]["ocr_performed"] is False
    assert manifest["coverage"]["business_understanding_complete"] is False
    if kind == "mixed":
        assert "Product material" in manifest["text"]
        assert manifest["pages"][0]["image_resource_count"] == 1
    else:
        assert manifest["coverage"]["pages_without_text"] == [1]
        assert manifest["coverage"]["processed_text_complete"] is False


@requires_sdk
def test_table_retains_structured_blocks_and_values():
    manifest = pdf.parse_pdf(make_pdf("table"))
    assert manifest["extractor"].startswith("liteparse:")
    assert "29 yuan" in manifest["text"] and "500 g" in manifest["text"]
    blocks = manifest["pages"][0]["blocks"]
    assert any(block["kind"] == "table" and block.get("rows") for block in blocks)
    assert manifest["coverage"]["pending_visual"] is True


@requires_sdk
def test_lazy_page_render_returns_only_selected_bounded_png():
    data = make_pdf("text", pages=2)
    png = pdf.render_pdf_page(data, 2)
    with Image.open(BytesIO(png)) as image:
        assert image.format == "PNG"
        assert image.width * image.height <= pdf.MAX_RENDER_PIXELS
        assert max(image.size) <= pdf.MAX_RENDER_DIMENSION
    assert len(png) <= pdf.MAX_RENDER_BYTES
    with pytest.raises(ContractError) as error:
        pdf.render_pdf_page(data, 3)
    assert error.value.code == "MATERIAL_SECTION_NOT_FOUND"


def test_strict_encryption_page_count_and_input_preflight():
    with pytest.raises(ContractError) as error:
        pdf.parse_pdf(b"hello")
    assert error.value.code == "MATERIAL_PDF_INVALID"
    with pytest.raises(ContractError) as error:
        pdf.parse_pdf(make_pdf("text", pages=65))
    assert error.value.code == "MATERIAL_PDF_PAGE_OR_ENCRYPTION_LIMIT"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    encrypted = BytesIO()
    writer.write(encrypted)
    with pytest.raises(ContractError) as error:
        pdf.parse_pdf(encrypted.getvalue())
    assert error.value.code == "MATERIAL_PDF_PAGE_OR_ENCRYPTION_LIMIT"
    with pytest.raises(ContractError) as error:
        pdf.render_pdf_page(make_pdf(), True)
    assert error.value.code == "MATERIAL_SECTION_NOT_FOUND"


def test_sdk_failure_has_explicit_fallback_and_conservative_visual_coverage(monkeypatch):
    def missing(_):
        raise ImportError("SDK unavailable")
    monkeypatch.setattr(pdf, "_native_extract", missing)
    data = make_pdf("mixed")
    with pdf._pypdf_limits():
        reader, inventory = pdf._preflight(data)
        manifest = pdf._parse_local(data, reader, inventory, [])
    assert manifest["extractor"] == "pypdf:compatibility_fallback"
    assert manifest["parse_metrics"]["fallback_reason"] == "ImportError"
    assert manifest["coverage"]["pending_visual"] is True
    assert "fallback_visual_coverage_unknown" in manifest["pages"][0]["visual_reasons"]


def test_missing_chinese_language_data_is_explicit_and_does_not_start_ocr(monkeypatch):
    monkeypatch.setenv("SHENGBO_OCR_BACKEND", "tesseract")
    monkeypatch.setattr(pdf, "_tessdata_directory", lambda: None)
    def unexpected_render(*_):
        pytest.fail("OCR language readiness must be checked before rendering or downloading")
    monkeypatch.setattr(pdf, "_render_local", unexpected_render)
    with pytest.raises(ContractError) as error:
        pdf._ocr_local(make_pdf("scan"), {"number": 1}, [])
    assert error.value.code == "MATERIAL_OCR_LANGUAGE_NOT_READY"
    assert error.value.details["visual_fallback"] == "read_original_image"


def test_subprocess_timeout_is_not_retried_as_unbounded_fallback(monkeypatch):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(pdf.subprocess, "run", timeout)
    with pytest.raises(ContractError) as error:
        pdf.parse_pdf(make_pdf("text"))
    assert error.value.code == "MATERIAL_PDF_OPERATION_TIMEOUT"
    assert error.value.details["timeout_seconds"] == pdf.PARSE_TIMEOUT_SECONDS


def test_image_ocr_enforces_input_byte_budget_and_rejects_invalid_raster():
    with pytest.raises(ContractError) as error:
        pdf.ocr_image(b"x" * (pdf.MAX_IMAGE_BYTES + 1))
    assert error.value.code == "MATERIAL_IMAGE_BYTE_LIMIT"
    with pytest.raises(ContractError) as error:
        pdf.ocr_image(b"not an image")
    assert error.value.code == "MATERIAL_IMAGE_INVALID"


@pytest.mark.parametrize("image_source,source", [(False, "page:3"), (True, "image:1")])
def test_rapidocr_adapter_preserves_negation_confidence_and_image_coordinates(monkeypatch, image_source, source):
    # Captured official RapidOCR result shape; no repeated OCR/model inference.
    output = SimpleNamespace(txts=("不包邮", "不含贈品"), scores=(0.99996, 0.99918),
                             boxes=SimpleNamespace(tolist=lambda: [[[124, 616], [383, 616], [383, 719], [124, 719]],
                                                                   [[57, 246], [425, 246], [425, 359], [57, 359]]]),
                             elapse=0.2, elapse_list=[0.1, 0.01, 0.09])
    caps = {"ready": True, "models": [], "rapidocr_version": "3.10.0", "onnxruntime_version": "1.31.0"}
    monkeypatch.setattr(chinese_ocr, "rapidocr_capabilities", lambda: caps)
    monkeypatch.setattr(chinese_ocr, "_build_engine", lambda _: lambda png: output)
    monkeypatch.setattr(pdf, "_ocr_backend_info", lambda: {"selected": "rapidocr", "ready": True})
    png = BytesIO()
    Image.new("RGB", (1200, 1754), "white").save(png, format="PNG")
    result = pdf._ocr_png(png.getvalue(), {"number": 3}, [], image_source=image_source)
    assert result["text"] == "不包邮\n不含贈品"
    assert result["source_locator"] == source
    assert result["locators"][0]["confidence"] == 0.99996
    assert result["locators"][0]["bbox"] == {"x": 124.0, "y": 616.0, "width": 259.0, "height": 103.0}
    assert result["locators"][0]["coordinate_space"] == "ocr_input_image_pixels"
    assert result["parse_metrics"]["ocr_engine"] == "rapidocr_onnxruntime_cpu"
    assert result["parse_metrics"]["network_downloads_enabled"] is False
    assert result["coverage"]["pending_visual"] is True


@requires_sdk
def test_local_chinese_ocr_is_opt_in_single_page_and_reports_real_confidence():
    if not pdf.pdf_reader_capabilities()["ocr_language_ready"]:
        pytest.skip("Offline chi_sim+eng traineddata is not provisioned")
    result = pdf.ocr_pdf_page(make_pdf("chinese_scan"), 1)
    assert "29" in result["text"] and "500" in result["text"]
    assert any(word in result["text"] for word in ("毛巾", "净", "价格"))
    assert result["language"] == pdf.OCR_LANGUAGE
    assert result["source_locator"] == "page:1"
    assert result["parse_metrics"]["ocr_performed"] is True
    assert result["parse_metrics"]["selected_page_count"] == 1
    assert result["parse_metrics"]["local_only"] is True
    assert result["confidence"] == "unknown" or 0 <= result["confidence"] <= 1
    assert result["coverage"]["visual_content_complete"] is False
