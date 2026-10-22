"""Material role vocabulary and immutable manifest/read-projection contracts."""
from copy import deepcopy
from io import BytesIO

from docx import Document
from pydantic import ValidationError
import pytest

from shengbo_contracts.common import canonical_hash
from shengbo_prelive.material_roles import CONTENT_ROLES
from shengbo_prelive.material_understanding import ContentGroup
from shengbo_prelive.material_views import source_view
from shengbo_prelive.materials import MaterialStore
from shengbo_prelive.store import RuntimeStore


@pytest.fixture
def material_store(tmp_path):
    return MaterialStore(tmp_path / 'materials', RuntimeStore(tmp_path / 'runtime.sqlite3'))


def docx_bytes():
    document = Document()
    document.add_paragraph('商品资料：测试商品，129元。')
    document.add_paragraph('现成话术：欢迎了解这款商品。')
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def assert_original_unchanged(material_store, ref, original):
    exported = material_store.export_original_manifest(ref, 'conversation-roles')['manifest']
    assert exported == original
    assert canonical_hash(exported) == ref['content_hash']
    assert material_store.store.get('material', ref['id'])['artifact_ref'] == ref


@pytest.mark.parametrize('role', CONTENT_ROLES)
def test_every_published_role_is_accepted_by_content_group_dto(role):
    group = ContentGroup(group_id='group-1', role=role, language='zh-CN', script_variant_label='原稿')
    assert group.role == role
    assert ContentGroup.model_json_schema()['properties']['role']['enum'] == list(CONTENT_ROLES)


@pytest.mark.parametrize('role', ['script', 'operation_instruction', 'product', 'unclassified'])
def test_unpublished_roles_remain_explicit_validation_errors(role):
    with pytest.raises(ValidationError) as caught:
        ContentGroup(group_id='group-1', role=role)
    assert caught.value.errors(include_input=False)[0]['loc'] == ('role',)


def test_new_office_manifest_and_readers_publish_the_same_role_vocabulary(material_store):
    ref = material_store.accept('conversation-roles', 'roles.docx',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document', docx_bytes())['material_ref']
    original = material_store.export_original_manifest(ref, 'conversation-roles')['manifest']
    assert original['content_hints']['allowed_roles'] == list(CONTENT_ROLES)
    assert original['content_hints']['requires_content_classification'] is True
    assert original['content_hints']['product_identity'] == 'unresolved'
    for section in [None, 'full', 'overview', original['locators'][0]['id']]:
        read = material_store.read(ref, 'conversation-roles', section)
        assert read['material_ref'] == ref
        assert read['manifest']['content_hints']['allowed_roles'] == list(CONTENT_ROLES)
        assert not read['manifest'].get('reader_contract_is_projection')
        assert read['manifest']['coverage']['business_understanding_complete'] is False
    assert_original_unchanged(material_store, ref, original)


def test_legacy_role_hints_are_projected_without_rehashing_or_mutating_original(material_store, monkeypatch):
    # Accept through the real upload/store API, with a historical parser result.
    # The old allowed_roles list genuinely participates in the original hash.
    legacy = {'kind': 'document', 'mime': 'text/plain', 'extractor': 'historical-reader',
        'text': '历史原稿正文', 'locators': [{'id': 'text:1', 'kind': 'text', 'text': '历史原稿正文'}],
        'pages': [], 'unknowns': [], 'content_hints': {'requires_content_classification': True,
            'allowed_roles': ['product_facts', 'script', 'operation_instruction', 'unknown'],
            'product_identity': 'unresolved'}}
    monkeypatch.setattr(material_store, '_extract', lambda *args: deepcopy(legacy))
    ref = material_store.accept('conversation-roles', 'legacy.txt', 'text/plain', '历史原稿正文'.encode())['material_ref']
    original = material_store.export_original_manifest(ref, 'conversation-roles')['manifest']
    assert original['content_hints']['allowed_roles'] != list(CONTENT_ROLES)
    for section in [None, 'full', 'overview', 'text:1']:
        read = material_store.read(ref, 'conversation-roles', section)
        assert read['material_ref'] == ref
        assert read['manifest']['content_hints']['allowed_roles'] == list(CONTENT_ROLES)
        if section != 'overview':
            assert read['manifest']['reader_contract_is_projection'] is True
        projected = source_view(read)
        assert projected['manifest_is_projection'] is True
        assert projected['manifest']['content_hints']['allowed_roles'] == list(CONTENT_ROLES)
        # Both returned objects can be changed by a caller without touching authority.
        projected['manifest']['content_hints']['allowed_roles'].append('caller-only')
        read['manifest']['content_hints']['product_identity'] = 'caller-only'
        assert_original_unchanged(material_store, ref, original)


def test_plain_text_without_historical_role_hints_keeps_original_contract_and_hash(material_store):
    ref = material_store.accept('conversation-roles', 'plain.txt', 'text/plain', '商品及原稿待模型分类'.encode())['material_ref']
    original = material_store.export_original_manifest(ref, 'conversation-roles')['manifest']
    for section in [None, 'overview']:
        read = material_store.read(ref, 'conversation-roles', section)
        assert read['manifest']['content_hints']['requires_content_classification'] is True
        assert read['manifest']['content_hints']['product_identity'] == 'unresolved'
        assert read['material_ref'] == ref
    assert_original_unchanged(material_store, ref, original)
