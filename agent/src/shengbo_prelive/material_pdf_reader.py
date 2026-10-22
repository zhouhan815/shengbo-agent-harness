"""Local PDF reader: bounded structural intake, lazy page renders and opt-in OCR.

All untrusted PDF work runs in a disposable subprocess. Upload-time parsing never
enables OCR or calls a model. The pypdf pass validates and inventories resources;
LiteParse performs the single normal text/layout extraction pass.
"""
from __future__ import annotations

import base64
from contextlib import nullcontext, redirect_stdout
from dataclasses import asdict, is_dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO, StringIO
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import warnings

from shengbo_contracts.common import ContractError

ENGINE_VERSION = "pdf-reader-sdk-v1"
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_DIMENSION = 12000
MAX_PAGES = 64
MAX_TEXT = 2_000_000
MAX_RENDER_PIXELS = 12_000_000
MAX_RENDER_DIMENSION = 6000
MAX_RENDER_BYTES = 16 * 1024 * 1024
MAX_RESOURCE_IMAGE_PIXELS = 40_000_000
MAX_DECODED_CONTENT_BYTES = 40_000_000
MAX_PROTOCOL_BYTES = 48 * 1024 * 1024
PARSE_TIMEOUT_SECONDS = 30
RENDER_TIMEOUT_SECONDS = 20
OCR_TIMEOUT_SECONDS = 45
OCR_LANGUAGE = "chi_sim+chi_tra+eng"


def _fail(code, message, *, status=422, details=None):
    raise ContractError(code, message, status=status, details=details)


def _distribution_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return "unavailable"


def _tessdata_directory():
    candidates = [os.environ.get("SHENGBO_TESSDATA_PATH"), os.environ.get("TESSDATA_PREFIX")]
    candidates.append(str(Path(__file__).resolve().parents[2] / "runtime" / "reader-models" / "tessdata"))
    if os.environ.get("APPDATA"):
        candidates.append(str(Path(os.environ["APPDATA"]) / "tesseract-rs" / "tessdata"))
    candidates.extend([str(Path.home() / ".tesseract-rs" / "tessdata"),
                       str(Path.home() / ".cache" / "liteparse" / "tessdata")])
    for candidate in candidates:
        if candidate:
            directory = Path(candidate).expanduser()
            if all((directory / (language + ".traineddata")).is_file()
                   and (directory / (language + ".traineddata")).stat().st_size > 100_000
                   for language in ("chi_sim", "chi_tra", "eng")):
                return directory.resolve()
    return None


def pdf_reader_capabilities():
    """Report installed engines and offline OCR readiness without loading a PDF."""
    tessdata = _tessdata_directory()
    backend = _ocr_backend_info()
    return {"engine": ENGINE_VERSION, "liteparse_version": _distribution_version("liteparse"),
            "pypdf_version": _distribution_version("pypdf"), "local_only": True,
            "upload_ocr_enabled": False, "lazy_page_render": True,
            "ocr_image_supported": True,
            "ocr_language": OCR_LANGUAGE, "ocr_language_ready": backend["ready"],
            "ocr_backend": backend["selected"], "ocr_backend_details": backend,
            "tesseract_language_ready": tessdata is not None,
            "tessdata_path": str(tessdata) if tessdata else None,
            "limits": {"input_bytes": MAX_PDF_BYTES, "pages": MAX_PAGES,
                       "ocr_image_bytes": MAX_IMAGE_BYTES, "ocr_image_pixels": MAX_IMAGE_PIXELS,
                       "ocr_image_dimension": MAX_IMAGE_DIMENSION,
                       "render_pixels": MAX_RENDER_PIXELS, "render_dimension": MAX_RENDER_DIMENSION,
                       "parse_timeout_seconds": PARSE_TIMEOUT_SECONDS,
                       "render_timeout_seconds": RENDER_TIMEOUT_SECONDS, "ocr_timeout_seconds": OCR_TIMEOUT_SECONDS}}


def _validate_input(data, page_no=None, *, image=False):
    if image:
        if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
            _fail("MATERIAL_IMAGE_BYTE_LIMIT", "OCR图片须为非空文件，且不超过24MiB", status=413)
        return
    if not isinstance(data, bytes) or not data or len(data) > MAX_PDF_BYTES:
        _fail("MATERIAL_SIZE_INVALID", "PDF须为非空文件，且不超过10MiB", status=413)
    if not data.startswith(b"%PDF-"):
        _fail("MATERIAL_PDF_INVALID", "PDF文件头无效")
    if page_no is not None and (isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 1 or page_no > MAX_PAGES):
        _fail("MATERIAL_SECTION_NOT_FOUND", "PDF页码无效", status=404)


