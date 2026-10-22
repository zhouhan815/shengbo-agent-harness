from pathlib import Path
R = Path(__file__).resolve().parents[1]/'engineering-v2'

def update(file,pairs):
    p=R/file
    t=p.read_text(encoding='utf-8')
    for a,b in pairs:
        if a not in t: raise ValueError((file,a[:60]))
        t=t.replace(a,b)
    p.write_text(t,encoding='utf-8')

update('00_后端工程设计.md',[
 ('    services = make_services(store, client, settings)',
  '    trace = await resources.enter_async_context(\n        TraceServices.open(settings.observability_dir))\n    await trace.migrate()\n    await trace.recovery.reconcile_open_spans()\n    services = make_services(store, client, settings, trace=trace)'),
 ('# supervisor、inbox poller、scheduler', '# supervisor、inbox、scheduler、trace outbox镜像/补写'),
 ('| `make_services(store,client,settings) -> Services`', '| `make_services(store,client,settings,trace) -> Services`'),
 ('`AppContainer.close`先关闭后台服务，再退出resources', '`AppContainer.close`先关闭后台服务，flush trace应急记录/追加关闭事件，再退出resources'),
 ('独立 Agent 本地端口默认 8791',
  'TraceServices显式装配PayloadStore、TraceJournal、TraceCallbackHandler、health和outbox镜像；open/migrate失败标写入不可用，不启动新Agent动作。重启先对账未闭合span，不能用checkpoint伪造成功；关闭TraceServices及其SQLite/blob句柄由resources逆序管理。\n\n独立 Agent 本地端口默认 8791'),
])
update('02_运行时与中间件.md',[
 ("graph_config(thread_id=f'branch:{task.id}', recursion_limit=100)",
  "graph_config(thread_id=f'branch:{task.id}', scope=scope,\n                          trace_callbacks=deps.trace_callbacks, recursion_limit=100)"),
 ("graph_config(thread_id=f'main:{run_id}', recursion_limit=200)",
  "graph_config(thread_id=f'main:{run_id}', scope=scope,\n                          trace_callbacks=self.deps.trace_callbacks, recursion_limit=200)"),
 ('await self.deps.scopes.assert_main_invoke(runtime.context)',
  "await self.deps.audit.checked(runtime.context, 'M1.main_fence',\n            lambda: self.deps.scopes.assert_main_invoke(runtime.context))"),
 ('await self.deps.scopes.assert_child(runtime.context)',
  "await self.deps.audit.checked(runtime.context, 'M2.child_fence',\n            lambda: self.deps.scopes.assert_child(runtime.context))"),
 ("    if not policy.execution_allowed(name):\n        return error_tool_message(request, 'NOT_AUTHORIZED')",
  "    if not policy.execution_allowed(name):\n        await deps.audit.decision(scope, 'M4.tool_policy', 'denied', tool_call=request.tool_call)\n        return error_tool_message(request, 'NOT_AUTHORIZED')"),
 ("    if registry.require(name).mutates_business and scope.grant_ref is None:\n        return error_tool_message(request, 'NOT_AUTHORIZED')",
  "    if registry.require(name).mutates_business and scope.grant_ref is None:\n        await deps.audit.decision(scope, 'M4.write_grant', 'denied', tool_call=request.tool_call)\n        return error_tool_message(request, 'NOT_AUTHORIZED')"),
 ("    await policy_service.validate_targets(scope, name, request.tool_call['args'])",
  "    await deps.audit.checked(scope, 'M4.targets',\n        lambda: policy_service.validate_targets(scope, name, request.tool_call['args']))"),
 ('    await scopes.assert_for_tool(scope, spec)',
  "    await deps.audit.checked(scope, 'M5.action_scope', lambda: scopes.assert_for_tool(scope, spec))\n    if spec.name == 'prepare_required_audio':\n        await deps.audit.checked(scope, 'M5.text_audio_gate',\n            lambda: deps.approvals.require_current_for_tool(scope, request.tool_call['args']))"),
 ("    if existing and existing.status == 'unknown':\n        return await reconcile_command_without_resubmit(existing, scope)",
  "    if existing and existing.status == 'unknown':\n        await deps.audit.decision(scope, 'M5.reconcile', 'query_only', action_ref=existing.ref)\n        return await reconcile_command_without_resubmit(existing, scope)"),
 ('商品库、本场、顾客研究、旧稿、用户决定五类完整输入保存在声播',
  '商品库、本场、顾客研究、旧稿、用户决定及已绑定复盘完整输入保存在声播'),
])
p=R/'02_运行时与中间件.md'
t=p.read_text(encoding='utf-8')
t+='''

## 14. Callback、外层拒绝和观测生命周期的最终接线

```python
def graph_config(*, thread_id: str, scope: RunScope | ChildScope,
                 trace_callbacks: TraceCallbackHandler, recursion_limit: int) -> dict:
    return {
        'configurable': {'thread_id': thread_id},
        'recursion_limit': recursion_limit,
        'callbacks': [trace_callbacks.with_scope(scope)],
        'metadata': {'run_id': scope.run_id, 'invocation_id': scope.invocation_id,
                     'branch_task_id': getattr(scope, 'branch_task_id', None)},
    }

async def checked(scope, rule: str, check: Callable[[], Awaitable[T]]) -> T:
    handle = await audit.begin_check_or_stop(scope, rule)
    try:
        result = await check()
    except BaseException as original:
        await audit.try_end_or_spool(handle, error=original)
        raise
    await audit.try_end_or_spool(handle, result=result)
    return result

async def decision(scope, rule: str, outcome: str, **safe_details) -> None: ...
```

M9位于内层不能自动看到全部外层拒绝，因此M1/M2/M4/M5上文已经显式接checked/decision。M3在投影持久化时追加 `context.projected`（refs/hash）；M6每次reserve/release/limit-denied调用decision；M7/M8在终态receipt校验通过/拒绝/jump纠正时调用decision。所有方法共用11的安全Capture/Journal，不再造不同日志系统。原始control异常继续原样上抛，不因记录失败吞interrupt。

工厂启动、主/子invoke、所有before/wrap/after检查和业务job分别有采集责任；不能仅靠M9外壳声明已经记录final_model_messages。主与每个branch的graph_config独立配置callbacks，所以SDK摘要模型调用也可按实际scope关联；摘要预算仍单独计量。尚未能公开观测的字段显式unknown，不能猜测。
'''
p.write_text(t,encoding='utf-8')
print('Applied cross-review observability fixes.')
