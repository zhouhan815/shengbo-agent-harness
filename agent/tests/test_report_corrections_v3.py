"""Rejected-report correction through real worker SDK tools; no provider calls."""
from copy import deepcopy
from io import BytesIO
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from PIL import Image
from pydantic import Field, ValidationError
import pytest

from shengbo_contracts.business import AnalysisReport
from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import RunScope
from shengbo_prelive.policy import ROLE_TOOLS
from shengbo_prelive.report_corrections import CorrectReportInput, correct_report
from shengbo_prelive.runtime import CURRENT_SCOPE
from shengbo_prelive.tools import build_tools
from test_runtime_business import BindableFakeModel, env, new_run


ROLE_PATH = '/material_assessment/content_groups/0/role'


async def correction_branch(engine):
    run = await new_run(engine, '分析上传资料和现成原稿，只整理候选，不写正式商品')
    source = engine.materials.accept(run['conversation_id'], 'correction.txt', 'text/plain',
        '测试商品，129元。原稿：欢迎了解这款商品。'.encode())['material_ref']
    engine.store.update('run', run['id'], phase='execute', status='RUNNING',
        input={**run['input'], 'material_refs': [source]})
    branch = engine.store.put('branch', {'id': 'correction-product-facts', 'run_id': run['id'],
        'role': 'product_facts', 'status': 'RUNNING', 'task': {'role': 'product_facts',
        'instruction': '逐项整理原资料，保留原稿及依据', 'product_ids': [],
        'source_refs': [source], 'input_refs': []}})
    return run, source, RunScope(run['id'], 'correction-worker', 'execute', 'product_facts', branch['id'])


def rich_report(source, *, role='script'):
    """The only invalid field is a plausible, unsupported content role."""
    return {'summary': '完整候选报告，报告校验不等于主 Agent 语义采纳', 'product_ids': [],
        'fields': {'name': '测试商品', 'price': 129, 'details': {'size': 'M', 'enabled': True}},
        'insights': [{'type': 'script_origin', 'text': '原稿应保留原文和语言', 'evidence_ids': ['e1']}],
        'evidence': [{'id': 'e1', 'source_ref': source, 'locator': 'text:1',
            'quote': '测试商品，129元。原稿：欢迎了解这款商品。'}],
        'hypotheses': ['风格适配须由主 Agent 判断'],
        'conflicts': [{'field': 'size', 'status': 'nonblocking', 'values': ['M', '未独立核实']}],
        'missing_fields': ['可选品牌故事'],
        'asset_suggestions': [{'kind': 'image', 'description': '后续可补充外观图', 'required': False}],
        'source_refs': [source], 'dependencies': [source],
        'material_assessment': {'product_candidates': [{'candidate_id': 'candidate-1',
            'name': '测试商品', 'identity_status': 'new', 'sku_keys': ['M'], 'evidence_ids': ['e1']}],
            'content_groups': [{'group_id': 'original-1', 'role': role,
                'product_candidate_ids': ['candidate-1'], 'language': 'zh-CN',
                'script_variant_label': '商家原稿', 'evidence_ids': ['e1'], 'note': '逐字保留原稿'}],
            'field_observations': [{'observation_id': 'name', 'product_candidate_id': 'candidate-1',
                'field_path': 'name', 'value': '测试商品', 'claim_kind': 'fact', 'evidence_ids': ['e1']}],
            'missing_info': [{'item_id': 'story', 'product_candidate_id': 'candidate-1',
                'field_path': 'brand_story', 'level': 'optional', 'why': '仅补充风格',
                'question': '是否补充品牌故事', 'can_skip': True}],
            'notes': ['来源未授予写入权限', '资料字段与现成话术须区分']}}


