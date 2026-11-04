"""Bounded, read-only merchant document parsers; no OCR, execution or network IO.

The returned locators are source coordinates, not claims about product identity.
Images stay in their immutable original container and are read only on demand.
"""
from __future__ import annotations

from .material_roles import CONTENT_ROLES

from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import tempfile
import time
import warnings
import zipfile
from xml.etree import ElementTree as ET

from shengbo_contracts.common import ContractError
from .material_office_sdk import (XlsxValueReader, docx_cell, docx_document,
                                 docx_paragraph, docx_part, library, rtf_visible_text)

MAX_TEXT = 2_000_000
MAX_XML_MEMBER = 32 * 1024 * 1024
MAX_ARCHIVE_EXPANDED = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4000
MAX_EMBEDDED_IMAGE_BYTES = 24 * 1024 * 1024
MAX_EMBEDDED_IMAGE_TOTAL = 192 * 1024 * 1024
MAX_EMBEDDED_IMAGES = 256
MAX_CELLS = 150_000
MAX_DRAWING_OBJECTS = 4000
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "d": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "v": "urn:schemas-microsoft-com:vml",
}


def fail(code, message="材料解析未完成，请提供其他格式或较小的文件", status=422):
    raise ContractError(code, message, status=status)


def _tag(prefix, name):
    return "{" + NS[prefix] + "}" + name


def _xml(data):
    if len(data) > MAX_XML_MEMBER or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
        fail("MATERIAL_XML_LIMIT_OR_DTD")
    return ET.fromstring(data)


def _archive(data, extension):
    archive = zipfile.ZipFile(BytesIO(data))
    entries = archive.infolist()
    try:
        names = [item.filename for item in entries]
        if len(entries) > MAX_ARCHIVE_ENTRIES or len(names) != len(set(names)):
            fail("MATERIAL_ARCHIVE_LIMIT")
        if sum(item.file_size for item in entries) > MAX_ARCHIVE_EXPANDED:
            fail("MATERIAL_ARCHIVE_LIMIT")
        for item in entries:
            path = item.filename.replace("\\", "/")
            if path.startswith("/") or ".." in path.split("/") or ":" in path:
                fail("MATERIAL_ARCHIVE_PATH_INVALID")
            if item.flag_bits & 1 or item.file_size > max(item.compress_size, 1) * (200 if extension == ".docx" else 1000):
                fail("MATERIAL_ARCHIVE_LIMIT")
            if "vbaproject" in path.lower() or path.lower().endswith("vbadata.xml"):
                fail("MATERIAL_ACTIVE_CONTENT_REJECTED")
            if path.lower().endswith((".xml", ".rels")) and item.file_size > MAX_XML_MEMBER:
                fail("MATERIAL_ARCHIVE_LIMIT")
        media = [item for item in entries if "/media/" in item.filename and not item.is_dir()]
        if len(media) > MAX_EMBEDDED_IMAGES or sum(x.file_size for x in media) > MAX_EMBEDDED_IMAGE_TOTAL:
            fail("MATERIAL_EMBEDDED_IMAGE_LIMIT")
        if any(x.file_size > MAX_EMBEDDED_IMAGE_BYTES for x in media):
            fail("MATERIAL_EMBEDDED_IMAGE_LIMIT")
        return archive
    except Exception:
        archive.close()
        raise


def _relationships(archive, part):
    relpart = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if relpart not in archive.namelist():
        return {}
    result = {}
    for item in _xml(archive.read(relpart)):
        row = dict(item.attrib)
        if row.get("TargetMode") != "External":
            target = row.get("Target", "").replace("\\", "/")
            resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(part), target))
            if resolved.startswith("../") or ":" in resolved:
                fail("MATERIAL_RELATIONSHIP_PATH_INVALID")
            row["resolved"] = resolved
        # External relationships are recorded only; never fetched or executed.
        result[row.get("Id")] = row
    return result


