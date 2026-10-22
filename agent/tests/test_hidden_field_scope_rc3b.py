"""Real Agent field selection must survive inactive-storage compatibility."""
from copy import deepcopy

import pytest

from test_runtime_business import env, product
from test_selection_binding import freeze, select
from shengbo_contracts.common import ContractError
from app.commerce_config import active_product_fields
from app.prelive.service import Product


@pytest.mark.asyncio
async def test_selected_name_patch_preserves_old_hidden_storage_and_blocks_other_groups(env):
    engine, service = env
    original = await product(service)
    with service.store.transaction() as connection:
        body = deepcopy(service.store.exact(connection, original)["body"])
        body.update(material="历史隐藏材质", knowledge="历史隐藏知识", hidden_optional_fields=["material", "knowledge"])
        ref = service.store.put(connection, "product", original["id"], body, expected=original["revision"])
    _, scope = await freeze(engine, "只修改选中的名称", select(ref, field="/name"), "catalog_update")
    fields = {key: body[key] for key in Product.model_fields}
    fields["name"] = "授权的新名称"
    args = {"product_id": ref["id"], "expected_revision": ref["revision"],
        "schema_hash": service.schema()["schema_hash"], "fields": fields,
        "patches": [{"path": "/name", "op": "set", "value": fields["name"]}]}
    receipt = await engine.business_command(scope, "save_product", args, "name-only-hidden")
    saved = receipt["data"]
    assert saved["changed_paths"] == ["/name"]
    assert saved["product"]["material"] == body["material"]
    assert saved["product"]["knowledge"] == body["knowledge"]
    assert active_product_fields(saved["product"])["material"] == ""
    assert await engine.business_command(scope, "save_product", args, "name-only-hidden") == receipt
    wrong = deepcopy(args)
    wrong["expected_revision"] = saved["product_ref"]["revision"]
    wrong["fields"]["hidden_optional_fields"] = ["material", "knowledge", "ingredients"]
    wrong["patches"] = [{"path": "/hidden_optional_fields", "op": "set",
        "value": wrong["fields"]["hidden_optional_fields"]}]
    with pytest.raises(ContractError, match="FIELD_SCOPE_DENIED"):
        await engine.business_command(scope, "save_product", wrong, "forbidden-hidden-cascade")
    concealed = deepcopy(args)
    concealed["expected_revision"] = saved["product_ref"]["revision"]
    concealed["fields"]["material"] = ""
    with pytest.raises(ContractError, match="ASSEMBLY_PATCH_MISMATCH"):
        await engine.business_command(scope, "save_product", concealed, "hidden-unpatched-clear")
    with service.store.transaction() as connection:
        assert service.store.exact(connection, ref)["body"] == body