def _isolated(operation, data, page_no=None):
    _validate_input(data, page_no, image=operation == "image_ocr")
    started = time.perf_counter()
    deadline = {"parse": PARSE_TIMEOUT_SECONDS, "render": RENDER_TIMEOUT_SECONDS,
                "ocr": OCR_TIMEOUT_SECONDS, "image_ocr": OCR_TIMEOUT_SECONDS}[operation]
    # Executing this file avoids importing the app/runtime in the worker. No shell,
    # user paths or cloud endpoints participate in the subprocess command.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # The parent owns scratch cleanup, including when a timed-out worker is
    # forcibly killed before its Python context managers can run.
    with tempfile.TemporaryDirectory(prefix="shengbo-pdf-op-") as scratch, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        request = json.dumps({"operation": operation, "data": base64.b64encode(data).decode("ascii"),
                              "page": page_no, "scratch": scratch}).encode()
        try:
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                       input=request, stdout=output, stderr=errors, timeout=deadline,
                                       check=False, creationflags=creationflags)
        except subprocess.TimeoutExpired:
            _fail("MATERIAL_PDF_OPERATION_TIMEOUT", "PDF本地读取超时，请缩小页数或按单页截图提供", status=408,
                  details={"operation": operation, "timeout_seconds": deadline})
        output.seek(0, 2)
        if output.tell() > MAX_PROTOCOL_BYTES:
            _fail("MATERIAL_PDF_OUTPUT_LIMIT", "PDF结构化结果超出安全预算", status=413)
        output.seek(0)
        payload = output.read()
    try:
        response = json.loads(payload)
    except (ValueError, UnicodeError):
        _fail("MATERIAL_PDF_WORKER_FAILED", "PDF本地读取进程未能返回完整结果", status=422)
    if completed.returncode or "error" in response:
        error = response.get("error", {})
        _fail(error.get("code", "MATERIAL_PDF_WORKER_FAILED"), error.get("message", "PDF本地读取失败"),
              status=error.get("status", 422), details=error.get("details"))
    result = response["result"]
    if operation != "render":
        result.setdefault("parse_metrics", {})["total_operation_ms"] = round((time.perf_counter() - started) * 1000, 3)
        result["parse_metrics"].update(isolated_subprocess=True, timeout_seconds=deadline)
    return result


def parse_pdf(data: bytes):
    """Extract native text/layout once, leaving scanned and visual content pending."""
    return _isolated("parse", data)


def render_pdf_page(data: bytes, page_no: int) -> bytes:
    """Render exactly one 1-based PDF page as bounded PNG bytes, without OCR."""
    return base64.b64decode(_isolated("render", data, page_no)["png"], validate=True)


def ocr_pdf_page(data: bytes, page_no: int):
    """OCR one rendered page locally using cached simplified/traditional Chinese and English data.

    OCR output remains untrusted evidence with explicit confidence provenance;
    it does not mutate the intake manifest or claim business understanding.
    """
    return _isolated("ocr", data, page_no)


def ocr_image(data: bytes):
    """OCR a bounded raster image locally; source_locator is image:1 by default.

    A material reader can replace image:1 with the selected embedded-image ID
    before returning this result. Intake itself never calls this function.
    """
    return _isolated("image_ocr", data)


def _pypdf_limits():
    from pypdf import apply_configuration
    return apply_configuration(maximum_declared_stream_length=20_000_000, zlib_maximum_output_length=20_000_000,
                               array_based_stream_maximum_output_length=20_000_000, lzw_maximum_output_length=20_000_000,
                               run_length_maximum_output_length=20_000_000, brotli_maximum_output_length=20_000_000,
                               stream_decoding_work_maximum_length=40_000_000, page_tree_maximum_entries=1000,
                               xform_maximum_invocations_per_extraction=200)


def _bounded_content(stream, budget):
    content = stream.get_data() if stream is not None else b""
    budget["content_bytes"] += len(content)
    if len(content) > 20_000_000 or budget["content_bytes"] > MAX_DECODED_CONTENT_BYTES:
        _fail("MATERIAL_PDF_CONTENT_STREAM_LIMIT", "PDF解压内容流超出安全预算")
    return content


