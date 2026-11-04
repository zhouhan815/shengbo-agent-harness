"""Check the disposition of source clauses before a catalog plan can be approved."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
import re
import unicodedata

from shengbo_contracts.common import ContractError

_MAX_UNITS = 140
_STOCK_OR_PROMO = re.compile(r"直播(?:价|售价|间价)|活动(?:价|到手价)|促销价|领券|拍下直减|库存(?:为|快照|数量|扣减)|可售库存|(?:SKU|规格|款式)\s*(?:独立)?扣减|进货价|采购价|供货(?:价|渠道|库存|链接)|供应商(?:名称|库存|报价)|批发价", re.I)
_METADATA = re.compile(r"^用于.+(?:测试|演示)$|^仅供.+(?:测试|参考)$|^.+均为模拟数据$")
_HEADING = re.compile(r"^(?:#{1,6}\s*)?[\w\u3400-\u9fff]{2,20}(?:信息|规格|方式|价格|售后|问题|说明|卖点|事项|参数|清单|资料|详情)$")
_SHARED = re.compile(r"(?:所有|两个|两种|各)(?:款式|规格|SKU)(?:均|都|通用)|适用于所有")
_SHARED_PREFIX = re.compile(r"^(?:所有|两(?:个|种)|各)(?:款式|规格|sku)(?:均|都)", re.I)
_SKU_HEADER = re.compile(r"^(?:SKU(?:编号|编码)?|规格(?:型号|编号|编码))$", re.I)
_PRICE_HEADER = re.compile(r"日常(?:售)?价|常规(?:售)?价|商品售价|零售价")
_PRICE_EMPTY = r"(?:未提供|未填写|未定|待补充|待确认|未确认|暂无|未知|无报价|[-—–]+|N/?A)"
_MISSING_DAILY_PRICE_TEXT = re.compile(rf"(?:{_PRICE_HEADER.pattern})\s*(?:为|[:：])?\s*(?:{_PRICE_EMPTY})?", re.I)
_PRICE_TEXT_LABEL = re.compile(rf"{_PRICE_HEADER.pattern}|^(?:售价|价格|定价)\s*(?:为|[:：])")
_PRICE_VALUE_ONLY = re.compile(r"(?:CNY|TWD|USD|AUD|NT\$|RMB|[¥$])?\s*\d+(?:\.\d+)?\s*(?:元|块|人民币|新台币|美元|澳元)?", re.I)
_MONEY = re.compile(r"\d+(?:\.\d+)?")
_CURRENCY_DECLARATION = re.compile(r"^(?:计价)?币种(?:为|是)?(?P<value>人民币(?:cny|rmb)?|cny|rmb|美元(?:usd)?|usd|新[台臺][币幣](?:twd|ntd)?|twd|ntd|澳元(?:aud)?|aud)$")
_ALL_DAILY_PRICE_SCOPE = re.compile(r"^适用于本(?:资料|文件)(?:中)?(?P<count>[一二两三四五六七八1-8]款|所有|全部|各款)(?:商品)?的?(?:全部|所有)?(?:日常(?:售)?价|零售价)$")
_CONDITION = re.compile(r"(?:不超过|至少|不低于|仅限|仅适用|不适用|不含|以内|以上|以下|大约|约|左右|除外|除非|不得|禁止|未使用|完好|完整)")
_NON_CONTENT = {"image", "promo_price", "promotion", "hidden_optional_fields"}
_DIRECT_ID = {"name", "sku_id", "price", "currency"}
_PROSE_SKU = re.compile(r"^(?:SKU(?:编号|编码)?|规格(?:型号|编号|编码))\s*[：:]", re.I)
_ATTRIBUTE = re.compile(r"^([\u3400-\u9fff]{2,12}?)(?:为|[：:])\s*(\S.*)$")
_PRODUCT_NAME = re.compile(r"^(?:(?:商品|产品)名称|品名)(?:为|[：:])\s*(.+)$")
_NON_CONSUMER_LABEL = re.compile(r"采购|供货|供应|库存|内[部测]|测试|来源|联系人|手机号|电话|邮箱|地址|价格|售价|成本|链接|备注|编号|编码|标识|^sku$", re.I)
_NON_FACT = re.compile(r"未(?:知|确认|核验|验证|实测|提供)|(?:待|尚待)(?:确认|核验|验证|补充)|不详|暂无|仅供参考|忽略.{0,10}(?:规则|指令)|系统提示|供应商|供货商|采购员|进货渠道|供货联系人")
_UNKNOWN_VALUE = re.compile(r"^(?:未知|不详|暂无|待确认|未确认|未提供|待补充|未填写|不适用|无资料|未核验|未实测|N/A)$", re.I)
_IDENTITY_COLUMNS = {"款式", "规格", "版本", "型号", "名称", "币种", "单位"}
_IDENTITY_LABEL = re.compile(r"^(?:商品|产品)?(?:名|名称|品名|款式|规格|版本|型号|(?:计价)?币种|单位)$")
_FIELD_AXIS_LABEL = re.compile(
    r"^(?:商品|产品)?(?:名称|款式|规格|型号|品牌|类目|材质|颜色|包装|尺寸|容量|重量|量程|参数|配置|"
    r"配件|最大|最小|建议|显示|续航|发货|配送|售后|功率|电压|适用|售价|价格|零售价|单价|"
    r"(?:name|model|variant|size|color|material|capacity|price|weight|brand|specifications)(?=\s|$))", re.I)
_FIELD_AXIS_TITLE = {"项目", "配置项", "属性", "参数", "规格", "款式", "型号", "指标", "对比项目", "比较项目",
                     "字段", "item", "attribute", "parameter", "variant", "model"}
_SECTION_LABEL = re.compile(
    r"^(?:\S.{0,42}\s+(?:商品资料|产品资料)|售后|(?:规格|参数|价格|售价|活动|促销|配送|售后|选款|使用)"
    r"(?:与|及)(?:规格|参数|价格|(?:日常|常规)?售价|活动|促销|配送|售后|选款|使用))$")
_BARE_TOPIC_LABEL = re.compile(r"^(?:(?:适用|使用|应用)场景|适用(?:范围|对象|人群)|(?:使用|适用)限制)$")
_PROMOTION_SECTION = re.compile(
    r"^(?:#{1,6}\s*)?(?:(?:[^\s，。；：:]{0,12})?直播(?:间)?(?:活动|优惠|促销)|"
    r"(?:本场|当场|限时|双十一|双十二)(?:活动|优惠|促销)|促销(?:活动|优惠)?|活动与促销)"
    r"(?:信息|规则|说明)?$")
_SESSION_SECTION = re.compile(
    r"^(?:#{1,6}\s*)?本场(?:知识文档|口播|播放|话术|演示|配置|直播设置|直播排期)"
    r"(?:(?:与|及|和)(?:知识文档|口播|播放|话术|演示|配置|直播设置|直播排期))?(?:安排|说明|要求)?$")
SESSION_SOURCE_REASONS = {"session_promotion", "session_configuration"}
_MARKDOWN_SECTION = re.compile(r"^#{1,6}\s+\S")
_FAQ_LABEL = re.compile(r"(^|[\n?？])\s*(?:问(?:题)?|答(?:案)?|[QA])\s*[：:]", re.I)
_LABELED_FAQ = re.compile(r"^\s*(?:问(?:题)?|Q)\s*[：:]\s*[^？?]+[？?]\s*(?:答(?:案)?|A)\s*[：:]", re.I)
_LABELED_IDENTITY = re.compile(r"^(?:商品|产品)?(品牌|类目)(?:为|[：:]\s*|\s+)\s*(\S.*)$")
_FAQ_SHARED_ANSWER = re.compile(r"^(?:两款|各款|所有款式|所有规格)(?:均|都)")
_TYPED_SOURCE_LABELS = {
    "usage_instructions": re.compile(r"^(?:使用(?:限制|方法|说明|注意事项)|操作说明)\s*[：:]\s*"),
    "return_policy": re.compile(r"^(?:售后(?:政策|说明)?|退货(?:政策|说明|条件)?|退换货(?:政策|说明|条件)?)\s*[：:]\s*"),
    "material": re.compile(r"^(?:材质|材料|面料)\s*[：:]\s*"),
}
_SHARED_COMPONENT_MATERIAL = re.compile(
    r"^(?:两款|各款|所有款式|所有规格)[\u3400-\u9fff]{1,10}(?:身|盖|壳|面|体|层|芯|板|柄|垫|胆)"
    r"(?:均|都)为(?:聚[\u3400-\u9fff]{1,10}(?:烯|酯|醚|胺)|不锈钢|硅胶|玻璃|陶瓷|涤纶|尼龙|塑料)$")
_PRODUCT_SOURCE_TITLE = re.compile(r"^(\S.{0,42}?)\s+(?:商品资料|产品资料)$")
_PRODUCT_MARKED_TITLE = re.compile(r"^(\S.{0,42}?)\s*(?:商品资料|产品资料)$")
_GENERIC_FAMILY_TITLE = re.compile(r"^(?:本次|本场|本批|本店|商家|店铺|商品|产品|多个|多款|全部|汇总|清单|导入|"
                                   r"样例|示例|测试|附件|资料|通用|原始|合集|文档)(?:的)?$")
_SHARED_VARIANT_SENTENCE = re.compile(r"^(?:两款|各款|每款|所有款式|(?:两种|各|每|所有)规格|两种款式)(?:均|都|通用)")
_FREIGHT = re.compile(r"运费|包邮|邮费|免邮|配送(?:范围|地区|费用)|不(?:予)?配送")
_RETURN = re.compile(r"退[换货]|退款|售后")
_SHIPPING_ORIGIN = re.compile(r"^(?:模拟)?(?:发货地|发货仓所在地)(?:为|是|[:：])\s*(\S.*)$")
_SHIPPING_DEADLINE = re.compile(
    r"^(?:(?:下单|付(?:款|清款项)|支付|订单确认|订单支付)(?:成功)?后\s*)?"
    r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百]+)\s*(?:个\s*)?"
    r"(?:小时|工作日|天)(?:以|之)?内(?:安排)?(?:发货|寄出)$")
_EXCEPTION_END = re.compile(r"(?:除外|不适用|不参与|不包邮|不免运费)[。；;]?$")
_PERMITTED_WHEN = re.compile(r"^可在(.{1,60}?)(?:条件下|时)(.+)$")
_BINARY_STATEMENT = re.compile(r"^(不支持|不允许|禁止|不可|不能|支持|允许|可以|可|能)(.+)$")
_NEGATIVE = {"不支持", "不允许", "禁止", "不可", "不能", "否", "不可以"}
_POSITIVE = {"支持", "允许", "可以", "可", "能", "是"}


def _norm(text):
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    return re.sub(r"[\s，,。；;？?！!：:、()（）+]+", "", text)


def _split_outside_brackets(text, delimiters):
    closing = {"(": ")", "（": "）", "[": "]", "【": "】"}
    stack, start = [], 0
    for index, char in enumerate(text):
        if char in closing:
            stack.append(closing[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif not stack and char in delimiters:
            yield text[start:index + 1]
            start = index + 1
    if start < len(text):
        yield text[start:]


def _parts_with_spans(text):
    if _LABELED_FAQ.match(text) or re.search(r"[？?][^\n]+", text):
        yield text.strip(), 0, len(text)
        return
    cursor = 0
    for sentence in _split_outside_brackets(text, "。！？；;\n"):
        # Keep condition fragments attached to their predicate. Commas between
        # independently stated facts still create separate accountable units.
        pending = ""
        start = cursor
        parts = list(_split_outside_brackets(sentence, "，,"))
        for index, piece in enumerate(parts):
            offset = cursor
            cursor += len(piece)
            if not piece.strip(" \t\r\n，,。！？；;"):
                continue
            if not pending:
                start = offset
            pending += piece
            following = parts[index + 1].lstrip() if index + 1 < len(parts) else ""
            if (re.search(r"(?:后|前|内|时|下|起|若|如|如果)\s*[，,]$", pending.rstrip())
                    or re.match(r"^(?:才|方可|否则)", following)
                    or _EXCEPTION_END.search(following.rstrip("，,"))):
                continue
            yield pending.strip().rstrip("，,。；;"), start, cursor
            pending = ""
        if pending:
            yield pending.strip().rstrip("，,。；;"), start, cursor


def _parts(text):
    return [piece for piece, _, _ in _parts_with_spans(text)]


def has_daily_price_evidence(unit, sku_ids):
    """An explicitly labelled amount or uncertainty cannot become a missing price."""
    if not sku_ids or unit.get("excluded_reason") or (unit.get("sku_id") and unit["sku_id"] not in sku_ids):
        return False
    text = unicodedata.normalize("NFKC", unit["text"])
    return bool(_PRICE_HEADER.search(unit.get("column", "")) or (
        _PRICE_TEXT_LABEL.search(text) and (_MONEY.search(text) or _NON_FACT.search(text))))


def daily_price_evidence_unit(unit, *, allow_value_only):
    if unit.get("excluded_reason"):
        return False
    if column := unit.get("column"):
        return bool(_PRICE_HEADER.search(column))
    text = unicodedata.normalize("NFKC", unit["text"])
    return bool(_PRICE_TEXT_LABEL.search(text) or allow_value_only and _PRICE_VALUE_ONLY.fullmatch(text))


def _field_axis_label(label):
    return (label in _IDENTITY_COLUMNS or _PRICE_HEADER.search(label)
            or _STOCK_OR_PROMO.search(label) or _FIELD_AXIS_LABEL.search(label))


def _table_layout(rows):
    """Locate an explicit SKU axis before assigning any table cell to a product."""
    axes = [(index, cell["grid_column"]) for index, row in enumerate(rows)
            for cell in row["cells"] if _SKU_HEADER.fullmatch(cell["text"].strip())]
    if len(axes) != 1:
        return None
    index, axis = axes[0]
    anchor = rows[index]["cells"]
    axis_cell = next(cell for cell in anchor if cell["grid_column"] == axis)
    other = {cell["grid_column"]: cell["text"].strip() for cell in anchor
             if cell["grid_column"] != axis and cell["text"].strip()}
    if not other or axis_cell.get("column_span", 1) != 1:
        return None
    row_layout = any(_field_axis_label(label) for label in other.values())
    labels_below = [cell["text"].strip() for row in rows[index + 1:] for cell in row["cells"]
                    if cell["grid_column"] == axis and cell["text"].strip()]
    title_above = index > 0 and any(
        cell["grid_column"] == axis and cell["text"].strip().casefold() in _FIELD_AXIS_TITLE
        for cell in rows[index - 1]["cells"]
    )
    column_layout = any(_field_axis_label(label) for label in labels_below) or (title_above and bool(labels_below))
    if row_layout == column_layout:
        return None
    if row_layout:
        return "rows", index, axis, {**other, axis: axis_cell["text"].strip()}, None
    if (len(set(sku.casefold() for sku in other.values())) != len(other)
            or any(cell.get("column_span", 1) != 1 for cell in anchor)):
        return None
    return "columns", index, axis, other, index - 1 if title_above else None


def _bare_topic_heading(location):
    return bool(location.get("section_label") is True and not location.get("cells")
                and location.get("kind") not in {"image", "table_row"}
                and _BARE_TOPIC_LABEL.fullmatch(location["text"].strip()))


def build_source_units(locations):
    """Use existing exact locations and keep each table value tied to its header."""
    units = []
    tables = defaultdict(list)
    for location in locations:
        if location.get("cells"):
            tables[location["source_ref"]["id"], location.get("table_id")].append(location)
    layouts = {key: _table_layout(rows) for key, rows in tables.items()}
    row_positions = {id(row): index for rows in tables.values() for index, row in enumerate(rows)}

    def add(location, text, **meta):
        if text.strip():
            units.append({"id": f"U{len(units) + 1}", "location": location["location"],
                          "source_ref": location["source_ref"], "locator": location["locator"],
                          "text": text, **meta})

    def add_cell(location, cell, label, sku):
        value = cell["text"]
        meta = {"column": label}
        if sku:
            meta["sku_id"] = sku
        if _STOCK_OR_PROMO.search(label):
            meta["excluded_reason"] = "out_of_scope"
        elif _PRICE_HEADER.search(label) and re.fullmatch(_PRICE_EMPTY, value.strip(), re.I):
            meta["excluded_reason"] = "missing_daily_price"
        elif (label not in _IDENTITY_COLUMNS and not _SKU_HEADER.fullmatch(label)
              and not _PRICE_HEADER.search(label) and _UNKNOWN_VALUE.fullmatch(value.strip())):
            meta["excluded_reason"] = "unresolved_optional"
        add(location, value, **meta)

    from .document_import import _session_source_role

    session_reason = None
    session_heading_level = None
    last_source = None
    for location in locations:
        text = location["text"].strip()
        cells = location.get("cells")
        if location["source_ref"] != last_source:
            session_reason = None
            session_heading_level = None
            last_source = location["source_ref"]
        if not cells and (location.get("section_label") or _SECTION_LABEL.fullmatch(text)
                           or _MARKDOWN_SECTION.match(text) or _SESSION_SECTION.fullmatch(text)
                           or _PROMOTION_SECTION.fullmatch(text)):
            heading_level = location.get("heading_level")
            if _PROMOTION_SECTION.fullmatch(text):
                session_reason = "session_promotion"
                session_heading_level = heading_level
            elif _SESSION_SECTION.fullmatch(text):
                session_reason = "session_configuration"
                session_heading_level = heading_level
            elif not (session_heading_level and heading_level and heading_level > session_heading_level):
                session_reason = None
                session_heading_level = None
            if session_reason:
                add(location, text, excluded_reason=session_reason)
                continue
        if session_reason:
            pieces = [text] if cells else _parts(text)
            for piece in pieces:
                add(location, piece, excluded_reason=session_reason)
            continue
        if not cells and (role := _session_source_role(text)):
            add(location, text, excluded_reason="session_promotion" if role == "window" else "session_configuration")
            continue
        if cells:
            key = (location["source_ref"]["id"], location.get("table_id"))
            layout = layouts[key]
            position = row_positions[id(location)]
            if layout:
                direction, anchor, axis, labels, variant_row = layout
                leading = next((cell["text"].strip() for cell in cells if cell["grid_column"] == axis), "")
                if direction == "rows" and position == anchor:
                    add(location, text, excluded_reason="metadata")
                    continue
                if direction == "rows" and position > anchor:
                    for cell in cells:
                        add_cell(location, cell, labels.get(cell["grid_column"], ""), leading)
                    continue
                if direction == "columns" and leading and (position >= anchor or position == variant_row):
                    values = [cell for cell in cells if cell["grid_column"] != axis and cell["text"].strip()]
                    if values and all(cell["grid_column"] in labels and cell.get("column_span", 1) == 1
                                      and not cell.get("vertical_merge") for cell in values):
                        for cell in values:
                            add_cell(location, cell, "款式" if position == variant_row else leading,
                                     labels[cell["grid_column"]])
                        continue
            # Keep unclassified rows intact for the Agent, without a guessed SKU.
            add(location, text)
            continue
        if (_bare_topic_heading(location) or _SECTION_LABEL.fullmatch(text) or location.get("section_label")
                and 1 <= len(text) <= 60
                and not re.search(r"[\d：:。！？；|\n]|^(?:不|仅|可|需|禁|含|无|有|支持|适用)", text)
                and not _CONDITION.search(text)):
            add(location, text, excluded_reason="metadata")
            continue
        for piece in _parts(text):
            meta = {}
            attribute = _ATTRIBUTE.fullmatch(_SHARED_PREFIX.sub("", piece))
            if _METADATA.fullmatch(piece):
                meta["excluded_reason"] = "metadata"
            elif _STOCK_OR_PROMO.search(piece) and not _CONDITION.search(piece):
                meta["excluded_reason"] = "out_of_scope"
            elif _MISSING_DAILY_PRICE_TEXT.fullmatch(piece):
                meta["excluded_reason"] = "missing_daily_price"
            elif _HEADING.fullmatch(piece) and not re.search(r"(?:不|可|需|禁|含|仅|已|约|无|有|\d)", piece):
                meta["excluded_reason"] = "metadata"
            elif (attribute and not _PRODUCT_NAME.fullmatch(piece)
                    and attribute[1] not in _IDENTITY_COLUMNS and not _NON_CONSUMER_LABEL.search(attribute[1])
                    and _UNKNOWN_VALUE.fullmatch(attribute[2])):
                meta["excluded_reason"] = "unresolved_optional"
            elif _SHARED.search(piece):
                meta["fixed_scope"] = "all_skus"
            add(location, piece, **meta)
    return units if 0 < len(units) <= _MAX_UNITS else None


def _verbatim(value):
    if isinstance(value, str):
        return [_norm(re.sub(r"(?:^|\n)\s*(?:问|答)[：:]", "\n", value))]
    if isinstance(value, dict):
        if {"name", "value"} <= value.keys():
            return [_norm(value["name"]) + _norm(value["value"]), _norm(value["value"])]
        return [_norm(value["content"])] if isinstance(value.get("content"), str) else []
    if isinstance(value, list):
        return [part for item in value for part in _verbatim(item)]
    return [_norm(value)] if isinstance(value, (int, float)) else []


def _contains(value, source):
    text = _norm(source)
    text = re.sub(r"^模拟(?=发货地)", "", text)
    unscoped = _SHARED_PREFIX.sub("", text)
    text = unscoped.removeprefix("为") if unscoped != text else text
    attribute = _ATTRIBUTE.fullmatch(text)
    forms = {text, re.sub(r"^可在(.{1,28}?(?:以内|之内|内|之后|之前|时))(?=[\u3400-\u9fff])",
                          r"\1可", text)}
    if attribute:
        forms.add("".join(attribute.groups()))
    if text.startswith("采用"):
        forms.add(text.removeprefix("采用"))
    for part in _verbatim(value):
        for form in forms - {""}:
            if re.search(r"(?<![不非无未])(?<!禁止)" + re.escape(form), part):
                return True
    return False


def _restore_conditional_wording(value, source):
    """Restore an explicit permission when the draft elides only its grammar."""
    if not isinstance(value, str):
        return None
    statement = _SHARED_PREFIX.sub("", _norm(source))
    match = _PERMITTED_WHEN.fullmatch(statement)
    if not match or not _CONDITION.search(match[1]):
        return None
    short_form = "".join(match.groups())
    for piece in _parts(value):
        if _norm(piece) == short_form:
            return value.replace(piece, source, 1)
    return None


def _numbers(text):
    return _MONEY.findall(unicodedata.normalize("NFKC", str(text)))


def _matches(row, field, unit):
    value = row.get(field)
    source = unit["text"]
    if field == "faq" and isinstance(value, str):
        value = _FAQ_LABEL.sub(r"\1", value)
        source = _FAQ_LABEL.sub(r"\1", source)
    if field in _TYPED_SOURCE_LABELS:
        source = _TYPED_SOURCE_LABELS[field].sub("", source)
    if field == "price":
        amounts = _numbers(source)
        return daily_price_evidence_unit(unit, allow_value_only=True) and len(amounts) == 1 and float(amounts[0]) == value
    if _contains(value, source):
        return True
    if field == "currency" and value in {"CNY", "USD", "TWD", "AUD"}:
        if declared := _declared_currency(source):
            return declared == value
        labels = {"CNY": "人民币", "USD": "美元", "TWD": "新台币", "AUD": "澳元"}
        return bool(re.fullmatch(r"(?:(?:以上|上述|以下|全部|所有)?(?:金额|价格|售价|标价)?|计价币种)"
                                 r"(?:均|皆|都)?(?:为|以|采用|使用)?" + labels[value]
                                 + r"(?:计价|结算)?", _norm(source)))
    if field in {"name", "shipping_origin", "material"} and value:
        match = re.fullmatch(r"(?:模拟)?[\u3400-\u9fff]{1,12}[为：:](.+)", source)
        return bool(match and not _CONDITION.search(source) and _norm(match[1]) in _norm(value))
    return False


def _restore_spec_qualifiers(row, unit):
    source = unit["text"]
    if not _numbers(source):
        return False
    # A single matching specification may safely retain the complete clause.
    # Require both a value match and a label anchor to avoid same-number joins.
    matches = [item for item in row.get("specifications", [])
               if isinstance(item, dict) and isinstance(item.get("name"), str)
               and isinstance(item.get("value"), str) and _numbers(item["value"]) == _numbers(source)
               and len(_norm(item["name"])) >= 2 and _norm(item["name"])[:2] in _norm(source)
               and _norm(item["value"]) in _norm(source)]
    if len(matches) == 1:
        matches[0]["value"] = source
        return True
    return False


def _missing(issues, **metrics):
    raise ContractError("CATALOG_COVERAGE_INCOMPLETE", "有原文信息未能核对入库去向", status=422,
                        details={"issues": issues[:20], "issue_count": len(issues), **metrics})


def _shared_attribute(unit, peers, candidates, variants):
    """Recover an explicit attribute when the same paragraph names the product family."""
    match = _ATTRIBUTE.fullmatch(unit["text"])
    if not match or unit.get("sku_id") or not _is_consumer_label(match[1]):
        return None
    anchors = [found[1] for peer in peers
               if (found := _PRODUCT_NAME.fullmatch(peer["text"]))]
    if len(anchors) != 1:
        return None
    family = _norm(anchors[0])
    for sku, row in candidates.items():
        name = _norm(row["name"])
        suffix = name.removeprefix(family).lstrip("-—")
        if not name.startswith(family) or (suffix and suffix not in variants.get(sku, set())):
            return None
    if any(re.search(r"[A-Za-z0-9]+[-_][A-Za-z0-9]+", sku) and sku in unit["text"] for sku in candidates):
        return None
    return match[1], match[2]


def _freight_clause(text):
    return bool(_FREIGHT.search(text) and not _RETURN.search(text))


def _without_freight_clauses(value):
    # Keep the punctuation within each surviving stretch of original text.
    # _parts splits factual clauses for coverage and omits their delimiters.
    blocks = []
    start, retained_end = 0, 0
    for piece, offset, end in _parts_with_spans(value):
        if _freight_clause(piece):
            if start is not None and start < retained_end:
                blocks.append(value[start:retained_end])
            start = None
        else:
            if start is None:
                start = offset
            retained_end = end
    if start is not None:
        blocks.append(value[start:])
    elif blocks:
        blocks[-1] = blocks[-1].rstrip(" \t\r\n，,；;")
    return "".join(blocks)


def _fulfillment_destination(source, fields):
    text = _SHARED_PREFIX.sub("", source)
    if "shipping_origin" in fields and (match := _SHIPPING_ORIGIN.fullmatch(text)):
        if not _CONDITION.search(match[1]) and not _NON_FACT.search(match[1]):
            return "shipping_origin", match[1]
    if "shipping_time" in fields and _SHIPPING_DEADLINE.fullmatch(text):
        return "shipping_time", text
    return None


def _family_is_proven(anchors, candidates, variants):
    if len(anchors) != 1 or set(variants) != candidates.keys():
        return False
    family = _norm(next(iter(anchors)))
    return all(_norm(row["name"]).startswith(family)
               and _norm(row["name"]).removeprefix(family).lstrip("-—") in variants[sku]
               for sku, row in candidates.items())


def source_family_anchor(locations, units, variant_units, *, candidates=None):
    """Return the exact family label above the source's single authoritative SKU table."""
    if not locations or not variant_units:
        return None
    indexed = {block["location"]: block for block in locations}
    heading = locations[0]
    if any(block["source_ref"] != heading["source_ref"] for block in locations):
        return None
    variants = list(variant_units.values() if isinstance(variant_units, dict) else variant_units)
    eligible = list(units.values() if isinstance(units, dict) else units)
    table_ids = {indexed[unit["location"]].get("table_id") for unit in variants}
    sku_units = [unit for unit in eligible if unit.get("sku_id") and _SKU_HEADER.fullmatch(unit.get("column", ""))]
    if (None in table_ids or len(table_ids) != 1 or not sku_units
            or {indexed[unit["location"]].get("table_id") for unit in sku_units} != table_ids
            or {unit["sku_id"] for unit in sku_units} != {unit.get("sku_id") for unit in variants}):
        return None
    table_positions = {block["location"]: index for index, block in enumerate(locations)}
    first_sku_row = min(table_positions[unit["location"]] for unit in sku_units + variants)
    names = [(found[1].strip(), unit) for unit in eligible if not indexed[unit["location"]].get("cells")
             and not unit.get("excluded_reason") and (found := _PRODUCT_NAME.fullmatch(unit["text"]))]
    if names:
        if len({_norm(name) for name, _ in names}) != 1:
            return None
        return next(((name, unit["location"], unit["text"]) for name, unit in names
                     if table_positions[unit["location"]] < first_sku_row and not _NON_FACT.search(name)), None)
    text = heading["text"].strip()
    title = (_PRODUCT_MARKED_TITLE if heading.get("section_label") else _PRODUCT_SOURCE_TITLE).fullmatch(text)
    if (title and not _GENERIC_FAMILY_TITLE.fullmatch(title[1]) and not _NON_FACT.search(title[1])
            and not any(_PRODUCT_MARKED_TITLE.fullmatch(block["text"].strip()) for block in locations[1:]
                        if block.get("section_label"))):
        return title[1], heading["location"], text
    if candidates and heading.get("section_label") and not heading.get("cells") and not _NON_FACT.search(text):
        brands = {match[2] for unit in eligible if table_positions[unit["location"]] < first_sku_row
                  and not unit.get("excluded_reason") and (match := _ATTRIBUTE.fullmatch(unit["text"]))
                  and match[1] == "品牌"}
        expected = defaultdict(set)
        for unit in variants:
            expected[unit["sku_id"]].add(_norm(unit["text"]))
        if (len(brands) == 1 and not _GENERIC_FAMILY_TITLE.fullmatch(text)
                and _norm(text).startswith(_norm(next(iter(brands))))
                and _family_is_proven({text}, candidates, expected)):
            return text, heading["location"], text
    return None