def _image_info(data):
    from PIL import Image
    if len(data) > MAX_EMBEDDED_IMAGE_BYTES:
        return {"visual_status": "image_byte_budget_exceeded", "confidence": "unknown"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.width * image.height > 20_000_000 or max(image.size) > 12000:
                    return {"visual_status": "image_pixel_budget_exceeded", "confidence": "unknown"}
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    return {"visual_status": "unsupported_image_format", "format": image.format, "confidence": "unknown"}
                result = {"width": image.width, "height": image.height, "format": image.format,
                          "mime": Image.MIME.get(image.format), "visual_status": "pending_visual_read", "confidence": "unknown"}
                image.verify()
                return result
    except Exception:
        return {"visual_status": "invalid_or_unsupported_image", "confidence": "unknown"}


def _images(archive, prefix):
    images, bypart, byhash = [], {}, {}
    for info in archive.infolist():
        if not info.filename.startswith(prefix + "/media/") or info.is_dir():
            continue
        raw = archive.read(info.filename)
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if digest in byhash:
            image = byhash[digest]
            image["container_members"].append(info.filename)
        else:
            image = {"id": f"image:{len(images) + 1}", "kind": "image", "container_member": info.filename,
                     "container_members": [info.filename], "sha256": digest, "bytes": len(raw),
                     "anchors": [], "text": "", **_image_info(raw)}
            images.append(image)
            byhash[digest] = image
        bypart[info.filename] = image
    return images, bypart


@contextmanager
def _timed(metrics, name):
    started = time.perf_counter()
    try:
        yield
    finally:
        metrics.append({"stage": name, "duration_ms": round((time.perf_counter() - started) * 1000, 3)})


def _text_bound(locators):
    total = sum(len(x.get("text", "")) for x in locators if x.get("kind") not in {"table_cell", "sheet"})
    if total > MAX_TEXT:
        fail("MATERIAL_DOCUMENT_TEXT_TOO_LARGE")


def _finish(kind, extractor, locators, sections, images, metrics, unknowns=None, extra=None):
    _text_bound(locators)
    text = "\n".join(x.get("text", "") for x in locators if x.get("kind") not in {"table_cell", "image"})
    if len(text) > MAX_TEXT:
        fail("MATERIAL_DOCUMENT_TEXT_TOO_LARGE")
    pending = len(images)
    unknowns = list(unknowns or [])
    if pending:
        unknowns.append("embedded_images_not_ocr_extracted")
    result = {"kind": kind, "extractor": extractor, "text": text,
              "locators": locators + images, "pages": [], "sections": sections,
              "embedded_images": images, "unknowns": list(dict.fromkeys(unknowns)),
              "coverage": {"processed_text_complete": True, "pending_visual": pending > 0,
                           "embedded_image_count": pending, "visual_extracted_count": 0,
                           "business_understanding_complete": False,
                           "read_boundaries": ["images_indexed_not_ocr", "source_content_not_yet_product_facts"]},
              "parse_metrics": {"stages": metrics, "text_characters": len(text), "locator_count": len(locators) + pending},
              "content_hints": {"requires_content_classification": True,
                                "allowed_roles": list(CONTENT_ROLES),
                                "product_identity": "unresolved", "not_a_product_per_file": True}}
    if extra:
        result.update(extra)
    return result


def _word_text(element):
    out = []
    for item in element.iter():
        if item.tag == _tag("w", "t"):
            out.append(item.text or "")
        elif item.tag == _tag("w", "tab"):
            out.append("\t")
        elif item.tag in {_tag("w", "br"), _tag("w", "cr")}:
            out.append("\n")
    return "".join(out)


def parse_docx(data):
    metrics, locators, sections, unknowns = [], [], [], []
    with _timed(metrics, "archive_validation"), _archive(data, ".docx") as archive:
        if "word/document.xml" not in archive.namelist():
            fail("MATERIAL_DOCX_INVALID")
    with _archive(data, ".docx") as archive:
        with _timed(metrics, "python_docx_document_load"):
            # The SDK follows package relationships. Validate every XML part
            # before that happens, including parts outside the visible body.
            for member in archive.namelist():
                if member.lower().endswith((".xml", ".rels")):
                    _xml(archive.read(member))
            sdk_document = docx_document(data)
        with _timed(metrics, "embedded_image_index"):
            images, bypart = _images(archive, "word")
        with _timed(metrics, "ordered_text_and_tables"):
            document = sdk_document._element
            part_names = ["word/document.xml"] + [name for name in archive.namelist()
                          if re.fullmatch(r"word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml", name)]
            for part in part_names:
                root = document if part == "word/document.xml" else docx_part(archive.read(part))
                body = root.find("w:body", NS) if part == "word/document.xml" else root
                relations = _relationships(archive, part)
                if any(r.get("TargetMode") == "External" for r in relations.values()):
                    unknowns.append("external_relationships_recorded_not_fetched")
                pcount, tcount, image_position = 0, 0, 0
                prefix = "" if part == "word/document.xml" else part + ":"

                def anchor_images(element, locator_id):
                    nonlocal image_position
                    for node in element.iter():
                        rid = node.get(_tag("r", "embed")) if node.tag == _tag("a", "blip") else node.get(_tag("r", "id")) if node.tag == _tag("v", "imagedata") else None
                        relation = relations.get(rid, {})
                        image = bypart.get(relation.get("resolved"))
                        if image is not None:
                            image_position += 1
                            image["anchors"].append({"locator_id": locator_id, "part": part,
                                                     "document_order": image_position})

                # Body order is retained; paragraphs and tables are not collected in separate passes.
                def walk(container):
                    nonlocal pcount, tcount
                    for element in container:
                        if element.tag == _tag("w", "p"):
                            pcount += 1
                            identity = prefix + f"paragraph:{pcount}"
                            text, text_reader = docx_paragraph(element, sdk_document, _word_text(element))
                            location = {"id": identity, "kind": "paragraph", "text": text, "part": part,
                                        "text_reader": text_reader}
                            style = element.find("w:pPr/w:pStyle", NS)
                            if style is not None and re.fullmatch(r"(?:Title|Subtitle|Heading[1-9])",
                                                                  style.get(_tag("w", "val"), ""), re.I):
                                location["section_label"] = True
                            locators.append(location)
                            anchor_images(element, identity)
                        elif element.tag == _tag("w", "tbl"):
                            tcount += 1
                            table_id = prefix + f"table:{tcount}"
                            sections.append({"id": table_id, "kind": "table", "name": table_id})
                            for ri, row in enumerate(element.findall("w:tr", NS), 1):
                                texts = []
                                grid_column = 1
                                for ci, cell in enumerate(row.findall("w:tc", NS), 1):
                                    source = "\n".join(_word_text(p) for p in cell.findall(".//w:p", NS)
                                                       if not any(a.tag == _tag("w", "p") for a in p.iterancestors()))
                                    text, text_reader = docx_cell(cell, sdk_document, source)
                                    texts.append(text)
                                    identity = table_id + f":row:{ri}:cell:{ci}"
                                    span = cell.find("w:tcPr/w:gridSpan", NS)
                                    merge = cell.find("w:tcPr/w:vMerge", NS)
                                    column_span = int(span.get(_tag("w", "val"), "1")) if span is not None else 1
                                    locators.append({"id": identity, "kind": "table_cell", "text": text, "part": part,
                                                     "table_id": table_id, "row": ri, "column": ci,
                                                     "grid_column": grid_column, "column_span": column_span,
                                                     "vertical_merge": merge.get(_tag("w", "val"), "continue") if merge is not None else None,
                                                     "text_reader": text_reader, "physical_source_cell": True})
                                    grid_column += column_span
                                    anchor_images(cell, identity)
                                locators.append({"id": table_id + f":row:{ri}", "kind": "table_row", "text": " | ".join(texts),
                                                 "part": part, "table_id": table_id, "row": ri})
                        elif element.tag in {_tag("w", "sdt"), _tag("w", "sdtContent"), _tag("w", "footnote"), _tag("w", "endnote"), _tag("w", "comment"),
                                             _tag("w", "ins"), _tag("w", "del"), _tag("w", "customXml")}:
                            walk(element)
                walk(body)
        for image in images:
            image["orphaned"] = not image["anchors"]
        if document.find(".//w:altChunk", NS) is not None:
            unknowns.append("altchunk_content_not_extracted")
        result = _finish("document", "python_docx_ooxml_ordered_v3", locators, sections, images, metrics, unknowns,
                         {"parser": {"name": "python_docx_ooxml_ordered", "version": "3", "libraries": [library("python-docx")]},
                          "capabilities": ["sdk_paragraphs_and_physical_table_cells", "ooxml_special_story_supplement",
                                           "paragraph_table_order", "source_image_anchors", "no_merged_grid_cell_duplication"]})
        result["coverage"].update(source_coordinate_system="ooxml_parts_and_physical_cells", layout_rendered=False,
                                  sdk_text_locator_count=sum(x.get("text_reader") == "python-docx" for x in locators),
                                  supplemented_text_locator_count=sum("+ooxml" in x.get("text_reader", "") for x in locators))
        if "altchunk_content_not_extracted" in unknowns:
            result["coverage"]["processed_text_complete"] = False
        return result


def _all_text(element):
    return "".join(node.text or "" for node in element.iter() if node.tag.rsplit("}", 1)[-1] == "t") if element is not None else ""


def _drawing_text(body):
    paragraphs = []
    for paragraph in body.findall("a:p", NS):
        words = []
        for node in paragraph.iter():
            if node.tag == _tag("a", "t"):
                words.append(node.text or "")
            elif node.tag == _tag("a", "br"):
                words.append("\n")
            elif node.tag == _tag("a", "tab"):
                words.append("\t")
        paragraphs.append("".join(words))
    return "\n".join(paragraphs)


def _xlsx_drawing_regions(archive, worksheet, relations, sheet_id, summary, bypart, unknowns, budget):
    """Supplement SDK cells with actual DrawingML text and explicit boundaries.

    Unsupported objects have source coordinates, never invented raster IDs.
    Their presence does not imply business relevance or permission to act.
    """
    locators, unsupported = [], []
    metadata_names = {"from", "to", "pos", "ext", "clientData", "nvGrpSpPr", "grpSpPr"}

    def unsupported_region(identity, context, object_type, reason):
        item = {"id": identity, "kind": "unsupported_visual", "text": "", **context,
                "object_type": object_type, "reason": reason, "source": "xlsx_drawing_object",
                "visual_status": "source_screenshot_required", "render_available": False,
                "remediation": "提供此区域的清晰截图或另存为PDF；不可从未渲染对象猜测条件或参数",
                "confidence": "unknown"}
        unsupported.append(item)
        locators.append(item)
        unknowns.append("unsupported_drawing_objects_require_source_screenshot")

    drawings = [*worksheet.findall("s:drawing", NS), *worksheet.findall("s:legacyDrawing", NS)]
    for drawing_number, drawing in enumerate(drawings, 1):
        budget["references"] += 1
        if budget["references"] > MAX_DRAWING_OBJECTS:
            fail("MATERIAL_XLSX_DRAWING_OBJECT_LIMIT")
        base = f"{sheet_id}:drawing:{drawing_number}"
        relation_id = drawing.get(_tag("r", "id"))
        relation = relations.get(relation_id, {})
        part = relation.get("resolved")
        context = {"sheet_id": sheet_id, "sheet_name": summary["name"], "sheet_state": summary["state"],
                   "drawing_part": part, "relationship_id": relation_id}
        if relation.get("TargetMode") == "External" or part not in archive.namelist():
            unsupported_region(base, context, "drawing", "drawing_relationship_missing_or_external_not_loaded")
            continue
        if drawing.tag == _tag("s", "legacyDrawing"):
            unsupported_region(base, context, "legacyDrawing", "legacy_vml_drawing_not_rendered")
            continue
        drawing_relations = _relationships(archive, part)
        object_number = 0
        for anchor_number, anchor in enumerate(_xml(archive.read(part)), 1):
            def anchor_position(name):
                element = anchor.find("d:" + name, NS)
                return {node.tag.rsplit("}", 1)[-1]: node.text for node in element} if element is not None else None

            anchor_context = {**context, "anchor_number": anchor_number,
                              "anchor_type": anchor.tag.rsplit("}", 1)[-1],
                              "from_zero_based": anchor_position("from"), "to_zero_based": anchor_position("to")}
            # Preserve the existing image index/order and anchors, including
            # pictures used as a shape fill or inside a grouped shape.
            for blip in anchor.findall(".//a:blip", NS):
                media_relation = drawing_relations.get(blip.get(_tag("r", "embed")), {})
                image = bypart.get(media_relation.get("resolved"))
                if image is None:
                    unknowns.append("drawing_image_missing_or_external_not_loaded")
                    continue
                properties = anchor.find(".//d:cNvPr", NS)
                image["anchors"].append({**anchor_context,
                    "name": properties.get("name") if properties is not None else None,
                    "description": properties.get("descr") if properties is not None else None})
                if image["id"] not in summary["image_ids"]:
                    summary["image_ids"].append(image["id"])

            def walk_object(node, group_path=()):
                nonlocal object_number
                if len(group_path) > 64:
                    fail("MATERIAL_XLSX_DRAWING_DEPTH_LIMIT")
                object_type = node.tag.rsplit("}", 1)[-1]
                if object_type in metadata_names:
                    return
                object_number += 1
                budget["objects"] += 1
                if budget["objects"] > MAX_DRAWING_OBJECTS:
                    fail("MATERIAL_XLSX_DRAWING_OBJECT_LIMIT")
                identity = base + f":object:{object_number}"
                properties = node.find(".//d:cNvPr", NS)
                source = {**anchor_context, "source_shape_id": properties.get("id") if properties is not None else None,
                          "name": properties.get("name") if properties is not None else object_type,
                          "group_path": list(group_path)}
                if node.tag == _tag("d", "grpSp"):
                    for child in node:
                        walk_object(child, (*group_path, identity))
                    return
                if node.tag == _tag("d", "pic"):
                    blips = node.findall(".//a:blip", NS)
                    if blips and all(bypart.get(drawing_relations.get(blip.get(_tag("r", "embed")), {}).get("resolved"))
                                     for blip in blips):
                        return
                    unsupported_region(identity, source, object_type, "drawing_picture_missing_or_external_not_loaded")
                    return
                if node.tag == _tag("d", "sp"):
                    text_body = node.find("d:txBody", NS)
                    text = _drawing_text(text_body) if text_body is not None else ""
                    if text:
                        budget["text_characters"] += len(text)
                        if budget["text_characters"] > MAX_TEXT:
                            fail("MATERIAL_DOCUMENT_TEXT_TOO_LARGE")
                        locators.append({"id": identity + ":text", "kind": "drawing_text", "text": text,
                                         **source, "drawing_object_index": object_number,
                                         "text_reader": "ooxml_drawingml_text_supplement", "layout_rendered": False})
                        missing_fill = any(not bypart.get(drawing_relations.get(blip.get(_tag("r", "embed")), {}).get("resolved"))
                                           for blip in node.findall(".//a:blip", NS))
                        if not missing_fill:
                            return
                        unsupported_region(identity, source, object_type, "drawing_shape_fill_missing_or_external_not_loaded")
                        return
                    unsupported_region(identity, source, object_type, "drawing_shape_without_readable_text_not_rendered")
                    return
                unsupported_region(identity, source, object_type, "drawing_object_not_rendered")

            for child in anchor:
                walk_object(child)
    return locators, unsupported


def parse_xlsx(data):
    metrics, locators, sections, unknowns, sheet_summaries = [], [], [], [], []
    unsupported_regions, drawing_budget = [], {"objects": 0, "references": 0, "text_characters": 0}
    with _timed(metrics, "archive_validation"), _archive(data, ".xlsx") as archive:
        if "xl/workbook.xml" not in archive.namelist():
            fail("MATERIAL_XLSX_INVALID")
    with _archive(data, ".xlsx") as archive:
        with _timed(metrics, "embedded_image_index"):
            images, bypart = _images(archive, "xl")
        with _timed(metrics, "workbook_text_cells_and_anchors"):
            wb = _xml(archive.read("xl/workbook.xml"))
            with _timed(metrics, "openpyxl_styles_and_shared_strings"):
                if "xl/styles.xml" in archive.namelist():
                    _xml(archive.read("xl/styles.xml"))
                props = wb.find("s:workbookPr", NS)
                sdk_values = XlsxValueReader(archive, date1904=props is not None and props.get("date1904") in {"1", "true"})
                if "xl/sharedStrings.xml" in archive.namelist():
                    shared_source = archive.read("xl/sharedStrings.xml")
                    _xml(shared_source)
                    shared = sdk_values.shared_strings(shared_source)
                else:
                    shared = []
            if sum(map(len, shared)) > MAX_TEXT:
                fail("MATERIAL_DOCUMENT_TEXT_TOO_LARGE")
            wr = _relationships(archive, "xl/workbook.xml")
            sheets = wb.find("s:sheets", NS)
            if sheets is None or len(sheets) > 100:
                fail("MATERIAL_XLSX_SHEET_LIMIT")
            cell_count, text_chars = 0, 0
            for si, sh in enumerate(sheets, 1):
                sheet_id = f"sheet:{si}"
                relation = wr.get(sh.get(_tag("r", "id")), {})
                if relation.get("TargetMode") == "External" or relation.get("resolved") not in archive.namelist():
                    fail("MATERIAL_XLSX_SHEET_RELATIONSHIP_INVALID")
                part = relation["resolved"]
                root = _xml(archive.read(part))
                sr = _relationships(archive, part)
                state = sh.get("state", "visible")
                dimension = root.find("s:dimension", NS)
                summary = {"id": sheet_id, "kind": "sheet", "name": sh.get("name", sheet_id), "state": state,
                           "part": part, "declared_dimension": dimension.get("ref") if dimension is not None else None,
                           "dimension_used_for_iteration": False,
                           "merged_ranges": [x.get("ref") for x in root.findall("s:mergeCells/s:mergeCell", NS)],
                           "hidden_rows": [], "hidden_columns": [dict(x.attrib) for x in root.findall("s:cols/s:col", NS) if x.get("hidden") in {"1", "true"}],
                           "cell_count": 0, "formula_count": 0, "image_ids": []}
                sections.append(summary)
                sheet_summaries.append(summary)
                for row in root.findall("s:sheetData/s:row", NS):
                    hidden = row.get("hidden") in {"1", "true"}
                    if hidden:
                        summary["hidden_rows"].append(row.get("r"))
                    for cell in row.findall("s:c", NS):
                        value_node, formula = cell.find("s:v", NS), cell.find("s:f", NS)
                        raw = value_node.text if value_node is not None else None
                        typ = cell.get("t")
                        if typ == "s" and raw is not None:
                            try:
                                value = shared[int(raw)]
                            except (IndexError, ValueError):
                                fail("MATERIAL_XLSX_SHARED_STRING_INVALID")
                        elif typ == "inlineStr":
                            value = _all_text(cell.find("s:is", NS))
                        else:
                            value = raw
                        if value is None and formula is None:
                            continue
                        coordinate = cell.get("r")
                        if not coordinate or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", coordinate):
                            fail("MATERIAL_XLSX_CELL_COORDINATE_INVALID")
                        decoded = sdk_values.cell(raw, typ, cell.get("s"))
                        text = value if typ in {"s", "inlineStr"} else decoded["display_text"]
                        if text is None:
                            text = ""
                        cell_count += 1
                        text_chars += len(text)
                        if cell_count > MAX_CELLS or text_chars > MAX_TEXT:
                            fail("MATERIAL_XLSX_CELL_OR_TEXT_LIMIT")
                        item = {"id": sheet_id + ":cell:" + coordinate, "kind": "spreadsheet_cell", "text": text,
                                "sheet_id": sheet_id, "sheet_name": summary["name"], "sheet_state": state,
                                "cell": coordinate, "row_hidden": hidden, "source_type": typ or "n",
                                "number_format_style_id": cell.get("s"), **decoded}
                        if typ in {"s", "inlineStr"}:
                            item["display_text"] = text
                            item["value_interpretation"] = "openpyxl_shared_string" if typ == "s" else "inline_source_string"
                        if not decoded["format_supported"]:
                            unknowns.append("some_cell_number_formats_preserved_raw_not_fully_rendered")
                        if formula is not None:
                            item.update(formula=formula.text or "", formula_attributes=dict(formula.attrib), cached_value=raw,
                                        cache_present=value_node is not None and raw is not None,
                                        formula_executed=False, formula_cache_freshness="unknown")
                            summary["formula_count"] += 1
                            if raw is None:
                                unknowns.append("formula_without_cached_value_not_calculated")
                        locators.append(item)
                        summary["cell_count"] += 1
                with _timed(metrics, "drawingml_text_and_region_index"):
                    drawing_locators, unsupported = _xlsx_drawing_regions(
                        archive, root, sr, sheet_id, summary, bypart, unknowns, drawing_budget)
                    locators.extend(drawing_locators)
                    unsupported_regions.extend(unsupported)
                    summary["drawing_text_locator_ids"] = [item["id"] for item in drawing_locators if item["kind"] == "drawing_text"]
                    summary["unsupported_visual_region_ids"] = [item["id"] for item in unsupported]
                external = [r for r in sr.values() if r.get("TargetMode") == "External"]
                if external:
                    unknowns.append("external_relationships_recorded_not_fetched")
                    summary["external_relationship_count"] = len(external)
                summary["actual_cell_coordinates"] = [x["cell"] for x in locators if x.get("sheet_id") == sheet_id and x.get("kind") == "spreadsheet_cell"]
                # Keep the overview bounded; exact coordinates remain in locators.
                summary["actual_cell_coordinate_sample"] = summary.pop("actual_cell_coordinates")[:12]
            for image in images:
                image["orphaned"] = not image["anchors"]
            if any(image["orphaned"] for image in images):
                unknowns.append("orphan_media_indexed_without_product_or_sheet_inference")
            if any(name.startswith("xl/embeddings/") for name in archive.namelist()):
                unknowns.append("embedded_ole_objects_not_executed_or_extracted")
        result = _finish("spreadsheet", "openpyxl_sparse_ooxml_cells_v4", locators, sections, images, metrics, unknowns,
                         {"sheets": sheet_summaries, "unsupported_visual_regions": unsupported_regions,
                          "parser": {"name": "openpyxl_sparse_ooxml_cells", "version": "4", "libraries": [library("openpyxl")]},
                          "capabilities": ["sparse_physical_cells", "sdk_styles_shared_strings_dates", "common_percent_currency_display",
                                           "raw_formula_cache_retained", "hidden_sheets_rows_columns", "drawing_image_anchors",
                                           "drawingml_text_locators", "unsupported_drawing_region_index"]})
        result["coverage"].update(sheet_count=len(sheet_summaries), hidden_sheet_count=sum(x["state"] != "visible" for x in sheet_summaries),
                                  nonempty_cell_count=cell_count, source_media_count=len(bypart),
                                  orphan_image_count=sum(x["orphaned"] for x in images), formulas_recalculated=False)
        result["coverage"].update(workbook_loading="sparse_xml_with_openpyxl_value_decoders", full_workbook_loaded=False,
                                  number_format_rendering="common_formats_only_other_formats_keep_raw",
                                  source_coordinate_system="worksheet_parts_and_actual_cell_addresses")
        drawing_text_count = sum(item.get("kind") == "drawing_text" for item in locators)
        unsupported_format_count = sum(item.get("kind") == "spreadsheet_cell" and not item.get("format_supported", True) for item in locators)
        result["coverage"].update(drawing_text_locator_count=drawing_text_count,
                                  unsupported_visual_region_count=len(unsupported_regions),
                                  unsupported_number_format_count=unsupported_format_count,
                                  pending_visual=bool(images or unsupported_regions))
        result["coverage"]["read_boundaries"].append("drawingml_text_extracted_without_layout_rendering")
        if unsupported_regions:
            result["coverage"]["read_boundaries"].append("unsupported_visual_regions_require_source_screenshot")
        result["parse_metrics"].update(drawing_object_count=drawing_budget["objects"],
                                      drawing_text_locator_count=drawing_text_count,
                                      unsupported_visual_region_count=len(unsupported_regions),
                                      unsupported_number_format_count=unsupported_format_count)
        if "embedded_ole_objects_not_executed_or_extracted" in unknowns or unsupported_regions:
            result["coverage"]["processed_text_complete"] = False
        return result


def _rtf_text(data):
    """Interpret visible RTF text with group state, Unicode and multibyte hex.

    Destination instructions, OLE objects and field instructions are discarded;
    field *results* remain visible. No fields, macros or external links run.
    """
    source = data.decode("latin1")
    if not re.match(r"\s*\{\\rtf[1-9]", source):
        fail("MATERIAL_RTF_INVALID")
    codepage_match = re.search(r"\\ansicpg(\d+)", source[:4096])
    encoding = "cp" + (codepage_match.group(1) if codepage_match else "1252")
    try:
        "".encode(encoding)
    except LookupError:
        fail("MATERIAL_RTF_CODEPAGE_UNSUPPORTED")
    ignored = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "objdata", "fldinst", "filetbl", "listtext", "pntext",
               "listtable", "listoverridetable", "revtbl", "rsidtbl", "generator", "xmlnstbl", "datastore", "themedata", "colorschememapping"}
    stack, out, byte_buffer = [], [], bytearray()
    state = {"skip": False, "uc": 1}
    fallback, i = 0, 0
    control_word = re.compile(r"\\([A-Za-z]+)(-?\d+)? ?")

    def flush():
        if byte_buffer:
            if not state["skip"]:
                decoded = bytes(byte_buffer).decode(encoding, errors="replace")
                # Word emits these RTF control bytes for nonbreaking and
                # optional hyphens. Do not let splitlines silently eat them.
                out.append(decoded.replace("\x1e", "\u2011").replace("\x1f", "\u00ad"))
            byte_buffer.clear()

    while i < len(source):
        c = source[i]
        if c == "{":
            flush()
            stack.append(dict(state))
            if len(stack) > 256:
                fail("MATERIAL_RTF_GROUP_DEPTH_LIMIT")
            i += 1
        elif c == "}":
            flush()
            if not stack:
                fail("MATERIAL_RTF_INVALID")
            state = stack.pop()
            i += 1
        elif c == "\\":
            if i + 1 >= len(source):
                fail("MATERIAL_RTF_INVALID")
            n = source[i + 1]
            if n == "'" and i + 3 < len(source):
                try:
                    value = int(source[i + 2:i + 4], 16)
                except ValueError:
                    fail("MATERIAL_RTF_INVALID")
                if fallback:
                    fallback -= 1
                elif not state["skip"]:
                    byte_buffer.append(value)
                i += 4
                continue
            flush()
            if n in "{}\\":
                if fallback:
                    fallback -= 1
                elif not state["skip"]:
                    out.append(n)
                i += 2
                continue
            if not n.isalpha():
                if n == "*":
                    state["skip"] = True
                elif n in {"~", "_", "-"} and not state["skip"]:
                    if fallback:
                        fallback -= 1
                    else:
                        out.append({"~": "\u00a0", "_": "\u2011", "-": ""}[n])
                i += 2
                continue
            match = control_word.match(source, i)
            word, value = match.group(1), match.group(2)
            i = match.end()
            if word in ignored:
                state["skip"] = True
            elif word == "bin":
                count = int(value or 0)
                if count < 0 or i + count > len(source):
                    fail("MATERIAL_RTF_INVALID")
                i += count
            elif word == "uc":
                state["uc"] = min(max(int(value or 1), 0), 10)
            elif word == "u":
                if not state["skip"]:
                    out.append(chr(int(value or 0) % 65536))
                fallback = state["uc"]
            elif not state["skip"]:
                replacements = {"par": "\n", "line": "\n", "page": "\n", "tab": "\t", "cell": " | ", "row": "\n",
                                "emdash": "—", "endash": "–", "bullet": "•", "lquote": "‘", "rquote": "’", "ldblquote": "“", "rdblquote": "”"}
                if word in replacements:
                    out.append(replacements[word])
        else:
            if c not in "\r\n" and not state["skip"]:
                if fallback:
                    fallback -= 1
                else:
                    byte_buffer.append(ord(c))
            i += 1
    flush()
    if stack:
        fail("MATERIAL_RTF_INVALID")
    # Combine UTF-16 surrogate pairs produced by the RTF signed Unicode escapes.
    text = "".join(out).encode("utf-16", errors="surrogatepass").decode("utf-16", errors="replace")
    if len(text) > MAX_TEXT:
        fail("MATERIAL_DOCUMENT_TEXT_TOO_LARGE")
    return text, encoding


