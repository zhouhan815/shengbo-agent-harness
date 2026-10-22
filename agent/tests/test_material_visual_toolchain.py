"""SDK -> controlled source -> cached OCR/image -> honest coverage, no model."""
from io import BytesIO
from pathlib import Path
import hashlib
import json

from PIL import Image, ImageDraw, ImageFont
import pytest

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.materials import MaterialStore
from shengbo_prelive.store import RuntimeStore
from shengbo_prelive.material_pdf_reader import pdf_reader_capabilities
from test_material_pdf_sdk_v1 import make_pdf


@pytest.fixture
def materials(tmp_path):
    return MaterialStore(tmp_path, RuntimeStore(tmp_path/'agent.sqlite3'))


def price_image():
    image=Image.new('RGB',(1200,500),'white')
    font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',58)
    draw=ImageDraw.Draw(image)
    for n,line in enumerate(['活动价29元','不包邮','不含赠品']):
        draw.text((45,35+n*130),line,font=font,fill='black')
    stream=BytesIO();image.save(stream,format='PNG')
    return stream.getvalue()


def test_pdf_mixed_coverage_workset_and_lazy_render_cache(materials,monkeypatch):
    raw=make_pdf('mixed')
    accepted=materials.accept('conv','mixed.pdf','application/pdf',raw)
    ref=accepted['material_ref']
    assert accepted['coverage']['pending_visual'] is True
    before=materials._record(ref,'conv')[0]['manifest']
    work=materials.read(ref,'conv','visual_workset')['manifest']['visual_workset']
    assert work['total']==1 and work['items'][0]['understanding_status']=='unknown'
    assert work['items'][0]['read_args']['section']=='image:1'
    first=materials.image_messages(ref,'conv','image:1')
    assert first[1]['image_url']['url'].startswith('data:image/png;base64,')
    monkeypatch.setattr('shengbo_prelive.material_pdf_reader.render_pdf_page',lambda *a:pytest.fail('render cache not reused'))
    assert materials.image_messages(ref,'conv','page:1')==[dict(first[0],text=first[0]['text'].replace('image:1','page:1')),first[1]]
    assert materials._record(ref,'conv')[0]['manifest']==before
    assert ref['content_hash']==canonical_hash(before)
    assert materials.list_visual_receipts(ref,'conv')==[]  # delivery recorded by ToolMessage adapter only


def test_plain_pdf_page_can_be_visually_inspected_without_pending_images(materials):
    ref=materials.accept('conv','text.pdf','application/pdf',make_pdf('text'))['material_ref']
    assert materials.read(ref,'conv','visual_workset')['manifest']['visual_workset']['total']==0
    assert materials.image_messages(ref,'conv','page:1')[1]['type']=='image_url'
    receipt=materials.record_visual_delivery(ref,'conv',locator='page:1',run_id='run')
    assert receipt['delivery_is_understanding'] is False


def test_image_local_ocr_is_cached_without_mutating_source_or_claiming_understanding(materials,monkeypatch):
    raw=price_image()
    ref=materials.accept('conv','price.png','image/png',raw)['material_ref']
    original=materials._record(ref,'conv')[0]['manifest']
    value=materials.read(ref,'conv','ocr:image:1')
    compact=''.join(value['text'].split())
    assert all(token in compact for token in ['29','不包邮','不含赠品'])
    assert value['ocr_result']['semantic_support_verified'] is False
    assert value['manifest']['coverage']['pending_visual'] is True
    assert value['ocr_result']['cache_hit'] is False
    monkeypatch.setattr('shengbo_prelive.material_pdf_reader.ocr_image',lambda *a:pytest.fail('OCR cache not reused'))
    second=materials.read(ref,'conv','ocr:image:1')
    assert second['ocr_result']['cache_hit'] is True and second['text']==value['text']
    assert materials._record(ref,'conv')[0]['manifest']==original
    assert hashlib.sha256(materials.blob_bytes(ref,'conv')).hexdigest()==hashlib.sha256(raw).hexdigest()


def test_docx_embedded_image_uses_same_local_ocr_and_source_locator(materials):
    from docx import Document
    document=Document();document.add_paragraph('商品活动条件在下图，不得猜测。')
    document.add_picture(BytesIO(price_image()))
    output=BytesIO();document.save(output)
    ref=materials.accept('conv','picture.docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document',output.getvalue())['material_ref']
    value=materials.read(ref,'conv','ocr:image:1')
    assert '不含赠品' in ''.join(value['text'].split())
    assert value['ocr_result']['source_locator']=='image:1'
    assert value['ocr_result']['source_ref']==ref
    assert all(line['source_locator']=='image:1' and 'page' not in line for line in value['ocr_result']['locators'])


