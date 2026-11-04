import pytest

from app.copywriting_quality import content_issues
from app.prelive.module_generation import validate_output
from app.prelive.storage import BusinessError


PRODUCT = {
    'name': '单人回弹训练器',
    'knowledge': '套装包含球拍、回弹球和底座。可用于单人回弹和户外练习。',
    'specifications': [{'name': '拍杆材质', 'value': '镁合金'}],
}


def codes(text, **product_updates):
    findings = []
    content_issues({**PRODUCT, **product_updates}, [{'sentences': [{'text': text}]}],
                   require_coverage=False, sentence_findings=findings, verify_usage_claims=True)
    assert all(row['sentence_index'] == 0 for row in findings)
    return {row['code'] for row in findings}


@pytest.mark.parametrize(('text', 'expected'), [
    ('这套开箱即用。', 'UNSUPPORTED_IMMEDIATE_READINESS'),
    ('给自己选一套能马上开练的。', 'UNSUPPORTED_IMMEDIATE_READINESS'),
    ('这套已经够你开始第一次单人练习了。', 'UNSUPPORTED_IMMEDIATE_READINESS'),
    ('收到货那天，打开包装，器材往那儿一摆，下楼就能开始。', 'UNSUPPORTED_IMMEDIATE_READINESS'),
    ('自己想练的时候，把底座一放，网球一打，回弹回来继续接。', 'UNSUPPORTED_SETUP_STEP'),
    ('把底座放好，拿起球拍就能对着回弹球打起来。', 'UNSUPPORTED_SETUP_STEP'),
    ('他握住拍柄，按商品说明把底座放好，试着把球打向回弹网球。', 'UNSUPPORTED_SETUP_STEP'),
    ('我把使用画面说具体：找一块允许活动的户外空地，把底座放好，不用先约人。', 'UNSUPPORTED_SETUP_STEP'),
    ('儿童课外想接触接触球拍，有这套在家里或合适场地都能安排。', 'UNSUPPORTED_VENUE_CLAIM'),
    ('不用先找到搭档，不用先约场地，一套到手，想练就练。', 'UNSUPPORTED_VENUE_CLAIM'),
    ('它挺适合拿到小区空地或者公园里用。', 'UNSUPPORTED_VENUE_CLAIM'),
    ('镁合金的拍杆握起来挺扎实。', 'UNSUPPORTED_HANDLING_FEEL'),
    ('拍面反馈也比较直接。', 'UNSUPPORTED_HANDLING_FEEL'),
])
def test_concrete_usage_guarantees_need_positive_evidence(text, expected):
    assert codes(text) == {expected}


@pytest.mark.parametrize('text', [
    '一个人也能安排练习，不用等搭档回消息。',
    '三件一起挑，收到货之后先清点，再看看什么时间适合出门。',
    '如果公园允许练习，再确认周围环境适合挥拍。',
    '在家先把器材收好，下班有空，再找个合适的户外场地练几拍。',
    '想象一下，你在合适的户外场地拿起拍子，想多挥几拍就多练一会儿。',
    '可以在家附近合适的户外空地用它，不用约人。',
    '手心有点紧张也正常，先给自己留一点熟悉动作的时间。',
    '拍杆是镁合金，挥拍之前别着急比较手感。',
    '收到货按说明安装后再使用。',
    '收到货之后先看商品说明，确认器材怎么摆放。',
])
def test_ordinary_scene_and_supported_marketing_remain_available(text):
    assert not codes(text)


def test_confirmed_use_and_setup_can_be_stated():
    usage = '这套可在室内练习。使用前将底座放稳。开箱即可使用。'
    assert not codes('它可在室内使用，把底座放稳，再挥拍。', usage_instructions=usage)
    assert not codes('开箱即可使用。', usage_instructions=usage)
    assert not codes('拍杆握起来很扎实。', knowledge_documents=[{'content': '拍杆握起来很扎实。'}])


def test_a_prerequisite_or_other_location_is_not_affirmative_evidence():
    usage = '可在室内练习。收到货安装完成后即可开始使用。场地无需预约。'
    assert codes('开箱即可使用。', usage_instructions=usage) == {'UNSUPPORTED_IMMEDIATE_READINESS'}
    assert codes('可以在家里练。', usage_instructions=usage) == {'UNSUPPORTED_VENUE_CLAIM'}
    assert codes('不需要找专门的场地。', usage_instructions=usage) == {'UNSUPPORTED_VENUE_CLAIM'}
    assert codes('拍面反馈直接。', usage_instructions='拍面反馈是否直接，待确认。') == {'UNSUPPORTED_HANDLING_FEEL'}
    assert codes('把底座插好，立刻开始挥拍。', usage_instructions='使用前将底座放稳。') == {'UNSUPPORTED_SETUP_STEP'}
    assert codes('按商品说明把底座放在桌上。', usage_instructions='使用前将底座放稳。') == {'UNSUPPORTED_SETUP_STEP'}