def _rtf_pictures(data):
    # Only direct picture groups are decoded; OLE/object execution is never used.
    pictures = []
    for match in re.finditer(rb"\{\\pict\b", data):
        start, i, depth = match.start(), match.start(), 0
        while i < len(data):
            char = data[i]
            if char == 92:
                binary = re.match(rb"\\bin(-?\d+) ?", data[i:])
                if binary:
                    count = int(binary.group(1))
                    if count < 0 or i + binary.end() + count > len(data):
                        fail("MATERIAL_RTF_INVALID")
                    i += binary.end() + count
                else:
                    i += 2
                continue
            if char == 123:
                depth += 1
            elif char == 125:
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            fail("MATERIAL_RTF_INVALID")
        group = data[start:i + 1]
        binary = re.search(rb"\\bin(\d+) ?", group)
        if binary:
            raw = group[binary.end():binary.end() + int(binary.group(1))]
        else:
            hexes = list(re.finditer(rb"(?:[0-9a-fA-F]{2}[\r\n\t ]*){32,}", group))
            if not hexes:
                continue
            raw = bytes.fromhex(re.sub(rb"\s", b"", max(hexes, key=lambda x: len(x.group())).group()).decode("ascii"))
        if len(raw) > MAX_EMBEDDED_IMAGE_BYTES:
            fail("MATERIAL_EMBEDDED_IMAGE_LIMIT")
        pictures.append((start, raw))
        if len(pictures) > MAX_EMBEDDED_IMAGES or sum(len(x[1]) for x in pictures) > MAX_EMBEDDED_IMAGE_TOTAL:
            fail("MATERIAL_EMBEDDED_IMAGE_LIMIT")
    return pictures


