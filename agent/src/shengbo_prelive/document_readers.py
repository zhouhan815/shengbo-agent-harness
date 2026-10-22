"""Public, credential-free reader inventory; this is readiness, not a semantic SLA."""
from importlib.metadata import PackageNotFoundError, version
import sys


def _version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def reader_tool_capabilities():
    versions = {name:_version(name) for name in ('python-docx','striprtf','openpyxl','pywin32',
                'pypdf','liteparse','Pillow','rapidocr','onnxruntime')}
    word_registered = False
    if sys.platform == 'win32' and versions['pywin32']:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'Word.Application\CLSID'):
                word_registered = True
        except OSError:
            pass
    return {'schema_version':'document-reader-tools.v1', 'tool':'read_source',
            'versions':versions, 'local_only':True,
            'formats':[
                {'extensions':['.doc'],'engine':'Word COM → python-docx + source anchors',
                 'ready':word_registered and bool(versions['python-docx']), 'requires':'Windows Word + pywin32'},
                {'extensions':['.docx'],'engine':'python-docx + bounded OOXML source/embedded-image indexing',
                 'ready':bool(versions['python-docx'])},
                {'extensions':['.rtf'],'engine':'striprtf + bounded embedded-image indexing','ready':bool(versions['striprtf'])},
                {'extensions':['.xlsx'],'engine':'sparse OOXML + openpyxl values/styles/date normalization',
                 'ready':bool(versions['openpyxl'])},
                {'extensions':['.pdf'],'engine':'pypdf preflight + LiteParse text/layout + lazy page images',
                 'ready':bool(versions['pypdf'] and versions['liteparse'])},
                {'extensions':['.png','.jpg','.jpeg','.webp'],'engine':'Pillow validation + original visual read + opt-in local OCR',
                 'ready':bool(versions['Pillow'])},
                {'extensions':['.txt','.md','.csv','.json'],'engine':'bounded native text/CSV/JSON readers','ready':True}],
            'ocr':{'engine':'RapidOCR ONNX Runtime CPU','ready':bool(versions['rapidocr'] and versions['onnxruntime']),
                   'fallback':'explicit Tesseract compatibility, cached language data required',
                   'automatic_at_upload':False, 'confidence_is_fact_accuracy':False},
            'sections':['overview','locator_index:N','image_index:N','visual_workset:N',
                        'page:N','image:N','ocr:page:N','ocr:image:N','full'],
            'note':'ready表示依赖和配置可用，不代表任意文件必然解析或理解正确；coverage和未读区域须逐材料核验。'}