def rejected_draft(engine, scope, report):
    with pytest.raises(ContractError) as caught:
        engine.submit_report(scope, report)
    assert caught.value.code == 'MATERIAL_ASSESSMENT_INVALID'
    detail = caught.value.as_error().model_dump(mode='json')['details']
    assert detail['commit_state'] == 'not_committed'
    assert detail['report_correction']['formal_report_saved'] is False
    return detail['report_correction']['draft_ref']


class CorrectingWorker(BindableFakeModel):
    bad_report: dict
    seen: list = Field(default_factory=list)
    expected_code: str = 'MATERIAL_ASSESSMENT_INVALID'
    patches: list = Field(default_factory=lambda: [{'path': ROLE_PATH, 'value': 'existing_script'}])
    read_before_patch: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(deepcopy(messages))
        if len(self.seen) == 1:
            message = AIMessage(content='', tool_calls=[{'id': 'invalid-report',
                'name': 'submit_product_facts_report', 'args': {'report': self.bad_report}}])
        elif len(self.seen) == 2 or (len(self.seen) == 3 and self.read_before_patch):
            feedback = next(item for item in reversed(messages) if isinstance(item, ToolMessage)
                and item.tool_call_id == 'invalid-report')
            assert feedback.tool_call_id == 'invalid-report' and feedback.status == 'error'
            wire = json.loads(feedback.content)['error']
            assert wire['code'] == self.expected_code
            detail = wire['details']
            if self.expected_code == 'MATERIAL_ASSESSMENT_INVALID':
                assert ['content_groups', 0, 'role'] in [item['loc'] for item in detail['issues']]
                assert detail['semantic_acceptance'] is False
            correction = detail['report_correction']
            assert correction['tool'] == 'correct_analysis_report'
            if self.read_before_patch and len(self.seen) == 2:
                call_id, args = 'read-rejected-report', {'draft_ref': correction['draft_ref'], 'action': 'read'}
            else:
                if self.read_before_patch:
                    read = next(item for item in reversed(messages) if isinstance(item, ToolMessage)
                        and item.tool_call_id == 'read-rejected-report')
                    assert json.loads(read.content)['report'] == AnalysisReport.model_validate(self.bad_report).model_dump(mode='json')
                call_id, args = 'fix-role-only', {'draft_ref': correction['draft_ref'], 'patches': self.patches}
            message = AIMessage(content='', tool_calls=[{'id': call_id, 'name': correction['tool'], 'args': args}])
        else:
            raise AssertionError('A PREPARED report must end the worker without a third model call')
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.asyncio
async def test_real_worker_tool_feedback_corrects_only_role_and_preserves_complete_report(env):
    engine, _ = env
    run, source, scope = await correction_branch(engine)
    bad = rich_report(source)
    model = CorrectingWorker(responses=[], bad_report=bad)
    engine.model, engine.saver = model, InMemorySaver()
    graph = engine._build_graph('product_facts')
    token = CURRENT_SCOPE.set(scope)
    try:
        result = await graph.ainvoke({'messages': [HumanMessage('按资料提交完整候选报告')],
            'files': engine._initial_files('product_facts')},
            {'configurable': {'thread_id': 'report-correction-v3'}, 'recursion_limit': 32}, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)
    feedback = next(item for item in result['messages'] if isinstance(item, ToolMessage)
        and item.tool_call_id == 'invalid-report')
    details = json.loads(feedback.content)['error']['details']
    draft_ref = details['report_correction']['draft_ref']
    correction_call = next(item for item in result['messages'] if isinstance(item, AIMessage)
        and item.tool_calls and item.tool_calls[0]['name'] == 'correct_analysis_report').tool_calls[0]
    assert set(correction_call['args']) == {'draft_ref', 'patches'}
    assert correction_call['args']['patches'] == [{'path': ROLE_PATH, 'value': 'existing_script'}]
    expected = AnalysisReport.model_validate(bad).model_dump(mode='json')
    expected['material_assessment']['content_groups'][0]['role'] = 'existing_script'
    branch = engine.store.get('branch', scope.branch_task_id)
    assert branch['status'] == 'PREPARED'
    assert branch['report'] == expected and branch['report_hash'] == canonical_hash(expected)
    assert not branch.get('accepted_ref')
    rejected = engine.store.get('report_retry_draft', draft_ref['id'])
    assert rejected['report'] == AnalysisReport.model_validate(bad).model_dump(mode='json')
    assert canonical_hash(rejected['report']) == draft_ref['content_hash']
    assert rejected['is_formal'] is False and len(model.seen) == 2
    assert not engine.store.list('operation')
    assert json.loads(result['messages'][-1].content)['status'] == 'PREPARED'
    events = engine.trace.events(run['id'])['events']
    corrected = next(item for item in events if item['type'] == 'branch.report_corrected')['data']
    assert corrected['patched_paths'] == [ROLE_PATH]
    assert corrected['full_report_reemitted'] is False and corrected['formal_acceptance'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['read', 'patch'])