def _shared_family_introduction(locations, candidates, peers, variants, *, confirmed_family=None):
    """Recognize shared opening facts of a titled document with one proven SKU table."""
    if len(candidates) < 2 or not locations:
        return set()
    heading, *blocks = locations
    title = _PRODUCT_SOURCE_TITLE.fullmatch(heading["text"].strip())
    tables = [block for block in blocks if block.get("cells")]
    if (not (title or confirmed_family) or not tables or len({block.get("table_id") for block in tables}) != 1
            or any(block["source_ref"] != heading["source_ref"] for block in blocks)
            or set(variants) != candidates.keys()):
        return set()
    family = _norm(title[1] if title else confirmed_family)
    if any(_norm(row["name"]).removeprefix(family).lstrip("-—") not in variants[sku]
           for sku, row in candidates.items()):
        return set()
    introductions = set()
    full_names_match = all(_norm(row["name"]).startswith(family) for row in candidates.values())
    first_fact_block = True
    for block in blocks:
        if block.get("cells"):
            break
        location = block["location"]
        consumer = [unit for unit in peers[location] if not unit.get("excluded_reason")]
        if not consumer:
            continue
        # A brand/category declared in the opening of a proven single-family
        # document applies to its variants even when the nearby prose does
        # not explicitly start with “两款均”.
        declared_identity = full_names_match and first_fact_block and any(
            _LABELED_IDENTITY.fullmatch(unit["text"]) for unit in consumer)
        if declared_identity or any(_SHARED_VARIANT_SENTENCE.match(unit["text"]) for unit in consumer):
            introductions.add(location)
        first_fact_block = False
    return introductions


