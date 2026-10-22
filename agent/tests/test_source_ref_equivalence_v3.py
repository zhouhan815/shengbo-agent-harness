"""Exact source refs across optional schema defaults; preserve raw reports/hashes."""
from copy import deepcopy
from itertools import product

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
import pytest

from shengbo_contracts import common
from shengbo_contracts.business import AnalysisReport
from shengbo_contracts.common import ArtifactRef, ContractError, SCHEMA_VERSION, canonical_hash
from shengbo_prelive.runtime import CURRENT_SCOPE
from test_report_corrections_v3 import correction_branch, rich_report, visual_report
from test_runtime_business import BindableFakeModel, env


def wire(ref, schema):
    value = deepcopy(ref)
    if schema:
        value['schema_version'] = SCHEMA_VERSION
    else:
        value.pop('schema_version', None)
    return value


def changed_ref(ref, change):
    value = deepcopy(ref)
    alterations = {'kind': ('kind', 'another_source_kind'), 'id': ('id', 'foreign-source'),
        'revision': ('revision', ref['revision'] + 1), 'hash': ('content_hash', canonical_hash('other-source')),
        'schema': ('schema_version', 'WRONG-SCHEMA'), 'extra': ('extra_untrusted_field', 'ignored-is-not-allowed'),
        'bool_revision': ('revision', True), 'string_revision': ('revision', '1'), 'float_revision': ('revision', 1.0)}
    key, value_ = alterations[change]
    value[key] = value_
    return value


def assert_original_manifest(engine, run, ref, original):
    assert engine.materials.export_original_manifest(ref, run['conversation_id'])['manifest'] == original
    assert canonical_hash(original) == ref['content_hash']


@pytest.mark.parametrize('left_schema,right_schema', tuple(product([False, True], repeat=2)))
def test_same_artifact_ref_normalizes_only_the_legal_optional_default(left_schema, right_schema):
    ref = ArtifactRef(kind='source_manifest', id='material-one', revision=1,
        content_hash=canonical_hash('source')).model_dump(mode='json')
    left, right = wire(ref, left_schema), wire(ref, right_schema)
    originals = deepcopy((left, right))
    assert common.same_artifact_ref(left, right)
    assert common.same_artifact_ref(right, left)
    assert (left, right) == originals


@pytest.mark.parametrize('change', ['kind', 'id', 'revision', 'hash', 'schema', 'extra',
    'bool_revision', 'string_revision', 'float_revision'])
def test_same_artifact_ref_rejects_changed_identity_or_non_strict_wire_values(change):
    ref = ArtifactRef(kind='source_manifest', id='material-one', revision=1,
        content_hash=canonical_hash('source')).model_dump(mode='json')
    invalid = changed_ref(ref, change)
    assert common.same_artifact_ref(invalid, ref) is False
    assert common.same_artifact_ref(ref, invalid) is False
    if change in {'schema', 'extra', 'bool_revision', 'string_revision', 'float_revision'}:
        assert common.same_artifact_ref(invalid, deepcopy(invalid)) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence_schema,outer_schema', tuple(product([False, True], repeat=2)))
async def test_real_report_accepts_four_and_five_field_refs_without_rewriting_raw_evidence(env, evidence_schema, outer_schema):
    engine, _ = env
    run, source, scope = await correction_branch(engine)
    original = engine.materials.export_original_manifest(source, run['conversation_id'])['manifest']
    report = rich_report(source, role='existing_script')
    report['evidence'][0]['source_ref'] = wire(source, evidence_schema)
    report['source_refs'] = [wire(source, outer_schema)]
    report_before = deepcopy(report)
    expected = AnalysisReport.model_validate(report).model_dump(mode='json')
    expected['source_refs'] = [source]
    result = engine.submit_report(scope, report)
    saved = engine.store.get('branch', scope.branch_task_id)
    assert result['status'] == saved['status'] == 'PREPARED'
    assert saved['report'] == expected
    assert saved['report']['evidence'][0]['source_ref'] == report_before['evidence'][0]['source_ref']
    assert saved['report_hash'] == result['report_hash'] == canonical_hash(expected)
    assert report == report_before
    assert_original_manifest(engine, run, source, original)
    assert not saved.get('accepted_ref') and not engine.store.list('operation')


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed_schema,review_schema,evidence_schema,receipt_schema', tuple(product([False, True], repeat=4)))
async def test_visual_exact_source_receipt_and_evidence_accept_both_wire_shapes(env,
        allowed_schema, review_schema, evidence_schema, receipt_schema):
    engine, _ = env
    run, scope, report, patch = await visual_report(engine, 'MATERIAL_VISUAL_READ_NOT_OBSERVED')
    review = report['material_assessment']['visual_reviews'][0]
    source = deepcopy(review['source_ref'])
    original = engine.materials.export_original_manifest(source, run['conversation_id'])['manifest']
    review['read_receipt_ids'] = patch['value']
    review['source_ref'] = wire(source, review_schema)
    report['evidence'][1]['source_ref'] = wire(source, evidence_schema)
    branch = engine.store.get('branch', scope.branch_task_id)
    allowed = [wire(ref, allowed_schema) for ref in branch['task']['source_refs']]
    engine.store.update('branch', scope.branch_task_id, task={**branch['task'], 'source_refs': allowed})
    receipt_id = review['read_receipt_ids'][0]
    receipt = engine.store.get('material_visual_read', receipt_id)
    # Historical receipt wire shape is fixture state; its source identity and
    # actual prior delivery/run/locator are unchanged. No authority is invented.
    receipt = engine.store.update('material_visual_read', receipt_id, source_ref=wire(source, receipt_schema))
    before = deepcopy(report)
    expected = AnalysisReport.model_validate(report).model_dump(mode='json')
    expected['source_refs'] = allowed
    result = engine.submit_report(scope, report)
    saved = engine.store.get('branch', scope.branch_task_id)
    assert result['status'] == 'PREPARED'
    assert saved['report'] == expected and saved['report_hash'] == canonical_hash(expected)
    assert saved['report']['evidence'][1]['source_ref'] == before['evidence'][1]['source_ref']
    assert saved['report']['material_assessment']['visual_reviews'][0]['source_ref'] == before['material_assessment']['visual_reviews'][0]['source_ref']
    assert engine.store.get('material_visual_read', receipt_id) == receipt
    assert report == before
    assert_original_manifest(engine, run, source, original)


