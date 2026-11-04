"""Lossless merchant-script import; no model call and no generated promises."""
from copy import deepcopy
import re
from .storage import BusinessError, digest, now
from ..welcome import build_product_templates


def original_script_context(service, c, spec, preparation_id):
    """One immutable original snapshot shared by both generation protocols."""
    if not spec.get("original_script_ref"):
        return {}
    original = service._artifact(c, spec["original_script_ref"], "script_revision", preparation_id)
    if original["body"]["product_id"] != spec["product_ref"]["id"]:
        raise BusinessError("ORIGINAL_SCRIPT_TARGET_MISMATCH")
    return {"original_script": {"ref": original["ref"], "body": original["body"],
        "modules": {key: service._artifact(c, ref, "module")["body"]
                    for key, ref in original["body"]["module_refs"].items() if ref},
        "purpose": "创作新方案的依据；保留商品事实及否定条件，不能凭原稿虚构性能/库存/售后"}}


def import_script(service, c, args):
    prep = service._prep(c, args.preparation_id, args.expected_revision)
    product = service._artifact(c, args.product_ref, "product")
    if product["ref"] not in prep["body"]["selected_product_refs"]:
        raise BusinessError("PRODUCT_NOT_SELECTED")
    if any(r["body"].get("variant_id") == args.variant_id for r in service.store.all(c, "script_revision")):
        raise BusinessError("VARIANT_ALREADY_EXISTS")
    source_ref = args.source_ref.model_dump(exclude_none=True)
    if args.source_manifest:
        item = args.source_manifest
        if item.source_ref != args.source_ref or digest(item.manifest) != item.source_ref.content_hash:
            raise BusinessError("SOURCE_MANIFEST_HASH_MISMATCH")
        blob = service._artifact(c, item.original_blob_ref, "blob")
        if (item.manifest.get("original_sha256") != blob["body"]["sha256_bytes"] or
                item.manifest.get("original_size") != blob["body"]["size"]):
            raise BusinessError("SOURCE_ORIGINAL_BLOB_MISMATCH")
        manifest = item.manifest
        source_ref = service.store.artifact(c, "source_manifest", {
            "manifest": manifest, "original_blob_ref": item.original_blob_ref.model_dump(exclude_none=True),
            "origin_source_ref": source_ref})
    else:
        manifest = service._artifact(c, args.source_ref, "source_manifest")["body"]["manifest"]
    # A range is explicit, contiguous and exact. No trimming, translation,
    # expansion, or inference of an image-only text transcript at this boundary.
    locators = manifest.get("locators", [])
    region = ({"text": manifest.get("text")} if args.locator == "full" else
              next((row for row in locators if row.get("id") == args.locator), None))
    if region is None or not isinstance(region.get("text"), str):
        raise BusinessError("SCRIPT_SOURCE_RANGE_INVALID", details={"commit_state": "not_committed"})
    offset = region["text"].find(args.text)
    if offset < 0 or not args.text.strip():
        raise BusinessError("SCRIPT_SOURCE_TEXT_MISMATCH", details={"commit_state": "not_committed"})
    chunks, start = [], 0
    # Keep separators and whitespace; chunks concatenate to the exact input.
    for match in re.finditer(r"[。！？!?\n]+|.{2500}", args.text):
        stop = match.end()
        if stop > start and args.text[start:stop].strip():
            chunks.append((start, stop)); start = stop
    if start < len(args.text):
        if args.text[start:].strip():
            chunks.append((start, len(args.text)))
        elif chunks:
            chunks[-1] = (chunks[-1][0], len(args.text))
    if not chunks:
        raise BusinessError("EMPTY_SCRIPT_TEXT")
    rows = [{"id": "sentence_" + digest([args.variant_id, first, last, args.text[first:last]])[7:31],
             "text": args.text[first:last], "spoken_text": args.text[first:last],
             "source_span": {"start": offset + first, "end": offset + last}}
            for first, last in chunks]
    if "".join(row["text"] for row in rows) != args.text:
        raise BusinessError("SCRIPT_IMPORT_LOSSLESS_INVARIANT")
    module_ref = service.store.artifact(c, "module", {"module_key": "product_intro", "parent_ref": None,
        "sentences": rows, "origin": "imported", "raw_text_hash": digest(args.text)})
    numbers = service._script_version_numbers(c, prep["body"]).get(product["ref"]["id"], ())
    body = {"preparation_id": args.preparation_id, "product_id": product["ref"]["id"],
        "product_ref": product["ref"], "variant_id": args.variant_id, "version_number": max(numbers, default=0) + 1,
        "script_revision": 1, "parent_ref": None, "language": args.language, "origin": "imported",
        "raw_text": args.text, "module_refs": {"product_intro": module_ref},
        "welcome_templates": build_product_templates(product["body"]),
        "source_provenance": {"source_ref": source_ref, "locator": args.locator,
            "start": offset, "end": offset + len(args.text), "text_hash": digest(args.text)}, "created_at": now()}
    script_ref = service.store.artifact(c, "script_revision", body)
    updated = deepcopy(prep["body"])
    updated["script_refs"].append(script_ref)
    updated["playlist_ref"], _ = service._update_script_playlist(c, updated, append_missing=True)
    updated.update(plan_ref=None, compiled_plan_ref=None, requirements_ref=None, audio_manifest_ref=None)
    if source_ref not in updated["source_refs"]:
        updated["source_refs"].append(source_ref)
    return {**service._save_prep(c, prep, updated), "script_ref": script_ref, "origin": "imported",
            "lossless": True, "provider_calls": 0, "source_provenance": body["source_provenance"]}
