"""Product facts and room overrides. No product copies are stored in rooms.

Money is calculated with Decimal, never by the language model. Models reject
unknown keys so a misspelled configuration cannot silently appear to work.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import re
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, model_validator
from .consumer_catalog import consumer_context_view


OPTIONAL_PRODUCT_FIELDS = {
    "material": ("material",), "ingredients": ("ingredients",),
    "specifications": ("specifications",), "target_audience": ("target_audience",),
    "storage_conditions": ("storage_conditions",),
    "shelf_life": ("shelf_life_value", "shelf_life_unit"),
    "shipping_origin": ("shipping_origin",), "shipping_time": ("shipping_time",),
    "return_policy": ("return_policy",), "usage_instructions": ("usage_instructions",),
    "faq": ("faq",), "knowledge": ("knowledge",),
    "knowledge_documents": ("knowledge_documents",),
}


def active_product_fields(product, *, consumer_safe=True):
    """Project active facts without mutating a saved revision or its hash.

    Old records may retain values behind an inactive flag. Neither generation
    nor retrieval may treat those values as facts. Empty values remain unknown,
    rather than gaining placeholder prose such as '资料未提供'.
    """
    result = deepcopy(product)
    for group in result.get("hidden_optional_fields", []):
        for key in OPTIONAL_PRODUCT_FIELDS.get(group, ()):
            result[key] = (None if key == "shelf_life_value" else "month" if key == "shelf_life_unit"
                           else [] if key in {"specifications", "knowledge_documents"} else "")
    return consumer_context_view(result) if consumer_safe else result


def active_context_products(value):
    """Sanitize product snapshots embedded in generation-only context copies."""
    def project(node):
        if isinstance(node, list):
            return [project(item) for item in node]
        if not isinstance(node, dict):
            return deepcopy(node)
        projected = (active_product_fields(node, consumer_safe=False) if "hidden_optional_fields" in node
                     and any(key in node for key in ("name", "price", "selling_points")) else node)
        return {key: project(item) for key, item in projected.items()}
    return consumer_context_view(project(value))


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class Specification(ConfigModel):
    name: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=500)


class ProductFields(ConfigModel):
    sku_id: str = Field(default="", max_length=80)
    sales_platform: Literal["unspecified", "douyin", "tiktok", "kuaishou", "other"] = "unspecified"
    sale_status: Literal["available", "unavailable"] = "available"
    specifications: list[Specification] = Field(default_factory=list, max_length=60)
    material: str = Field(default="", max_length=2000)
    ingredients: str = Field(default="", max_length=2000)
    target_audience: str = Field(default="", max_length=4000)
    storage_conditions: str = Field(default="", max_length=2000)
    shelf_life_value: float | None = Field(default=None, gt=0, le=100000)
    shelf_life_unit: Literal["day", "month", "year"] = "month"
    shipping_origin: str = Field(default="", max_length=500)
    shipping_time: str = Field(default="", max_length=2000)
    return_policy: str = Field(default="", max_length=4000)
    core_selling_points: list[str] = Field(default_factory=list, max_length=60)
    usage_instructions: str = Field(default="", max_length=15000)
    faq: str = Field(default="", max_length=15000)
    hidden_optional_fields: list[Literal["material", "ingredients", "specifications", "target_audience",
        "storage_conditions", "shelf_life", "shipping_origin", "shipping_time", "return_policy",
        "usage_instructions", "faq", "knowledge", "knowledge_documents"]] = Field(default_factory=list, max_length=13)

    @model_validator(mode="after")
    def check_points(self, info: ValidationInfo):
        if any(not p.strip() or len(p) > 4000 for p in self.core_selling_points):
            raise ValueError("每条核心卖点须为1至4000字")
        if len("\n".join(self.core_selling_points)) > 15000:
            raise ValueError("核心卖点合计不得超过15000字")
        object.__setattr__(self, "core_selling_points", [p.strip() for p in self.core_selling_points])
        # This base validator also covers Product/ProductInput's knowledge
        # fields. Restrict writes to actual model fields, not extra metadata.
        # Patch commands preserve historical inactive storage unless the user
        # explicitly changes the inactive groups. Read/generation projections
        # still exclude those values. Full product inputs keep normalization.
        if not (info.context or {}).get("preserve_hidden_optional_values", False):
            # Validation may clear explicitly disabled fields, never silently
            # sanitize an approved payload. Admission rejects it separately.
            projected = active_product_fields(self.model_dump(), consumer_safe=False)
            for group in self.hidden_optional_fields:
                for key in OPTIONAL_PRODUCT_FIELDS[group]:
                    if key in type(self).model_fields:
                        object.__setattr__(self, key, projected[key])
        return self


class Document(ConfigModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=15000)


class Coupon(ConfigModel):
    mode: Literal["none", "discount", "coupon"] = "none"
    discount: float | None = Field(default=None, gt=0, lt=1)
    amount: float | None = Field(default=None, gt=0, le=10000000)
    currency: Literal["CNY", "TWD", "USD", "AUD"] = "CNY"
    minimum_spend: float | None = Field(default=None, ge=0, le=10000000)
    condition: str = Field(default="", max_length=1000)
    scope: Literal["all", "selected"] = "all"
    product_ids: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def check_mode(self):
        if self.mode == "discount" and self.discount is None:
            raise ValueError("折扣模式必须填写0至1之间的折扣比率")
        if self.mode == "coupon" and self.amount is None:
            raise ValueError("优惠券模式必须填写优惠面额")
        if self.mode != "none" and self.scope == "selected" and not self.product_ids:
            raise ValueError("部分商品优惠必须选择适用商品")
        return self


class ProductOverride(ConfigModel):
    mode: Literal["none", "discount", "price"] = "none"
    discount: float | None = Field(default=None, gt=0, lt=1)
    price: float | None = Field(default=None, ge=0, le=10000000)
    exclusive: bool = True
    promotion: str = Field(default="", max_length=4000)
    gifts: str = Field(default="", max_length=4000)
    shipping_override: bool = False
    shipping_time: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def check_mode(self):
        if self.mode == "discount" and self.discount is None:
            raise ValueError("单品折扣模式必须填写折扣比率")
        if self.mode == "price" and self.price is None:
            raise ValueError("单品优惠价模式必须填写优惠价")
        if self.shipping_override and not self.shipping_time:
            raise ValueError("开启发货覆写后须填写本场发货时效")
        return self


class RoomCommerce(ConfigModel):
    scene: str = Field(default="", max_length=6000)
    coupon: Coupon = Field(default_factory=Coupon)
    promotion: str = Field(default="", max_length=4000)
    promotion_scope: Literal["all", "selected"] = "all"
    promotion_product_ids: list[str] = Field(default_factory=list, max_length=1000)
    knowledge_documents: list[Document] = Field(default_factory=list, max_length=20)
    product_overrides: dict[str, ProductOverride] = Field(default_factory=dict, max_length=1000)
    hidden_sections: list[Literal["scene", "coupon", "promotion", "knowledge_documents"]] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def check_docs(self):
        if len({d.id for d in self.knowledge_documents}) != len(self.knowledge_documents):
            raise ValueError("直播间知识文档编号不得重复")
        if self.promotion and self.promotion_scope == "selected" and not self.promotion_product_ids:
            raise ValueError("本场促销必须选择适用商品")
        return self


def parse_room(value):
    try:
        result = RoomCommerce.model_validate(value).model_dump()
        for key in result["hidden_sections"]:
            if key == "coupon":
                result[key] = Coupon().model_dump()
            elif key == "promotion":
                result.update(promotion="", promotion_scope="all", promotion_product_ids=[])
            else:
                result[key] = [] if key == "knowledge_documents" else ""
        return result
    except ValidationError as e:
        errors = "；".join(".".join(map(str, x["loc"])) + "：" + x["msg"] for x in e.errors()[:4])
        raise HTTPException(400, "直播间业务配置无效：" + errors) from None


def validate_room(value, products):
    result = parse_room(value)
    ids = set(products)
    references = set(result["product_overrides"]) | set(result["coupon"]["product_ids"]) | set(result["promotion_product_ids"])
    if references - ids:
        raise HTTPException(400, "本场配置只能关联本直播间已选择的商品")
    for pid, override in result["product_overrides"].items():
        if override["mode"] == "price" and Decimal(str(override["price"])) > Decimal(str(products[pid]["price"])):
            raise HTTPException(400, "优惠价不得高于日常售价")
    return result


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def quote(product, config):
    """Quote one unit, not an imaginary basket. Manual conditions stay uncertain."""
    c = config.get("commerce", parse_room({}))
    override = c["product_overrides"].get(product["id"], ProductOverride().model_dump())
    daily = money(product["price"])
    base = daily
    steps = [{"source": "product", "operation": "daily_price", "amount": float(base)}]
    if override["mode"] == "price":
        base = money(override["price"])
    elif override["mode"] == "discount":
        base = money(daily * Decimal(str(override["discount"])))
    if base > daily:
        raise HTTPException(409, "商品日常售价已降低，本场优惠价高于日常售价，请修改本场配置")
    if override["mode"] != "none":
        steps.append({"source": "room_product", "operation": override["mode"], "amount": float(base)})
    single_price = base
    coupon = c["coupon"]
    applicable = coupon["scope"] == "all" or product["id"] in coupon["product_ids"]
    reason = "未配置全场优惠"
    pending = False
    if coupon["mode"] != "none" and applicable:
        if override["mode"] != "none" and override["exclusive"]:
            reason = "优先单品优惠，不叠加全场优惠券"
        elif coupon["currency"] != product["currency"]:
            reason = "币种不一致，不参与全场优惠"
        else:
            condition = coupon["condition"].strip()
            parsed = re.fullmatch(r"满\s*(\d+(?:\.\d{1,2})?)\s*(?:元)?\s*(?:可用|减\s*\d+(?:\.\d{1,2})?\s*(?:元)?)?", condition)
            threshold = coupon["minimum_spend"]
            if condition and not parsed:
                # Extra eligibility rules do not make an already determined
                # single-item price unknown when that item misses the required
                # spend. Check textual contradictions before using that gate;
                # a qualified basket still needs its manual rules confirmed.
                text_thresholds = re.findall(r"满\s*(\d+(?:\.\d{1,2})?)(?![\d.])", condition)
                text_reductions = re.findall(r"减\s*(\d+(?:\.\d{1,2})?)(?![\d.])", condition)
                conflicting_threshold = threshold is not None and (
                    any(money(value) != money(threshold) for value in text_thresholds)
                    or money(threshold) > 0 and re.search(r"无门槛|不设(?:金额)?门槛|不限(?:订单)?金额", condition))
                conflicting_reduction = text_reductions and (
                    coupon["mode"] != "coupon" or any(money(value) != money(coupon["amount"]) for value in text_reductions))
                if conflicting_threshold:
                    pending, reason = True, "文字优惠门槛与数值门槛冲突，需要确认"
                elif conflicting_reduction:
                    pending, reason = True, "文字减免金额与优惠面额冲突，需要确认"
                elif threshold is not None and base < money(threshold):
                    reason = "单件未达到全场优惠门槛；多件订单需另行核对；附加优惠条件仍须满足：" + condition
                else:
                    pending, reason = True, "优惠条件需要人工确认：" + condition
            else:
                if parsed:
                    parsed_threshold = float(parsed.group(1))
                    if threshold is not None and money(threshold) != money(parsed_threshold):
                        pending, reason = True, "文字优惠门槛与数值门槛冲突，需要确认"
                    threshold = parsed_threshold
                    reduction = re.search(r"减\s*(\d+(?:\.\d{1,2})?)", condition)
                    if reduction and (coupon["mode"] != "coupon" or money(reduction.group(1)) != money(coupon["amount"])):
                        pending, reason = True, "文字减免金额与优惠面额冲突，需要确认"
                if not pending:
                    if threshold is not None and base < money(threshold):
                        reason = "单件未达到全场优惠门槛；多件订单需另行核对"
                    else:
                        if coupon["mode"] == "discount":
                            base = money(base * Decimal(str(coupon["discount"])))
                        else:
                            base = max(Decimal("0.00"), base - money(coupon["amount"]))
                        steps.append({"source": "room_coupon", "operation": coupon["mode"], "amount": float(base)})
                        reason = "满足已配置金额条件，已应用全场优惠"
    elif coupon["mode"] != "none":
        reason = "该商品不在全场优惠适用范围"
    # Legacy global promotional prices are kept as archived inputs, not room offers.
    return {"product_id": product["id"], "sku_id": product.get("sku_id") or product["id"],
            "currency": product["currency"], "daily_price": float(daily), "base_price": float(single_price),
            "final_price": None if pending else float(base), "confirmed": not pending,
            "reason": reason, "steps": steps, "quantity": 1}


def effective_product(product, config):
    if product.get("archived") or product.get("sale_status", "available") != "available":
        raise HTTPException(409, "商品不可售，不能生成话术、推荐或开播")
    result = active_product_fields(product)
    c = config.get("commerce", parse_room({}))
    override = c["product_overrides"].get(product["id"], ProductOverride().model_dump())
    q = quote(product, config)
    result["promo_price"] = q["final_price"] if q["confirmed"] and q["final_price"] != product["price"] else None
    promotions = []
    if c["promotion"] and (c["promotion_scope"] == "all" or product["id"] in c["promotion_product_ids"]):
        promotions.append(c["promotion"])
    if override["promotion"]:
        promotions = [override["promotion"]]
    if override["gifts"]:
        promotions.append("本场赠品：" + override["gifts"])
    if c["coupon"]["mode"] != "none" or override["mode"] != "none":
        promotions.append("后端价格规则：" + q["reason"])
    result["promotion"] = "\n".join(promotions)
    if override["shipping_override"]:
        result["shipping_time"] = override["shipping_time"]
        # An explicitly configured room override is an active room fact; the
        # catalog flag still disables only the global default, not that choice.
        if "hidden_optional_fields" in result:
            result["hidden_optional_fields"] = [k for k in result["hidden_optional_fields"] if k != "shipping_time"]
    result["_room_scene"] = c["scene"]
    result["_price_quote"] = q
    result["_shipping_overridden"] = override["shipping_override"]
    result["_gifts"] = override["gifts"]
    return result


GENERATION_KEYS = ["name", "price", "promo_price", "currency", "selling_points", "promotion",
                   "specifications", "material", "ingredients", "target_audience", "_room_scene"]


def generation_signature(product):
    canonical = {k: product.get(k) for k in GENERATION_KEYS if product.get(k) not in (None, "", [])}
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def script_is_stale(script, product, config):
    effective = effective_product(product, config)
    if script.get("commerce_signature"):
        return script["commerce_signature"] != generation_signature(effective)
    # Existing scripts remain usable when no business overrides have changed.
    keys = ["name", "price", "promo_price", "currency", "selling_points", "promotion"]
    old = script["product_snapshot"]
    effective["promotion"] = effective["promotion"].removesuffix("\n后端价格规则：未配置全场优惠")
    # Before migration, global promo fields were the only source of offers.
    knowledge_changed = any(str(f.get("id", "")).startswith("KNOWLEDGE_") for f in script.get("facts", [])) and old.get("knowledge") != effective.get("knowledge")
    return knowledge_changed or any(old.get(k) != effective.get(k) for k in keys) or bool(effective.get("_room_scene")) or any(
        effective.get(k) for k in ["specifications", "material", "ingredients", "target_audience"])
