"""Controlled visual reads over immutable sources; delivery is not understanding."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time
from weakref import WeakValueDictionary

from shengbo_contracts.common import ContractError, canonical_hash, now_iso, same_artifact_ref
from uuid import uuid4

OCR_CACHE_SCHEMA = 'ocr-computation-v2'
_OCR_CACHE_LOCKS = WeakValueDictionary()
_OCR_CACHE_LOCKS_GUARD = threading.Lock()


def _ocr_cache_lock(identity):
    # Tool calls can run concurrently in the server's thread pool. Keep a
    # strong reference while waiting/computing, without retaining old keys.
    with _OCR_CACHE_LOCKS_GUARD:
        lock = _OCR_CACHE_LOCKS.get(identity)
        if lock is None:
            lock = threading.Lock()
            _OCR_CACHE_LOCKS[identity] = lock
        return lock


def _fail(code, message):
    raise ContractError(code, message, status=422)


def visual_items(row):
    manifest = row['manifest']
    if row['mime'].startswith('image/'):
        return [{'id': 'image:1', 'kind': 'image', 'mime': row['mime'],
                 'sha256': manifest['original_sha256'], 'visual_status': 'pending_visual_read',
                 'anchors': [], 'source': 'original_image'}]
    if row['mime'] == 'application/pdf' and manifest.get('extractor') == 'pypdf':
        # Existing immutable uploads predate visual coverage. Conservatively
        # expose their real page locators; do not rewrite source hashes/history.
        return [{'id': f"page:{page['number']}", 'kind': 'page', 'page': page['number'],
                 'mime': 'image/png', 'source': 'pdf_page_render', 'render_strategy': 'lazy_page',
                 'visual_status': 'pending_visual_read', 'anchors': [],
                 'visual_reasons': ['legacy_pdf_visual_coverage_unknown']}
                for page in manifest.get('pages', [])]
    return [*manifest.get('embedded_images', []), *manifest.get('unsupported_visual_regions', [])]


def list_visual_receipts(materials, ref, conversation_id):
    row, _ = materials._record(ref, conversation_id)
    return [value for value in materials.store.list('material_visual_read')
            if value.get('conversation_id') == conversation_id and same_artifact_ref(value.get('source_ref'), row['artifact_ref'])]


def record_visual_delivery(materials, ref, conversation_id, *, locator, run_id,
                           branch_task_id=None, method='vision', content_hash=None):
    row, _ = materials._record(ref, conversation_id)
    materials.validate_locators(ref, conversation_id, [locator])
    image_ids = {item['id'] for item in visual_items(row)
                 if item.get('kind') in {'image', 'page'} and item.get('visual_status') == 'pending_visual_read'}
    page_ids = {f"page:{p['number']}" for p in row['manifest'].get('pages', [])}
    if locator not in image_ids | page_ids or method not in {'vision', 'ocr'}:
        _fail('MATERIAL_VISUAL_LOCATOR_INVALID', '视觉回执必须对应真实图像或PDF页')
    identity = 'visual_read_' + canonical_hash([row['artifact_ref'], locator, run_id, branch_task_id,
                                                method, content_hash])[7:]
    previous = materials.store.get('material_visual_read', identity, required=False)
    value = {'id': identity, 'conversation_id': conversation_id, 'source_ref': row['artifact_ref'],
             'locator': locator, 'run_id': run_id, 'branch_task_id': branch_task_id, 'method': method,
             'status': 'delivered_to_tool_response' if method == 'vision' else 'ocr_text_returned',
             'content_hash': content_hash, 'created_at': previous['created_at'] if previous else now_iso(),
             'last_delivered_at': now_iso(), 'delivery_count': (previous or {}).get('delivery_count', 0) + 1,
             'semantic_support_verified': False, 'delivery_is_understanding': False}
    return materials.store.put('material_visual_read', value)


def workset(materials, ref, conversation_id, *, page=1, page_size=24):
    row, _ = materials._record(ref, conversation_id)
    items = visual_items(row)
    if type(page) is not int or page < 1 or page > max(1, (len(items) + page_size - 1) // page_size):
        _fail('MATERIAL_SECTION_NOT_FOUND', '视觉工作集页码无效')
    receipts = list_visual_receipts(materials, ref, conversation_id)
    rows = []
    for image in items[(page - 1)*page_size:page*page_size]:
        locator = image['id']
        matching = [r for r in receipts if r['locator'] == locator]
        page_locator = f"page:{image['page']}" if image.get('page') else None
        page_matching = [r for r in receipts if page_locator and r['locator'] == page_locator]
        render_available = image.get('visual_status') == 'pending_visual_read' and image.get('kind') in {'image', 'page'}
        rows.append({**{key: image[key] for key in ('id', 'kind', 'page', 'width', 'height', 'anchors',
                       'visual_status', 'orphaned', 'mime', 'source', 'object_type', 'reason', 'remediation',
                       'sheet_id', 'drawing_part', 'from_zero_based', 'to_zero_based') if key in image},
                     'locator': locator, 'read_args': {'ref': row['artifact_ref'], 'section': locator},
                     'render_available': render_available,
                     'ocr_read_args': {'ref': row['artifact_ref'], 'section': 'ocr:' + locator} if render_available else None,
                     'delivery_receipt_ids': [r['id'] for r in matching],
                     'delivery_observed': bool(matching), 'understanding_status': 'unknown',
                     'review_locator_choices': [{'locator': locator, 'read_receipt_ids': [r['id'] for r in matching]}]
                         + ([{'locator': page_locator, 'read_receipt_ids': [r['id'] for r in page_matching]}] if page_locator else []),
                     'criticality': 'not_yet_assessed_by_agent'})
    return {'source_ref': row['artifact_ref'], 'total': len(items), 'page': page, 'page_size': page_size,
            'items': rows, 'next_section': f'visual_workset:{page+1}' if page*page_size < len(items) else None,
            'reported_delivery_count': len(receipts), 'semantic_support_verified': False,
            'complete_source_coverage_verified': False,
            'note': '索引、OCR与交付回执不等于已理解；未报告图像保持unknown，关键条件必须核对原图。'}


def visual_payload(materials, ref, conversation_id, locator):
    started = time.perf_counter()
    row, path = materials._record(ref, conversation_id)
    if row['mime'].startswith('image/') and locator == 'image:1':
        return row['mime'], materials.blob_bytes(ref, conversation_id), {'cache_hit': False}
    if locator.startswith('page:') and (not locator[5:].isdecimal() or int(locator[5:]) < 1):
        _fail('MATERIAL_SECTION_NOT_FOUND', 'PDF页定位无效')
    image = next((i for i in visual_items(row) if i['id'] == locator or
                  (locator.startswith('page:') and i.get('page') == int(locator[5:]))), None)
    if image is None and row['mime'] == 'application/pdf' and locator.startswith('page:'):
        page_no = int(locator[5:])
        if any(p['number'] == page_no for p in row['manifest'].get('pages', [])):
            image = {'id': f'image:{page_no}', 'page': page_no, 'source': 'pdf_page_render',
                     'visual_status': 'pending_visual_read'}
    if image is None:
        _fail('MATERIAL_IMAGE_NOT_FOUND', '未找到原资料中的图像或待视觉页面')
    if image.get('visual_status') != 'pending_visual_read':
        _fail('MATERIAL_IMAGE_NOT_RENDERABLE', '图片格式或大小不支持直接视觉读取，请提供清晰截图')
    if image.get('source') == 'pdf_page_render':
        from .material_pdf_reader import render_pdf_page
        parser = row['manifest'].get('extractor', 'pdf')
        identity = 'visual_cache_' + canonical_hash([row['artifact_ref'], image['id'], parser, 'page-png-v1'])[7:]
        cached = materials.store.get('material_visual_cache', identity, required=False)
        directory = materials.root / (row['id'] + '_visual')
        if cached:
            cache_path = Path(cached['path']).resolve()
            if cache_path.parent != directory.resolve() or not cache_path.is_file():
                _fail('MATERIAL_VISUAL_CACHE_INVALID', '页面图缓存不可用，请重新提取资料')
            data = cache_path.read_bytes()
            if 'sha256:' + hashlib.sha256(data).hexdigest() != cached['sha256']:
                _fail('MATERIAL_BLOB_INTEGRITY_FAILED', '页面图完整性校验失败')
            return 'image/png', data, {'cache_hit': True, 'duration_ms': round((time.perf_counter()-started)*1000,3)}
        data = render_pdf_page(materials.blob_bytes(ref, conversation_id), image['page'])
        directory.mkdir(exist_ok=True)
        cache_path = directory / (image['id'].replace(':', '_') + '.png')
        temporary = directory / (cache_path.name + '.' + uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as handle:
                handle.write(data)
            os.replace(temporary, cache_path)
        finally:
            temporary.unlink(missing_ok=True)
        materials.store.put('material_visual_cache', {'id': identity, 'conversation_id': conversation_id,
            'source_ref': row['artifact_ref'], 'locator': image['id'], 'path': str(cache_path),
            'sha256': 'sha256:' + hashlib.sha256(data).hexdigest(), 'created_at': now_iso()})
        return 'image/png', data, {'cache_hit': False, 'duration_ms': round((time.perf_counter()-started)*1000,3)}
    if image.get('derived_from_conversion'):
        derived = materials.root / (row['id'] + '_images') / (image['id'].replace(':', '_') + '.bin')
        if not derived.is_file():
            _fail('MATERIAL_EMBEDDED_IMAGE_MISSING', '转换后的原图缺失')
        data = derived.read_bytes()
    else:
        from .material_parsers import embedded_image_bytes
        data = embedded_image_bytes(materials.blob_bytes(ref, conversation_id), path.suffix.lower(), image)
    if 'sha256:' + hashlib.sha256(data).hexdigest() != image['sha256']:
        _fail('MATERIAL_BLOB_INTEGRITY_FAILED', '原图完整性校验失败')
    return image['mime'], data, {'cache_hit': False, 'duration_ms': round((time.perf_counter()-started)*1000,3)}


def _ocr_target(row, locator):
    if row['mime'] == 'application/pdf':
        image = next((item for item in visual_items(row) if item['id'] == locator), None)
        number = int(locator[5:]) if locator.startswith('page:') and locator[5:].isdecimal() else image.get('page') if image else None
        if not number or not any(page['number'] == number for page in row['manifest'].get('pages', [])):
            _fail('MATERIAL_VISUAL_LOCATOR_INVALID', 'OCR须指定PDF真实页或真实页图')
        return {'kind': 'pdf_page', 'page': number}
    image = next((item for item in visual_items(row) if item['id'] == locator), None)
    if not locator.startswith('image:') or image is None or image.get('visual_status') != 'pending_visual_read':
        _fail('MATERIAL_VISUAL_LOCATOR_INVALID', 'OCR须指定可读取图像，未渲染对象请提供清晰截图或PDF')
    return {'kind': 'source_image', 'locator': locator}


def _ocr_fingerprint(capabilities):
    """Separate computation identity from presentation paths and locator aliases."""
    explicit = capabilities.get('ocr_model_fingerprint')
    models = []
    backend = capabilities.get('ocr_backend')
    details = (capabilities.get('ocr_backend_details') or {}).get('rapidocr') or {}
    if explicit is None and backend == 'rapidocr':
        for item in details.get('models', []):
            path = Path(item['path'])
            models.append({'name': item['name'], 'bytes': path.stat().st_size if path.is_file() else 0,
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'missing'})
    elif explicit is None and backend == 'tesseract' and capabilities.get('tessdata_path'):
        directory = Path(capabilities['tessdata_path'])
        for language in capabilities.get('ocr_language', '').split('+'):
            if not language or not language.replace('_', '').isalnum():
                _fail('MATERIAL_OCR_LANGUAGE_NOT_READY', '本地OCR语言标识无效')
            path = directory / (language + '.traineddata')
            models.append({'name': path.name, 'bytes': path.stat().st_size if path.is_file() else 0,
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'missing'})
    return canonical_hash({'cache_schema': OCR_CACHE_SCHEMA, 'reader_engine': capabilities.get('engine'),
        'backend': backend, 'language': capabilities.get('ocr_language'),
        'liteparse_version': capabilities.get('liteparse_version'),
        'rapidocr_version': details.get('rapidocr_version'), 'onnxruntime_version': details.get('onnxruntime_version'),
        'render_limits': {key: (capabilities.get('limits') or {}).get(key) for key in
                          ('render_pixels', 'render_dimension', 'ocr_image_pixels', 'ocr_image_dimension')},
        'models': models, 'explicit_model_fingerprint': explicit,
        'other_backend_capabilities': capabilities if not backend and explicit is None else None})


def _verified_ocr_cache(record, row, conversation_id):
    if (record.get('source_ref') != row['artifact_ref'] or record.get('conversation_id') != conversation_id
            or canonical_hash(record.get('result')) != record.get('result_hash')):
        _fail('MATERIAL_BLOB_INTEGRITY_FAILED', 'OCR结果来源或完整性校验失败')
    return record['result']


def ocr_read(materials, ref, conversation_id, locator):
    """Explicit, local, cached OCR. Original source manifest/hash never changes."""
    started = time.perf_counter()
    row, _ = materials._record(ref, conversation_id)
    materials.validate_locators(ref, conversation_id, [locator])
    from .material_pdf_reader import ocr_pdf_page, ocr_image, pdf_reader_capabilities
    capabilities = pdf_reader_capabilities()
    target = _ocr_target(row, locator)
    fingerprint_started = time.perf_counter()
    fingerprint = _ocr_fingerprint(capabilities)
    fingerprint_ms = round((time.perf_counter() - fingerprint_started) * 1000, 3)
    identity = 'material_ocr_' + canonical_hash([OCR_CACHE_SCHEMA, row['artifact_ref'], target, fingerprint])[7:]
    with _ocr_cache_lock(identity):
        previous = materials.store.get('material_ocr_result', identity, required=False)
        legacy_cache_ignored = False
        if previous is None:
            # A v1 entry does not attest the actual model/reader fingerprint. Do
            # not promote it into v2. Legacy rows remain untouched; one fresh
            # v2 compute is then shared by all valid aliases.
            old_state = capabilities.get('ocr_model_fingerprint', capabilities.get('ocr_language'))
            legacy_id = 'material_ocr_' + canonical_hash([row['artifact_ref'], locator, capabilities, old_state])[7:]
            legacy_cache_ignored = materials.store.get('material_ocr_result', legacy_id, required=False) is not None
        if previous:
            if previous.get('computation_target') != target or previous.get('model_reader_fingerprint') != fingerprint:
                _fail('MATERIAL_BLOB_INTEGRITY_FAILED', 'OCR计算缓存身份不一致')
            result = _verified_ocr_cache(previous, row, conversation_id)
        else:
            if target['kind'] == 'pdf_page':
                result = ocr_pdf_page(materials.blob_bytes(ref, conversation_id), target['page'])
            else:
                _, data, _ = visual_payload(materials, ref, conversation_id, locator)
                result = ocr_image(data)
            materials.store.put('material_ocr_result', {'id': identity, 'conversation_id': conversation_id,
                'source_ref': row['artifact_ref'], 'locator': locator, 'result': result,
                'cache_schema': OCR_CACHE_SCHEMA, 'computation_target': target, 'model_reader_fingerprint': fingerprint,
                'result_hash': canonical_hash(result), 'created_at': now_iso()})
    result = json.loads(json.dumps(result, ensure_ascii=False))
    result['source_locator'] = locator
    source_image = next((item for item in visual_items(row) if item['id'] == locator), None)
    source_page = int(locator[5:]) if row['mime'] == 'application/pdf' and locator.startswith('page:') else (
        source_image.get('page') if row['mime'] == 'application/pdf' and source_image else None)
    result['image_locator'] = f'image:{source_page}' if source_page else locator
    if source_page:
        result['page'] = source_page
    else:
        result.pop('page', None)  # A one-page OCR scratch PDF is not a Word/Excel source page.
    for index, child in enumerate(result.get('locators', []), 1):
        child['engine_local_locator'] = child.get('id')
        child['id'] = f'{locator}:ocr:{index}'
        child['source_locator'] = locator
        child['image_locator'] = result['image_locator']
        if source_page:
            child['page'] = source_page
        else:
            child.pop('page', None)
    result['source_ref'] = row['artifact_ref']
    result['cache_hit'] = previous is not None
    result['cache_schema'] = OCR_CACHE_SCHEMA
    result['computation_target'] = target
    result['model_reader_fingerprint'] = fingerprint
    result['cache_fingerprint_ms'] = fingerprint_ms
    result['legacy_cache_ignored'] = legacy_cache_ignored
    result['read_duration_ms'] = round((time.perf_counter()-started)*1000,3)
    result['semantic_support_verified'] = False
    result['requires_visual_confirmation_for_critical_facts'] = True
    result['note'] = 'OCR只作读取辅助，可能漏字、错数字或否定条件；关键金额、规格、活动限制须结合原图核对，不能凭置信分视为正确。'
    return {'material_ref': row['artifact_ref'], 'name': row['name'], 'text': result.get('text',''),
            'view': 'ocr', 'ocr_result': result,
            'manifest': {'kind': row['manifest']['kind'], 'extractor': result.get('extractor','local_ocr'),
                         'coverage': {'processed_text_complete': False, 'pending_visual': True,
                                      'business_understanding_complete': False},
                         'locators': [{'id': locator, 'kind': 'image' if locator.startswith('image:') else 'page',
                                       'text': result.get('text','')}], 'text': result.get('text',''),
                         'parse_metrics': result.get('parse_metrics',{})}}
