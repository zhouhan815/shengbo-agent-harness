"""Real wire budgets and native tail offload, using a network-free transport."""
import json
from unittest.mock import patch

import httpx
import pytest
from deepagents.backends.utils import file_data_to_string
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from shengbo_contracts.common import ContractError, WorkScope, canonical_hash
from shengbo_prelive.middleware import Audit, ContextProjection, ToolPolicy
from shengbo_prelive.models import RunScope
from shengbo_prelive.policy import EFFECTS
from shengbo_prelive.providers.chatgpt import build_chatgpt_model, preview_request_payload
from shengbo_prelive.runtime import CURRENT_SCOPE, RuntimeEngine
from shengbo_prelive.tools import COMMAND_MAP, build_tools


class OfflineClient:
    async def close(self):
        pass


class FakeCredentials:
    calls = 0

    def get_access_token(self):
        self.calls += 1
        return 'fake-isolated-test-credential'


@pytest.fixture
async def setup(tmp_path):
    engine = RuntimeEngine(tmp_path, OfflineClient(), context_input_token_limit=24000)
    conversation = engine.create_conversation({'scope': {'kind': 'catalog'}})
    scope = WorkScope(kind='preparation', preparation_id='prep-budget').model_dump()
    effects = sorted(EFFECTS['prepare_session'])
    run = engine.store.put('run', {'id': 'run-budget', 'conversation_id': conversation['id'],
        'scope': scope, 'status': 'RUNNING', 'phase': 'execute', 'input': {'text': '准备整场直播，99元单价，满180减20，批准后才合成语音', 'material_refs': []},
        'goal': {'objective': 'prepare_session', 'scope': scope, 'allowed_effects': effects},
        'allowed_effects': effects, 'allowed_tools': [name for name, command in COMMAND_MAP.items() if command in effects]})
    engine.store.put('question', {'id': 'pending-review', 'run_id': run['id'], 'kind': 'text_audio_approval', 'status': 'OPEN',
        'prompt': '99元、满180减20是否正确？', 'options': [], 'approval_binding': {'review_hash': 'sha256:exact-review', 'generation_epoch': 3}})
    engine.store.put('operation', {'id': 'unknown-audio', 'run_id': run['id'], 'key': 'prepare_required_audio', 'status': 'unknown', 'args': {'price': 99}})
    sent, credentials = [], FakeCredentials()
    engine.test_response_tool_calls = []

    def respond(request):
        payload = json.loads(request.content)
        assert engine._estimate_payload(payload) <= engine.context_input_token_limit, 'the actual final wire, including Skills and schemas, must fit'
        sent.append(payload)
        text = getattr(engine, 'test_summary_text', '短工作摘要，完整工具结果应按文件回读。') if not payload.get('tools') else '测试完成'
        output = engine.test_response_tool_calls.pop(0) if payload.get('tools') and engine.test_response_tool_calls else [
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}]
        event = {'type': 'response.completed', 'response': {'status': 'completed', 'output': output,
            'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}}
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, text='data: ' + json.dumps(event) + '\n\n')

    engine.model = build_chatgpt_model(credentials=credentials, transport=httpx.MockTransport(respond), verify_catalog=False,
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request, on_response=engine._model_response)
    engine.saver = InMemorySaver()
    # Preserve real framework tools, Skills, summarizer and provider serialization.
    # Only completion/business mutation boundaries are outside this budget test.
    with patch('shengbo_prelive.runtime.main_middleware', lambda e: [ContextProjection(e), ToolPolicy(e), Audit(e)]):
        graph = engine._build_graph('main')
    # This fixture intentionally exposes every authorized tool (production's
    # CLI uses progressive disclosure). Its fixed wire now exceeds the old 24k
    # artificial ceiling before there is any evictable history. Measure that
    # irreducible wire, then lend the test a fixed 4k conversation allowance.
    # The temporary 32k probe stops before auth/reservation/HTTP. Neither this
    # probe nor the test allowance changes the production 48k ceiling, the 6k
    # summary trigger or the 1.5k retained-prefix policy initialized above.
    class FixedWireReady(Exception):
        pass

    fixed_probe = []
    def capture_fixed(payload):
        fixed_probe.append(payload)
        raise FixedWireReady()

    probe_scope = RunScope(run['id'], 'fixture-fixed-budget-probe', 'execute')
    token = CURRENT_SCOPE.set(probe_scope)
    engine.context_input_token_limit = 32000
    try:
        with patch.object(engine.model, '_before_send', capture_fixed):
            with pytest.raises(FixedWireReady):
                await graph.ainvoke({'messages': [HumanMessage('只测固定请求形状。')],
                    'files': engine._initial_files('main')},
                    {'configurable': {'thread_id': 'fixture-fixed-budget-probe'}}, context=probe_scope)
    finally:
        CURRENT_SCOPE.reset(token)
    assert len(fixed_probe) == 1 and not sent and credentials.calls == 0
    with engine.store.tx() as db:
        assert db.execute('SELECT COUNT(*) FROM attempts WHERE run_id=?', (run['id'],)).fetchone()[0] == 0
    fixed_wire = {**fixed_probe[0], 'input': [item for item in fixed_probe[0]['input'] if item.get('role') == 'developer']}
    fixed_tokens = engine._estimate_payload(fixed_wire)
    assert fixed_tokens <= 32000 - 4096, 'fixed schema growth must not silently consume the test working set'
    engine.context_input_token_limit = fixed_tokens + 4096
    assert engine.context_summary_trigger_tokens == 6000
    assert engine.context_summary_keep_tokens == 1500
    yield engine, graph, run, sent, credentials
    await engine.stop()


def six_tool_batch():
    names = ['read_run_history', 'read_preparation', 'read_business_schema', 'read_voice_options', 'read_source', 'read_source']
    originals = {}
    calls, results = [], []
    for index, name in enumerate(names):
        call_id = 'budget-call-' + str(index)
        calls.append({'id': call_id, 'name': name, 'args': {}})
        content = json.dumps({'exact_ref': {'kind': 'source_manifest', 'id': 'source-' + str(index), 'revision': 1, 'content_hash': canonical_hash(index)},
            'text': ('原始资料不得丢失。99元单价、满180减20；未知顾客假设不可写为事实。' * 90) + '尾部精确标记-' + str(index)}, ensure_ascii=False)
        originals[call_id] = content
        results.append(ToolMessage(content, tool_call_id=call_id, name=name))
    history = HumanMessage('较早原始历史必须保留。' * 500)
    return [history, AIMessage(content='', tool_calls=calls), *results], originals


@pytest.mark.asyncio
async def test_six_parallel_tool_results_fit_real_wire_and_remain_exactly_readable(setup):
    engine, graph, run, sent, credentials = setup
    messages, originals = six_tool_batch()
    config = {'configurable': {'thread_id': 'wire-tail-test'}, 'recursion_limit': 15}
    scope = RunScope(run['id'], 'invoke-budget', 'execute')
    token = CURRENT_SCOPE.set(scope)
    try:
        # The shared fixture already measured the actual SDK fixed wire and
        # lent this scenario 4k for its retained tool turn and two page reads.
        # Do not repeat that probe or make this depend on an old schema size.
        assert 8000 <= engine.context_input_token_limit <= 32000, 'unexpected fixed-schema growth must remain visible'
        assert engine.context_summary_trigger_tokens == 6000, 'keep the original 6k pressure/summary policy'
        await graph.ainvoke({'messages': messages, 'files': engine._initial_files('main')}, config, context=scope)
        snapshot = await graph.aget_state(config)
        assert snapshot.values['_summarization_event']['summary_message'].additional_kwargs.get('lc_source') == 'summarization'
        assert snapshot.values['messages'][0].content == messages[0].content
        files = snapshot.values['files']
        for call_id, original in originals.items():
            stored = [file_data_to_string(data) for path, data in files.items() if path.endswith('/' + call_id)]
            assert stored == [original], 'offload must preserve the entire original, including its tail'
            paged = [file_data_to_string(data) for path, data in files.items() if path.endswith('/' + call_id + '.pages.jsonl')]
            assert len(paged) == 1
            assert ''.join(json.loads(line)['text'] for line in paged[0].splitlines()) == original
        main = [payload for payload in sent if payload.get('tools')]
        assert len(main) == 1 and len(sent) == 2, 'one summary and one real main request; local rejection sends nothing'
        inputs = main[0]['input']
        assert {item['call_id'] for item in inputs if item.get('type') == 'function_call'} == set(originals)
        assert {item['call_id'] for item in inputs if item.get('type') == 'function_call_output'} == set(originals)
        system = '\n'.join(str(item.get('content', '')) for item in inputs if item.get('role') == 'developer')
        assert system.count('<shengbo_trusted_context>') == 1
        assert '满180减20' in system and 'sha256:exact-review' in system and 'unknown-audio' in system
        # The exact remaining budget now recovers before reaching the final
        # provider fence, rather than requiring a rejected request first.
        assert not any(event['type'] == 'model.window_rejected' for event in engine.events(run['id'])['events'])
        assert any(event['type'] == 'context.tools_offloaded' for event in engine.events(run['id'])['events'])
        await graph.ainvoke({'messages': [HumanMessage('只读继续，必要时分段回读完整原文')]}, config, context=scope)
        assert len(sent) == 3 and sent[-1].get('tools'), 'persisted replacements must prevent repeat summary/offload loops'
        paged_path = next(path for path in files if path.endswith('/budget-call-2.pages.jsonl'))
        engine.test_response_tool_calls = [[{'type': 'function_call', 'namespace': 'functions', 'name': 'read_file',
            'call_id': 'read-page-' + str(offset), 'arguments': json.dumps({'file_path': paged_path, 'offset': offset, 'limit': 1})} for offset in (0, 1)]]
        result = await graph.ainvoke({'messages': [HumanMessage('请读取schema的前两页，每次limit=1')]}, config, context=scope)
        page_results = [message for message in result['messages'] if isinstance(message, ToolMessage) and message.tool_call_id.startswith('read-page-')]
        assert len(page_results) == 2
        recovered = [json.loads(message.content[message.content.index('{'):])['text'] for message in page_results]
        assert ''.join(recovered) == originals['budget-call-2'][:2400]
        assert len(sent) == 5 and credentials.calls == 5, 'paged read_file results must fit without further summary/offload'
    finally:
        CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_native_read_file_recovery_uses_exact_existing_files_without_4000_char_heads(setup):
    from deepagents.backends.utils import create_file_data
    engine, graph, run, sent, _ = setup
    files = {f'/documents/{index}.json': create_file_data(json.dumps({'text': '精确中文资料不应丢失。' * 1500, 'tail': str(index)}, ensure_ascii=False)) for index in range(3)}
    engine.test_response_tool_calls = [[{'type': 'function_call', 'namespace': 'functions', 'name': 'read_file',
        'call_id': 'large-file-' + str(index), 'arguments': json.dumps({'file_path': path, 'offset': 0, 'limit': 100})}
        for index, path in enumerate(files)]]
    config = {'configurable': {'thread_id': 'read-file-overflow'}, 'recursion_limit': 10}
    scope = RunScope(run['id'], 'read-file-overflow', 'execute')
    token = CURRENT_SCOPE.set(scope)
    try:
        await graph.ainvoke({'messages': [HumanMessage('读取三个具体资料文件')], 'files': files}, config, context=scope)
        state = await graph.aget_state(config)
        assert all(file_data_to_string(state.values['files'][path]) == file_data_to_string(original) for path, original in files.items())
        outputs = [item for item in sent[-1]['input'] if item.get('type') == 'function_call_output']
        assert {item['call_id'] for item in outputs} == {'large-file-' + str(index) for index in range(3)}
        for output in outputs:
            pointer = json.loads(output['output'])
            assert pointer['status'] == 'read_view_referenced'
            assert pointer['file_content_hash'] == canonical_hash(file_data_to_string(files[pointer['file_path']]))
            assert len(output['output']) < 1000
        assert not any(event['type'] == 'model.window_rejected' for event in engine.events(run['id'])['events'])
        assert any(event['type'] == 'context.tools_offloaded' for event in engine.events(run['id'])['events'])
    finally:
        CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_successful_skill_read_survives_native_compaction_in_exact_m3(setup):
    engine, graph, run, sent, _ = setup
    files = engine._initial_files('main')
    path = next(iter(files))
    engine.test_response_tool_calls = [[{'type': 'function_call', 'namespace': 'functions', 'name': 'read_file',
        'call_id': 'skill-read-once', 'arguments': json.dumps({'file_path': path, 'offset': 0, 'limit': 1000})}]]
    config = {'configurable': {'thread_id': 'skill-read-ledger'}, 'recursion_limit': 10}
    scope = RunScope(run['id'], 'skill-read', 'execute')
    token = CURRENT_SCOPE.set(scope)
    try:
        await graph.ainvoke({'messages': [HumanMessage('读取适用技能')], 'files': files}, config, context=scope)
        read = (await engine.project_context(scope))['read_skills']['items'][0]
        assert read['path'] == path and read['content_hash'] == canonical_hash(file_data_to_string(files[path]))
        assert read['status'] == 'complete' and read['read_count'] == 1
        await graph.ainvoke({'messages': [HumanMessage('前段工作记录。' * 1800), HumanMessage('新的当前问题。')]}, config, context=scope)
        assert any(event['type'] == 'context.summarized' for event in engine.events(run['id'])['events'])
        developer = '\n'.join(str(item.get('content', '')) for item in sent[-1]['input'] if item.get('role') == 'developer')
        assert path in developer and read['content_hash'] in developer and '"read_count": 1' in developer
        assert (await engine.project_context(scope))['read_skills']['items'][0]['read_count'] == 1
    finally:
        CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_irreducible_m3_fails_before_paid_summary_or_main_request(setup):
    engine, graph, run, sent, credentials = setup
    engine.store.update('run', run['id'], input={'text': '不可省略的当前真实指令。' * 5000, 'material_refs': []})
    token = CURRENT_SCOPE.set(RunScope(run['id'], 'static-too-large', 'execute'))
    try:
        with pytest.raises(ContractError, match='CONTEXT_LIMIT') as failure:
            await graph.ainvoke({'messages': [HumanMessage('历史内容。' * 3000)], 'files': {}},
                {'configurable': {'thread_id': 'fixed-too-large'}}, context=CURRENT_SCOPE.get())
        assert isinstance(failure.value, ContextOverflowError)
        assert failure.value.details['fixed_input_tokens'] > engine.context_input_token_limit
        assert not sent and credentials.calls == 0
        diagnostics = [event['data'] for event in engine.events(run['id'])['events']
                       if event['type'] == 'context.window_rejected']
        assert len(diagnostics) == 1
        assert diagnostics[0]['stage'] == 'fixed_context_precheck'
        assert diagnostics[0]['fixed_wire_tokens'] > diagnostics[0]['input_token_limit']
        assert diagnostics[0]['trusted_context_field_tokens']['original_user_request'] > 24000
        assert '不可省略的当前真实指令' not in json.dumps(diagnostics, ensure_ascii=False)
        with engine.store.tx() as db:
            assert db.execute('SELECT COUNT(*) FROM attempts WHERE run_id=?', (run['id'],)).fetchone()[0] == 0
    finally:
        CURRENT_SCOPE.reset(token)


def test_wire_preview_has_real_schemas_without_constructing_credentials():
    registered = build_tools(object())
    with patch('shengbo_prelive.providers.chatgpt.CredentialManager', side_effect=AssertionError('preview must not access auth')):
        payload = preview_request_payload([HumanMessage('预算预检')], registered)
    tools = payload['tools'][0]['tools']
    original_tools = set("""
        accept_analysis_reports build_context_bundle compile_playback_plan dispatch_analysis_batch execute_script_plan
        find_audience_research finish_turn import_recap inspect_change_impact link_recap_to_preparation
        prepare_required_audio preview_playlist_reorder publish_preparation read_analysis_batch read_asset_checklist
        read_audio_readiness read_business_schema read_capabilities read_context read_job read_preparation
        read_product read_recap read_room read_run_history read_script read_script_usage read_session_history
        read_source read_text_review read_voice_options request_user_decision retry_failed_items save_asset_checklist
        save_playlist save_product save_script_change_plan save_session_draft save_task_assessment search_catalog
        update_preparation_products validate_product_assembly wf_analysis_coordination wf_context_bundle
        wf_finalize_preparation wf_playback_review wf_product_materials wf_required_audio wf_script_execution
    """.split())
    names = {tool['name'] for tool in tools}
    assert names == {tool.name for tool in registered}, 'the SDK must serialize every public registered tool exactly once'
    assert len(tools) == len(registered) == len(names)
    assert 'read_generation_output' in names
    assert names - {'read_generation_output', 'import_script', 'patch_script', 'wf_selling_points'} == original_tools
    assert next(tool for tool in tools if tool['name'] == 'save_task_assessment')['parameters']['properties']
    readiness = next(tool for tool in tools if tool['name'] == 'read_audio_readiness')['parameters']
    assert readiness['required'] == ['preparation_id']
    assert readiness['properties']['cursor']['minimum'] == 0
    assert readiness['properties']['limit']['maximum'] == 20


@pytest.mark.asyncio
async def test_no_gain_summary_fits_wire_and_does_not_summarize_same_tool_tail_again(setup):
    engine, graph, run, sent, credentials = setup
    # Added optional workflow schemas increase the fixed part. This case needs
    # a compressible tail larger than the 6k trigger; the bounded-workset tests
    # continue checking overflow/offload. Stay below production's 48k ceiling.
    engine.context_input_token_limit = 32000
    engine.test_summary_text = '已读取工具结果，后续按用户要求继续，所有事实仍以精确原文为准。' * 5
    call = {'id': 'retained-batch', 'name': 'read_source', 'args': {}}
    messages = [HumanMessage('最早请求。'), AIMessage(content='', tool_calls=[call])]
    result = ToolMessage('', tool_call_id=call['id'], name=call['name'])
    config = {'configurable': {'thread_id': 'no-gain-summary'}, 'recursion_limit': 10}
    scope = RunScope(run['id'], 'no-gain', 'execute')
    token = CURRENT_SCOPE.set(scope)
    try:
        # Measure native Skills, actual visible schemas and M3 through the real
        # SDK, stopping before any reservation, credentials or HTTP request.
        # A fixed 7k tool fixture can cross the final fence as schemas grow and
        # then correctly offload, which is a different scenario from no gain.
        class WireShapeReady(Exception):
            pass

        captured = []

        def capture_wire(payload):
            captured.append(payload)
            raise WireShapeReady()

        with patch.object(engine.model, '_before_send', capture_wire):
            with pytest.raises(WireShapeReady):
                await graph.ainvoke({'messages': [HumanMessage('只测完整请求形状。')], 'files': {}},
                    {'configurable': {'thread_id': 'no-gain-fixed-wire-probe'}}, context=scope)
        assert len(captured) == 1 and not sent and credentials.calls == 0
        with engine.store.tx() as db:
            assert db.execute('SELECT COUNT(*) FROM attempts WHERE run_id=?', (run['id'],)).fetchone()[0] == 0
        fixed = {**captured[0], 'input': [item for item in captured[0]['input'] if item.get('role') == 'developer']}

        def estimate(repetitions):
            result.content = '保留的独立工具批次。' * repetitions
            history = preview_request_payload([*messages, result], [])['input']
            return engine._estimate_payload({**fixed, 'input': [*fixed['input'], *history]})

        # Stay within the real 1k pressure margin while reserving room for the
        # slightly longer no-gain summary; keep the production 6k trigger.
        low, high = 0, 5000
        target = engine.context_input_token_limit - 640
        while low < high:
            middle = (low + high) // 2
            if estimate(middle) < target:
                low = middle + 1
            else:
                high = middle
        initial_wire = estimate(low)
        assert engine.context_input_token_limit - 1024 < initial_wire <= engine.context_input_token_limit - 512
        messages.append(result)
        await graph.ainvoke({'messages': messages, 'files': {}}, config, context=scope)
        events = [event for event in engine.events(run['id'])['events'] if event['type'] == 'context.summarized']
        assert len(sent) == 2 and len(events) == 1
        assert events[0]['data']['token_reduction'] < 0
        assert events[0]['data']['status'] == 'retained_no_gain'
        assert events[0]['data']['compressible_tokens_before'] >= engine.context_summary_trigger_tokens
        assert not any(event['type'] in {'context.tools_offloaded', 'model.window_rejected'}
                       for event in engine.events(run['id'])['events'])
        first_wire = sent[-1]
        assert next(item['output'] for item in first_wire['input'] if item.get('type') == 'function_call_output') == result.content
        snapshot = await graph.aget_state(config)
        for index in range(2):
            # Reconstruct the same safely retained tool turn with the native
            # persisted event. No new evictable prefix has appeared, so a new
            # paid summary would merely summarize the prior summary again.
            replay = {'configurable': {'thread_id': 'same-retained-turn-' + str(index)}}
            await graph.aupdate_state(replay, {'messages': messages, 'files': snapshot.values['files'],
                '_summarization_event': snapshot.values['_summarization_event']}, as_node='model')
            stored = await graph.aget_state(replay)
            assert stored.values['_summarization_event'] == snapshot.values['_summarization_event']
            await graph.ainvoke(None, replay, context=scope)
        assert len(sent) == 4 and all(payload.get('tools') for payload in sent[2:])
        assert credentials.calls == 4, 'the same preserved tool turn must not cause paid re-summarization'
        assert len([event for event in engine.events(run['id'])['events'] if event['type'] == 'context.summarized']) == 1
    finally:
        CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_large_tool_only_summary_prefix_is_sent_instead_of_sdk_empty_placeholder(setup):
    engine, graph, run, sent, _ = setup
    # The old 14k start_on='human' trim dropped this entire tool-only prefix.
    # Its loss was then reported as a successful summary without a model call.
    original = '精确报告字段与来源必须保留。' * 1800 + '工具前缀末尾精确标记'
    messages = [AIMessage(content='', tool_calls=[{'id': 'report-only-prefix', 'name': 'read_run_history', 'args': {}}]),
        ToolMessage(original, tool_call_id='report-only-prefix', name='read_run_history'),
        HumanMessage('继续当前只读任务。')]
    scope = RunScope(run['id'], 'tool-only-prefix', 'execute')
    token = CURRENT_SCOPE.set(scope)
    config = {'configurable': {'thread_id': 'tool-only-prefix'}, 'recursion_limit': 15}
    try:
        await graph.ainvoke({'messages': messages, 'files': {}}, config, context=scope)
        assert len(sent) == 2 and not sent[0].get('tools') and sent[1].get('tools')
        summary_input = json.dumps(sent[0]['input'], ensure_ascii=False)
        assert original in summary_input
        assert '本图的可信当前角色为main' in summary_input
        snapshot = await graph.aget_state(config)
        summary = snapshot.values['_summarization_event']['summary_message'].content
        assert 'Previous conversation was too long to summarize.' not in summary
        assert '[reference_only_summary]' not in summary
        assert original in file_data_to_string(snapshot.values['files'][snapshot.values['_summarization_event']['file_path']])
        event = next(event for event in engine.events(run['id'])['events'] if event['type'] == 'context.summarized')
        assert event['data']['status'] == 'compacted'
        assert event['data']['projected_wire_tokens_before'] > engine.context_input_token_limit - event['data']['pressure_margin_tokens']
    finally:
        CURRENT_SCOPE.reset(token)