def _is_consumer_label(label):
    return not (_IDENTITY_LABEL.fullmatch(label) or _NON_CONSUMER_LABEL.search(label))


def _source_destination(unit, candidates, peers, variants, family_is_proven):
    """Recover only facts whose applicable products can be proven from the source."""
    text, column = unit["text"], unit.get("column", "")
    if (_NON_FACT.search(text) or _STOCK_OR_PROMO.search(text)
            or re.search(r"[？?]", text)):
        return None
    freight = _freight_clause(text)
    if unit.get("sku_id"):
        sku = unit["sku_id"]
        if (sku not in candidates or not column or _SKU_HEADER.fullmatch(column)
                or _PRICE_HEADER.search(column) or not _is_consumer_label(column)):
            return None
        return [sku], None if freight else (column, text)
    shared = bool(unit.get("fixed_scope") == "all_skus" and _SHARED_PREFIX.match(text))
    if (any(sku.casefold() in text.casefold() for sku in candidates)
            or any(variant in _norm(text) for names in variants.values() for variant in names)
            or shared and re.search(r"除(?:了)?[^，,。]{1,18}(?:款|型|sku)", text, re.I)):
        return None
    unscoped = len(candidates) == 1 or family_is_proven
    attribute = _shared_attribute(unit, peers, candidates, variants)
    if not attribute and (shared or unscoped):
        match = _ATTRIBUTE.fullmatch(_SHARED_PREFIX.sub("", text))
        if (match and _is_consumer_label(match[1])
                and not _PRODUCT_NAME.fullmatch(text)
                and not re.search(r"^(?:以上|上述|以下|全部|所有)?(?:金额|币种|售价)", match[1])):
            attribute = match.groups()
    if (freight or _fulfillment_destination(text, {"shipping_origin", "shipping_time"})) and (shared or unscoped):
        return list(candidates), None
    if attribute and (shared or unscoped):
        return list(candidates), attribute
    if shared:
        return list(candidates), None
    return None


