"""Deterministic queue ordering and position-bound required-only compilation.

The saved queue is one playback cycle. Repeating or jumping into that queue
does not change the opening assigned to an occurrence's saved position.
Compilation creates new artifacts; it never mutates an approved old plan.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import math
from shengbo_contracts.business import PLAYBACK_STRUCTURE_VERSION
from .storage import BusinessError, digest


def reorder(items, spec):
    spec = spec.model_dump() if hasattr(spec, "model_dump") else spec
    ids = [i["occurrence_id"] for i in items]
    if len(ids) != len(set(ids)):
        raise BusinessError("DUPLICATE_OCCURRENCE_ID")
    groups = spec["product_group_order"]
    if len(groups) != len(set(groups)) or set(groups) != {x["product_id"] for x in items}:
        raise BusinessError("GROUP_ORDER_MISMATCH")
    seed = spec.get("seed") or "0" * 32
    if len(seed) != 32 or any(x not in "0123456789abcdef" for x in seed):
        raise BusinessError("INVALID_SEED", "随机种子必须为32位小写十六进制")
    def ranked(values, domain):
        return sorted(values, key=lambda x: (hashlib.sha256(("reorder-v1|" + domain + "|" + seed + "|" + x["occurrence_id"]).encode()).hexdigest(), x["occurrence_id"]))
    buckets = {p: [deepcopy(x) for x in items if x["product_id"] == p] for p in groups}
    if spec.get("within_product") == "shuffle":
        buckets = {p: ranked(v, "within_product") for p, v in buckets.items()}
    if spec["mode"] == "full_shuffle":
        result = ranked(deepcopy(items), "full_shuffle")
    elif spec["mode"] == "grouped":
        result = [x for p in groups for x in buckets[p]]
    elif spec["mode"] == "interleave":
        result, previous = [], None
        while any(buckets.values()):
            candidates = [p for p in groups if buckets[p] and p != previous]
            if not candidates:
                candidates = [p for p in groups if buckets[p]]
            p = min(candidates, key=lambda p: (-len(buckets[p]), groups.index(p)))
            result.append(buckets[p].pop(0))
            previous = p
    else:
        raise BusinessError("INVALID_REORDER_MODE")
    if sorted((x["occurrence_id"], digest(x)) for x in items) != sorted((x["occurrence_id"], digest(x)) for x in result):
        raise BusinessError("INVALID_PERMUTATION")
    n = len(items)
    same = sum(result[i]["product_id"] == result[i-1]["product_id"] for i in range(1, n))
    minimum = max(0, 2 * max(Counter(x["product_id"] for x in items).values(), default=0) - n - 1)
    if spec["mode"] == "interleave" and same != minimum:
        raise BusinessError("INTERLEAVE_INVARIANT_FAILED")
    return {"proposed_items": result, "resolved_spec": {**spec, "seed": seed}, "input_hash": digest(items), "output_hash": digest(result), "adjacent_same_product_pairs": same, "minimum_internal_adjacent_pairs": minimum, "loop_boundary_same_product": bool(n and result[0]["product_id"] == result[-1]["product_id"])}


def opening_for_position(position):
    """Return the opening for a one-based saved queue position."""
    if not isinstance(position, int) or isinstance(position, bool) or position < 1:
        raise BusinessError("INVALID_PLAYLIST_POSITION")
    return "opening_long" if (position - 1) % 5 == 0 else "opening_short"


def playback_statistics(entries, assets=None):
    """Count the selected spoken sequence, separately from unique TTS work.

    Audio durations are observational only. A missing/non-finite duration is
    unknown, never a zero-second estimate; no character-to-duration conversion
    is used. Callers must pass assets already checked for validity/voice by
    the business service when presenting these as current playable durations.
    """
    audio = {a["requirement_id"]: a for a in assets or [] if a.get("requirement_id")}
    unique, selected, duration_total, known_total = {}, [], 0.0, 0
    for entry in entries:
        duration, known = 0.0, 0
        sentences = entry["sentences"]
        for sentence in sentences:
            spoken = sentence.get("spoken_text") or sentence["text"]
            requirement_id = sentence["requirement_id"]
            unique[requirement_id] = spoken
            asset = audio.get(requirement_id) or sentence.get("audio") or {}
            seconds = asset.get("duration")
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and math.isfinite(seconds) and seconds > 0:
                duration += seconds
                known += 1
        complete = known == len(sentences) and bool(sentences)
        selected.append({"original_position": entry["original_position"],
            "occurrence_id": entry["occurrence_id"], "script_revision_ref": entry["script_revision_ref"],
            "opening_mode": entry["opening_mode"], "sentence_count": len(sentences),
            "spoken_characters": sum(len(s.get("spoken_text") or s["text"]) for s in sentences),
            "audio_duration_seconds": round(duration, 3) if complete else None,
            "known_audio_duration_seconds": round(duration, 3),
            "audio_duration_complete": complete})
        duration_total += duration
        known_total += known
    sentence_total = sum(e["sentence_count"] for e in selected)
    complete = bool(sentence_total) and known_total == sentence_total
    return {"basis": "saved_queue_one_cycle", "entries": selected,
        "entry_count": len(entries), "sentence_count": sentence_total,
        "spoken_characters": sum(e["spoken_characters"] for e in selected),
        "unique_synthesis_sentences": len(unique), "unique_synthesis_characters": sum(map(len, unique.values())),
        "audio_duration_seconds": round(duration_total, 3) if complete else None,
        "known_audio_duration_seconds": round(duration_total, 3),
        "audio_duration_complete": complete}


def compile_items(items, loop_mode, script_reader, module_reader):
    if not items:
        raise BusinessError("EMPTY_PLAYLIST")
    if len({item["occurrence_id"] for item in items}) != len(items):
        raise BusinessError("DUPLICATE_OCCURRENCE_ID")
    entries, requirements, missing_modules = [], {}, {}
    # All readers refer to exact immutable revisions within this compilation.
    # Repeated occurrences should not repeatedly fetch the same script/module.
    script_cache, module_cache = {}, {}
    # Validate every occurrence before returning any compiled plan. Group
    # exact gaps to let the main Agent choose a targeted repair, not a whole
    # script regeneration. Unselected alternative openings are not gaps.
    for position, item in enumerate(items, 1):
        script_key = digest(item["script_revision_ref"])
        if script_key not in script_cache:
            script_cache[script_key] = script_reader(item["script_revision_ref"])
        script = script_cache[script_key]
        if script["product_id"] != item["product_id"]:
            raise BusinessError("SCRIPT_PRODUCT_MISMATCH")
        opening = opening_for_position(position)
        imported = script.get("origin") == "imported"
        # A merchant's finished script remains verbatim, including its own
        # opening/closing. Do not invent missing generated modules to play it.
        module_keys = ["product_intro"] if imported else [opening, "product_intro", "selling_points", "campaign", "cta"]
        sentences = []
        for key in module_keys:
            ref = script["module_refs"].get(key)
            if ref is None:
                if key == "campaign":
                    continue
                gap_id = digest({"script_revision_ref": item["script_revision_ref"], "module_key": key})
                gap = missing_modules.setdefault(gap_id, {"module_key": key, "key": key,
                    "script_revision_ref": deepcopy(item["script_revision_ref"]), "product_id": item["product_id"],
                    "variant_id": script.get("variant_id"), "version_number": script.get("version_number"),
                    "product_ref": deepcopy(script.get("product_ref")), "positions": [], "occurrence_ids": [],
                    "reusable_module_refs": deepcopy({k: v for k, v in script["module_refs"].items() if v and k != "transition"})})
                gap["positions"].append(position)
                gap["occurrence_ids"].append(item["occurrence_id"])
                continue
            module_key = digest(ref)
            if module_key not in module_cache:
                module_cache[module_key] = module_reader(ref)
            module = module_cache[module_key]
            if module.get("module_key", key) != key:
                raise BusinessError("MODULE_KEY_MISMATCH", details={"key": key, "module_ref": ref})
            if not module["sentences"]:
                raise BusinessError("EMPTY_SCRIPT_TEXT", details={"key": key, "module_ref": ref})
            for sentence in module["sentences"]:
                spoken = sentence.get("spoken_text") or sentence["text"]
                if not isinstance(spoken, str) or not spoken.strip():
                    raise BusinessError("EMPTY_SCRIPT_TEXT", details={"key": key, "module_ref": ref})
                rid = digest({"spoken_text": spoken})
                sentences.append({**deepcopy(sentence), "module_key": key, "module_ref": deepcopy(ref), "requirement_id": rid})
                if rid not in requirements:
                    requirements[rid] = {"requirement_id": rid, "spoken_text": spoken, "sentence_refs": [deepcopy(ref)]}
                elif ref not in requirements[rid]["sentence_refs"]:
                    requirements[rid]["sentence_refs"].append(deepcopy(ref))
        entry_id = digest({"compiler_version": PLAYBACK_STRUCTURE_VERSION, "position": position, "occurrence": item, "opening": opening})
        entries.append({"compiled_entry_id": entry_id, "original_position": position, "occurrence_id": item["occurrence_id"], "product_id": item["product_id"], "script_revision_ref": deepcopy(item["script_revision_ref"]), "opening_mode": "merchant_original" if imported else opening, "sentences": sentences})
    if missing_modules:
        gaps = list(missing_modules.values())
        raise BusinessError("MISSING_MODULE", "编排所需模块尚未准备；可定向补齐缺失模块，保留有效正文。", details={
            "key": gaps[0]["module_key"], "missing_modules": gaps, "requires_replan": True,
            "suggested_next_step": {"action": "save_script_plan", "scope": "missing_modules_only",
                "preserve_existing_body": True, "base_script_refs": [g["script_revision_ref"] for g in gaps]},
            "compiler_version": PLAYBACK_STRUCTURE_VERSION})
    return {"compiler_version": PLAYBACK_STRUCTURE_VERSION, "base_items": deepcopy(items), "loop_mode": loop_mode,
        # Keep this old field as a response alias, not an expanded supercycle.
        "supercycle_length": len(items), "cycle_length": len(items), "opening_policy": "saved_position_every_five",
        "entries": entries, "requirements": list(requirements.values()), "playback_statistics": playback_statistics(entries)}