def test_visual_receipt_cannot_borrow_source_conversation_or_nonvisual_section(materials):
    ref=materials.accept('conv','price.png','image/png',price_image())['material_ref']
    with pytest.raises(ContractError):
        materials.record_visual_delivery(ref,'foreign',locator='image:1',run_id='r')
    for locator in ['image:2','full','ocr:image:1','visual_workset']:
        with pytest.raises(ContractError):
            materials.record_visual_delivery(ref,'conv',locator=locator,run_id='r')
    receipt=materials.record_visual_delivery(ref,'conv',locator='image:1',run_id='r')
    work=materials.read(ref,'conv','visual_workset')['manifest']['visual_workset']
    assert work['items'][0]['delivery_receipt_ids']==[receipt['id']]
    assert work['items'][0]['understanding_status']=='unknown'


def test_ocr_cache_is_integrity_checked(materials,monkeypatch):
    ref=materials.accept('conv','price.png','image/png',price_image())['material_ref']
    monkeypatch.setattr('shengbo_prelive.material_pdf_reader.ocr_image',lambda *a:{'text':'活动价29元'})
    materials.read(ref,'conv','ocr:image:1')
    saved=materials.store.list('material_ocr_result')[0]
    saved['result']['text']='活动价1元'
    materials.store.put('material_ocr_result',saved)
    with pytest.raises(ContractError) as caught:
        materials.read(ref,'conv','ocr:image:1')
    assert caught.value.code=='MATERIAL_BLOB_INTEGRITY_FAILED'


def test_selected_second_embedded_image_rebinds_all_ocr_locations(materials,monkeypatch):
    from docx import Document
    document=Document()
    document.add_picture(BytesIO(price_image()))
    image=Image.open(BytesIO(price_image()));image.putpixel((0,0),(1,2,3));stream=BytesIO();image.save(stream,format='PNG')
    document.add_picture(BytesIO(stream.getvalue()))
    output=BytesIO();document.save(output)
    ref=materials.accept('conv','two.docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document',output.getvalue())['material_ref']
    monkeypatch.setattr('shengbo_prelive.material_pdf_reader.ocr_image',lambda *a:{'text':'不含赠品','page':1,
        'locators':[{'id':'image:1:ocr:1','page':1,'source_locator':'image:1','image_locator':'image:1','text':'不含赠品'}]})
    result=materials.read(ref,'conv','ocr:image:2')['ocr_result']
    assert result['source_locator']=='image:2' and 'page' not in result
    assert result['locators'][0]['id']=='image:2:ocr:1'
    assert result['locators'][0]['source_locator']==result['locators'][0]['image_locator']=='image:2'
    assert 'page' not in result['locators'][0]


def test_page_receipt_workset_never_claims_image_locator_receipt(materials):
    ref=materials.accept('conv','mixed.pdf','application/pdf',make_pdf('mixed'))['material_ref']
    receipt=materials.record_visual_delivery(ref,'conv',locator='page:1',run_id='r')
    item=materials.read(ref,'conv','visual_workset')['manifest']['visual_workset']['items'][0]
    assert item['delivery_receipt_ids']==[] and item['delivery_observed'] is False
    assert item['review_locator_choices'][1]=={'locator':'page:1','read_receipt_ids':[receipt['id']]}


def test_legacy_pdf_exposes_conservative_real_page_reads_without_rewriting_source_hash(materials):
    ref=materials.accept('conv','legacy.pdf','application/pdf',make_pdf('mixed'))['material_ref']
    row=materials.store.get('material',ref['id'])
    old=row['manifest']
    old['extractor']='pypdf'
    old['locators']=[x for x in old['locators'] if x['id']=='page:1']
    old.pop('embedded_images',None)
    old['coverage']['pending_visual']=False
    row['artifact_ref']={**ref,'content_hash':canonical_hash(old)}
    materials.store.put('material',row)
    reference=row['artifact_ref']
    overview=materials.read(reference,'conv','overview')
    assert overview['manifest']['coverage']['pending_visual'] is True
    item=materials.read(reference,'conv','visual_workset')['manifest']['visual_workset']['items'][0]
    assert item['id']=='page:1'
    assert materials.image_messages(reference,'conv','page:1')[1]['type']=='image_url'
    assert materials._record(reference,'conv')[0]['manifest']['coverage']['pending_visual'] is False
    assert canonical_hash(materials._record(reference,'conv')[0]['manifest'])==reference['content_hash']