def parse_rtf(data):
    metrics, locators, images = [], [], []
    with _timed(metrics, "rtf_structure_and_codepage_validation"):
        _, codepage = _rtf_text(data)
    with _timed(metrics, "striprtf_visible_text"):
        text = rtf_visible_text(data, codepage)
        if len(text) > MAX_TEXT:
            fail("MATERIAL_DOCUMENT_TEXT_TOO_LARGE")
        for number, paragraph in enumerate(text.splitlines(), 1):
            if paragraph.strip():
                locators.append({"id": f"paragraph:{number}", "kind": "paragraph", "text": paragraph, "text_reader": "striprtf"})
    with _timed(metrics, "rtf_embedded_image_index"):
        for pi, (offset, raw) in enumerate(_rtf_pictures(data), 1):
            images.append({"id": f"image:{pi}", "kind": "image", "rtf_picture_index": pi,
                           "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "text": "",
                           "anchors": [{"rtf_byte_offset": offset}], **_image_info(raw)})
    unknowns = []
    if "\ufffd" in text or "\x00" in text:
        unknowns.append("text_decoding_has_unresolved_characters")
    if re.search(rb"\\(?:object|objdata)\b", data):
        unknowns.append("embedded_ole_objects_not_executed_or_extracted")
    result = _finish("document", "striprtf_visible_text_v3:" + codepage, locators, [], images, metrics, unknowns,
                     {"parser": {"name": "striprtf_visible_text", "version": "3", "libraries": [library("striprtf")]},
                      "capabilities": ["sdk_visible_text_and_font_codepages", "rtf_group_validation", "unicode_and_multibyte_text",
                                       "original_picture_byte_offsets", "field_results_without_instruction_execution"]})
    result["coverage"].update(source_coordinate_system="visible_paragraphs_and_original_rtf_picture_byte_offsets",
                              layout_rendered=False, field_instructions_executed=False)
    if any(x in unknowns for x in ("embedded_ole_objects_not_executed_or_extracted", "text_decoding_has_unresolved_characters")):
        result["coverage"]["processed_text_complete"] = False
    return result