def _resource_images(resources, budget, seen=None, depth=0):
    """Inventory image references without decoding large image pixel streams."""
    seen = set() if seen is None else seen
    if depth > 8 or len(seen) > 256:
        _fail("MATERIAL_PDF_RESOURCE_LIMIT", "PDF图形资源嵌套或数量超出安全预算")
    if resources is None:
        return 0
    resources = resources.get_object()
    objects = resources.get("/XObject", {})
    objects = objects.get_object() if hasattr(objects, "get_object") else objects
    count = 0
    for reference in objects.values():
        identity = (getattr(reference, "idnum", None), getattr(reference, "generation", None)) if hasattr(reference, "idnum") else id(reference)
        if identity in seen:
            continue
        seen.add(identity)
        if len(seen) > 256:
            _fail("MATERIAL_PDF_RESOURCE_LIMIT", "PDF图形资源数量超出安全预算")
        obj = reference.get_object()
        if obj.get("/Subtype") == "/Image":
            width, height = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
            if width < 1 or height < 1 or width * height > MAX_RESOURCE_IMAGE_PIXELS or max(width, height) > 20000:
                _fail("MATERIAL_PDF_IMAGE_DIMENSION_LIMIT", "PDF内嵌图像尺寸超出安全预算")
            count += 1
        elif obj.get("/Subtype") == "/Form":
            content = _bounded_content(obj, budget)
            count += bool(re.search(rb"(?:^|\s)BI(?:\s|$)", content))
            count += _resource_images(obj.get("/Resources"), budget, seen, depth + 1)
    return count


def _preflight(data, page_no=None):
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(data), strict=True, root_object_recovery_limit=1000)
    if reader.is_encrypted:
        _fail("MATERIAL_PDF_PAGE_OR_ENCRYPTION_LIMIT", "不接受加密PDF，请提供解密后的副本")
    page_count = len(reader.pages)
    if not 1 <= page_count <= MAX_PAGES:
        _fail("MATERIAL_PDF_PAGE_OR_ENCRYPTION_LIMIT", "PDF须为1至64页")
    if page_no is not None and page_no > page_count:
        _fail("MATERIAL_SECTION_NOT_FOUND", "未找到对应PDF页", status=404)
    inventory = []
    budget = {"content_bytes": 0}
    for number, page in enumerate(reader.pages, 1):
        user_unit = float(page.get("/UserUnit", 1))
        width, height = abs(float(page.mediabox.width) * user_unit), abs(float(page.mediabox.height) * user_unit)
        if not all(math.isfinite(value) and 1 <= value <= 14400 for value in (width, height)):
            _fail("MATERIAL_PDF_PAGE_DIMENSION_LIMIT", "PDF页面尺寸超出安全预算")
        if int(page.get("/Rotate", 0)) % 180:
            width, height = height, width
        content = _bounded_content(page.get_contents(), budget)
        inline_images = bool(re.search(rb"(?:^|\s)BI(?:\s|$)", content))
        inventory.append({"number": number, "width_points": width, "height_points": height,
                          "image_resource_count": _resource_images(page.get("/Resources"), budget),
                          "inline_image_possible": inline_images,
                          "has_annotations": bool(page.get("/Annots"))})
    return reader, inventory


def _plain(value):
    if is_dataclass(value):
        return asdict(value)
    return value


def _block_text(block):
    if block.get("text"):
        return block["text"]
    rows = ([block["header"]] if block.get("header") else []) + (block.get("rows") or [])
    if rows:
        return "\n".join(" | ".join(cell.get("text") or "" for cell in row) for row in rows)
    return "\n".join(block.get("lines") or [])


def _liteparse():
    try:
        from liteparse import LiteParse
        return LiteParse
    except (ImportError, OSError):
        _fail("MATERIAL_PDF_SDK_UNAVAILABLE", "本地LiteParse SDK未就绪；保留PDF原件，可提供清晰页面截图", status=503)


def _native_extract(data):
    parser = _liteparse()(ocr_enabled=False, extract_blocks=True, include_complexity=True,
                          extract_images=False, extract_screenshots=False, quiet=True,
                          max_pages=MAX_PAGES, num_workers=1, output_format="json")
    try:
        return parser.parse(data)
    finally:
        parser.close()