@pytest.mark.parametrize('case', ['branch', 'run', 'role', 'kind', 'revision', 'hash', 'payload_hash', 'finished_branch'])
async def test_correction_rejects_other_scope_or_changed_immutable_draft(env, case, action):
    engine, _ = env
    run, source, scope = await correction_branch(engine)
    ref = rejected_draft(engine, scope, rich_report(source))
    target = scope
    if case == 'branch':
        other = engine.store.get('branch', scope.branch_task_id)
        engine.store.put('branch', {**other, 'id': 'different-worker'})
        target = RunScope(run['id'], 'different-worker-invocation', 'execute', 'product_facts', 'different-worker')
    elif case == 'run':
        target = RunScope('another-run', 'other-run-invocation', 'execute', 'product_facts', scope.branch_task_id)
    elif case == 'role':
        target = RunScope(run['id'], 'other-role-invocation', 'execute', 'session_config', scope.branch_task_id)
    elif case in {'kind', 'revision', 'hash'}:
        key, value = {'kind': ('kind', 'analysis_report'), 'revision': ('revision', 2),
            'hash': ('content_hash', canonical_hash('different-report'))}[case]
        ref = {**ref, key: value}
    elif case == 'payload_hash':
        saved = engine.store.get('report_retry_draft', ref['id'])
        saved['report']['summary'] = 'changed behind the same ref'
        engine.store.put('report_retry_draft', saved)
    else:
        engine.store.update('branch', scope.branch_task_id, status='PREPARED')
    with pytest.raises(ContractError) as caught:
        correct_report(engine, target, ref,
            [{'path': ROLE_PATH, 'value': 'existing_script'}] if action == 'patch' else [], action=action)
    assert caught.value.code == 'REPORT_CORRECTION_SCOPE_DENIED' and caught.value.status == 403
    assert not engine.store.get('branch', scope.branch_task_id).get('report')
    assert not engine.store.list('operation')


@pytest.mark.asyncio
@pytest.mark.parametrize('patches', [
    [{'path': '/material_assessment/content_groups/12/role', 'value': 'existing_script'}],
    [{'path': '/material_assessment/new_field', 'value': 'unexpected'}],
    [{'path': ROLE_PATH, 'value': 'existing_script'}, {'path': ROLE_PATH, 'value': 'product_facts'}],
    [{'path': 'material_assessment/content_groups/0/role', 'value': 'existing_script'}],
])
async def test_patch_requires_existing_unique_json_pointer_and_never_mutates_draft(env, patches):
    engine, _ = env
    _, source, scope = await correction_branch(engine)
    ref = rejected_draft(engine, scope, rich_report(source))
    original = engine.store.get('report_retry_draft', ref['id'])
    with pytest.raises(ContractError) as caught:
        correct_report(engine, scope, ref, patches)
    assert caught.value.code == 'MATERIAL_ASSESSMENT_INVALID'
    assert engine.store.get('report_retry_draft', ref['id']) == original
    assert not engine.store.get('branch', scope.branch_task_id).get('report')


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_part,expected_code', [
    ('missing_evidence', 'MATERIAL_EVIDENCE_MISSING'), ('missing_locator', 'MATERIAL_SECTION_NOT_FOUND')])
