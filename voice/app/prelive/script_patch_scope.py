"""A prewrite ambiguity check for large whole-module sentence deletion.

Counts are a narrow scope signal, never a judge of prose quality. An explicit
whole-module replacement stays autonomous and passes all existing grants/CAS.
"""
from copy import deepcopy
from .storage import BusinessError, digest

MIN_ORIGINAL_SENTENCES = 10
MAX_RETAINED_FRACTION_DENOMINATOR = 5  # At most 20% retained, at least 80% removed.


def selected_sentence_objects(sentences, sentence_ids):
    """One exact, contiguous selection contract for read coordinates and patch.

    Preserve source order and the complete immutable sentence objects; hashes
    must not be calculated over a display/spoken-text projection.
    """
    if not sentence_ids:
        return sentences, list(range(len(sentences)))
    selected = [row for row in sentences if row["id"] in sentence_ids]
    indexes = [i for i, row in enumerate(sentences) if row["id"] in sentence_ids]
    if (len(sentence_ids) != len(set(sentence_ids)) or len(selected) != len(sentence_ids)
            or indexes != list(range(min(indexes, default=0), max(indexes, default=-1) + 1))):
        raise BusinessError("TEXT_SELECTION_STALE")
    return selected, indexes


def guard_patch_scope(args, script, original, replacement):
    old, new = original["sentences"], replacement["sentences"]
    if (args.sentence_ids or args.intentional_full_module_replacement
            or len(old) < MIN_ORIGINAL_SENTENCES or len(new) * MAX_RETAINED_FRACTION_DENOMINATOR > len(old)):
        return
    reader = {"tool": "read_script", "args": {"ref": deepcopy(script["ref"]), "sections": [args.module_key]}}
    raise BusinessError("PATCH_SCOPE_COLLAPSE_REQUIRES_EXPLICIT_INTENT",
        "本次整模块替换会删除至少80%的原句，尚未声明有意替换完整模块；本次修改未提交", status=422,
        details={"command": "patch_script", "commit_state": "not_committed",
            "script_ref": deepcopy(script["ref"]), "module_key": args.module_key,
            "original_sentence_count": len(old), "replacement_sentence_count": len(new),
            "removed_sentence_fraction": round(1 - len(new) / len(old), 4),
            "original_sentence_ids": [row["id"] for row in old], "reader": reader,
            "single_sentence_edit_targets": [{"sentence_ids": [row["id"]], "selected_text_hash": digest([row])} for row in old],
            "correction_options": [
                {"mode": "contiguous_sentences", "tool": "patch_script",
                    "required_parameters": ["sentence_ids", "selected_text_hash"],
                    "note": "从精确reader核对原句；单句可用给出的坐标，多句须为连续原句及对应原句对象数组hash，未选句保留"},
                {"mode": "intentional_full_module", "tool": "patch_script",
                    "parameters": {"sentence_ids": [], "intentional_full_module_replacement": True},
                    "note": "仅确实计划替换完整模块时声明；保留原整模块hash及全部现有权限，不代替人工批准或扩大选区"}],
            "automatic_action_taken": False, "semantic_quality_verified": False})
