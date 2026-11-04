"""Scope the two document actions in the product workspace."""
from pydantic import ValidationError
from shengbo_contracts.common import ContractError, RecordRef

from .selection import same_ref

ACTION = "product_material_action"


def validate_product_material_request(request, conversation):
    view = request.view_context
    if ACTION not in view:
        return
    action = view[ACTION]
    if (action not in ("create", "update") or view.get("view") != "products"
            or conversation["scope"]["kind"] != "catalog" or conversation.get("room_id")
            or view.get("room_id") is not None or view.get("preparation_id") is not None
            or request.intent_mode != "new_task" or request.related_run_id is not None
            or not request.material_refs or not request.text.strip()):
        raise ContractError("PRODUCT_MATERIAL_REQUEST_INVALID", "请从商品库上传资料后重新提交商品任务。", status=422)
    product_id = view.get("product_id")
    if action == "create":
        if product_id is not None or request.selection is not None or request.observed_versions:
            raise ContractError("PRODUCT_MATERIAL_TARGET_INVALID", "资料新增任务不能选择已有商品。", status=422)
        return
    selection = request.selection or {}
    target = selection.get("target")
    try:
        ref = RecordRef.model_validate(target.get("object_ref")) if isinstance(target, dict) else None
    except ValidationError:
        ref = None
    if ("items" in selection or not isinstance(product_id, str) or not product_id
            or not ref or ref.kind != "product" or ref.id != product_id
            or set(target) != {"object_ref"}
            or len(request.observed_versions) != 1
            or not same_ref(ref.model_dump(mode="json"), request.observed_versions[0].model_dump(mode="json"))):
        raise ContractError("PRODUCT_MATERIAL_TARGET_INVALID", "请从要更新的商品详情重新提交资料。", status=422)


def validate_product_material_intent(run, proposal):
    context = run["input"].get("view_context") or {}
    action = context.get(ACTION)
    if not action:
        return
    product_id = context.get("product_id")
    if (proposal.objective != "catalog_update" or proposal.room_id or proposal.preparation_id
            or set(proposal.target_product_ids) - ({product_id} if action == "update" else set())):
        raise ContractError("PRODUCT_MATERIAL_INTENT_DENIED",
            "本任务只处理商品库。新增任务的 target_product_ids 留空；更新任务只使用界面选中的商品。", status=422,
            details={"commit_state": "not_committed",
                     "fields": ["assessment.objective", "assessment.target_product_ids", "assessment.room_id", "assessment.preparation_id"]})


def guard_product_material_write(run, key, args):
    context = run["input"].get("view_context") or {}
    action = context.get(ACTION)
    if key != "save_product" or not action:
        return
    identity = args.get("product_id")
    if action == "create" and identity is not None:
        raise ContractError("PRODUCT_MATERIAL_CREATE_ONLY", "此任务只能新增商品，不能修改已有商品。", status=403)
    if action == "update" and identity != context["product_id"]:
        raise ContractError("PRODUCT_MATERIAL_UPDATE_ONLY", "此任务只能修改从详情页选中的商品。", status=403)