async def test_role_correction_reenters_full_evidence_and_locator_validation(env, invalid_part, expected_code):
    engine, _ = env
    _, source, scope = await correction_branch(engine)
    bad = rich_report(source)
    if invalid_part == 'missing_evidence':
        bad['evidence'] = []
    else:
        bad['evidence'][0]['locator'] = 'text:9999'
    ref = rejected_draft(engine, scope, bad)
    with pytest.raises(ContractError) as caught:
        correct_report(engine, scope, ref, [{'path': ROLE_PATH, 'value': 'existing_script'}])
    assert caught.value.code == expected_code
    assert caught.value.details['report_correction']['formal_report_saved'] is False
    assert engine.store.get('branch', scope.branch_task_id)['status'] == 'RUNNING'
    assert not engine.store.get('branch', scope.branch_task_id).get('report')


@pytest.mark.asyncio
async def test_correction_is_worker_only_and_available_in_each_role_policy(env):
    engine, _ = env
    run, source, scope = await correction_branch(engine)
    ref = rejected_draft(engine, scope, rich_report(source))
    with pytest.raises(ContractError) as caught:
        correct_report(engine, RunScope(run['id'], 'main', 'execute'), ref,
            [{'path': ROLE_PATH, 'value': 'existing_script'}])
    assert caught.value.code == 'CHILD_REQUIRED'
    for role in ROLE_TOOLS:
        assert 'correct_analysis_report' in ROLE_TOOLS[role]
        tools = {item.name: item for item in build_tools(engine, role)}
        assert 'correct_analysis_report' in tools
        assert set(tools['correct_analysis_report'].tool_call_schema.model_fields) == {'draft_ref', 'patches', 'action'}
    assert 'correct_analysis_report' not in {item.name for item in build_tools(engine, 'main')}


def test_contract_error_projects_nested_tuple_diagnostics_to_json_without_losing_types():
    details = {'issues': ({'loc': ('content_groups', 0, 'role'), 'msg': 'unsupported role',
        'bounds': (0, 24), 'nested': ({'enabled': False, 'optional': None, 'weight': 1.5},)},)}
    error = ContractError('MATERIAL_ASSESSMENT_INVALID', 'invalid', status=422, details=details)
    value = error.as_error().model_dump(mode='json')
    assert value['details'] == {'issues': [{'loc': ['content_groups', 0, 'role'],
        'msg': 'unsupported role', 'bounds': [0, 24],
        'nested': [{'enabled': False, 'optional': None, 'weight': 1.5}]}]}
    assert isinstance(details['issues'], tuple) and isinstance(details['issues'][0]['loc'], tuple)
    assert json.loads(json.dumps(value)) == value


def test_contract_error_does_not_stringify_arbitrary_objects_into_wire_feedback():
    error = ContractError('INVALID', 'invalid', details={'exception': ValueError('not a wire value')})
    with pytest.raises(TypeError, match='supported JSON contract value'):
        error.as_error()


