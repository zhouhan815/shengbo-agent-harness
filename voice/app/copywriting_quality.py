"""Bounded, deterministic cross-version checks; no model self-rating in production."""
from difflib import SequenceMatcher
from decimal import Decimal, InvalidOperation
import re
from opencc import OpenCC

SIMPLIFY = OpenCC('t2s').convert

EXEMPT_STAGES = {'identity', 'specification', 'spec_notice', 'price', 'promotion'}

CORE_FEATURES = [
    ('EVA材质', r'E\s*V\s*A', r'E\s*V\s*A'),
    ('涤纶材质', r'涤纶', r'涤纶'), ('陶瓷材质', r'陶瓷', r'陶瓷'),
    ('一体成型', r'一体成型', r'一体成型'), ('足弓支撑', r'足弓', r'足弓'),
    ('厚底', r'厚底', r'厚底'), ('静音', r'静音', r'静音|声音.*轻|声音.*小'),
    ('防滑', r'防滑', r'防滑'), ('轻便', r'轻便', r'轻便|轻盈|轻巧'),
    ('耐磨', r'耐磨', r'耐磨'), ('快干', r'快干', r'快干|干得快|干.*比较快'),
    ('防臭', r'防臭', r'防臭'), ('可折叠', r'可折叠', r'折叠|折起来'),
]


def required_features(product):
    source = SIMPLIFY(str(product.get('selling_points', '')))
    return [(label, output) for label, pattern, output in CORE_FEATURES if re.search(pattern, source, re.I)]