def _clean_candidate_fields(candidates, evidence, units):
    has_freight = any(not unit.get("excluded_reason") and _freight_clause(unit["text"]) for unit in units)
    emptied = set()
    for sku, row in candidates.items():
        for field, value in row.items():
            if field not in _DIRECT_ID and isinstance(value, str) and _UNKNOWN_VALUE.fullmatch(value.strip()):
                row[field] = ""
                emptied.add((sku, field))
        specs = row.get("specifications")
        if isinstance(specs, list):
            retained = [spec for spec in specs if not (isinstance(spec, dict)
                        and _UNKNOWN_VALUE.fullmatch(str(spec.get("value", "")).strip()))]
            if retained != specs:
                row["specifications"] = retained
                if not retained:
                    emptied.add((sku, "specifications"))
        if not has_freight:
            continue
        for field in ("shipping_time", "return_policy", "shipping_origin"):
            value = row.get(field)
            if not isinstance(value, str) or not _FREIGHT.search(value):
                continue
            kept = _without_freight_clauses(value)
            if kept != value:
                row[field] = kept
                if not kept:
                    emptied.add((sku, field))
    return [{**item, "fields": fields} for item in evidence
            if (fields := [field for field in item["fields"] if (item["sku_id"], field) not in emptied])]


def _spec_polarity_conflicts(row, source):
    match = _BINARY_STATEMENT.fullmatch(_SHARED_PREFIX.sub("", _norm(source)))
    specs = row.get("specifications")
    if not match or not isinstance(specs, list):
        return False
    predicate, label = match.groups()
    opposites = _POSITIVE if predicate in _NEGATIVE else _NEGATIVE
    return any(isinstance(spec, dict) and _norm(spec.get("name", "")) == label
               and _norm(spec.get("value", "")) in opposites for spec in specs)