@pytest.mark.asyncio
async def test_explicit_draft_read_uses_real_worker_tool_then_patch_without_reemitting_report(env):
    engine, _ = env
    run, source, scope = await correction_branch(engine)
    model = CorrectingWorker(responses=[], bad_report=rich_report(source), read_before_patch=True)
    engine.model, engine.saver = model, InMemorySaver()
    graph = engine._build_graph('product_facts')
    token = CURRENT_SCOPE.set(scope)
    try:
        result = await graph.ainvoke({'messages': [HumanMessage('读取前次未提交草案，再只修明确错误')],
            'files': engine._initial_files('product_facts')},
            {'configurable': {'thread_id': 'report-correction-explicit-read'}, 'recursion_limit': 40}, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)
    read = next(item for item in result['messages'] if isinstance(item, ToolMessage)
        and item.tool_call_id == 'read-rejected-report')
    value = json.loads(read.content)
    assert read.status == 'success' and value['formal_report_saved'] is False
    assert value['draft_ref']['kind'] == 'report_retry_draft'
    assert value['report']['material_assessment']['content_groups'][0]['role'] == 'script'
    assert engine.store.get('branch', scope.branch_task_id)['status'] == 'PREPARED'
    assert len(model.seen) == 3 and not engine.store.list('operation')
    # The error gives only an explicit read capability; it does not auto-copy the report.
    feedback = next(item for item in result['messages'] if isinstance(item, ToolMessage)
        and item.tool_call_id == 'invalid-report')
    assert 'report' not in json.loads(feedback.content)['error']['details']['report_correction']


@pytest.mark.asyncio
async def test_explicit_draft_read_is_detached_and_does_not_prepare_or_accept(env):
    engine, _ = env
    _, source, scope = await correction_branch(engine)
    ref = rejected_draft(engine, scope, rich_report(source))
    before = engine.store.get('report_retry_draft', ref['id'])
    value = correct_report(engine, scope, ref, [], action='read')
    assert value['report'] == before['report'] and value['draft_ref'] == ref
    assert value['formal_report_saved'] is False
    value['report']['fields']['name'] = 'caller-modified'
    assert engine.store.get('report_retry_draft', ref['id']) == before
    branch = engine.store.get('branch', scope.branch_task_id)
    assert branch['status'] == 'RUNNING' and not branch.get('report') and not branch.get('accepted_ref')


@pytest.mark.parametrize('action,patches', [('patch', []), ('read', [{'path': ROLE_PATH, 'value': 'existing_script'}])])
def test_read_and_patch_dto_operations_have_disjoint_argument_contracts(action, patches):
    ref = {'kind': 'report_retry_draft', 'id': 'rejected', 'revision': 1, 'content_hash': canonical_hash({})}
    with pytest.raises(ValidationError):
        CorrectReportInput(draft_ref=ref, action=action, patches=patches)
    assert CorrectReportInput(draft_ref=ref, action='read').patches == []


async def visual_report(engine, code):
    run, source, scope = await correction_branch(engine)
    image = BytesIO()
    Image.new('RGB', (4, 4), 'white').save(image, format='PNG')
    visual = engine.materials.accept(run['conversation_id'], 'image.png', 'image/png', image.getvalue())['material_ref']
    engine.store.update('branch', scope.branch_task_id,
        task={**engine.store.get('branch', scope.branch_task_id)['task'], 'source_refs': [source, visual]})
    receipt = engine.materials.record_visual_delivery(visual, run['conversation_id'], locator='image:1',
        run_id=run['id'], branch_task_id='other-owner' if code == 'MATERIAL_VISUAL_REUSE_BASIS_REQUIRED'
        else scope.branch_task_id, method='vision', content_hash=canonical_hash('fake-image-delivery'))
    report = rich_report(source, role='existing_script')
    report['source_refs'] = [source, visual]
    report['evidence'].append({'id': 'visual-evidence', 'source_ref': visual,
        'locator': 'text:1' if code == 'MATERIAL_VISUAL_EVIDENCE_MISMATCH' else 'image:1',
        'quote': '测试视觉来源，不声称程序理解图片'})
    report['material_assessment']['visual_reviews'] = [{'review_id': 'visual-1', 'source_ref': visual,
        'locator': 'image:1', 'product_candidate_ids': ['candidate-1'], 'status': 'reviewed',
        'relevance': 'relevant', 'summary': 'fake worker 显式判断；收据校验不等于语义核实',
        'evidence_ids': ['visual-evidence'], 'reuse_note': '',
        'read_receipt_ids': ['nonexistent-receipt'] if code == 'MATERIAL_VISUAL_READ_NOT_OBSERVED' else [receipt['id']]}]
    patch = {
        'MATERIAL_VISUAL_EVIDENCE_MISMATCH': {'path': '/evidence/1/locator', 'value': 'image:1'},
        'MATERIAL_VISUAL_READ_NOT_OBSERVED': {'path': '/material_assessment/visual_reviews/0/read_receipt_ids', 'value': [receipt['id']]},
        'MATERIAL_VISUAL_REUSE_BASIS_REQUIRED': {'path': '/material_assessment/visual_reviews/0/reuse_note',
            'value': '同一精确不可变来源和 image:1；已明确检查其他分支交付与本商品的关联'},
    }[code]
    return run, scope, report, patch


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['MATERIAL_VISUAL_EVIDENCE_MISMATCH',
    'MATERIAL_VISUAL_READ_NOT_OBSERVED', 'MATERIAL_VISUAL_REUSE_BASIS_REQUIRED'])