def _build_manifest(reader, inventory, result, stages, fallback_reason=None):
    pages, locators, sections, images, unknowns = [], [], [], [], []
    total_text = 0
    native_pages = {} if result is None else {int(page.page_num): page for page in result.pages}
    if result is not None and (len(native_pages) != len(inventory) or result.page_errors):
        _fail("MATERIAL_PDF_PAGE_EXTRACTION_INCOMPLETE", "PDF页面提取不完整，请按单页提供")
    for info in inventory:
        number = info["number"]
        native = native_pages.get(number)
        value = (reader.pages[number - 1].extract_text() or "") if result is None else native.text
        total_text += len(value)
        if total_text > MAX_TEXT:
            _fail("MATERIAL_DOCUMENT_TEXT_TOO_LARGE", "PDF文字总量超出安全预算", status=413)
        blocks = [_plain(block) for block in (native.blocks or [])] if native else []
        complexity = _plain(native.complexity) if native else None
        reasons = []
        if not value.strip():
            reasons.append("no_native_text")
        if info["image_resource_count"]:
            reasons.append("embedded_image_resources")
        if info.get("inline_image_possible"):
            reasons.append("inline_image_content")
        if info["has_annotations"]:
            reasons.append("annotations_require_visual_review")
        if complexity:
            if complexity.get("needs_ocr"):
                reasons.extend(complexity.get("reasons", []))
            if complexity.get("image_block_count", 0) or complexity.get("has_substantial_images"):
                reasons.append("image_content_requires_visual_read")
            if (complexity.get("layout") or {}).get("is_complex"):
                reasons.append("complex_layout_requires_visual_review")
        if any(block.get("kind") in {"figure", "table", "grid_fallback"} for block in blocks):
            reasons.append("figure_or_table_requires_visual_review")
        if result is None:
            reasons.append("fallback_visual_coverage_unknown")
        reasons = list(dict.fromkeys(reasons))
        image_ids = [f"image:{number}"] if reasons else []
        page = {**info, "text": value, "blocks": blocks, "complexity": complexity,
                "image_ids": image_ids, "pending_visual": bool(reasons), "visual_reasons": reasons}
        pages.append(page)
        locator = {"id": f"page:{number}", "kind": "page", "page": number, "text": value,
                   "image_ids": image_ids, "confidence": "unknown"}
        locators.append(locator)
        sections.append({**locator, "name": f"第{number}页"})
        for index, block in enumerate(blocks, 1):
            locators.append({"id": f"page:{number}:block:{index}", "kind": "pdf_block", "page": number,
                             "block_kind": block["kind"], "text": _block_text(block),
                             "bbox": block.get("bbox"), "source_locator": f"page:{number}",
                             **({"header": block.get("header"), "rows": block.get("rows")} if block["kind"] == "table" else {}),
                             "confidence": "unknown"})
        if reasons:
            image = {"id": f"image:{number}", "kind": "image", "page": number,
                     "name": f"第{number}页页面图", "mime": "image/png", "source": "pdf_page_render",
                     "render_strategy": "lazy_page", "visual_status": "pending_visual_read",
                     "confidence": "unknown", "anchors": [{"locator_id": f"page:{number}", "page": number}],
                     "text": "", "visual_reasons": reasons}
            images.append(image)
            locators.append(image.copy())
    if images:
        unknowns.extend(["pdf_visual_content_not_read", "ocr_not_performed"])
    if any(not page["text"].strip() for page in pages):
        unknowns.append("no_text_extracted_scanned_pdf_needs_visual_or_text_source")
    if fallback_reason:
        unknowns.append("liteparse_unavailable_or_failed_pypdf_compatibility_fallback")
    text = "\n\n".join(page["text"] for page in pages)
    empty = [page["number"] for page in pages if not page["text"].strip()]
    capabilities = pdf_reader_capabilities()
    return {"kind": "document", "mime": "application/pdf", "text": text, "pages": pages,
            "locators": locators, "sections": sections, "embedded_images": images,
            "extractor": "liteparse:" + capabilities["liteparse_version"] if result is not None else "pypdf:compatibility_fallback",
            "unknowns": unknowns,
            "coverage": {"processed_text_complete": not empty, "pending_visual": bool(images),
                         "pages_without_text": empty, "pages_pending_visual": [image["page"] for image in images],
                         "page_count": len(pages), "business_understanding_complete": False,
                         "read_boundaries": ["native_text_only_at_upload", "pdf_visual_content_requires_explicit_page_read",
                                             "layout_and_tables_are_machine_reconstructed_not_verified"]},
            "parse_metrics": {"stages": stages, "engine": ENGINE_VERSION,
                              "engines": {"liteparse": capabilities["liteparse_version"], "pypdf": capabilities["pypdf_version"]},
                              "capabilities": capabilities, "fallback_reason": fallback_reason,
                              "liteparse_attempts": 1, "pypdf_preflight_passes": 1,
                              "pypdf_text_extraction_passes": 1 if result is None else 0,
                              "ocr_performed": False, "model_calls": 0,
                              "text_characters": len(text), "locator_count": len(locators),
                              "block_count": sum(len(page["blocks"]) for page in pages),
                              "page_count": len(pages), "pending_visual_count": len(images)}}