def _evidence_location(item):
    ref = item["source_ref"]
    return ref["id"], ref.get("revision"), ref.get("content_hash"), item["locator"]


def _declared_currency(text):
    from .catalog_extract import _explicit_currencies

    if _CURRENCY_DECLARATION.fullmatch(_norm(text)):
        codes = _explicit_currencies(text)
        if len(codes) == 1:
            return next(iter(codes))
    return None


def _scoped_currency_units(locations, units, candidates, evidence=None):
    """Reconcile a split declaration and its literal, document-wide daily-price scope."""
    from .catalog_extract import _explicit_currencies

    peers = defaultdict(list)
    for unit in units.values():
        if not unit.get("excluded_reason"):
            peers[_evidence_location(unit)].append(unit)
    for location in locations:
        coordinate = _evidence_location(location)
        group = peers.get(coordinate, [])
        if len(group) != 2:
            continue
        declaration, applicability = group
        code = _declared_currency(declaration["text"])
        scope = _ALL_DAILY_PRICE_SCOPE.fullmatch(_norm(applicability["text"]))
        if not code or not scope or any(row.get("currency") != code for row in candidates.values()):
            continue
        count = scope["count"].removesuffix("款")
        count = {"一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8"}.get(count, count)
        if count.isdecimal() and int(count) != len(candidates):
            continue
        if not re.fullmatch(re.escape(declaration["text"]) + r"[，,；;]\s*" + re.escape(applicability["text"]) + r"[。.]?",
                            location["text"].strip()):
            continue
        local_units = [unit for unit in units.values() if _evidence_location(unit)[:-1] == coordinate[:-1]]
        explicit = set().union(*(_explicit_currencies(unit["text"]) for unit in local_units))
        if explicit != {code} or {u["sku_id"] for u in local_units if u.get("sku_id")} != candidates.keys():
            continue
        price_units = {u["sku_id"] for u in local_units if u.get("sku_id") in candidates
                       and _matches(candidates[u["sku_id"]], "price", u)}
        confirmed = candidates.keys() if evidence is None else {item["sku_id"] for item in evidence if _evidence_location(item) == coordinate
                     and "currency" in item["fields"] and declaration["text"] in item.get("quote", "")}
        if price_units == confirmed == candidates.keys():
            return {declaration["id"], applicability["id"]}
    return set()


def _conflicting_scoped_measurement(row, unit):
    column = unit.get("column", "")
    specs = row.get("specifications")
    if not column or not _is_consumer_label(column) or not isinstance(specs, list):
        return False
    source = _norm(unit["text"])
    source_numbers = _MONEY.findall(source)
    if not source_numbers:
        return False
    source_shape = _MONEY.sub("#", source)
    for spec in specs:
        if not isinstance(spec, dict) or _norm(spec.get("name", "")) != _norm(column):
            continue
        value = _norm(spec.get("value", ""))
        if (_MONEY.sub("#", value) == source_shape
                and list(map(Decimal, _MONEY.findall(value))) != list(map(Decimal, source_numbers))):
            return True
    return False


def _reclassify_audience(candidates, units, submitted, evidence, session_locations, belongs):
    """Keep only a provable permanent audience statement when session scope was mixed in."""
    repaired = {}
    for sku, row in candidates.items():
        if not isinstance(row.get("target_audience"), str):
            continue
        links = [item for item in evidence if item["sku_id"] == sku and "target_audience" in item["fields"]]
        permanent = [item for item in links if _evidence_location(item) not in session_locations]
        if len(permanent) == len(links):
            continue
        mapped = [units[key] for key, item in submitted.items() if item.get("disposition") == "retained" and any(
            isinstance(target, dict) and target.get("sku_id") in {sku, "*"} and target.get("field") == "target_audience"
            for target in item.get("targets") or [])]
        if not permanent and not mapped:
            row.pop("target_audience")
            repaired[sku] = None
        elif len(mapped) == 1:
            unit, = mapped
            text = unit["text"]
            if (permanent and belongs(unit, sku) and not re.search(r"[?？]", text)
                    and not _NON_FACT.search(text) and not _STOCK_OR_PROMO.search(text)
                    and all(_evidence_location(item) == _evidence_location(unit) and text in item.get("quote", "")
                            for item in permanent)):
                row["target_audience"] = text
                repaired[sku] = unit
    if not repaired:
        return evidence, repaired
    evidence = [{**item, "fields": fields} for item in evidence if (fields := [field for field in item["fields"]
        if field != "target_audience" or item["sku_id"] not in repaired])]
    for sku, unit in repaired.items():
        if unit:
            evidence.append({"sku_id": sku, "fields": ["target_audience"], "source_ref": unit["source_ref"],
                             "locator": unit["locator"], "quote": unit["text"]})
    return evidence, repaired


