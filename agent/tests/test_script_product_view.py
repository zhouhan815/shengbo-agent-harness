from types import SimpleNamespace

import pytest

from shengbo_prelive.models import RunScope
from shengbo_prelive.tools import build_tools
from shengbo_prelive.workflows import run_workflow
from test_optional_workflows import authorize
from test_runtime_business import env, new_run, prepared_text


@pytest.mark.asyncio
@pytest.mark.parametrize("typed_script", [True, False])
async def test_script_text_product_reads_omit_image_only_for_typed_task(env, monkeypatch, typed_script):
    engine, service = env
    room, prep, _ = await prepared_text(service)
    run = await new_run(engine, "生成商品话术", room_id=room["id"], preparation_id=prep["id"])
    run = await authorize(engine, run, ["save_context_bundle"])
    if typed_script:
        engine.store.update("run", run["id"], goal={**run["goal"],
            "script_generation": {"preparation_id": prep["id"]}})
    scope = RunScope(run["id"], "product-view", "execute")
    preparation = await service.query("read_preparation", {"preparation_id": prep["id"]})
    product_ref = preparation["body"]["selected_product_refs"][0]
    original_query = engine.business_query
    image = "data:image/png;base64," + "A" * 100_000

    async def query_with_image(scope, key, args):
        value = await original_query(scope, key, args)
        if key == "read_product":
            return {**value, "body": {**value["body"], "image": image}}
        return value

    monkeypatch.setattr(engine, "business_query", query_with_image)
    product_tool = next(tool for tool in build_tools(engine) if tool.name == "read_product")
    direct = await product_tool.coroutine(SimpleNamespace(context=scope), ref=product_ref)
    expected = await engine.business_query(scope, "read_product", {"ref": product_ref})
    if typed_script:
        expected = {**expected, "body": {key: value for key, value in expected["body"].items() if key != "image"}}
    assert direct == expected
    for detail in ("index", "plan", "full"):
        response = await run_workflow(engine, scope, "wf_context_bundle", {"action": "inspect", "detail": detail})
        assert response["result"]["products"] == [expected]
        if detail != "full":
            snapshot_ref = response["result"]["full_ref"]
            snapshot = engine.store.get("context_snapshot", snapshot_ref["id"])
            assert snapshot["body"]["document"]["inputs"]["products"] == [expected]
    assert (await engine.business_query(scope, "read_product", {"ref": product_ref}))["body"]["image"] == image