def _parse_local(data, reader, inventory, stages):
    started = time.perf_counter()
    result, fallback_reason = None, None
    try:
        result = _native_extract(data)
    except Exception as error:
        # This is compatibility recovery, explicitly reflected in the manifest.
        # It never disguises absent OCR/layout as successful visual extraction.
        fallback_reason = getattr(error, "code", type(error).__name__)
    stages.append({"stage": "liteparse_native_structure", "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                   "ocr_enabled": False, "success": result is not None})
    started = time.perf_counter()
    manifest = _build_manifest(reader, inventory, result, stages, fallback_reason)
    stages.append({"stage": "pypdf_compatibility_extract" if result is None else "manifest_assembly",
                   "duration_ms": round((time.perf_counter() - started) * 1000, 3)})
    return manifest


def _render_local(data, info, scratch=None):
    from PIL import Image
    width, height = info["width_points"], info["height_points"]
    # Leave a small rounding margin: PDFium rounds raster dimensions to pixels.
    scale = min(150 / 72, (MAX_RENDER_PIXELS / (width * height)) ** 0.5 * 0.995,
                MAX_RENDER_DIMENSION / max(width, height) * 0.995)
    dpi = scale * 72
    parser = _liteparse()(ocr_enabled=False, quiet=True, max_pages=MAX_PAGES, dpi=dpi, num_workers=1)
    try:
        with (nullcontext(scratch) if scratch else tempfile.TemporaryDirectory(prefix="shengbo-pdf-render-")) as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(data)
            screenshots = parser.screenshot(source, page_numbers=[info["number"]])
    finally:
        parser.close()
    if len(screenshots) != 1 or screenshots[0].page_num != info["number"]:
        _fail("MATERIAL_PDF_RENDER_FAILED", "PDF页面渲染不完整")
    png = screenshots[0].image_bytes
    if len(png) > MAX_RENDER_BYTES:
        _fail("MATERIAL_PDF_RENDER_LIMIT", "PDF页面图像超出字节预算", status=413)
    with Image.open(BytesIO(png)) as image:
        if image.format != "PNG" or image.width * image.height > MAX_RENDER_PIXELS or max(image.size) > MAX_RENDER_DIMENSION:
            _fail("MATERIAL_PDF_RENDER_LIMIT", "PDF页面图像超出像素预算", status=413)
        image.verify()
    return png


def _require_ocr_languages():
    tessdata = _tessdata_directory()
    if tessdata is None:
        _fail("MATERIAL_OCR_LANGUAGE_NOT_READY", "本地简繁中文和英文OCR语言数据未就绪；可继续按page/image读取原图", status=503,
              details={"language": OCR_LANGUAGE, "required_files": [language + ".traineddata" for language in OCR_LANGUAGE.split("+")],
                       "configuration": "SHENGBO_TESSDATA_PATH", "visual_fallback": "read_original_image"})
    return tessdata


def _ocr_backend_info():
    from shengbo_prelive.material_chinese_ocr import rapidocr_capabilities
    rapid = rapidocr_capabilities()
    requested = os.environ.get("SHENGBO_OCR_BACKEND", "auto").strip().lower()
    if requested not in {"auto", "rapidocr", "tesseract"}:
        return {"selected": requested, "ready": False, "reason": "unknown_backend", "rapidocr": rapid}
    if requested == "rapidocr" or (requested == "auto" and rapid["ready"]):
        return {"selected": "rapidocr", "ready": rapid["ready"], "rapidocr": rapid, "fallback_reason": None}
    ready = _tessdata_directory() is not None
    return {"selected": "tesseract", "ready": ready, "rapidocr": rapid,
            "fallback_reason": "rapidocr_unavailable_tesseract_compatibility" if requested == "auto" else None}


def _ensure_ocr_backend():
    backend = _ocr_backend_info()
    if backend["selected"] == "tesseract":
        _require_ocr_languages()
    elif not backend["ready"]:
        _fail("MATERIAL_OCR_SDK_NOT_READY", "本地OCR引擎或随包模型未就绪；可继续读取原图", status=503,
              details={"backend": backend["selected"], "reason": backend.get("reason")})
    return backend


def _image_png(data):
    from PIL import Image
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP", "TIFF", "BMP"} or min(image.size) < 1:
                    _fail("MATERIAL_IMAGE_FORMAT_INVALID", "OCR仅支持PNG/JPEG/WebP/TIFF/BMP图像")
                if getattr(image, "n_frames", 1) > 1:
                    _fail("MATERIAL_IMAGE_MULTIFRAME_UNSUPPORTED", "OCR多帧图片须拆为单张提供，避免遗漏后续帧")
                if image.width * image.height > MAX_IMAGE_PIXELS or max(image.size) > MAX_IMAGE_DIMENSION:
                    _fail("MATERIAL_IMAGE_PIXEL_LIMIT", "OCR图片超出20MP或12000像素边长预算", status=413)
                png = BytesIO()
                image.convert("RGB").save(png, format="PNG")
                return png.getvalue()
        except ContractError:
            raise
        except Exception:
            _fail("MATERIAL_IMAGE_INVALID", "OCR图片已损坏或无法安全解码")


def _raster_pdf(png):
    """Lossless single-page PDF avoids JPEG damage before OCR of small glyphs."""
    from PIL import Image
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
    with Image.open(BytesIO(png)) as image:
        raster = image.convert("RGB")
        writer = PdfWriter()
        width, height = raster.width * 72 / 150, raster.height * 72 / 150
        page = writer.add_blank_page(width=width, height=height)
        bitmap = DecodedStreamObject()
        bitmap.set_data(raster.tobytes())
        bitmap.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                       NameObject("/Width"): NumberObject(raster.width), NameObject("/Height"): NumberObject(raster.height),
                       NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Raster"): writer._add_object(bitmap.flate_encode())})})
        content = DecodedStreamObject()
        content.set_data(f"q {width} 0 0 {height} 0 0 cm /Raster Do Q".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
        result = BytesIO()
        writer.write(result)
        return result.getvalue()


def _ocr_local(data, info, stages, scratch=None):
    # Check before enabling Tesseract: the SDK otherwise downloads missing
    # traineddata. Document bytes are never passed to an HTTP OCR service.
    _ensure_ocr_backend()
    started = time.perf_counter()
    png = _render_local(data, info, scratch=scratch)
    stages.append({"stage": "single_page_render", "duration_ms": round((time.perf_counter() - started) * 1000, 3)})
    return _ocr_png(png, info, stages)


def _ocr_png(png, info, stages, *, image_source=False):
    backend = _ensure_ocr_backend()
    if backend["selected"] == "rapidocr":
        from shengbo_prelive.material_chinese_ocr import ocr_png_local
        return ocr_png_local(png, info, stages, image_source=image_source)
    result = _ocr_png_tesseract(png, info, stages, image_source=image_source)
    if backend.get("fallback_reason"):
        result["parse_metrics"]["fallback_reason"] = backend["fallback_reason"]
    return result


def _ocr_png_tesseract(png, info, stages, *, image_source=False):
    tessdata = _require_ocr_languages()
    # Flatten just this page so native text cannot cause LiteParse to skip the
    # embedded image part of a mixed page. This is one OCR pass on one page.
    raster_pdf = _raster_pdf(png)
    parser = _liteparse()(ocr_enabled=True, ocr_language=OCR_LANGUAGE, tessdata_path=str(tessdata),
                          ocr_server_url=None, quiet=True, max_pages=1, num_workers=1, dpi=150,
                          output_format="json", ocr_failure_fatal=True)
    started = time.perf_counter()
    try:
        result = parser.parse(raster_pdf)
    except Exception:
        _fail("MATERIAL_OCR_FAILED", "本地中文OCR未完成；页面原图仍可用于视觉读取", status=422)
    finally:
        parser.close()
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    stages.append({"stage": "tesseract_chi_sim_chi_tra_eng", "duration_ms": elapsed})
    if len(result.pages) != 1 or result.page_errors:
        _fail("MATERIAL_OCR_FAILED", "本地中文OCR没有返回完整单页结果")
    page = result.pages[0]
    if len(page.text) > MAX_TEXT:
        _fail("MATERIAL_DOCUMENT_TEXT_TOO_LARGE", "OCR文本超出安全预算", status=413)
    items = []
    confidences = []
    source_locator = "image:1" if image_source else f"page:{info['number']}"
    image_locator = "image:1" if image_source else f"image:{info['number']}"
    for index, item in enumerate(page.text_items, 1):
        confidence = item.confidence
        known = isinstance(confidence, (int, float)) and math.isfinite(confidence) and 0 <= confidence <= 1
        if known:
            confidences.append(confidence)
        items.append({"id": f"{source_locator}:ocr:{index}", "kind": "ocr_text", "page": info["number"],
                      "source_locator": source_locator, "image_locator": image_locator,
                      "text": item.text, "bbox": {"x": item.x, "y": item.y, "width": item.width, "height": item.height},
                      "confidence": confidence if known else "unknown", "confidence_source": "tesseract" if known else "unavailable"})
    return {"text": page.text, "page": info["number"], "source_locator": source_locator,
            "image_locator": image_locator, "language": OCR_LANGUAGE, "locators": items,
            "confidence": sum(confidences) / len(confidences) if confidences else "unknown",
            "confidence_source": "mean_tesseract_item_confidence" if confidences else "unavailable",
            "image_sha256": "sha256:" + hashlib.sha256(png).hexdigest(),
            "coverage": {"ocr_performed": True, "business_understanding_complete": False,
                         "visual_content_complete": False, "pending_visual": True,
                         "empty_ocr_text": not bool(page.text.strip())},
            "parse_metrics": {"engine": ENGINE_VERSION, "ocr_engine": "liteparse_tesseract",
                              "liteparse_version": _distribution_version("liteparse"), "ocr_performed": True,
                              "ocr_language": OCR_LANGUAGE, "ocr_ms": elapsed, "stages": stages,
                              "confidence_is_accuracy": False,
                              "model_calls": 0, "local_only": True, "selected_page_count": 1}}


def _worker():
    try:
        request = json.loads(sys.stdin.buffer.read(max(MAX_PDF_BYTES, MAX_IMAGE_BYTES) * 2))
        data = base64.b64decode(request["data"], validate=True)
        operation, page_no = request["operation"], request.get("page")
        _validate_input(data, page_no, image=operation == "image_ocr")
        with redirect_stdout(StringIO()), _pypdf_limits():
            stages = []
            started = time.perf_counter()
            if operation == "image_ocr":
                _ensure_ocr_backend()
                png = _image_png(data)
                stages.append({"stage": "raster_image_validation", "duration_ms": round((time.perf_counter() - started) * 1000, 3)})
                result = _ocr_png(png, {"number": 1}, stages, image_source=True)
                result["input_sha256"] = "sha256:" + hashlib.sha256(data).hexdigest()
            else:
                reader, inventory = _preflight(data, page_no)
                stages.append({"stage": "pypdf_strict_preflight", "duration_ms": round((time.perf_counter() - started) * 1000, 3)})
            if operation == "parse":
                result = _parse_local(data, reader, inventory, stages)
            elif operation == "render":
                result = {"png": base64.b64encode(_render_local(data, inventory[page_no - 1], scratch=request["scratch"])).decode("ascii")}
            elif operation == "ocr":
                result = _ocr_local(data, inventory[page_no - 1], stages, scratch=request["scratch"])
            elif operation != "image_ocr":
                _fail("MATERIAL_PDF_OPERATION_INVALID", "PDF读取操作无效")
        response = {"result": result}
    except ContractError as error:
        response = {"error": {"code": error.code, "message": error.message, "status": error.status, "details": error.details}}
    except Exception:
        response = {"error": {"code": "MATERIAL_PDF_INVALID", "message": "PDF已损坏或无法安全读取", "status": 422}}
    sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker()