def _source_faq(text):
    """Accept one literal question followed by its answer in a single source unit."""
    if len(re.findall(r"[？?]", text)) != 1:
        return None
    question, answer = re.split(r"(?<=[？?])", text, maxsplit=1)
    question = re.sub(r"^\s*(?:问(?:题)?|Q)\s*[：:]\s*", "", question, flags=re.I).strip()
    answer = re.sub(r"^\s*(?:答(?:案)?|A)\s*[：:]\s*", "", answer, flags=re.I).strip()
    if (len(question) < 5 or len(answer) < 5 or "\n" in question or "\n" in answer
            or _NON_FACT.search(answer) or _STOCK_OR_PROMO.search(answer)):
        return None
    return question, answer


def _labeled_faq_pairs(value):
    blocks = re.split(r"\r?\n\s*\r?\n(?=\s*(?:问(?:题)?|Q)\s*[：:])", value, flags=re.I)
    pairs = [_source_faq(block) if _LABELED_FAQ.match(block) else None for block in blocks]
    if all(pairs):
        return {tuple(map(_norm, pair)) for pair in pairs}
    return set()


def _required_source_unit(unit):
    text, column = unit["text"], unit.get("column", "")
    return bool(_SKU_HEADER.fullmatch(column) or _IDENTITY_LABEL.fullmatch(column)
                or _PRODUCT_NAME.fullmatch(text) or _PROSE_SKU.match(text)
                or has_daily_price_evidence(unit, {unit.get("sku_id")}) or _declared_currency(text))


