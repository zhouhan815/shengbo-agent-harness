"""Bind explicitly captioned product photos to approved catalog candidates."""
from __future__ import annotations

import asyncio
import base64
from collections import Counter, defaultdict
from hashlib import sha256
from io import BytesIO
import re

from PIL import Image, ImageOps

from shengbo_contracts.common import ContractError, canonical_hash
from .material_visual_tools import visual_payload


_PHOTO_LABEL = re.compile(r"商品(?:主)?图|产品(?:主)?图|实物(?:照(?:片)?|图)|外观(?:照片|图)|商品照片|产品照片")
_FIGURE_NUMBER = re.compile(r"(?<![A-Za-z0-9])(?:商品图|产品图|实物图|图|Figure)\s*([1-9]\d*)\s*[:：.、-]?", re.I)
_PHOTO_LAYOUT_NOTE = re.compile(r"^(?:商品|产品|实物)(?:主)?图按\s*SKU\s*(?:对应展示|展示|区分)$", re.I)
_SMALL_ORIGINAL_BYTES = 120 * 1024


def _numbered_captions(text, skus):
    marks = list(_FIGURE_NUMBER.finditer(text))
    matches = {}
    for index, marker in enumerate(marks):
        number = int(marker[1])
        quote = text[marker.start():marks[index + 1].start() if index + 1 < len(marks) else len(text)].strip()
        targets = [sku for sku in skus if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(sku) + r"(?![A-Za-z0-9_-])", quote)]
        if number in matches or len(targets) != 1 or len(quote) > 500:
            return {}
        matches[number] = {"sku_id": targets[0], "caption_quote": quote}
    return matches


def catalog_photo_links(locations, units, locator_orders):
    """Use real drawing order and adjacent explicit captions, never media-file order."""
    by_source = defaultdict(dict)
    anchored = defaultdict(list)
    known_skus = defaultdict(set)
    for unit in units:
        if unit.get("sku_id"):
            known_skus[unit["source_ref"]["id"]].add(unit["sku_id"])
    for block in locations:
        source = block["source_ref"]["id"]
        by_source[source][block["locator"]] = block
        anchors = block.get("anchors", [])
        if block["kind"] == "image" and len(anchors) == 1:
            anchor = anchors[0]
            if (anchor.get("part") == "word/document.xml" and anchor.get("document_order")
                    and anchor.get("locator_id", "").startswith("paragraph:")):
                anchored[source, anchor["locator_id"]].append((anchor["document_order"], block))

    links = []
    for (source, anchor), figures in anchored.items():
        order = locator_orders[source]
        if anchor not in order:
            continue
        position = order.index(anchor)
        neighbours = [by_source[source].get(key) for key in order[max(0, position - 1):position + 2]]
        paragraphs = [block for block in neighbours if block and block["kind"] == "paragraph"]
        if not any(_PHOTO_LABEL.search(block["text"]) for block in paragraphs):
            continue
        expected = {number for number, _ in figures}
        candidates = [(block, captions) for block in paragraphs
                      if (captions := _numbered_captions(block["text"], known_skus[source])).keys() == expected]
        if len(candidates) != 1:
            continue
        caption, mapping = candidates[0]
        for number, image in figures:
            links.append({**mapping[number], "source_ref": image["source_ref"], "locator": image["locator"],
                          "caption_locator": caption["locator"], "original_sha256": image["original_sha256"]})
    counts = Counter(link["sku_id"] for link in links)
    return [link for link in links if counts[link["sku_id"]] == 1]


def mark_catalog_photo_metadata(units, links):
    """Keep pure picture labels in source audit, outside consumer product knowledge."""
    def compact(text):
        return re.sub(r"[\s·:：、—]+", "", text)

    variants, captions = defaultdict(set), defaultdict(list)
    for unit in units:
        if unit.get("sku_id") and unit.get("column") in {"款式", "版本", "规格", "型号"}:
            variants[unit["source_ref"]["id"], unit["sku_id"]].add(compact(unit["text"]))
    for link in links:
        source = link["source_ref"]["id"]
        caption, sku = link["caption_quote"], link["sku_id"]
        marker = _FIGURE_NUMBER.match(caption)
        after_marker = compact(caption[marker.end():])
        allowed = {sku, *(sku + variant for variant in variants[source, sku])}
        captions[source, link["caption_locator"]].append((int(marker[1]), caption, after_marker in allowed))
    for unit in units:
        matches = captions[unit["source_ref"]["id"], unit["locator"]]
        if (_PHOTO_LAYOUT_NOTE.fullmatch(unit["text"]) or matches and all(ok for _, _, ok in matches)
                and compact(unit["text"]) == compact(" ".join(text for _, text, _ in sorted(matches)))):
            unit.setdefault("excluded_reason", "metadata")


def _product_image_data_url(mime, original):
    """Keep the actual photo in the existing image field without duplicating megabytes in a run."""
    if len(original) > _SMALL_ORIGINAL_BYTES:
        with Image.open(BytesIO(original)) as source:
            oriented = ImageOps.exif_transpose(source)
            oriented.thumbnail((720, 720), Image.Resampling.LANCZOS)
            converted = Image.new("RGB", oriented.size, "white")
            rgba = oriented.convert("RGBA")
            converted.paste(rgba, mask=rgba.getchannel("A"))
            data = BytesIO()
            converted.save(data, format="JPEG", quality=84, optimize=True)
            mime, original = "image/jpeg", data.getvalue()
    return f"data:{mime};base64," + base64.b64encode(original).decode("ascii")


async def attach_catalog_photos(engine, scope, conversation_id, report, links, *, record_evidence=True):
    rows = {row["sku_id"]: row for row in report["fields"]["products"]}
    for link in links:
        sku = link["sku_id"]
        if sku not in rows:
            continue
        mime, original, _ = await asyncio.to_thread(visual_payload, engine.materials,
            link["source_ref"], conversation_id, link["locator"])
        if "sha256:" + sha256(original).hexdigest() != link["original_sha256"]:
            raise ContractError("MATERIAL_BLOB_INTEGRITY_FAILED", "商品图无法与原始附件的精确位置核对。", status=409)
        rows[sku]["image"] = await asyncio.to_thread(_product_image_data_url, mime, original)
        if record_evidence:
            report["evidence"].append({"id": "image_caption_" + canonical_hash(link)[7:39], "sku_id": sku, "fields": ["image"],
                "source_ref": link["source_ref"], "locator": link["caption_locator"], "quote": link["caption_quote"]})
        report.setdefault("insights", []).append({"kind": "catalog_product_image", **link})