def test_module_output_reports_the_sentence_and_old_callers_keep_compatibility():
    text = '不用先约场地，一套到手，想练就练。'
    output = {'module_key': 'selling_points', 'sentences': [
        {'display_text': text, 'spoken_text': text, 'kind': 'product_claim', 'fact_ids': ['KNOWLEDGE']},
    ]}
    assert validate_output(output, PRODUCT, 'selling_points') == [text]
    with pytest.raises(BusinessError) as failure:
        validate_output(output, PRODUCT, 'selling_points', verify_usage_claims=True)
    assert failure.value.code == 'GENERATED_MODULE_NEEDS_REVIEW'
    assert [(row['sentence_index'], row['code']) for row in failure.value.details['sentence_findings']] == [
        (0, 'UNSUPPORTED_VENUE_CLAIM'),
    ]


def contradiction_codes(text, **updates):
    findings = []
    content_issues({**PRODUCT, 'price': 23.58, 'currency': 'CNY', **updates},
                   [{'sentences': [{'text': text}]}], require_coverage=False,
                   sentence_findings=findings, verify_confirmed_contradictions=True)
    assert all(row['sentence_index'] == 0 for row in findings)
    return {row['code'] for row in findings}


@pytest.mark.parametrize('text', [
    '给自己选一套能马上开练的。',
    '把球打出去，球回来，再接着挥拍，这份自在感真的太舒服了。',
    '这套已经够你开始第一次单人练习了。',
    '选套能开练的，收到后记得按商品说明准备。',
    '到了合适的场地，摆好底座，自己就能挥两拍。',
    '它挺适合拿到小区空地或者公园里用。',
    '握起来顺手，平时收着不占地方。',
    '日常售价二十三块五毛八，到手是球拍、回弹网球一个和底座。',
    '日常价二十三点五八元。',
    '点右下角的小黄车，把想练的事安排上。',
])
def test_contradiction_policy_allows_marketing_without_positive_proof(text):
    assert not contradiction_codes(text)


@pytest.mark.parametrize(('text', 'updates', 'code'), [
    ('日常售价二十四元。', {}, 'CONFIRMED_PRICE_CONFLICT'),
    ('日常售价二十三块五毛九。', {}, 'CONFIRMED_PRICE_CONFLICT'),
    ('到手价二十元。', {'_price_quote': {'confirmed': True, 'quantity': 1, 'final_price': 21.0}}, 'CONFIRMED_PRICE_CONFLICT'),
    ('套装包含球拍、两个回弹网球和底座。', {'knowledge': '套装包含球拍、一个回弹网球和底座。'}, 'CONFIRMED_COMPONENT_COUNT_CONFLICT'),
    ('套装不含底座。', {}, 'CONFIRMED_COMPONENT_CONFLICT'),
    ('套装有电池。', {'knowledge': '套装包含底座，但不含电池。'}, 'CONFIRMED_COMPONENT_CONFLICT'),
    ('开箱即可开练。', {'usage_instructions': '使用前必须安装底座。'}, 'CONFIRMED_SETUP_CONFLICT'),
    ('无需安装底座。', {'usage_instructions': '使用前必须安装底座。'}, 'CONFIRMED_SETUP_CONFLICT'),
    ('在室内也可以练习。', {'usage_instructions': '仅限户外使用。'}, 'CONFIRMED_VENUE_CONFLICT'),
    ('不同颜色的套装组成都一样。', {'knowledge': '不同颜色的组成不同。'}, 'CONFIRMED_VARIANT_CONFLICT'),
    ('不同颜色的套装组成不一样。', {'faq': '答：三种颜色的组成相同。'}, 'CONFIRMED_VARIANT_CONFLICT'),
    ('这套支持加印LOGO。', {'faq': '答：不支持加工定制或加印LOGO。'}, 'CONFIRMED_FEATURE_CONFLICT'),
])
def test_contradiction_policy_flags_comparable_confirmed_facts(text, updates, code):
    assert contradiction_codes(text, **updates) == {code}


def test_contradiction_policy_scopes_rules_to_confirmed_facts():
    assert not contradiction_codes('给自己选一套能马上开练的。', usage_instructions='使用前必须安装底座。')
    assert not contradiction_codes('按说明安装，之后就能直接开始练习。', usage_instructions='使用前必须安装底座。')
    assert not contradiction_codes('无需安装底座。', usage_instructions='使用前必须安装电池。')
    assert not contradiction_codes('不需要安装。', usage_instructions='使用前必须调试。')
    assert not contradiction_codes('开箱即可开练。', usage_instructions='使用前不需要安装底座。')
    assert not contradiction_codes('不能开箱就能使用。', usage_instructions='使用前需先安装底座。')
    assert not contradiction_codes('套装包含两个回弹网球？', knowledge='套装包含球拍、一个回弹网球和底座。')
    assert not contradiction_codes('在室内也可以练习。', usage_instructions='是否只能户外，尚未确认。')
    assert not contradiction_codes('到手价二十元。', _price_quote={'confirmed': False, 'final_price': 21.0})
    assert not contradiction_codes('用两个回弹网球轮换着练。', knowledge='套装包含球拍、一个回弹网球和底座。')
    assert not contradiction_codes('三种颜色组成相同。', knowledge='不同颜色的性能不同。')
    assert not contradiction_codes('不同颜色的性能相同。', knowledge='不同颜色的配置不同。')
    assert not contradiction_codes('保温杯能保温，放在桌边就好。', knowledge='这款桌子不支持保温。')
    assert not contradiction_codes('这款能保温。', knowledge='不能放进保温箱。')
    assert not contradiction_codes('这套不支持加印LOGO。', faq='答：不支持加工定制或加印LOGO。')