def apply_coverage(prepared, rows, coverage, evidence, *, allow_optional_gaps=False):
    """Reconcile all source units and copy safe consumer facts before approval."""
    original_locations = {location["location"]: location for location in prepared.get("locations", [])}
    topic_locations = {location["location"]: location for location in prepared.get("locations", [])
                       if _bare_topic_heading(location)}
    units = {}
    for unit in prepared["source_units"]:
        location = topic_locations.get(unit["location"])
        if (location and not unit.get("excluded_reason") and _evidence_location(location) == _evidence_location(unit)
                and location["text"].strip() == unit["text"]):
            unit = {**unit, "excluded_reason": "metadata"}
        units[unit["id"]] = unit
    candidates = {row["sku_id"]: row for row in deepcopy(rows)}
    evidence = _clean_candidate_fields(candidates, list(evidence), units.values())
    session_locations = defaultdict(list)
    for unit in units.values():
        if unit.get("excluded_reason") in SESSION_SOURCE_REASONS:
            session_locations[_evidence_location(unit)].append(unit)
    known_fields = set(prepared.get("product_schema", {}).get("properties", ()))
    if not known_fields:
        known_fields = set().union(*(row.keys() for row in rows)) | {"knowledge_documents"}
    valid = set(units) - {key for key, unit in units.items() if unit.get("excluded_reason")}
    submitted = {}
    issues = []
    for item in coverage if isinstance(coverage, list) else []:
        key = item.get("unit") if isinstance(item, dict) else None
        if isinstance(key, str) and units.get(key, {}).get("excluded_reason") == "metadata":
            continue  # Source classification is authoritative for known non-content.
        if not isinstance(key, str) or key not in valid or key in submitted:
            issues.append({"unit": key if isinstance(key, str) else None, "reason": "unknown_or_duplicate_unit"})
        else:
            submitted[key] = item
    if issues:
        _missing(issues)

    variant_units = [unit for unit in units.values() if unit.get("sku_id")
                     and unit.get("column") in {"款式", "规格", "版本", "型号"}]
    variants = defaultdict(set)
    for unit in variant_units:
        variants[unit["sku_id"]].add(_norm(unit["text"]))
    family = source_family_anchor(prepared.get("locations"), units, variant_units, candidates=candidates)
    shared_family = bool(family and _family_is_proven({family[0]}, candidates, variants))
    source_skus = {unit["sku_id"] for unit in units.values() if unit.get("sku_id")}
    sku_sources = {(item["sku_id"], _evidence_location(item)[:-1]) for item in evidence if "sku_id" in item["fields"]}

    def belongs(unit, sku):
        if unit.get("excluded_reason"):
            return False
        if unit.get("sku_id"):
            return unit["sku_id"] == sku
        if (sku, _evidence_location(unit)[:-1]) not in sku_sources:
            return False
        text = _norm(unit["text"])
        if any(_norm(other) in text or any(variant in text for variant in variants.get(other, ()))
               for other in candidates if other != sku):
            return False
        return (len(candidates) == 1 and source_skus <= {sku}) or shared_family

    evidence, audience_repairs = _reclassify_audience(candidates, units, submitted, evidence, session_locations, belongs)
    session_issues = []
    field_sources = defaultdict(list)
    for item in evidence:
        coordinate = _evidence_location(item)
        source_units = session_locations.get(coordinate)
        if source_units:
            quote = item.get("quote")
            unit = next((unit for unit in source_units if quote and quote in unit["text"]), source_units[0])
            reason = ("temporary_promotion_in_catalog_evidence" if unit["excluded_reason"] == "session_promotion"
                      else "session_configuration_in_catalog_evidence")
            session_issues.append({"unit": unit["id"], "reason": reason, "sku_id": item["sku_id"]})
        for field in item["fields"]:
            field_sources[item["sku_id"], coordinate, field].append(item.get("quote", ""))
    if session_issues:
        _missing(session_issues)
    provided_faq = {sku for sku, row in candidates.items() if row.get("faq")}

    def exact_faq(unit, sku):
        original = original_locations.get(unit["location"])
        if (original and original.get("kind") != "image" and belongs(unit, sku)
                and _evidence_location(original) == _evidence_location(unit)
                and _LABELED_FAQ.match(unit["text"])
                and (original["text"].strip() == unit["text"] or unit.get("sku_id") == sku
                     and unit["text"] in original["text"])):
            return _source_faq(unit["text"])
        return None

    def can_append_exact_faq(unit, sku, current):
        if not (pair := exact_faq(unit, sku)):
            return False
        question, answer = map(_norm, pair)
        supported = {tuple(map(_norm, source_pair)) for peer in units.values()
                     if (source_pair := exact_faq(peer, sku))}
        if any(other_answer != answer for other_question, other_answer in supported if other_question == question):
            return False
        if not current:
            return True
        existing = _labeled_faq_pairs(current)
        prior_questions = {prior for prior, _ in existing}
        return bool(existing and existing <= supported and len(prior_questions) == len(existing)
                    and question not in prior_questions)

    def normalize_shared_faq(unit, sku, current):
        if not shared_family or not isinstance(current, str) or not (original := exact_faq(unit, sku)):
            return None
        candidate = _source_faq(current)
        scope = _FAQ_SHARED_ANSWER.match(original[1])
        if not candidate or not scope or _norm(candidate[0]) != _norm(original[0]):
            return None
        answer = _norm(candidate[1])
        exact = _norm(original[1][scope.end():])
        if not exact or not any(answer == variant + exact for variant in variants.get(sku, ())):
            return None
        return f"问：{original[0]}\n答：{original[1]}"

    def add_proof(unit, sku, field):
        evidence.append({"sku_id": sku, "fields": [field], "source_ref": unit["source_ref"],
                         "locator": unit["locator"], "quote": unit["text"]})

    def recover_typed_text(unit, sku, field, targets):
        if field not in _TYPED_SOURCE_LABELS or not belongs(unit, sku):
            return False
        original = original_locations.get(unit["location"])
        if (not original or original.get("kind") == "image" or
                _evidence_location(original) != _evidence_location(unit) or unit["text"] not in original["text"]):
            return False
        if field == "material" and _SHARED_COMPONENT_MATERIAL.fullmatch(unit["text"]):
            candidates[sku][field] = unit["text"]  # Keep the material's component and applicability.
            add_proof(unit, sku, field)
            return True
        original_text = original["text"].strip()
        label = _TYPED_SOURCE_LABELS[field]
        if not label.match(original_text):
            return False
        local = [peer for peer in peers[unit["location"]] if not peer.get("excluded_reason")]
        if ([peer["text"] for peer in local] != _parts(original_text)
                or any(not belongs(peer, sku) or submitted.get(peer["id"], {}).get("targets") != targets
                       or _STOCK_OR_PROMO.search(peer["text"]) or _NON_FACT.search(peer["text"])
                       for peer in local)
                or field == "return_policy" and (not _RETURN.search(label.sub("", original_text))
                                                  or any(_FREIGHT.search(peer["text"]) for peer in local))):
            return False
        candidates[sku][field] = label.sub("", original_text)
        for peer in local:
            add_proof(peer, sku, field)
        return True

    def retain_fulfillment(unit, sku, destination):
        field, value = destination
        row = candidates[sku]
        if not row.get(field):
            row[field] = value
            add_proof(unit, sku, field)
        elif not _matches(row, field, unit):
            issues.append({"unit": unit["id"], "reason": "attribute_value_conflict", "sku_id": sku, "field": field})
        return {"sku_id": sku, "field": field}

    supplemental = defaultdict(list)

    def retain_source(unit, destination, fulfillment):
        skus, attribute = destination
        actual = []
        key = unit["id"]
        for sku in skus:
            row = candidates[sku]
            if _spec_polarity_conflicts(row, unit["text"]):
                issues.append({"unit": key, "reason": "conflicting_source_fact", "sku_id": sku})
                continue
            if fulfillment:
                actual.append(retain_fulfillment(unit, sku, fulfillment))
                continue
            if not attribute or "specifications" not in known_fields:
                if not _contains(row.get("knowledge_documents"), unit["text"]):
                    supplemental[sku].append(unit)
                actual.append({"sku_id": sku, "field": "knowledge_documents"})
                continue
            label, value = attribute
            specs = row.setdefault("specifications", [])
            if not isinstance(specs, list) or any(not isinstance(spec, dict) or
                    not {"name", "value"} <= spec.keys() for spec in specs):
                issues.append({"unit": key, "reason": "invalid_specifications", "sku_id": sku})
                continue
            existing = [spec for spec in specs if _norm(spec["name"]) == _norm(label)]
            if any(_norm(spec["value"]) != _norm(value) for spec in existing):
                issues.append({"unit": key, "reason": "attribute_value_conflict", "sku_id": sku})
                continue
            if not existing:
                specs.append({"name": label, "value": value})
            add_proof(unit, sku, "specifications")
            actual.append({"sku_id": sku, "field": "specifications"})
        status = "knowledge" if any(t["field"] == "knowledge_documents" for t in actual) else "retained"
        return {"status": status, "targets": actual, "recovery": "verified_source_scope"}

    peers = defaultdict(list)
    for unit in units.values():
        peers[unit["location"]].append(unit)
    anchors = {found[1] for unit in units.values() if (found := _PRODUCT_NAME.fullmatch(unit["text"]))}
    scope_is_complete = not source_skus or source_skus == candidates.keys()
    proven_family = _family_is_proven(anchors, candidates, variants)
    family_intro = _shared_family_introduction(prepared.get("locations"), candidates, peers, variants,
                                              confirmed_family=family[0] if shared_family else None)
    currency_units = _scoped_currency_units(prepared.get("locations", []), units, candidates, evidence)
    audit = []
    optional_gaps = []

    def record_gap(unit, reason):
        issue = {"unit": unit["id"], "reason": reason}
        if allow_optional_gaps and scope_is_complete and not _required_source_unit(unit):
            coordinate = _evidence_location(unit)
            claims = [(sku, field) for (sku, source, field), quotes in field_sources.items()
                      if source == coordinate and field not in _DIRECT_ID and candidates[sku].get(field)
                      and any(quote and (unit["text"] in quote or quote in unit["text"]) for quote in quotes)]
            if claims:
                if (all(belongs(unit, sku) and _matches(candidates[sku], field, unit) for sku, field in claims)
                        and (unit.get("fixed_scope") != "all_skus" or {sku for sku, _ in claims} == candidates.keys())):
                    audit.append({"unit": unit["id"], "location": unit["location"], "status": "retained",
                                  "recovery": "verified_source_scope", "targets": [
                                      {"sku_id": sku, "field": field} for sku, field in claims]})
                else:
                    issues.append(issue)  # A candidate claim must still be verified.
                return
            optional_gaps.append(issue)
            audit.append({**issue, "location": unit["location"], "status": "unresolved_optional"})
        else:
            issues.append(issue)

    for key, unit in units.items():
        if unit.get("excluded_reason"):
            audit.append({"unit": key, "location": unit["location"], "status": "excluded",
                          "reason": unit["excluded_reason"]})
            continue
        item = submitted.get(key, {})
        disposition, targets = item.get("disposition"), item.get("targets")
        fulfillment = _fulfillment_destination(unit["text"], known_fields)
        record = {"unit": key, "location": unit["location"], "status": disposition}
        if (sku := unit.get("sku_id")) in candidates and _conflicting_scoped_measurement(candidates[sku], unit):
            issues.append({"unit": key, "reason": "conflicting_source_fact", "sku_id": sku, "field": "specifications"})
            continue
        if key in currency_units:
            audit.append({**record, "status": "retained", "recovery": "verified_source_scope",
                          "targets": [{"sku_id": sku, "field": "currency"} for sku in candidates]})
            continue
        same_family = proven_family or unit["location"] in family_intro
        attribute = _LABELED_IDENTITY.fullmatch(unit["text"])
        if (shared_family and same_family and not unit.get("sku_id") and attribute
                and not _NON_FACT.search(unit["text"])):
            audit.append({**record, **retain_source(unit, (list(candidates), attribute.groups()), None)})
            continue
        if (disposition == "excluded" and item.get("reason") == "metadata" and not targets
                and _HEADING.fullmatch(unit["text"]) and not _CONDITION.search(unit["text"])):
            audit.append({**record, "reason": "metadata"})
            continue
        if scope_is_complete and (not item or (
                disposition == "excluded" and item.get("reason") in {"metadata", "out_of_scope"} and not targets)):
            fallback = _source_destination(unit, candidates, peers[unit["location"]], variants, same_family)
            if fallback:
                audit.append({**record, **retain_source(unit, fallback, fulfillment)})
                continue
        if not item:
            record_gap(unit, "missing_disposition")
            continue
        if (disposition not in {"retained", "knowledge"} and not
                (disposition == "excluded" and item.get("reason") == "duplicate")):
            record_gap(unit, "unverified_disposition")
            continue
        if not isinstance(targets, list) or not targets:
            record_gap(unit, "missing_destination")
            continue
        destinations = []
        for target in targets:
            if not isinstance(target, dict):
                destinations.clear()
                break
            sku, field = target.get("sku_id"), target.get("field")
            if (not isinstance(sku, str) or not isinstance(field, str) or field not in known_fields
                    or field in _NON_CONTENT or sku not in {*candidates, "*"}):
                destinations.clear()
                break
            destinations.extend((candidate, field) for candidate in (candidates if sku == "*" else [sku]))
        selected = {sku for sku, _ in destinations}
        column = unit.get("column", "")
        required_field = ("sku_id" if _SKU_HEADER.fullmatch(column)
                          else "price" if _PRICE_HEADER.search(column) else None)
        if (not destinations or (unit.get("sku_id") and selected != {unit["sku_id"]})
                or (unit.get("fixed_scope") == "all_skus" and selected != candidates.keys())
                or (required_field and (disposition != "retained"
                                        or any(field != required_field for _, field in destinations)))):
            issues.append({"unit": key, "reason": "invalid_sku_scope_or_destination"})
            continue
        if (scope_is_complete and disposition == "retained" and not required_field
                and all(field in _DIRECT_ID for _, field in destinations)):
            fallback = _source_destination(unit, candidates, peers[unit["location"]], variants, same_family)
            # A labelled consumer attribute needs its own destination even if
            # the value also appears as part of the product's full name.
            explicit_attribute = _ATTRIBUTE.fullmatch(_SHARED_PREFIX.sub("", unit["text"]))
            if (fallback and fallback[1] and selected == set(fallback[0])
                    and (explicit_attribute or all(not _matches(candidates[sku], field, unit)
                                                   for sku, field in destinations))):
                audit.append({**record, **retain_source(unit, fallback, fulfillment)})
                continue
        actual = []
        for sku, field in dict.fromkeys(destinations):
            row = candidates[sku]
            if _spec_polarity_conflicts(row, unit["text"]):
                issues.append({"unit": key, "reason": "conflicting_source_fact", "sku_id": sku, "field": "specifications"})
                continue
            if fulfillment:
                actual.append(retain_fulfillment(unit, sku, fulfillment))
                continue
            if _freight_clause(unit["text"]) and field in {"shipping_time", "return_policy", "shipping_origin"}:
                if not _contains(row.get("knowledge_documents"), unit["text"]):
                    supplemental[sku].append(unit)
                actual.append({"sku_id": sku, "field": "knowledge_documents"})
                continue
            if (field in {"knowledge_documents", "usage_instructions", "faq"} and "faq" in known_fields
                    and re.search(r"[？?]", unit["text"])):
                coordinate = _evidence_location(unit)
                if field_sources[sku, coordinate, "faq"] and _matches(row, "faq", unit):
                    actual.append({"sku_id": sku, "field": "faq"})
                    continue
                if (field == "faq" and field_sources[sku, coordinate, "faq"]
                        and (normalized := normalize_shared_faq(unit, sku, row.get("faq")))):
                    row["faq"] = normalized
                    actual.append({"sku_id": sku, "field": "faq"})
                    continue
                if (pair := _source_faq(unit["text"])) and belongs(unit, sku):
                    question, answer = pair
                    quotes = field_sources[sku, coordinate, "usage_instructions"] + field_sources[sku, coordinate, "faq"]
                    existing = row.get("faq") or ""
                    source_forms = {_norm(answer), _norm(unit["text"])}
                    if (field == "faq" and can_append_exact_faq(unit, sku, existing)
                            or sku not in provided_faq and _norm(question) not in _norm(existing)
                            and any(_norm(quote) in source_forms for quote in quotes)):
                        entry = f"问：{question}\n答：{answer}"
                        row["faq"] = f"{existing}\n\n{entry}" if existing else entry
                        add_proof(unit, sku, "faq")
                        actual.append({"sku_id": sku, "field": "faq"})
                        continue
            if (disposition == "retained" and field in _TYPED_SOURCE_LABELS and not row.get(field)
                    and recover_typed_text(unit, sku, field, targets)):
                actual.append({"sku_id": sku, "field": field})
                continue
            if disposition != "knowledge" and _matches(row, field, unit):
                actual.append({"sku_id": sku, "field": field})
                continue
            if disposition == "retained" and field == "specifications" and _restore_spec_qualifiers(row, unit):
                actual.append({"sku_id": sku, "field": field})
                continue
            if (allow_optional_gaps and disposition == "retained" and field == "specifications"
                    and unit.get("sku_id") == sku
                    and (scoped := _source_destination(unit, candidates, peers[unit["location"]], variants, False))
                    and scoped[1]):
                actual.extend(retain_source(unit, scoped, None)["targets"])
                continue
            value = row.get(field)
            if disposition == "retained" and field not in _DIRECT_ID | {"faq"}:
                restored = _restore_conditional_wording(value, unit["text"])
                if restored is not None:
                    row[field] = restored
                    actual.append({"sku_id": sku, "field": field})
                    continue
            if (disposition == "retained" and isinstance(value, str) and value.strip()
                    and _CONDITION.search(unit["text"]) and _norm(value) in _norm(unit["text"])
                    and field not in _DIRECT_ID | {"faq"}):
                row[field] = unit["text"]
                actual.append({"sku_id": sku, "field": field})
                continue
            if (disposition == "excluded" or field in _DIRECT_ID or field == "faq"
                    or (disposition == "retained" and isinstance(value, str) and value.strip()
                        and _CONDITION.search(unit["text"]))
                    or _STOCK_OR_PROMO.search(unit.get("column", "") + unit["text"])
                    or re.search(r"[？?]", unit["text"])):
                if (allow_optional_gaps and scope_is_complete and not value and field not in _DIRECT_ID
                        and not _required_source_unit(unit) and not _STOCK_OR_PROMO.search(unit["text"])
                        and disposition != "excluded"):
                    optional_gaps.append({"unit": key, "reason": "unverified_optional_fact", "sku_id": sku, "field": field})
                else:
                    issues.append({"unit": key, "reason": "destination_does_not_retain_source", "sku_id": sku, "field": field})
                continue
            if not _contains(row.get("knowledge_documents"), unit["text"]):
                supplemental[sku].append(unit)
            actual.append({"sku_id": sku, "field": "knowledge_documents"})
        status = ("knowledge" if any(t["field"] == "knowledge_documents" for t in actual)
                  else "retained" if actual else "unresolved_optional")
        audit.append({**record, "status": status, "targets": actual})
    unresolved_units = {item["unit"] for item in optional_gaps}
    if unresolved_units and len(unresolved_units) * 5 >= len(valid) * 4:
        _missing(issues + [{"unit": key, "reason": "material_extraction_mostly_unresolved"} for key in sorted(unresolved_units)],
                 meaningful_units=len(valid), unretained_units=len(unresolved_units), major_extraction_failure=True)
    if issues:
        _missing(issues)
    for sku, fragments in supplemental.items():
        documents = candidates[sku].setdefault("knowledge_documents", [])
        content = "\n".join(dict.fromkeys(item["text"] for item in fragments))
        if len(documents) >= 20 or len(content) > 6000:
            _missing([{"unit": item["id"], "reason": "supplement_exceeds_document_limit"} for item in fragments])
        identifiers = {doc.get("id") for doc in documents}
        index = 1
        while f"source_coverage_{index}" in identifiers:
            index += 1
        documents.append({"id": f"source_coverage_{index}", "title": "商品资料补充", "content": content})
        for unit in fragments:
            add_proof(unit, sku, "knowledge_documents")
    summary = {"total_units": len(units), "retained_units": sum(row["status"] == "retained" for row in audit),
               "knowledge_units": sum(row["status"] == "knowledge" for row in audit),
               "excluded_units": sum(row["status"] == "excluded" for row in audit), "dispositions": audit}
    if optional_gaps:
        summary.update(optional_gap_count=len(unresolved_units), optional_gaps=optional_gaps[:20],
                       meaningful_units=len(valid), unretained_units=len(unresolved_units))
    if audience_repairs:
        summary["reclassified_audience"] = [{"sku_id": sku, "unit": unit["id"] if unit else None}
                                            for sku, unit in audience_repairs.items()]
    # Pruning an empty field can remove an evidence entry in the middle.
    # The standard report has no external references to these local IDs.
    evidence = [{**item, "id": f"e{index}"} for index, item in enumerate(evidence, 1)]
    return list(candidates.values()), evidence, summary