async def test_real_visual_422_validation_is_correctable_in_worker_without_loosening_checks(env, code):
    engine, _ = env
    run, scope, report, patch = await visual_report(engine, code)
    model = CorrectingWorker(responses=[], bad_report=report, expected_code=code, patches=[patch])
    engine.model, engine.saver = model, InMemorySaver()
    graph = engine._build_graph('product_facts')
    token = CURRENT_SCOPE.set(scope)
    try:
        result = await graph.ainvoke({'messages': [HumanMessage('按真实视觉交付纠正明确字段')],
            'files': engine._initial_files('product_facts')},
            {'configurable': {'thread_id': 'visual-422-' + code}, 'recursion_limit': 32}, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)
    feedback = next(item for item in result['messages'] if isinstance(item, ToolMessage)
        and item.tool_call_id == 'invalid-report')
    error = json.loads(feedback.content)['error']
    assert error['code'] == code and error['details']['commit_state'] == 'not_committed'
    assert error['details']['report_correction']['formal_report_saved'] is False
    branch = engine.store.get('branch', scope.branch_task_id)
    assert branch['status'] == 'PREPARED' and not branch.get('accepted_ref')
    assert len(model.seen) == 2 and not engine.store.list('operation')
    original = engine.store.get('report_retry_draft', error['details']['report_correction']['draft_ref']['id'])
    assert original['report'] == AnalysisReport.model_validate(report).model_dump(mode='json')