def _daily_quote_detection_view(product, value):
    """Mask only a verified standalone CNY daily quote in a temporary view.

    No output is edited. Mixed promotional/savings clauses, other prices,
    unverified amounts and unsupported currency/readout forms stay strict.
    """
    if re.search(r'省钱|省下|节省|便宜|少花|更划算', value):
        return value
    if product.get('currency') != 'CNY' or type(product.get('price')) not in (int, float):
        return value
    try:
        price = Decimal(str(product['price']))
        if not price.is_finite() or price < 0 or price.normalize().as_tuple().exponent < -2:
            return value
        quote = product.get('_price_quote')
        if isinstance(quote, dict) and 'daily_price' in quote:
            quoted = quote['daily_price']
            if type(quoted) not in (int, float) or Decimal(str(quoted)) != price:
                return value
    except (InvalidOperation, ValueError, TypeError):
        return value

    def spoken_integer(number):
        digits = '零一二三四五六七八九'
        if number < 10:
            return digits[number]
        if number < 20:
            return '十' + (digits[number % 10] if number % 10 else '')
        if number < 100:
            return digits[number // 10] + '十' + (digits[number % 10] if number % 10 else '')
        base, unit = (1000, '千') if number >= 1000 else (100, '百')
        tail = number % base
        suffix = spoken_integer(tail) if tail else ''
        if 10 <= tail < 20:
            suffix = '一' + suffix
        return digits[number // base] + unit + ('零' if 0 < tail < base // 10 else '') + suffix

    forms = {format(price.normalize(), 'f'), format(price, '.2f')}
    if price == price.to_integral_value():
        forms.add(format(price, '.1f'))
    integer = int(price)
    if integer < 10000:
        whole = spoken_integer(integer)
        fractional = format(price, '.2f').split('.')[1]
        forms.add(whole if fractional == '00' else whole + '点' + ''.join('零一二三四五六七八九'[int(n)] for n in fractional.rstrip('0')))
    amount = '|'.join(re.escape(text) for text in sorted(forms, key=len, reverse=True))
    daily = re.compile(r'(?:日常(?:售)?价|原价)\s*(?:是|为)?\s*(?:人民币|[￥¥])?\s*(?:' + amount + r')\s*(?:元|块钱|块)')
    protected = r'优惠|折|满.{0,12}减|抵扣|活动价|券|省钱|省下|节省|便宜|少花|更划算'
    return ''.join(piece if re.search(protected, piece) else daily.sub('已核实日常报价', piece)
                   for piece in re.split(r'([。！？!?；;，,\n])', value))


def _has_offer_assertion(value):
    """Keep offer guards strict, excluding only clearly non-assertive previews.

    A mention such as '优惠后面再讲' does not state a discount. Do not exempt
    a mixed sentence that also quotes money, discounts, coupon actions or
    stacking. Those continue to require the real non-stackable condition.
    """
    marker = r'优惠|折|满.{0,12}减|抵扣|活动价|券'
    explicit = (
        r'(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+)\s*(?:元|块|折)'
        r'|[￥¥$]\s*\d|满.{0,12}减|叠加|抵扣|立减|打折|到手'
        r'|(?:先|请|记得|赶紧|现在|马上|直接|可以|可|要|需要|去|快).{0,3}(?:领券|用券)'
        r'|(?:领券|用券).{0,12}(?:优惠|享|减|省|抵|下单)'
        r'|(?:享受|享有|享|获得|领取|购买|下单)')
    preview = (
        r'(?:稍后|后面|后边|后续|待会儿?|一会儿?|接下来).{0,24}(?:讲|说|介绍|说明|解释|讨论)'
        r'|(?:放到|留到).{0,16}(?:环节|部分).{0,8}(?:讲|说|介绍|说明)')
    if not re.search(marker, value):
        return False  # Keep the legacy trigger scope; other price checks remain.
    for clause in re.split(r'[。！？!?；;\n]', value):
        if not re.search(marker, clause):
            # A safe preview cannot hide a quote in another sentence of the
            # same module merely because that sentence omits '优惠'.
            if re.search(explicit, clause):
                return True
            continue
        if re.search(preview, clause) and not re.search(explicit, clause):
            continue
        return True
    return False


def _has_space_saving_assertion(value, *, can_fold=False):
    """Exclude only an explicit customer-wish question, not product claims.

    '想找地方集中又不占地方？' asks about the customer's desired outcome.
    '这款不占地方，想要吗？' still asserts a property of the product and must
    remain checked. A question mark alone never exempts a performance claim.
    """
    previous = ''
    fold_action = r'折叠|折起来|折好|折起'
    for sentence in re.findall(r'[^。！？!?]+[。！？!?]?', value):
        for match in re.finditer(r'不占地方', sentence):
            prefix = sentence[:match.start()]
            wish = re.search(r'(?:想(?:要|找)?|希望|需要)[^。！？!?]{0,32}$', prefix)
            customer_question = (sentence.rstrip().endswith(('？', '?')) and wish
                and not re.search(r'这款|这件|本品|本款|这台|这个|这只|这双|这套|本商品|这盒|它', sentence))
            folding = re.search(r'(?:' + fold_action + r')([^。！？!?]{0,24})$', prefix)
            folded_now = folding and not re.search(r'展开|打开|铺开|摊开|用的时候|使用时', folding[1])
            folded_before = (prefix.strip() in {'这份', '这种', '这样'}
                and re.search(fold_action, previous)
                and re.search(r'归位|收纳|收好|收着|收起来', sentence[match.end():]))
            if not customer_question and not (can_fold and (folded_now or folded_before)):
                return True
        previous = sentence
    return False


def _thermal_claims(value):
    # An ordinary action with a named scene prop carries no claim about the
    # product being sold. Keep every other occurrence and the original copy.
    pouring = r'(?:给|往|从)保温(?:杯|壶|瓶|桶)(?:里|中)?(?:倒出?|加|装|灌)(?:热水|温水|水|茶|饮料)'
    unpacking = (
        r'(?:把|将)保温(?:袋|箱)(?:里|中)的(?:餐具|食物|饮料)(?:一样样|一件件)?(?:拿|取)出(?:来)?'
        r'|从保温(?:袋|箱)(?:里|中)(?:拿|取)出(?:餐具|食物|饮料)'
    )
    scene_placement = r'(?:把|将|提着|拎着)保温(?:杯|壶|瓶|桶|袋|箱)(?:带到|拿到|放在|放到|摆在|摆到)'
    claim_view = re.sub(f'{pouring}|{unpacking}|{scene_placement}', '使用场景道具', value)
    claims = set()
    if '保温' in claim_view:
        claims.add('保温')
    if re.search(r'杯壁[^。！？]{0,8}不烫|不烫手|防烫', claim_view):
        claims.add('防烫')
    return claims


def _has_order_to_use_promise(text):
    # An explicit link from placing the order to a definite upcoming use date
    # promises arrival. The same scene described after receipt is fine.
    return bool(re.search(
        r'(?:现在|今天|马上|这就)?(?:下单|拍下)[，, ]{0,2}'
        r'(?:下次|明天|后天|本周末|这个周末)[^。！？；，,]{0,10}(?:就能|就可|一定能|保证能)用上', text))


def _positive_product_text(product):
    text = _product_fact_text(product)
    unknown = r'未知|未提供|待确认|无数据|不适用|暂无|未测试|(?:不支持|没有|无|不具备)(?:保温|防烫|折叠)|不可折叠'
    parts = re.split(r'([。；;，,\n])', text)
    return ''.join(("" if re.search(unknown, clause) else clause) + separator
                   for clause, separator in zip(parts[::2], [*parts[1::2], ""]))


def _product_fact_text(product, *, include_faq=False):
    specifications = [row for row in product.get('specifications') or [] if isinstance(row, dict)]
    documents = [row for row in product.get('knowledge_documents') or [] if isinstance(row, dict)]
    values = [product.get(key) or '' for key in ('selling_points', 'usage_instructions', 'knowledge')]
    values.extend(f"{row.get('name', '')}：{row['value']}" for row in specifications if row.get('value'))
    values.extend(row.get('content') or '' for row in documents)
    if include_faq:
        values.extend((product.get('faq') or '', product.get('target_audience') or ''))
    return SIMPLIFY('。'.join(str(value) for value in values))


_STORAGE_TARGET = r'(?:(?:SUV|小轿车)(?:的)?(?:后备箱|后座)|(?:汽车|车)?后备箱(?:角落)?|储物箱|收纳箱)'
_STORAGE_FIT_FORWARD = re.compile(
    r'(?:^|[，,：:])\s*(?:折叠后|折好(?:后)?|折起来|本品|它|这(?:款|张|个)(?:桌子|桌)?)?\s*'
    r'(?:(?:都|也|一定|肯定)?(?:可以|能|可)(?:直接|轻松)?(?:放(?:进|入)?|装(?:进|入)|塞进|收进)'
    r'|直接(?:放(?:进|入|到|在)?|装进)|往(?=' + _STORAGE_TARGET + r'一放))'
    r'(?P<target>' + _STORAGE_TARGET + r')', re.I)
_STORAGE_FIT_REVERSE = re.compile(
    r'(?P<target>' + _STORAGE_TARGET + r')[，, ]{0,2}(?:都|也|一定|肯定)?(?:可以|能|可)'
    r'(?:给它找到(?:一个)?(?:小)?空间|把它放进去|放得下它|装得下它)', re.I)


def _storage_fit_targets(text, product_name, *, evidence=False):
    """Only direct fit assertions, keeping named receivers and conditions distinct."""
    if product_name:
        text = text.replace(product_name, '它')
    targets = set()
    for sentence in re.split(r'[。！？!?；;\n]', text):
        absolute = bool(re.search(r'(?:不论|无论|任何|所有)[^。！？]{0,16}' + _STORAGE_TARGET, sentence, re.I))
        conditional = re.search(r'(?:如果|只要)[^。！？]{0,28}(?:确认|量过|测过|核对)[^。！？]{0,12}(?:尺寸|空间|放得下)', sentence)
        if conditional and not absolute:
            continue
        if evidence and re.search(r'建议|请先|需先|先核对|先确认|待确认|能否|是否|如能|若能|部分车型|尺寸(?:合适|符合)|空间(?:合适|足够)', sentence):
            continue
        for pattern in (_STORAGE_FIT_FORWARD, _STORAGE_FIT_REVERSE):
            for match in pattern.finditer(sentence):
                broad = absolute or bool(re.search(r'都能|都可以', match[0]))
                target = match["target"].lower().replace('的', '')
                target = re.sub(r'^(?:汽车|车)(?=后备箱)', '', target)
                targets.add((target, broad))
    return targets


_USE_ACTION = r'(?:开始(?:回弹练习|用|练|打|挥拍)|开练|开打|开机|直接使用|上手|使用|练起来|挥拍|打(?:球|起来)|(?:网|羽毛)?球(?:一|被)?打(?:出|回)|(?:网|羽毛)?球一打)'
_ARRIVAL = r'(?:开箱|拆(?:开)?包装|拿到手|到手|收到(?:货)?|收到手|拿到货)'
_READY_TO_USE = re.compile(
    rf'{_ARRIVAL}[^。！？；\n]{{0,32}}?(?:即可|就能|就可|马上|立刻|立即|直接|即)(?:{_USE_ACTION}|用(?=[，,。！？；\n]|$))'
    rf'|{_ARRIVAL}[^。！？；\n]{{0,36}}?(?:就能|即可)开始(?=[，,。！？；\n]|$)'
    rf'|(?:这套|一套|套装|这款|器材|配齐)[^，,。！？；\n]{{0,14}}?(?:能|可|就)(?:马上|立刻|立即){_USE_ACTION}'
    rf'|(?:这套|套装|配齐)[^，,。！？；\n]{{0,10}}?(?:已经|就)?够(?:你|大家|顾客)?(?:直接)?开始(?:第一次)?(?:单人|独自)?(?:练习|使用|开练|打球)'
)
_READINESS_PREREQUISITE = re.compile(r'(?:按|依)(?:照|据)?(?:说明|指引|要求)|(?:确认|完成|核对|安装|检查)[^。！？；\n]{0,10}(?:后|再)|(?:如果|若|在)[^。！？；\n]{0,16}(?:符合|满足|允许)')
_SETUP = re.compile(
    r'(?:把|将)?(?P<part>底座|支架|电池|绳索|挂绳|电源线|把手|滤芯)'
    r'(?:(?P<place>一放|放好|放稳|安置好|稳住)|放在(?P<place_at>[^，,。！？；\n]{1,10})'
    r'|(?P<install>安装好|装上)|(?P<insert>插好|插上)|(?P<tie>绑好)|(?P<connect>接好|接上))'
)
_SETUP_INSTRUCTION = re.compile(r'(?:按|依据|根据)(?:照)?(?:商品|产品|包装)?(?:使用)?(?:说明书?|指引|要求)[^，,。！？；\n]{0,10}$')
_SETUP_USE_CONTEXT = re.compile(r'(?:使用前|练习前|使用画面)|(?:找|到|挑|选)[^。！？；\n]{0,20}(?:空地|场地|球场)[^。！？；\n]{0,4}$')
_NO_VENUE_REQUIREMENT = {
    'no_booking': re.compile(
        r'(?:不必|不用|无需|不需要|免)(?:事先|提前|先)?(?:预约|约)(?:球场|场地|场馆)'
        r'|(?:球场|场地|场馆)(?:都|也)?(?:不用|无需|不必|不需要|免)(?:先|提前)?(?:预约|订场)'
    ),
    'no_special_venue': re.compile(r'(?:不用|无需|不必|不需要)(?:特意|先)?找(?:专门|指定|专业)的?(?:场地|球场|场馆)'),
}
_VENUE_NAMES = {
    'home': r'家里|家中|在家(?!附近|旁边|门口|周围|楼下|等|看看|收纳|准备|休息)',
    'indoor': r'室内|屋内',
    'living_room': r'客厅',
    'bedroom': r'卧室',
    'public_courtyard': r'小区空地',
    'public_park': r'公园',
    'public_general': r'公共场地',
    'public_square': r'广场',
}
_USE_WORDS = r'使用|用(?:它|这套)?|练(?:习|球|起来)?|安排(?:一次)?练习|开练|挥拍|打球|操作'
_VENUE_CLAIMS = {
    kind: re.compile(
        rf'(?:可|适合|支持|能)(?:以)?(?:拿到|带到|在|用于|用在)?[^。！？；\n]{{0,9}}(?:{name})[^。！？；\n]{{0,13}}(?:{_USE_WORDS})'
        rf'|(?:{name})[^。！？；\n]{{0,11}}(?:也|都|就)?(?:可以|都能|就能|能|可)(?:使用|用|练|安排|挥拍|打球|操作)'
        rf'|适用(?:于|场地[：:]?)?(?:{name})'
    ) for kind, name in _VENUE_NAMES.items()
}
_HANDLING_FEEL = {
    'feedback': re.compile(r'(?:拍面|触键|按键|击球|触发|操作)反馈(?:也|很|挺|更|比较|相当|十分|特别){0,2}(?:直接|清晰|明显|灵敏|干脆)'),
    'grip': re.compile(r'(?:握|抓|持握|拿)(?:在手里|在手上|起来|着|住)?(?:也|挺|很|比较|更|特别|相当){0,2}(?:稳当|扎实|顺手|不打滑)'),
    'support': re.compile(r'(?:拍杆|把手|握把|手柄|握柄)[^。！？；\n]{0,12}(?:支撑感|托得稳)')
}


def _usage_claims(text, *, evidence=False):
    """Identify concrete operating/venue promises without treating scene props as claims."""
    found = set()
    for sentence in re.split(r'[。！？!?；;\n]', text):
        # A question, unknown value or unsupported feature in source material
        # must not be promoted into positive evidence for the output.
        if evidence and re.search(r'未知|待确认|未确认|暂无|未测试|无数据|未提供|是否|能否|不能|不可|并非|不(?:建议|支持|适合|保证|确定)|需(?:要)?确认', sentence):
            continue
        if any(not _READINESS_PREREQUISITE.search(match[0]) for match in _READY_TO_USE.finditer(sentence)):
            found.add(('readiness', 'arrival'))
        for match in _SETUP.finditer(sentence):
            before_setup = sentence[:match.start()]
            instructed = _SETUP_INSTRUCTION.search(before_setup) or _SETUP_USE_CONTEXT.search(before_setup)
            followed_by_use = re.search(_USE_ACTION, sentence[match.end():match.end() + 34])
            if evidence or instructed or followed_by_use:
                position = (match['place_at'] or '').replace('的', '')
                found.add(('setup', (match['part'], match.lastgroup, position)))
        for kind, pattern in _NO_VENUE_REQUIREMENT.items():
            if pattern.search(sentence):
                found.add(('venue', kind))
        # Explicit local permission makes a named venue a conditional example.
        # Generic marketing at a named location, with no guarantee of fit, is
        # already outside these capability patterns.
        conditional_public = re.search(r'(?:允许|获准|确认[^，,]{0,12}可以)[^，,]{0,8}(?:练|打球|使用)', sentence)
        unrestricted = re.search(r'(?:无论|不论|任何|所有|任意|随便)', sentence)
        for kind, pattern in _VENUE_CLAIMS.items():
            if pattern.search(sentence) and not (kind.startswith('public_') and conditional_public and not unrestricted):
                found.add(('venue', kind))
        for kind, pattern in _HANDLING_FEEL.items():
            if pattern.search(sentence):
                found.add(('feel', kind))
    return found


_USAGE_FINDINGS = {
    'readiness': ('UNSUPPORTED_IMMEDIATE_READINESS', '未确认开箱或到手即可使用，请只讲已确认的套装内容与适用用途'),
    'setup': ('UNSUPPORTED_SETUP_STEP', '未确认所述安装或操作步骤，请按商品使用资料表达'),
    'venue': ('UNSUPPORTED_VENUE_CLAIM', '未确认该使用地点或免预约条件，可用符合要求且允许使用的场景展开'),
    'feel': ('UNSUPPORTED_HANDLING_FEEL', '未确认该握持、支撑或反馈表现，请勿由材质或构造推导实际体验'),
}


def _sentence_fact_findings(text, thermal_facts, storage_fits, product_name, index):
    claims = _thermal_claims(text)
    if '保温' in claims and '保温' not in thermal_facts:
        yield {"sentence_index": index, "code": "UNSUPPORTED_INSULATION",
               "message": "未确认商品保温能力，请核对功能属于商品还是场景道具"}
    if '防烫' in claims and '防烫' not in thermal_facts:
        yield {"sentence_index": index, "code": "UNSUPPORTED_TOUCH_SAFETY",
               "message": "未确认商品防烫或不烫手，请核对商品的安全性能依据"}
    if _storage_fit_targets(text, product_name) - storage_fits:
        yield {"sentence_index": index, "code": "UNSUPPORTED_STORAGE_FIT",
               "message": "未确认商品能放进所述容器或车辆空间，可讲折叠收纳便利或提醒先核对尺寸"}
    if _has_order_to_use_promise(text):
        yield {"sentence_index": index, "code": "ORDER_TO_USE_PROMISE",
               "message": "这句话暗示下单后会在指定使用时点前送到，请按实际履约条件表达"}


_SOURCE_UNCERTAINTY = re.compile(r'未知|未提供|未确认|待确认|暂无|没有数据|是否|能否|未测试|未经测试|据称')
_CHINESE_DIGITS = {digit: value for value, digit in enumerate('零一二三四五六七八九')} | {'〇': 0, '两': 2}
_CHINESE_AMOUNT = r'[零〇一二两三四五六七八九十百千万]+(?:点[零〇一二三四五六七八九]+)?'
_AMOUNT = rf'(?:[0-9]+(?:\.[0-9]{{1,2}})?|{_CHINESE_AMOUNT})'
_PRICE_ASSERTION = re.compile(
    rf'(?:^|[，,：:；;])\s*(?:这套|这款|本品)?\s*'
    rf'(?P<kind>日常(?:售)?价|平时(?:卖|售价)|原价|本场价|直播(?:间)?(?:售价|价)|活动价|到手价|售价|价格)'
    rf'\s*[:：]?\s*(?:是|为|只要|仅需|才)?\s*(?:人民币|[￥¥])?\s*(?P<amount>{_AMOUNT})\s*'
    rf'(?P<unit>元|块钱|块)(?:(?P<jiao>[零〇一二三四五六七八九0-9])(?:毛|角)(?P<fen>[零〇一二三四五六七八九0-9])?分?)?'
    rf'(?=$|[\s，,。！？；;、！])'
)
_KIT_CONTEXT = re.compile(r'套装(?:构成|组成|包括|包含|含|配|有)|包装清单|这套(?:里|包含|包括|有)|到手(?:是|有|包含)|买到(?:的)?(?:是|有)|(?:一套|每套)(?:包含|包括|有|配)|收到(?:的)?(?:是|有)')
_KIT_ITEMS = r'回弹网球|回弹球|网球|羽毛球|球拍|底座|电池|滤芯|充电线|电源线'
_KIT_EXCLUSION = re.compile(r'(?:不含|不包括|不包含|不配|没有|不带)\s*(?:[个支只件颗条节枚块把]|[一二两三四五六七八九0-9][个支只件颗条节枚块把])?\s*(' + _KIT_ITEMS + ')')
_ITEM_COUNT = re.compile(
    rf'(?:(?P<before>[0-9]+|{_CHINESE_AMOUNT})\s*[个支只件颗条节枚块把]\s*(?P<right>{_KIT_ITEMS})'
    rf'|(?P<left>{_KIT_ITEMS})\s*(?P<after>[0-9]+|{_CHINESE_AMOUNT})\s*[个支只件颗条节枚块把])'
)
_SETUP_ACTION = r'(?P<operation>安装|组装|装配|调试)(?P<component>' + _KIT_ITEMS + r')?'
_REQUIRED_SETUP_MARKER = r'(?<![不无需])(?:必须|务必|须|需要|需|要)先?'
_SETUP_REQUIRED = (
    re.compile(r'(?:使用前|开练前|开机前)[^，,。！？；;\n]{0,12}?' + _REQUIRED_SETUP_MARKER + _SETUP_ACTION),
    re.compile(_REQUIRED_SETUP_MARKER + _SETUP_ACTION + r'[^。！？；;\n]{0,14}(?:才能|后(?:方可|再|才可|即可))'),
)
_SETUP_NOT_NEEDED = re.compile(r'(?:不(?:必|用|需要)|无需|免)(?:先)?' + _SETUP_ACTION)
_READY_FROM_BOX = re.compile(r'(?:开箱|拆开包装|到手|收到货)[^。！？；;\n]{0,12}(?:即|即可|就能|马上|立刻|直接)(?:开练|开打|开机|使用)')
_VARIANT_COMPARISON = re.compile(
    r'(?P<axis>颜色|规格|款式|各款|不同款)[^，,。！？；;\n]{0,12}(?P<property>配置|配件|组成|功能|性能)'
    r'[^，,。！？；;\n]{0,5}?(?P<relation>不相同|不同|不一样|有差异|有区别|都相同|相同|都一样|一致|无差别|没有差别)')
_FEATURE_NAMES = r'保温(?!杯|壶|瓶|桶|袋|箱|套|垫)|防烫|折叠|防水|(?:加工)?定制|加印LOGO'
_NEGATIVE_FEATURE = re.compile(r'(?:不支持|不具备|没有|不可|不能|不允许)\s*(?P<features>(?:'
                               + _FEATURE_NAMES + r')(?:(?:以及|或者|[、及与或和])\s*(?:' + _FEATURE_NAMES + r'))*)', re.I)
_POSITIVE_FEATURE = re.compile(r'(?:支持|能|可以|具有|具备|自带)\s*(?P<feature>' + _FEATURE_NAMES + r')', re.I)
_THERMAL_PROP = re.compile(r'保温(?:杯|壶|瓶|桶|袋|箱|套|垫)')


def _decimal_amount(value):
    try:
        return Decimal(value)
    except InvalidOperation:
        pass
    whole, _, fraction = value.partition('点')
    if re.search(r'[一二两三四五六七八九]{2}', whole):
        return None
    units = {'十': 10, '百': 100, '千': 1000}
    total = section = digit = 0
    for character in whole:
        if character in _CHINESE_DIGITS:
            digit = _CHINESE_DIGITS[character]
        elif character in units:
            section += (digit or 1) * units[character]
            digit = 0
        elif character == '万':
            total += (section + digit) * 10000
            section = digit = 0
        else:
            return None
    digits = ''.join(str(_CHINESE_DIGITS[n]) for n in fraction)
    return Decimal(f'{total + section + digit}.{digits or "0"}')


def _kit_counts(text):
    return [(match['right'] or match['left'], _decimal_amount(match['before'] or match['after']))
            for match in _ITEM_COUNT.finditer(text)]


def _variant_comparisons(text):
    differences = {'不相同', '不同', '不一样', '有差异', '有区别'}
    return {('款式' if '款' in match['axis'] else match['axis'],
             '组成' if match['property'] in {'组成', '配件'} else match['property'],
             match['relation'] in differences) for match in _VARIANT_COMPARISON.finditer(text)}


def _setup_scope(match):
    return ('调试' if match['operation'] == '调试' else '安装', match['component'])


def _feature_name(value):
    return value.removeprefix('加工').upper()


def _denied_features(text):
    return {_feature_name(feature) for match in _NEGATIVE_FEATURE.finditer(text)
            for feature in re.findall(_FEATURE_NAMES, match['features'], re.I)}


def _asserted_features(text):
    features = set()
    for match in _POSITIVE_FEATURE.finditer(text):
        subject = text[:match.start()]
        if subject.endswith(('不', '没', '未', '无')):
            continue
        if match['feature'] in {'保温', '防烫'} and _THERMAL_PROP.search(re.split(r'[，,。]', subject)[-1]):
            continue
        features.add(_feature_name(match['feature']))
    return features


def _confirmed_product_contradictions(product, scripts, sentence_findings):
    """Block only directly opposed statements about comparable known facts."""
    source = _product_fact_text(product, include_faq=True)
    source_clauses = [clause for clause in re.split(r'[。！？!?；;\n]', source)
                      if not _SOURCE_UNCERTAINTY.search(clause) and not clause.lstrip().startswith(('如果', '若', '假设', '问：'))]
    source = '。'.join(source_clauses)
    kit_clauses = [clause for clause in source_clauses if _KIT_CONTEXT.search(clause)]
    excluded = set(_KIT_EXCLUSION.findall('。'.join(clause for clause in source_clauses if re.search(r'套装|包装|这套', clause))))
    positive_kit = [_KIT_EXCLUSION.sub('', clause) for clause in kit_clauses]
    components = set(re.findall(_KIT_ITEMS, '。'.join(positive_kit)))
    known_counts = {}
    for clause in positive_kit:
        for component, count in _kit_counts(clause):
            if count is not None:
                known_counts.setdefault(component, set()).add(count)
    setup_required = {_setup_scope(match) for pattern in _SETUP_REQUIRED for clause in source_clauses
                      if not re.search(r'不一定|可选|仅建议|建议先', clause) for match in pattern.finditer(clause)}
    indoor_barred = bool(re.search(r'(?:仅限|只可|只能|只允许)(?:在)?户外|(?:不得|禁止|不(?:能|可|允许|适合|支持))(?:在)?(?:室内|屋内)(?:使用|练习|打球)|(?:室内|屋内)(?:禁止|不得|不适合)(?:使用|练习)', source))
    variant_facts = _variant_comparisons(source)
    fixed_features = _denied_features(source) - _asserted_features(source)
    quote = product.get('_price_quote')
    quote = quote if isinstance(quote, dict) else {}
    daily_price = product.get('price')
    current_price = quote.get('final_price') if quote.get('confirmed') is True and quote.get('quantity', 1) == 1 else None
    issues = []
    for script in scripts:
        for index, sentence in enumerate(script['sentences']):
            def finding(code, message):
                row = {'sentence_index': index, 'code': code, 'message': message}
                if sentence_findings is None or row not in sentence_findings:
                    issues.append(message)
                    if sentence_findings is not None:
                        sentence_findings.append(row)

            for clause in re.findall(r'[^。！？!?；;\n]+[。！？!?；;]?', SIMPLIFY(sentence['text'])):
                question_without_assertion = clause.endswith(('？', '?')) and not re.search(r'[，,]', clause)
                if question_without_assertion:
                    continue
                clause = clause.rstrip('。！？!?；;')
                if _SOURCE_UNCERTAINTY.search(clause) or clause.lstrip().startswith(('如果', '若', '假设')):
                    continue
                if product.get('currency') == 'CNY':
                    for match in _PRICE_ASSERTION.finditer(clause):
                        plain_list_price = match['kind'] in {'售价', '价格'} and not quote and not product.get('promotion') and product.get('promo_price') is None
                        price = daily_price if plain_list_price or match['kind'].startswith(('日常', '平时', '原价')) else current_price
                        if type(price) not in (int, float):
                            continue
                        amount = _decimal_amount(match['amount'])
                        if amount is None:
                            continue
                        if match['jiao'] is not None:
                            amount += (_decimal_amount(match['jiao']) / 10 + _decimal_amount(match['fen'] or '0') / 100)
                        if amount != Decimal(str(price)):
                            finding('CONFIRMED_PRICE_CONFLICT', '口播金额与该价格口径的已确认商品价格不一致')
                if _KIT_CONTEXT.search(clause):
                    present = _KIT_EXCLUSION.sub('', clause)
                    for component, count in _kit_counts(present):
                        expected = known_counts.get(component, set())
                        if len(expected) == 1 and count is not None and component not in excluded and count not in expected:
                            finding('CONFIRMED_COMPONENT_COUNT_CONFLICT', f'口播中的{component}件数与已确认套装组成不一致')
                    if set(re.findall(_KIT_ITEMS, present)) & (excluded - components):
                        finding('CONFIRMED_COMPONENT_CONFLICT', '口播承诺了已明确不在套装中的配件')
                if re.search(r'套装|这套|到手', clause) and set(_KIT_EXCLUSION.findall(clause)) & (components - excluded):
                    finding('CONFIRMED_COMPONENT_CONFLICT', '口播排除了已确认的套装配件')
                denied_setup = {_setup_scope(match) for match in _SETUP_NOT_NEEDED.finditer(clause)}
                denies_requirement = any((operation, None) in denied_setup or (operation, component) in denied_setup
                                         for operation, component in setup_required)
                immediate_use = _READY_FROM_BOX.search(clause) and not re.search(r'不保证|并非|不能|不可以|不是|别当成|不等于', clause)
                if setup_required and (denies_requirement or immediate_use) and not _READINESS_PREREQUISITE.search(clause):
                    finding('CONFIRMED_SETUP_CONFLICT', '商品资料明确要求完成安装或调试，口播省略了这个已确认前提')
                if indoor_barred and any(_VENUE_CLAIMS[venue].search(clause) for venue in ('indoor', 'living_room', 'bedroom')) and not re.search(r'不能|不可|不适合|不允许|禁止|别在|不要在|不得', clause):
                    finding('CONFIRMED_VENUE_CONFLICT', '商品资料明确限制室内使用，口播给出了相反的使用场景')
                if any((axis, prop, different) not in variant_facts and (axis, prop, not different) in variant_facts
                       for axis, prop, different in _variant_comparisons(clause)):
                    finding('CONFIRMED_VARIANT_CONFLICT', '口播对款式的比较与该项已确认的款式资料冲突')
                for feature in sorted(fixed_features & _asserted_features(clause)):
                    finding('CONFIRMED_FEATURE_CONFLICT', f'口播声称支持{feature}，商品资料有明确相反说明')
    return issues


def content_issues(product, scripts, *, require_coverage=True, sentence_findings=None,
                   verify_usage_claims=False, verify_confirmed_contradictions=False):
    if verify_confirmed_contradictions:
        return _confirmed_product_contradictions(product, scripts, sentence_findings)
    source = SIMPLIFY(str(product.get('selling_points', '')))
    positive_facts = _positive_product_text(product)
    can_fold = bool(re.search(r'(?:可|能|支持)折叠|折叠收起|折叠收纳|折叠后', positive_facts))
    thermal_prop = r'(?:保温|防烫)(?:杯|壶|瓶|桶|袋|箱|套|垫)'
    is_thermal_product = re.search(thermal_prop, SIMPLIFY(product.get('name') or ''))
    thermal_evidence = '。'.join(clause for clause in re.split(r'[。；;，,\n]', positive_facts)
        if is_thermal_product or not re.search(thermal_prop, clause))
    thermal_facts = _thermal_claims(thermal_evidence)
    product_name = SIMPLIFY(str(product.get('name') or ''))
    storage_fits = _storage_fit_targets(positive_facts, product_name, evidence=True)
    storage_fits |= {(target, False) for target, broad in storage_fits if broad}
    usage_evidence = _usage_claims(positive_facts, evidence=True) if verify_usage_claims else None
    required = required_features(product)
    issues = []
    for script in scripts:
        sentences = [SIMPLIFY(s['text']) for s in script['sentences']]
        value = ''.join(sentences)
        for index, text in enumerate(sentences):
            findings = list(_sentence_fact_findings(text, thermal_facts, storage_fits, product_name, index))
            if usage_evidence is not None:
                missing_kinds = {kind for kind, _ in _usage_claims(text) - usage_evidence}
                for kind in sorted(missing_kinds):
                    code, message = _USAGE_FINDINGS[kind]
                    findings.append({'sentence_index': index, 'code': code, 'message': message})
            for finding in findings:
                issues.append(finding["message"])
                if sentence_findings is not None and finding not in sentence_findings:
                    sentence_findings.append(finding)
        missing = [label for label, output in required if not re.search(output, value, re.I)]
        if require_coverage and missing: issues.append('遗漏输入中明确的重要特点：' + '、'.join(missing))
        if (re.search(r'不.*叠加', SIMPLIFY(str(product.get('promotion',''))))
                and (require_coverage or _has_offer_assertion(_daily_quote_detection_view(product, value)))
                and not re.search(r'不.*叠加', value)):
            issues.append('优惠存在不可叠加条件，口播必须准确说明不能与其他优惠叠加')
        if '限时' in value and '限时' not in SIMPLIFY(str(product.get('promotion',''))):
            issues.append('促销输入没有明确限时，不能把普通活动价说成限时价')
        if '浴室' in value and '浴室' not in source and '浴室' not in product.get('name',''):
            issues.append('稿件混入其他商品的浴室场景，请仅使用本商品适用场景')
        if re.search(r'比普通[^。！？]{0,25}(稳|抓地|防滑|耐磨|舒适)', value) and not re.search(r'比普通', source):
            issues.append('输入没有与普通商品的对比结果，不得说比普通拖鞋更稳或多一层抓地；只介绍本商品明确的防滑特性')
        positive_source = re.sub(r'未提供[^。；\n]*', '', source)
        for pattern, explanation in [
            (r'藏灰|不藏脏|不容易藏脏', '一体成型不能推导不藏灰或不藏脏'),
            (r'不(?:容易|易)?闷(?:出)?味|透气|不闷脚', '防臭不能推导透气或不闷脚'),
            (r'不(?:会)?影响(?:饮料|饮品|原本)[^。！？]{0,10}(?:味|口感)', '普通材质不能推导不影响饮料味道'),
        ]:
            if re.search(pattern, value) and not re.search(pattern, positive_source):
                issues.append(explanation + '；删除未知性能，仅讲输入明确特性')
        if '不磨脚' in value and '不磨脚' not in positive_source:
            issues.append('一体成型或软弹不能推导出不磨脚；删除未提供的不磨脚性能承诺')
        if (re.search(r'比薄[^。！？]{0,25}(踏实|舒服|稳)|压力分散', value)
                and not re.search(r'比薄|压力分散', positive_source)):
            issues.append('输入未确认与薄底商品的性能比较或压力分散机理，请按已知特点表达')
        if '上脚没什么重量感' in value and not re.search(r'轻便|轻盈|轻巧|上脚没什么重量感', positive_facts):
            issues.append('未确认商品轻便，请核对「上脚没什么重量感」的依据')
        if ((re.search(r'耐磨处理|不容易留污|不易留污|不容易藏脏|防污|磨损速度|鞋底很快磨(坏|薄)', value)
                and not re.search(r'耐磨处理|不容易留污|不易留污|不容易藏脏|防污|磨损速度|鞋底很快磨(坏|薄)', positive_source))
                or (_has_space_saving_assertion(value, can_fold=can_fold) and '不占地方' not in positive_source)):
            issues.append('不能把耐磨扩写为未知耐磨工艺或磨损速度、易洗扩写为防污、轻便扩写为体积不占地方；只介绍输入明确的特性')
        if re.search(r'直播间就能拍|点(?:下方|右下角)[^。！？]{0,8}(购物车|小黄车)', value):
            issues.append('当前输入没有提供已开通的下单入口，不得说直播间就能拍或指定购物车位置；改为按需要选择、合适再下单')
        if require_coverage and len(required) >= 8 and len(re.findall(r'[\u4e00-\u9fff]', value)) < 700:
            issues.append('丰富商品展开过短，请把重要卖点放入具体生活场景展开，不靠重复参数凑字')
    return issues


def narrative_lines(script):
    return [s['text'] for s in script['sentences'] if s['stage'] not in EXEMPT_STAGES and len(s['text']) >= 24]


def similarity(previous, current):
    clean = lambda values: {re.sub(r'\s', '', str(v)) for v in values if len(str(v)) >= 24}
    left, right = clean(previous), clean(current)
    shared = sorted(left & right)
    near = []
    for line in sorted(right):
        closest = max(left, key=lambda v: SequenceMatcher(None, v, line).ratio(), default='')
        score = SequenceMatcher(None, closest, line).ratio()
        if score >= 0.84:
            near.append({'previous': closest, 'current': line, 'similarity': round(score, 3)})
    return {'shared': shared, 'near': near, 'near_copy_ratio': round(len(near)/max(1, len(right)), 3),
            'pass': len(shared) <= 1 and len(near)/max(1, len(right)) <= 0.15}


def check_versions(scripts, prior_versions):
    prior = [v.get('sentences_to_avoid', []) for v in (prior_versions or [])]
    checks = []
    for script in scripts:
        lines = narrative_lines(script)
        for previous in prior:
            result = similarity(previous, lines)
            if not result['pass']: checks.append(result)
        prior.append(lines)
    return checks