def parse_legacy_doc(data):
    if data.lstrip().startswith(b"{\\rtf"):
        result = parse_rtf(data)
        result["extractor"] += ":doc_extension"
        return result
    if not data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        fail("MATERIAL_DOC_SIGNATURE_INVALID")
    # Use a separately owned hidden Word process. No active user document is
    # touched; macro/external-field updates are disabled before opening a copy.
    if os.name != "nt":
        fail("MATERIAL_DOC_CONVERTER_UNAVAILABLE", "旧版DOC需要本机Word只读转换，请另存为DOCX后上传")
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="shengbo-doc-read-") as folder:
        source, target, receipt = Path(folder) / "source.doc", Path(folder) / "converted.docx", Path(folder) / "receipt.json"
        source.write_bytes(data)
        worker = Path(__file__).with_name("material_word_reader.py")
        try:
            completed = subprocess.run([sys.executable, str(worker), str(source), str(target), str(receipt)],
                                       timeout=45, capture_output=True, creationflags=0x08000000)
        except subprocess.TimeoutExpired:
            # Only the helper's own Word PID, recorded immediately after
            # DispatchEx, may be terminated. Never kill all WINWORD processes.
            if receipt.exists():
                try:
                    pid = json.loads(receipt.read_text("utf-8")).get("owned_word_pid")
                    if isinstance(pid, int) and pid > 0:
                        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=5, creationflags=0x08000000)
                except Exception:
                    pass
            fail("MATERIAL_DOC_CONVERSION_TIMEOUT", "旧版DOC只读转换超时，请另存为DOCX后上传")
        if completed.returncode != 0 or not target.is_file():
            fail("MATERIAL_DOC_CONVERTER_UNAVAILABLE", "旧版DOC只读转换未完成，请另存为DOCX后上传")
        converted = target.read_bytes()
        conversion_ms = round((time.perf_counter() - started) * 1000, 3)
        result = parse_docx(converted)
        result["extractor"] = "word_readonly_macros_disabled_to_" + result["extractor"]
        result["parse_metrics"]["stages"].insert(0, {"stage": "legacy_doc_readonly_conversion", "duration_ms": conversion_ms})
        conversion_receipt = json.loads(receipt.read_text("utf-8")) if receipt.exists() else {}
        result["parser"]["conversion"] = {"name": "Microsoft Word COM", "sdk": library("pywin32"),
                                           "application_version": conversion_receipt.get("word_version", "unknown"),
                                           "readonly": True, "macros_disabled": True, "external_link_updates_disabled": True}
        result["capabilities"].append("isolated_word_readonly_doc_conversion")
        result["coverage"]["source_coordinate_system"] = "derived_docx_parts_original_doc_preserved"
        result["unknowns"].append("legacy_conversion_layout_may_differ_original_preserved")
        # Unlike native zip files, these images cannot be recovered from DOC
        # without conversion. Persist a bounded derived image cache separately.
        with zipfile.ZipFile(BytesIO(converted)) as archive:
            result["_derived_images"] = {image["id"]: archive.read(image["container_member"]) for image in result["embedded_images"]}
        for image in result["embedded_images"]:
            image.pop("container_member", None)
            image.pop("container_members", None)
            image["derived_from_conversion"] = True
        return result


def embedded_image_bytes(original, extension, descriptor):
    if descriptor.get("container_member"):
        with _archive(original, extension) as archive:
            return archive.read(descriptor["container_member"])
    if descriptor.get("rtf_picture_index"):
        pictures = _rtf_pictures(original)
        index = descriptor["rtf_picture_index"] - 1
        if not 0 <= index < len(pictures):
            fail("MATERIAL_EMBEDDED_IMAGE_MISSING")
        return pictures[index][1]
    fail("MATERIAL_EMBEDDED_IMAGE_MISSING")