@pytest.mark.asyncio
async def test_omitted_optional_leaf_is_corrected_by_explicit_existing_parent_patch(env):
    engine, _ = env
    _, scope, report, _ = await visual_report(engine, 'MATERIAL_VISUAL_REUSE_BASIS_REQUIRED')
    report['material_assessment']['visual_reviews'][0].pop('reuse_note')
    with pytest.raises(ContractError) as caught:
        engine.submit_report(scope, report)
    assert caught.value.code == 'MATERIAL_VISUAL_REUSE_BASIS_REQUIRED'
    draft_ref = caught.value.details['report_correction']['draft_ref']
    before = engine.store.get('report_retry_draft', draft_ref['id'])
    review = {**deepcopy(report['material_assessment']['visual_reviews'][0]),
        'reuse_note': '同一精确原图及定位，明确复用其他分支的交付并重新判断本商品关联'}
    patches = [{'path': '/material_assessment/visual_reviews/0', 'value': review}]
    result = correct_report(engine, scope, draft_ref, patches)
    expected = AnalysisReport.model_validate(report).model_dump(mode='json')
    expected['material_assessment']['visual_reviews'][0] = review
    branch = engine.store.get('branch', scope.branch_task_id)
    assert result['status'] == branch['status'] == 'PREPARED'
    assert branch['report'] == expected and branch['report_hash'] == canonical_hash(expected)
    assert engine.store.get('report_retry_draft', draft_ref['id']) == before
    assert patches[0]['value'] != report
    assert 'fields' not in patches[0]['value'] and 'material_assessment' not in patches[0]['value']


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['foreign_source_403', 'pending_unknown'])
async def test_report_permission_and_unknown_operation_remain_hard_worker_errors(env, case):
    engine, _ = env
    run, source, scope = await correction_branch(engine)
    report = rich_report(source, role='existing_script' if case == 'foreign_source_403' else 'script')
    if case == 'foreign_source_403':
        other = engine.materials.accept(run['conversation_id'], 'foreign.txt', 'text/plain', b'foreign')['material_ref']
        report['evidence'][0]['source_ref'] = other
        expected = 'MATERIAL_EVIDENCE_SCOPE_DENIED'
    else:
        engine.store.update('run', run['id'], pending_unknown_operation='unsettled-operation')
        expected = 'MATERIAL_ASSESSMENT_INVALID'
    model = BindableFakeModel(responses=[AIMessage(content='', tool_calls=[{'id': 'hard-reject',
        'name': 'submit_product_facts_report', 'args': {'report': report}}])])
    engine.model, engine.saver = model, InMemorySaver()
    graph = engine._build_graph('product_facts')
    token = CURRENT_SCOPE.set(scope)
    try:
        with pytest.raises(ContractError) as caught:
            await graph.ainvoke({'messages': [HumanMessage('不能绕过权限或未知副作用')],
                'files': engine._initial_files('product_facts')},
                {'configurable': {'thread_id': 'hard-report-' + case}, 'recursion_limit': 20}, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)
    assert caught.value.code == expected
    assert not engine.store.get('branch', scope.branch_task_id).get('report')
    assert not engine.store.get('run', run['id']).get('correctable_tool_errors')


@pytest.mark.asyncio
async def test_repeated_identical_rejection_keeps_outer_revision_and_draft_reference_stable(env):
    engine, _ = env
    _, source, scope = await correction_branch(engine)
    report = rich_report(source)
    first_ref = rejected_draft(engine, scope, report)
    first = engine.store.get('report_retry_draft', first_ref['id'])
    for _ in range(3):
        assert rejected_draft(engine, scope, deepcopy(report)) == first_ref
        assert engine.store.get('report_retry_draft', first_ref['id']) == first
    assert first['revision'] == 1 and first_ref['revision'] == 1
    assert len(engine.store.list('report_retry_draft')) == 1


@pytest.mark.asyncio
async def test_worker_context_keeps_only_last_five_owned_draft_refs_without_report_bodies(env):
    engine, _ = env
    run, source, scope = await correction_branch(engine)
    own_refs = []
    for index in range(7):
        report = rich_report(source)
        report['summary'] = f'本分支未提交候选 {index}'
        own_refs.append(rejected_draft(engine, scope, report))
    branch = engine.store.get('branch', scope.branch_task_id)
    engine.store.put('branch', {**branch, 'id': 'other-context-owner'})
    other_scope = RunScope(run['id'], 'other-context-invocation', 'execute', 'product_facts', 'other-context-owner')
    foreign = rich_report(source)
    foreign['summary'] = '其他分支不得被此工作集读取'
    other_ref = rejected_draft(engine, other_scope, foreign)
    value = (await engine.project_context(scope))['uncommitted_report_drafts']
    assert len(value) == 5 and [item['draft_ref'] for item in value] == own_refs[-5:]
    assert other_ref not in [item['draft_ref'] for item in value]
    assert all(item['read_tool'] == 'correct_analysis_report' and item['read_action'] == 'read'
        and item['formal_report_saved'] is False and item['rejected_code'] == 'MATERIAL_ASSESSMENT_INVALID'
        and 'report' not in item for item in value)
    other_value = (await engine.project_context(other_scope))['uncommitted_report_drafts']
    assert [item['draft_ref'] for item in other_value] == [other_ref]
    assert 'uncommitted_report_drafts' not in await engine.project_context(RunScope(run['id'], 'main-context', 'execute'))
