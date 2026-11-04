"""Human-facing decision text and the separate, non-approving revision path."""
from __future__ import annotations

import re
from .decision_options import read_stored_option, unambiguous_options


_FIELD_LABELS = {
    "price": "日常售价", "promo_price": "活动价", "promo price": "活动价",
    "image": "商品图片", "sales_platform": "销售平台", "target_audience": "适用人群",
    "storage_conditions": "储存条件", "shelf_life": "保质期",
    "shelf_life_value": "保质期时长", "shelf_life_unit": "保质期单位",
    "ingredients": "配料", "sku_id": "规格编号", "specifications": "商品规格",
    "sale_status": "销售状态", "shipping_origin": "发货地", "shipping_time": "发货时效",
    "return_policy": "退换政策", "core_selling_points": "核心卖点", "selling_points": "卖点",
    "usage_instructions": "使用说明", "knowledge_documents": "知识文档", "faq": "常见问答",
}
_FIELD_NAMES = re.compile(
    r"(?<![A-Za-z0-9_./`])`?(?:\$\.|fields\.|/fields/|/)?("
    + "|".join(_FIELD_LABELS) + r")`?(?![A-Za-z0-9_/`]|\.[A-Za-z0-9_])",
    re.IGNORECASE,
)
_PRICE_ASSIGNMENT = re.compile(r"日常售?价\s*(?:=|＝|→|->)\s*`?price`?(?![A-Za-z0-9_])", re.I)
_PROMO_ASSIGNMENT = re.compile(r"(?:直播(?:售|优惠)?价|活动价)\s*(?:=|＝|→|->)\s*`?promo[_ ]price`?(?![A-Za-z0-9_])", re.I)
_PROMO_FIELD = re.compile(r"(?<![A-Za-z0-9_])promo[_ ]price(?![A-Za-z0-9_])", re.I)
_SKU = re.compile(r"(?<![A-Za-z0-9_])SKU(?![A-Za-z0-9_])")
_REVISION_PATHS = {
    "catalog_intake_approval": ("revise", "说明你希望调整的规格、名称或价格"),
    "text_audio_approval": ("edit_text", "说明要改哪段文案或声音设置"),
    "script_audio_preview_approval": ("reject", "说明要调整的试听内容"),
    "playlist_order": ("revise", "说明希望怎样调整播放顺序"),
    "product_identity": ("revise", "说明是哪个商品，或补充区分信息"),
    "new_product_import": ("revise", "说明要怎样调整商品导入范围"),
}


def decision_text(text, *, catalog=False):
    if not isinstance(text, str):
        return text
    promo_label = "促销参考价" if catalog else "活动价"
    text = _PRICE_ASSIGNMENT.sub("日常售价", text)
    text = _PROMO_ASSIGNMENT.sub(promo_label, text)
    text = re.sub(r"(?<![A-Za-z0-9_])shelf_life\s*/\s*ingredients(?![A-Za-z0-9_])",
                  "保质期和配料", text, flags=re.I)
    text = (text.replace("【价格映射】", "【价格】").replace("【SKU结构】", "【商品规格】")
            .replace(f"日常售价；{promo_label}", f"分别记录日常售价和{promo_label}")
            .replace(f"日常售价、{promo_label}", f"日常售价和{promo_label}"))
    text = _SKU.sub("规格", text)
    return _FIELD_NAMES.sub(lambda match: promo_label if _PROMO_FIELD.fullmatch(match.group(1)) else
                            _FIELD_LABELS[match.group(1).lower()], text)


def revision_input(question):
    path = _REVISION_PATHS.get(question.get("kind"))
    if question.get("controller_question") and question.get("kind") == "task_scope":
        if (question.get("context") or {}).get("resolution_kind") == "room_selection":
            path = ("revise", "说明要换哪个直播间，或怎样调整任务范围")
    if path is None:
        return None
    choice, placeholder = path
    return {"choice": choice, "label": "提交修改意见", "placeholder": placeholder}


def is_revision_reply(question, answer):
    revision = revision_input(question)
    return (revision is not None and isinstance(answer, dict) and set(answer) == {"choice", "text"}
            and answer["choice"] == revision["choice"] and isinstance(answer["text"], str)
            and bool(answer["text"].strip()) and len(answer["text"]) <= 6000)