@pytest.mark.asyncio
@pytest.mark.parametrize('location', ['evidence', 'outer_source_refs'])
@pytest.mark.parametrize('change', ['kind', 'id', 'revision', 'hash', 'schema', 'extra', 'bool_revision'])
async def test_real_report_ref_compatibility_does_not_accept_forged_or_malformed_refs(env, location, change):
    engine, _ = env
    run, source, scope = await correction_branch(engine)
    original = engine.materials.export_original_manifest(source, run['conversation_id'])['manifest']
    report = rich_report(source, role='existing_script')
    invalid = changed_ref(wire(source, False), change)
    if location == 'evidence':
        report['evidence'][0]['source_ref'] = invalid
    else:
        report['source_refs'] = [invalid]
    with pytest.raises((ContractError, ValidationError)):
        engine.submit_report(scope, report)
    assert not engine.store.get('branch', scope.branch_task_id).get('report')
    assert_original_manifest(engine, run, source, original)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['kind', 'id', 'revision', 'hash', 'schema', 'extra', 'bool_revision'])
async def test_visual_receipt_ref_default_compatibility_does_not_accept_mutated_receipt_source(env, change):
    engine, _ = env
    run, scope, report, patch = await visual_report(engine, 'MATERIAL_VISUAL_READ_NOT_OBSERVED')
    review = report['material_assessment']['visual_reviews'][0]
    source = deepcopy(review['source_ref'])
    original = engine.materials.export_original_manifest(source, run['conversation_id'])['manifest']
    review['read_receipt_ids'] = patch['value']
    engine.store.update('material_visual_read', patch['value'][0], source_ref=changed_ref(source, change))
    with pytest.raises(ContractError) as caught:
        engine.submit_report(scope, report)
    assert caught.value.code == 'MATERIAL_VISUAL_READ_NOT_OBSERVED'
    assert not engine.store.get('branch', scope.branch_task_id).get('report')
    assert_original_manifest(engine, run, source, original)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['schema', 'extra', 'bool_revision'])
async def test_visual_review_ref_is_checked_before_dto_defaults_can_erase_invalid_input(env, change):
    engine, _ = env
    run, scope, report, patch = await visual_report(engine, 'MATERIAL_VISUAL_READ_NOT_OBSERVED')
    review = report['material_assessment']['visual_reviews'][0]
    source = deepcopy(review['source_ref'])
    original = engine.materials.export_original_manifest(source, run['conversation_id'])['manifest']
    review['read_receipt_ids'] = patch['value']
    review['source_ref'] = changed_ref(source, change)
    with pytest.raises(ContractError):
        engine.submit_report(scope, report)
    assert not engine.store.get('branch', scope.branch_task_id).get('report')
    assert_original_manifest(engine, run, source, original)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['foreign_source_403', 'pending_unknown'])
async def test_source_default_equivalence_preserves_real_worker_foreign_and_unknown_hard_boundaries(env, case):
    engine, _ = env
    run, source, scope = await correction_branch(engine)
    report = rich_report(source, role='existing_script' if case == 'foreign_source_403' else 'script')
    report['evidence'][0]['source_ref'] = wire(source, False)
    if case == 'foreign_source_403':
        foreign = engine.materials.accept(run['conversation_id'], 'not-assigned.txt', 'text/plain', b'foreign')['material_ref']
        report['evidence'][0]['source_ref'] = wire(foreign, False)
        expected_code = 'MATERIAL_EVIDENCE_SCOPE_DENIED'
    else:
        engine.store.update('run', run['id'], pending_unknown_operation='unsettled-operation')
        expected_code = 'MATERIAL_ASSESSMENT_INVALID'
    engine.model = BindableFakeModel(responses=[AIMessage(content='', tool_calls=[{
        'id': 'source-ref-hard-boundary', 'name': 'submit_product_facts_report', 'args': {'report': report}}])])
    engine.saver = InMemorySaver()
    graph = engine._build_graph('product_facts')
    token = CURRENT_SCOPE.set(scope)
    try:
        with pytest.raises(ContractError) as caught:
            await graph.ainvoke({'messages': [HumanMessage('默认schema兼容不能放松来源或未知状态边界')],
                'files': engine._initial_files('product_facts')},
                {'configurable': {'thread_id': 'source-ref-hard-' + case}, 'recursion_limit': 20}, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)
    assert caught.value.code == expected_code
    if case == 'foreign_source_403':
        assert caught.value.status == 403
    assert not engine.store.get('branch', scope.branch_task_id).get('report')
    assert not engine.store.get('run', run['id']).get('correctable_tool_errors')
