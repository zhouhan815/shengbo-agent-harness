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


def _has_space_saving_assertion(value):
    """Exclude only an explicit customer-wish question, not product claims.

    '想找地方集中又不占地方？' asks about the customer's desired outcome.
    '这款不占地方，想要吗？' still asserts a property of the product and must
    remain checked. A question mark alone never exempts a performance claim.
    """
    for sentence in re.findall(r'[^。！？!?]+[。！？!?]?', value):
        for match in re.finditer(r'不占地方', sentence):
            prefix = sentence[:match.start()]
            wish = re.search(r'(?:想(?:要|找)?|希望|需要)[^。！？!?]{0,32}$', prefix)
            customer_question = (sentence.rstrip().endswith(('？', '?')) and wish
                and not re.search(r'这款|这件|本品|本款|这台|这个|这只|这双|这套|本商品|这盒|它', sentence))
            if not customer_question:
                return True
    return False


def content_issues(product, scripts, *, require_coverage=True):
    source = SIMPLIFY(str(product.get('selling_points', '')))
    required = required_features(product)
    issues = []
    for script in scripts:
        value = SIMPLIFY(''.join(s['text'] for s in script['sentences']))
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
        if re.search(r'杯壁[^。！？]{0,8}不烫|不烫手|防烫|保温', value) and not re.search(r'保温|防烫|不烫手', positive_source):
            issues.append('输入未提供保温或防烫性能，不得说杯壁不烫手或新增保温功能；改为准确的把手或容量讲解')
        if '不磨脚' in value and '不磨脚' not in positive_source:
            issues.append('一体成型或软弹不能推导出不磨脚；删除未提供的不磨脚性能承诺')
        if re.search(r'穿上就不想脱|比薄[^。！？]{0,25}(踏实|舒服|稳)|压力分散|上脚没什么重量感', value) and not re.search(r'穿上就不想脱|比薄|压力分散|上脚没什么重量感', positive_source):
            issues.append('删除未提供的试穿结论、与薄底商品的性能比较和压力分散机理，只描述本品已有特点和用途')
        if ((re.search(r'耐磨处理|不容易留污|不易留污|不容易藏脏|防污|磨损速度|鞋底很快磨(坏|薄)', value)
                and not re.search(r'耐磨处理|不容易留污|不易留污|不容易藏脏|防污|磨损速度|鞋底很快磨(坏|薄)', positive_source))
                or (_has_space_saving_assertion(value) and '不占地方' not in positive_source)):
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