def playlist_prompt(items):
    lines = []
    for item in items[:20]:
        number = item.get("version_number")
        version = f"第{number}版话术" if number else "未编号话术"
        label = item.get("variant_id")
        if isinstance(label, str) and re.search(r"[\u3400-\u9fff]|[A-Za-z]+ [A-Za-z]+", label):
            version += f" · {label}"
        lines.append(f"{item['position']}. {item['product_name']} · {version}（修订{item['script_revision']}）")
    if len(items) > 20:
        lines.append(f"其余{len(items) - 20}项见完整预览。")
    lines.append("持续循环，播完从头继续。")
    return "请确认以下播放顺序。\n" + "\n".join(lines)


def _public_options(question):
    kind = question.get("kind")
    stored = question.get("options")
    if not isinstance(stored, list):
        return []
    options = []
    for raw in stored:
        try:
            option = read_stored_option(raw)
        except ValueError:
            continue
        value = option["value"]
        if kind == "text_audio_approval" and value == "approve":
            option.update(label="批准此整包文案生成语音", requires_human_action=True)
        elif kind == "product_identity" and value == "create_distinct":
            option.update(label="确认是不同商品，另建档案", requires_human_action=True)
            action = raw.get("human_action")
            binding = question.get("identity_binding")
            if (isinstance(action, dict) and action.get("kind") == "product_identity"
                    and isinstance(binding, dict) and action.get("body") == binding):
                option["human_action"] = action
        elif kind == "script_audio_preview_approval" and value == "approve":
            option["requires_human_action"] = True
            binding = question.get("approval_binding")
            operation_id = binding.get("operation_id") if isinstance(binding, dict) else None
            if isinstance(operation_id, str) and operation_id:
                option.update(label="核对已登记的试听授权并继续",
                              human_action={"kind": "verify_existing_receipt", "operation_id": operation_id})
        options.append(option)
    return options


def _text_audio_action(question):
    action = question.get("human_action")
    command = action.get("command") if isinstance(action, dict) and action.get("kind") in (None, "business_command") else None
    if not isinstance(command, dict) or command.get("command") != "approve_text_for_audio":
        return None
    args, mutation = command.get("args"), command.get("mutation")
    if not isinstance(args, dict) or not isinstance(mutation, dict):
        return None
    operation_id = mutation.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        return None
    binding = question.get("approval_binding")
    if binding is None:
        preview = question.get("preview")
        if not isinstance(preview, dict):
            return None
        binding = {**preview, "operation_id": operation_id}
    if not isinstance(binding, dict):
        return None
    if (binding.get("operation_id") == operation_id and args.get("review_hash") and args.get("preparation_id")
            and all(binding.get(key) == args[key] for key in ("preparation_id", "review_hash"))):
        return action
    return None


def project_decision(question, *, catalog=False):
    """Keep durable IDs, values, receipts and version bindings exact."""
    kind = question.get("kind")
    catalog = catalog or kind == "catalog_confirm"
    options = _public_options(question)
    prompt = question["prompt"] if isinstance(question.get("prompt"), str) else ""
    view = {**question, "prompt": decision_text(prompt, catalog=catalog)}
    view.pop("human_action", None)
    view.pop("requires_human_action", None)
    action = _text_audio_action(question) if kind == "text_audio_approval" else None
    if action is not None:
        view["human_action"] = action
    promo_note = "资料中的直播价只作参考。本场活动价需在直播间单独设置。"
    if catalog and promo_note not in view["prompt"] and (
            _PROMO_FIELD.search(prompt) or any(_PROMO_FIELD.search(option["label"]) for option in options)):
        view["prompt"] += "\n" + promo_note
    if "agent_explanation" in view:
        view["agent_explanation"] = decision_text(view["agent_explanation"], catalog=catalog)
    preview = view.get("preview")
    if kind == "text_audio_approval" and isinstance(preview, dict):
        voice = preview.get("voice")
        if isinstance(voice, dict):
            raw_voice = f"音色：{voice.get('provider', '未知')} / {voice.get('voice', '未知')}。"
            voice_label = preview.get("voice_display_name") or "本场选定的声音"
            view["prompt"] = view["prompt"].replace(raw_voice, f"声音为{voice_label}。")
    if kind == "playlist_order" and isinstance(preview, dict) and preview.get("items"):
        view["prompt"] = playlist_prompt(preview["items"])
    if kind == "task_scope" and (question.get("context") or {}).get("resolution_kind") == "room_selection":
        view["prompt"] = view["prompt"].replace("也可输入直播间完整ID。", "找不到要用的直播间时，可以补充说明。")
    revision = revision_input(question)
    view.setdefault("allow_text", True)
    if revision:
        view["allow_text"] = False
        view["revision_text"] = revision
    for option in options:
        option["label"] = decision_text(option["label"], catalog=catalog)
    view["options"] = unambiguous_options(options)
    return view
