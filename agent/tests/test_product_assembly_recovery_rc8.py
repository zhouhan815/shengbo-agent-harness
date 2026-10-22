"""RC8 narrow no-commit correction; fake models, real isolated native harness."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from shengbo_contracts.business import COMMAND_ARGS, ProductAssemblyInvalidDetails
from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.client import ShengboClient
from shengbo_prelive.middleware import ActionBoundary, Budget
from shengbo_prelive.models import RunScope
from shengbo_prelive.tools import COMMAND_MAP, build_tools
from shengbo_prelive.workflows import MaterialsFlowInput, run_workflow
from test_middleware import FakeEngine, tool_request
from test_optional_workflows import authorize
from test_runtime_business import env, human_command, new_run, product, Product
from test_selection_dependency_recovery_v1 import ScriptedModel

CODE = "PRODUCT_ASSEMBLY_INVALID"
SECRET = "fake-private-diagnostic-secret-do-not-echo"

def safe_details():
    return {"validation_stage": "supplied", "commit_state": "not_committed",
        "issues": [{"path": ["name"], "type": "missing"}, {"path": ["price"], "type": "missing"}],
        "product_ref": None, "schema_hash": "sha256:" + "a" * 64, "required_fields": ["name", "price"]}


def wire_error(status=422, details=None, code=CODE):
    return httpx.Response(status, json={"error": {"code": code, "message": SECRET,
        "details": safe_details() if details is None else details}}, request=httpx.Request("POST", "http://isolated/"))


def test_client_accepts_only_typed_public_diagnostics_and_constant_message():
    with pytest.raises(ContractError) as caught:
        ShengboClient._result(wire_error())
    error = caught.value
    assert error.code == CODE and error.status == 422
    assert error.details == safe_details()
    assert SECRET not in json.dumps(error.as_error().model_dump())
    assert not hasattr(error, "known_operation_id"), "HTTP cannot attest a runtime operation"


@pytest.mark.parametrize("status", [400, 401, 403, 409, 500, 503])
def test_client_wrong_status_never_becomes_correctable(status):
    with pytest.raises(ContractError) as caught:
        ShengboClient._result(wire_error(status))
    assert caught.value.code == "VOICE_RESPONSE_INVALID"
    assert caught.value.status == (status if status in {401, 403} or status >= 500 else 502)
    assert SECRET not in json.dumps(caught.value.as_error().model_dump())


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(input=SECRET),
    lambda d: d.update(commit_state="unknown"),
    lambda d: d.update(validation_stage=SECRET),
    lambda d: d.update(schema_hash=SECRET),
    lambda d: d.update(issues=[]),
    lambda d: d["issues"][0].update(input=SECRET),
    lambda d: d["issues"][0].update(type=SECRET),
    lambda d: d["issues"][0].update(path=[SECRET]),
    lambda d: d["issues"][0].update(path=[True]),
    lambda d: d["issues"][0].update(path=[-1]),
    lambda d: d.update(required_fields=[SECRET]),
    lambda d: d.update(product_ref={"kind":"draft","id":"p","revision":1,"content_hash":"hash"}),
    lambda d: d.update(product_ref={"kind":"product","id":"p","revision":"1","content_hash":"hash"}),
])
def test_client_invalid_dto_discards_all_untrusted_diagnostics(mutation):
    details = safe_details()
    mutation(details)
    with pytest.raises(ContractError) as caught:
        ShengboClient._result(wire_error(details=details))
    assert caught.value.code == "VOICE_RESPONSE_INVALID" and caught.value.status == 502
    assert caught.value.details == {} and SECRET not in str(caught.value)


def args_for_correction():
    return {"schema_hash": safe_details()["schema_hash"], "patches": [], "fields": {}}


def attested(engine, request, *, code=CODE, details=None):
    name = request.tool_call["name"]
    raw = request.tool_call["args"]
    logical = request.tool_call["id"]
    if name == "wf_product_materials":
        data = MaterialsFlowInput.model_validate(raw).model_dump(mode="json", exclude_none=True)
        raw = data["parameters"]
        flow = "flow_" + canonical_hash(["r1", name, data])[7:39]
        logical = flow + ":save_product:" + canonical_hash([raw, None])
    typed = COMMAND_ARGS["save_product"].model_validate(raw).model_dump(mode="json", exclude_none=True)
    op_id = "op_" + canonical_hash(["r1", logical, "save_product"])[7:39]
    engine.store.data[("operation", op_id)] = {"id":op_id,"run_id":"r1","key":"save_product",
        "status":"known_failed","error_code":code,"args":typed,"request_hash":canonical_hash(typed)}
    error = ContractError(code, SECRET, status=422, details=details or safe_details())
    error.known_operation_id = op_id
    error.known_operation_input_hash = canonical_hash(typed)
    return error, engine.store.data[("operation", op_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", [False, True])
async def test_m5_dedicated_correction_has_shared_three_attempt_limit_and_no_hidden_action(flow):
    engine = FakeEngine()
    raw = args_for_correction()
    req = tool_request("wf_product_materials" if flow else "save_product",
        args={"action":"save","parameters":raw} if flow else raw)
    error, _ = attested(engine, req)
    async def reject(_):
        raise error
    for count in range(1, 4):
        message = await ActionBoundary(engine).awrap_tool_call(req, reject)
        body = json.loads(message.content)
        assert message.status == "error" and body["correction_count"] == count
        assert body["recovery"]["automatic_action_taken"] is False
        assert body["recovery"]["resubmit"]["requires_complete_fields"] is True
        assert SECRET not in message.content
    with pytest.raises(ContractError, match=CODE):
        await ActionBoundary(engine).awrap_tool_call(req, reject)
    assert engine.store.get("run","r1")["correctable_tool_errors"] == {CODE:3}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["wrong_tool", "flow_inspect", "flow_select", "child", "intent", "status401", "status503",
    "unknown", "not_attested", "cross_run", "wrong_key", "sent", "wrong_code", "wrong_hash", "wrong_input",
    "wrong_id", "invalid_dto", "schema_changed", "product_mismatch", "generic_invalid", "goal_denied", "cas_denied"])
async def test_m5_wrong_authority_or_unknown_is_not_downgraded(case):
    engine = FakeEngine()
    req = tool_request("save_product", args=args_for_correction())
    error, op = attested(engine, req)
    if case == "wrong_tool": req.tool_call["name"] = "save_session_draft"
    elif case.startswith("flow_"): req.tool_call.update(name="wf_product_materials", args={"action":case[5:]})
    elif case == "child": req.runtime.context = RunScope("r1","i1","execute","product_facts","b1")
    elif case == "intent": req.runtime.context = RunScope("r1","i1","intent")
    elif case == "status401": error.status = 401
    elif case == "status503": error.status = 503
    elif case == "unknown": engine.store.update("run","r1",pending_unknown_operation="unknown-op")
    elif case == "not_attested": del error.known_operation_id
    elif case == "cross_run": op["run_id"] = "foreign-run"
    elif case == "wrong_key": op["key"] = "save_session_draft"
    elif case == "sent": op["status"] = "sent"
    elif case == "wrong_code": op["error_code"] = "OTHER"
    elif case == "wrong_hash": op["request_hash"] = "forged"
    elif case == "wrong_input": req.tool_call["args"]["fields"] = {"name":"other"}
    elif case == "wrong_id": req.tool_call["id"] = "foreign-call"
    elif case == "invalid_dto": error.details["input"] = SECRET
    elif case == "schema_changed": error.details["schema_hash"] = "sha256:" + "b"*64
    elif case == "product_mismatch": error.details["product_ref"] = {"kind":"product","id":"foreign","revision":1,"content_hash":"hash"}
    elif case == "generic_invalid": error.code = "INVALID_REQUEST"
    elif case == "goal_denied": error.code, error.status = "GOAL_EFFECT_DENIED",403
    elif case == "cas_denied": error.code, error.status = "REVISION_CONFLICT",403
    async def reject(_): raise error
    with pytest.raises(ContractError):
        await ActionBoundary(engine).awrap_tool_call(req, reject)
    assert not engine.store.get("run","r1").get("correctable_tool_errors")


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", [False, True])
async def test_real_runtime_explicit_model_correction_after_invalid_product_save(env, flow):
    engine, service = env
    ref = await product(service, "化妆品完整字段纠错回归")
    original = await service.query("read_product", {"ref":ref})
    run = await new_run(engine, "更新这个商品的规格，不改话术")
    await authorize(engine,run,["save_product"])
    run = engine.store.update("run",run["id"], target_product_ids=[ref["id"]], created_product_ids=[],
        allowed_tools=["save_product"], goal={"objective":"prepare_session","target_product_ids":[ref["id"]],
            "scope":run["scope"],"required_analyses":[]})
    original_goal = deepcopy(run["goal"])
    original_effects = deepcopy(run["allowed_effects"])
    await engine.start()
    engine.scheduler.cancel()
    await asyncio.gather(engine.scheduler, return_exceptions=True)
    bad = {"product_id":ref["id"],"expected_revision":ref["revision"],
        "schema_hash":service.schema()["schema_hash"],"fields":{},
        "patches":[{"path":"/specifications","op":"set","value":[{"name":"容量","value":"3克"}]}]}
    calls=[]
    state={"stage":0}
    def call(name,args):
        calls.append((name,deepcopy(args)))
        return AIMessage(content="",tool_calls=[{"id":"assembly-recovery-"+str(len(calls)),"name":name,"args":args}])
    from langchain_core.messages import AIMessage
    def save(args):
        return call("wf_product_materials",{"action":"save","parameters":args}) if flow else call("save_product",args)
    async def logic(messages):
        stage=state["stage"];state["stage"]+=1
        if stage==0:return save(bad)
        last=messages[-1]; value=json.loads(last.content)
        if stage==1:
            assert last.status=="error" and value["error"]["code"]==CODE
            assert value["correction_count"]==1 and value["recovery"]["automatic_action_taken"] is False
            before=await service.query("read_product",{"ref":ref})
            assert before==original
            assert not engine.store.get("run",run["id"]).get("pending_unknown_operation")
            return call("read_product",value["recovery"]["next_actions"][0]["args"])
        if stage==2:
            assert value["ref"]==ref
            fields={key:value["body"][key] for key in Product.model_fields if key in value["body"]}
            fields["specifications"]=[{"name":"容量","value":"3克"}]
            return save({**bad,"fields":fields})
        assert stage==3
        receipt=value["result"] if flow else value
        state["saved_ref"]=receipt["data"]["product_ref"]
        return call("finish_turn",{"outcome":"BLOCKED","summary":"离线纠错检查点：商品已保存，未生成话术或语音，不宣称全场完成。"})
    engine.model=ScriptedModel(logic)
    engine.main=engine._build_graph("main")
    await engine._invoke(run)
    assert state["stage"]==4
    assert [name for name,_ in calls]==[("wf_product_materials" if flow else "save_product"),"read_product",
        ("wf_product_materials" if flow else "save_product"),"finish_turn"]
    current=engine.store.get("run",run["id"])
    assert current["goal"]==original_goal and current["allowed_effects"]==original_effects
    assert current["correctable_tool_errors"]=={CODE:1} and not current.get("pending_unknown_operation")
    ops=[op for op in engine.store.list("operation") if op["key"]=="save_product"]
    assert sorted(op["status"] for op in ops)==["committed","known_failed"]
    after=await service.query("read_product",{"ref":state["saved_ref"]})
    assert after["ref"]["revision"]==ref["revision"]+1
    assert after["body"]["specifications"]==[{"name":"容量","value":"3克"}]
    assert not engine.store.list("question"),"No synthetic approval or hidden content/TTS action"


def test_short_tool_hint_requires_complete_fields_without_growing_old_description():
    tools={tool.name:tool for tool in build_tools(FakeEngine())}
    description=tools["save_product"].description
    assert "完整fields" in description and "更新" in description and "不能填{}" in description
    assert "不会自动选入" in description


@pytest.mark.asyncio
async def test_direct_and_flow_share_one_correction_counter():
    engine=FakeEngine()
    for n,name in enumerate(["save_product","wf_product_materials","save_product","wf_product_materials"],1):
        raw=args_for_correction()
        req=tool_request(name,args={"action":"save","parameters":raw} if name.startswith("wf_") else raw)
        error,_=attested(engine,req)
        async def reject(_, error=error):raise error
        if n<=3:
            result=await ActionBoundary(engine).awrap_tool_call(req,reject)
            assert json.loads(result.content)["correction_count"]==n
        else:
            with pytest.raises(ContractError,match=CODE):
                await ActionBoundary(engine).awrap_tool_call(req,reject)
    assert engine.store.get("run","r1")["correctable_tool_errors"]=={CODE:3}


@pytest.mark.asyncio
@pytest.mark.parametrize("code",["MODEL_CALL_BUDGET_EXHAUSTED","CONTEXT_LIMIT","MODEL_TIMEOUT","RETRY_BUDGET_EXHAUSTED"])
async def test_budget_rejections_remain_hard(code):
    engine=FakeEngine()
    async def reject(_):raise ContractError(code,"budget denied",status=429)
    with pytest.raises(ContractError,match=code):
        await ActionBoundary(engine).awrap_tool_call(tool_request("save_product",args=args_for_correction()),reject)
    assert not engine.store.get("run","r1").get("correctable_tool_errors")


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected",[("goal","GOAL_EFFECT_DENIED"),("target","TARGET_DENIED"),
    ("field","FIELD_SCOPE_DENIED"),("cas","REVISION_CONFLICT"),("unknown","RECONCILIATION_REQUIRED")])
async def test_real_business_goal_field_cas_unknown_guards_unchanged(env,case,expected):
    engine,service=env
    ref=await product(service,"仍受边界保护的商品")
    before=await service.query("read_product",{"ref":ref})
    run=await new_run(engine,"更新此商品规格，不改价格")
    run=await authorize(engine,run,["save_product"])
    run=engine.store.update("run",run["id"],target_product_ids=[ref["id"]],created_product_ids=[])
    if case=="goal":engine.store.update("run",run["id"],allowed_effects=[])
    elif case=="target":engine.store.update("run",run["id"],target_product_ids=[])
    elif case=="field":engine.store.update("run",run["id"],allowed_product_paths=["/name"])
    elif case=="unknown":engine.store.update("run",run["id"],pending_unknown_operation="unsettled")
    fields={key:before["body"][key] for key in Product.model_fields if key in before["body"]}
    fields["specifications"]=[{"name":"容量","value":"3克"}]
    args={"product_id":ref["id"],"expected_revision":ref["revision"]+(1 if case=="cas" else 0),
        "schema_hash":service.schema()["schema_hash"],"fields":fields,
        "patches":[{"path":"/specifications","op":"set","value":fields["specifications"]}]}
    scope=RunScope(run["id"],"guard-only","execute")
    with pytest.raises(ContractError,match=expected):
        await engine.business_command(scope,"save_product",args,"guard-only-save")
    assert await service.query("read_product",{"ref":ref})==before
    assert not engine.store.get("run",run["id"]).get("correctable_tool_errors")


@pytest.mark.asyncio
async def test_ambiguous_502_is_unknown_not_correctable_and_has_no_attestation(env,monkeypatch):
    engine,service=env
    ref=await product(service)
    run=await new_run(engine,"更新商品资料")
    await authorize(engine,run,["save_product"])
    engine.store.update("run",run["id"],target_product_ids=[ref["id"]],created_product_ids=[])
    async def malformed(*args,**kwargs):
        raise ContractError("VOICE_RESPONSE_INVALID","rejected malformed response",status=502)
    monkeypatch.setattr(engine.client,"command",malformed)
    args={"product_id":ref["id"],"expected_revision":ref["revision"],
        "schema_hash":service.schema()["schema_hash"],"patches":[],"fields":{}}
    with pytest.raises(ContractError) as caught:
        await engine.business_command(RunScope(run["id"],"502-test","execute"),"save_product",args,"unknown-save")
    assert not hasattr(caught.value,"known_operation_id")
    current=engine.store.get("run",run["id"])
    assert current["pending_unknown_operation"]
    assert engine.store.get("operation",current["pending_unknown_operation"])["status"]=="unknown"
    assert not current.get("correctable_tool_errors")
