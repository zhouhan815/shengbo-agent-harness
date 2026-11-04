"""Summarize which fields an approved standard catalog import actually saved."""

import asyncio

from shengbo_contracts.common import ContractError

from .catalog_intake_approval import resolve_catalog_intake_decision


_REQUIRED = {"name": "商品名称", "sku_id": "SKU", "currency": "币种", "price": "日常售价"}
_POINTS_LABEL = "核心卖点"
_POINTS_REQUIREMENT = {"id": "core_selling_points", "label": _POINTS_LABEL,
                       "reason": "至少填写 1 条。可先保留已入库的基础资料，稍后再补。"}
_PRICE_REQUIREMENT = {"id": "price", "label": "日常售价", "reason": "请填写实际日常售价。"}
_FIELD_GROUPS = (
    ("商品信息", (("image", "商品图片"), ("material", "材质"), ("ingredients", "成分"),
                  ("target_audience", "适用人群"), ("sales_platform", "销售平台"))),
    ("使用与服务", (("usage_instructions", "使用说明"), ("storage_conditions", "储存条件"),
                   ("shelf_life_value", "保质期"), ("shipping_origin", "发货地"),
                   ("shipping_time", "发货时效"), ("return_policy", "退换货政策"))),
)


def _present(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None


def _has_core_points(body):
    points = body.get("core_selling_points")
    legacy = body.get("selling_points")
    return (isinstance(points, list) and any(isinstance(point, str) and point.strip() for point in points)
            or isinstance(legacy, str) and bool(legacy.strip()))


def _with_core_requirement(product, has_points):
    """Keep a single readiness rule for persisted reports and current records."""
    missing = [label for label in product.get("missing_required", []) if label != _POINTS_LABEL]
    groups = [{**group, "items": [item for item in group["items"]
                                if group["label"] != "讲解与问答" or item != _POINTS_LABEL]}
              for group in product["configured_groups"]]
    if has_points:
        group = next((item for item in groups if item["label"] == "讲解与问答"), None)
        if group is None:
            groups.append({"label": "讲解与问答", "items": [_POINTS_LABEL]})
        else:
            group["items"].append(_POINTS_LABEL)
    else:
        missing.append(_POINTS_LABEL)
    return {**product, "configured_groups": [group for group in groups if group["items"]],
            "missing_required": missing,
            "required_actions": [dict(action) for action in (_PRICE_REQUIREMENT, _POINTS_REQUIREMENT)
                                 if action["label"] in missing],
            "recommendations": [entry for entry in product["recommendations"]
                                if entry.get("label") != _POINTS_LABEL]}


def upgrade_catalog_intake_result(report):
    """Migrate earlier saved reports without changing their product snapshots."""
    if report.get("version") == 2:
        return report
    return {**report, "version": 2, "products": [
        _with_core_requirement(product, any(group["label"] == "讲解与问答" and _POINTS_LABEL in group["items"]
                                           for group in product["configured_groups"]))
        for product in report["products"]]}


async def refresh_catalog_intake_requirements(client, report):
    """Read current selling-point status when the workbench opens a completed report."""
    async def refresh(product):
        try:
            async with asyncio.timeout(1.5):
                current = await client.query("read_product", {"product_id": product["product_id"]}, controller=True)
        except (ContractError, TimeoutError):
            return product
        body, ref = current.get("body"), current.get("ref")
        if not isinstance(body, dict) or not isinstance(ref, dict) or ref.get("id") != product["product_id"] or body.get("archived"):
            return product
        return _with_core_requirement(product, _has_core_points(body))

    products = await asyncio.gather(*(refresh(product) for product in report["products"]))
    return {**report, "products": products}


def build_catalog_intake_result(saved_products):
    products = []
    for saved in saved_products:
        body, ref = saved["body"], saved["ref"]
        required = [label for key, label in _REQUIRED.items() if _present(body.get(key))]
        groups = [{"label": "入库必填项", "items": required}]
        specifications = [item["name"] for item in body.get("specifications", [])
                          if item.get("name") and _present(item.get("value"))]
        if specifications:
            groups.append({"label": "规格参数", "items": specifications})
        for label, fields in _FIELD_GROUPS:
            configured = [name for key, name in fields
                          if _present(body.get(key)) and (key != "sales_platform" or body[key] != "unspecified")]
            if configured:
                groups.append({"label": label, "items": configured})

        documents = [item["title"] for item in body.get("knowledge_documents", [])
                     if item.get("title") and _present(item.get("content"))]
        if documents:
            groups.append({"label": "知识文档", "items": documents})
        other = [label for key, label in (("knowledge", "商品知识正文"), ("faq", "常见问答"))
                 if _present(body.get(key))]
        if other:
            groups.append({"label": "讲解与问答", "items": other})

        recommendations = []
        if not _present(body.get("faq")):
            highlights = [name for name in specifications if name not in {"款式", "品牌", "类目"}][:2]
            reason = ("可围绕" + "、".join(f"「{name}」" for name in highlights) + "补充选购和使用问答。"
                      if highlights else "可整理顾客常问的适用场景、使用方式和选购问题。")
            recommendations.append({"label": "常见问答", "reason": reason})
        knowledge_text = "\n".join([body.get("knowledge") or "", *(
            doc.get("content") or "" for doc in body.get("knowledge_documents", []))])
        if not _present(body.get("return_policy")) and not any(word in knowledge_text for word in ("退货", "退换", "售后")):
            recommendations.append({"label": "退换货说明", "reason": "可补充实际适用条件，方便直播时准确回答售后问题。"})
        if (not documents and not _present(body.get("usage_instructions"))
                and not _present(body.get("knowledge"))):
            recommendations.append({"label": "使用与注意事项", "reason": "可补充适用范围、使用条件和注意事项。"})
        product = {"product_id": ref["id"], "product_ref": ref, "name": body["name"],
                   "sku_id": body["sku_id"], "configured_groups": groups,
                   "missing_required": [label for label in _REQUIRED.values() if label not in required],
                   "recommendations": recommendations}
        products.append(_with_core_requirement(product, _has_core_points(body)))
    return {"version": 2, "products": products}


def historical_catalog_intake_result(engine, run):
    """Project a prior completed import from its approved fields and exact committed receipts."""
    if run.get("status") != "COMPLETED" or not run.get("catalog_intake_plan"):
        return None
    try:
        decision = resolve_catalog_intake_decision(engine, run)
    except ContractError:
        return None
    if decision["state"] != "approved":
        return None
    fields_by_sku = {row["sku_id"]: row["fields"] for row in run["catalog_intake_plan"]["candidates"]}
    saved = []
    for sku in decision["selected_sku_ids"]:
        operation = decision["operations_by_sku"].get(sku, {})
        if operation.get("status") != "committed":
            return None
        saved.append({"ref": operation["product_ref"], "body": fields_by_sku[sku]})
    return build_catalog_intake_result(saved) if saved else None
