"""Source-checked session lane and durable business steps for explicit document imports."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from shengbo_contracts.common import ContractError, MutationContext, canonical_hash
from .client import RemoteOutcomeUnknown

ROOM_ACTION = "room_material_action"
ROOM_DEFAULT_REQUEST = "请导入这批资料到当前本场。商品资料归入共享商品库并关联本场，活动资料只保存到当前本场配置。"
KIND = "document_import"


def import_entry(run):
    view = (run.get("input", {}).get("view_context") or {}) if isinstance(run, dict) else {}
    if view.get(ROOM_ACTION) == "import":
        return "room"
    if view.get("product_material_action") == "create" and view.get("catalog_create_room") is True:
        return "catalog"
    return None


def allows_default_session_projection(run):
    """A custom note may intentionally exclude even an explicit source field."""
    text = run.get("input", {}).get("text")
    entry = import_entry(run)
    if entry == "room":
        return text == ROOM_DEFAULT_REQUEST
    if entry == "catalog":
        from .catalog_intake import BASIC_CREATE_REQUEST

        return text == BASIC_CREATE_REQUEST
    return False


def validate_request(request, conversation):
    view = request.view_context
    if ROOM_ACTION not in view:
        from .catalog_intake import BASIC_CREATE_REQUEST
        if "catalog_create_room" in view and (view["catalog_create_room"] is not True
                or view.get("product_material_action") != "create" or not is_catalog_request(request.text, BASIC_CREATE_REQUEST)
                or not 1 <= len(request.material_refs) <= 4):
            raise ContractError("DOCUMENT_IMPORT_SCOPE", "请从商品库导入商品。", status=422)
        return
    scope = conversation["scope"]
    if (view.get(ROOM_ACTION) != "import" or view.get("view") != "session"
            or scope.get("kind") != "preparation"
            or not conversation.get("room_id") or view.get("room_id") != conversation["room_id"]
            or view.get("preparation_id") != scope.get("preparation_id")
            or request.intent_mode != "new_task" or request.related_run_id is not None
            or request.selection is not None or request.observed_versions
            or not request.text.strip() or not 1 <= len(request.material_refs) <= 4
            or "product_material_action" in view or "catalog_create_room" in view):
        raise ContractError("DOCUMENT_IMPORT_SCOPE", "请从当前直播间的本场配置上传资料。", status=422)


def is_catalog_request(text, base):
    return text == base or text.startswith(base + "\n\n在上述商品范围内的补充要求：")


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    location: str = Field(pattern=r"^L\d+$")
    quote: str = Field(min_length=1, max_length=1000)


class Price(Evidence):
    sku: str = Field(min_length=1, max_length=80)
    amount: float = Field(ge=0, le=10_000_000)


class Coupon(Evidence):
    amount: float = Field(gt=0, le=10_000_000)
    minimum_spend: float = Field(ge=0, le=10_000_000)
    currency: str = Field(pattern="^(CNY|TWD|USD|AUD)$")
    skus: list[str] = Field(default_factory=list, max_length=8)


class Window(Evidence):
    start: str | None = None
    end: str | None = None


class SessionImport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    theme: Evidence | None = None
    objective: Evidence | None = None
    scene: list[Evidence] = Field(default_factory=list, max_length=8)
    schedule: Window | None = None
    window: Window | None = None
    rules: list[Evidence] = Field(default_factory=list, max_length=15)
    prices: list[Price] = Field(default_factory=list, max_length=8)
    coupon: Coupon | None = None
    stacks_with_coupon: bool | None = None
    gaps: list[str] = Field(default_factory=list, max_length=8)


SESSION_PROMPT = """
本次还有独立的 session 输出。只从原文提取明确属于本场直播的设置或活动。普通售价、常态运费、跨场通用政策仍留在商品资料；
直播活动价、券、限购、活动付款渠道和起止窗口放到 session，不得写进商品。每项用原文 L 代号和逐字引句。
没有本场内容返回 session:null。仅本场文档 products:[]、questions:[]，不编造商品或日常价。
有商品也有活动时，同一次提交两部分。规则 rules 要完整引用限购、叠加、领用次数、门槛口径、渠道和逾期条件。
prices 仅列明能从同一出处核对 SKU 和本场价格的项目。coupon 仅结构化明确的满减券，写面额、门槛、币种和适用 SKU；
原文只写通用“元”且同文件未明确币种时，不猜。活动窗口 window 和直播排期 schedule 是独立信息，
仅在原文明确直播开播/结束计划时填写 schedule。时间转换为带时区的 ISO 8601，不补年份或时区。
theme、objective、scene 使用原文短引句。用户说明只用来缩小本次导入范围，不能作为未写入文件的价格或促销依据。
只把原文确实缺失或相互冲突的必要项列入 gaps；所有已明确的条件放 rules。空数组写 []，可选对象无值写 null。
如有 session_source_locations，逐项核对这些本场段落，完整提取明确的时间、价格、券和条件。演示顺序、口播和播放安排属于内部设置，不写进公开活动规则。
"""


def _time_offsets(source, quote):
    offsets = set()
    if "北京时间" in source or "北京" in quote:
        offsets.add(timedelta(hours=8))
    if re.search(r"\b(?:UTC|GMT)(?!\s*[+-])\b|\d{2}:\d{2}(?::\d{2})?Z\b", source, re.I):
        offsets.add(timedelta())
    for offset in re.finditer(r"(?:(?:UTC|GMT)\s*|(?<=\s)|(?<=:\d\d)(?=\+))([+-])(0\d|1[0-4]):([0-5]\d)(?!\d)",
                              source, re.I):
        sign, hours, minutes = offset.groups()
        offsets.add(timedelta(hours=int(hours), minutes=int(minutes)) * (-1 if sign == "-" else 1))
    return offsets


def _check_time(raw, source, quote, offsets):
    supplied = datetime.fromisoformat(raw)
    hour = f"0?{supplied.hour}" if supplied.hour < 10 else str(supplied.hour)
    if (supplied.utcoffset() is None
            or not re.search(rf"(?<!\d){hour}:{supplied.minute:02d}(?!\d)", quote)
            or not re.search(rf"{supplied.year}\D+0?{supplied.month}\D+0?{supplied.day}(?!\d)", source)):
        raise ValueError("时间需与原文的日期、时刻及明确时区一致")
    if offsets != {supplied.utcoffset()}:
        raise ValueError("时区需与该位置明确标注的时区一致")
    return supplied


_SESSION_PRICE_COLUMN = re.compile(r"^(?:本场(?:直播)?|直播间?)(?:活动|优惠|专享)?(?:售)?价(?:格)?$")
_PERMANENT_PRICE_LABEL = re.compile(r"(?:日常|常规|原|建议零售|供货|进货)(?:零售|售)?价(?:格)?")
_PROMOTIONAL_PRICE_LABEL = re.compile(r"(?:本场|活动|直播间?|促销|优惠|专享)(?:活动|优惠|专享)?(?:售)?价(?:格)?")
_NO_ACTIVITY_GAP = "文档没有本场活动信息，可在本场配置补填。"
_MISSING_PRICE_CURRENCY = re.compile(
    r"^(?:直播|本场|活动|单品|表格|价|格|售|\s)*(?:缺少|未提供|未注明|未标注|未明确)(?:明确的?)?(?:币种|货币单位)[。！.]?$"
    r"|^(?:直播|本场|活动|单品|表格|价|格|售|\s)*(?:币种|货币单位)(?:缺失|待补充|不明确|未提供)[。！.]?$")

_COUPON_TERMS = re.compile(r"(?:满|实付满|消费满)\s*[¥￥$]?\s*([\d,.]+)\s*(?:元|块|人民币)?\s*(?:立减|直减|减|抵扣|抵)\s*[¥￥$]?\s*([\d,.]+)")
_SESSION_DATE = r"\d{4}\s*(?:年|[-/.])\s*\d{1,2}\s*(?:月|[-/.])\s*\d{1,2}"
_SESSION_CLOCK = r"(?:[01]?\d|2[0-3]):[0-5]\d"
_SESSION_TIME_RANGE = re.compile(
    rf"(?<!\d)(?:(?P<start_date>{_SESSION_DATE})\s*[日号T]?\s*)?(?P<start_clock>{_SESSION_CLOCK})\s*"
    rf"(?:至|到|—|–|~|～|-)\s*(?:(?P<end_date>{_SESSION_DATE})\s*[日号T]?\s*)?"
    rf"(?P<end_clock>{_SESSION_CLOCK})(?!\d)"
)
_SESSION_LABELS = {
    "theme": re.compile(r"^\s*(?:本场主题|直播主题)\s*[:：|\t\u3000]+\s*\S"),
    "objective": re.compile(r"^\s*(?:本场目标|直播目标)\s*[:：|	　]+\s*\S"),
    "scene": re.compile(r"^\s*(?:本场场景|直播场景|受众与场景)\s*[:：|	　]+\s*\S"),
    "schedule": re.compile(r"^\s*(?:计划直播排期|直播排期|直播时间|直播时段|开播时间)\s*[:：|\t\u3000]+.*\d{1,2}:\d{2}"),
    "window": re.compile(r"^\s*(?:活动时间与入口|活动时间|活动窗口|促销时间|活动有效期)\s*[:：|\t\u3000]+.*\d{1,2}:\d{2}"),
}
_SKU_TOKEN = r"(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9][A-Za-z0-9_-]*"
_COUPON_SKUS = re.compile(r"(?:本券|该券|优惠券)?\s*(?:仅适用(?:于)?|仅限(?:于)?|适用(?:商品|款式|型号|SKU)\s*[:：]?)\s*(?:SKU\s*[:：]?\s*)?("
                         + _SKU_TOKEN + r"(?:\s*(?:、|[，,／/]|或|及|和)\s*" + _SKU_TOKEN + r")*)", re.I)
_NAMED_COUPON_SCOPE = re.compile(r"(?:本券|该券|优惠券)?\s*(?:仅适用(?:于)?|仅限(?:于)?|适用(?:款式|型号)\s*[:：])\s*([^。；;\n]{1,80})")


def _session_source_role(text):
    return next((field for field, pattern in _SESSION_LABELS.items() if pattern.search(text)), None)


def _explicit_time_bounds(source):
    ranges = list(_SESSION_TIME_RANGE.finditer(source))
    if len(ranges) != 1:
        return None
    start_date, start_clock, end_date, end_clock = ranges[0].groups()
    clocks = tuple(time(*map(int, value.split(":"))) for value in (start_clock, end_clock))
    if start_date and not end_date and clocks[0] < clocks[1]:
        # An adjacent leading date qualifies a same-day range. Do not infer a
        # date for an overnight bound or borrow a standalone date elsewhere.
        end_date = start_date
    try:
        dates = tuple(date(*map(int, re.findall(r"\d+", value))) if value else None
                      for value in (start_date, end_date))
    except ValueError:
        dates = (None, None)
    return tuple(zip(dates, clocks))


def _normalized_quote(value):
    """Return comparable text and source offsets without merging numeric tokens."""
    chars, offsets = [], []
    for match in re.finditer(r"\s+|\S", value):
        normalized = " " if match[0].isspace() else unicodedata.normalize("NFKC", match[0])
        chars.extend(normalized)
        offsets.extend([match.span()] * len(normalized))
    return "".join(chars), offsets


def _quote_match(source, key):
    def token(char):
        return char.isascii() and (char.isalnum() or char in "_-")

    for match in re.finditer(re.escape(key), source):
        start, end = match.span()
        if (start and token(key[0]) and (token(source[start - 1])
                or source[start - 1] == "." and start > 1 and source[start - 2].isdigit())):
            continue
        if (end < len(source) and token(key[-1]) and (token(source[end])
                or source[end] == "." and end + 1 < len(source) and source[end + 1].isdigit())):
            continue
        return start, end
    return None


def _source_excerpt(quote, text):
    if _quote_match(text, quote):
        return quote
    key, _ = _normalized_quote(quote)
    key = key.strip().rstrip("。.!！").rstrip()
    if not key:
        return None
    source, offsets = _normalized_quote(text)
    span = _quote_match(source, key)
    if span is None:
        return None
    start, end = span
    return text[offsets[start][0]:offsets[end - 1][1]]


def _session_scope_by_location(prepared):
    reasons = {}
    for unit in prepared.get("source_units", []):
        reasons.setdefault(unit["location"], set()).add(unit.get("excluded_reason"))
    return reasons


def _session_reference(field, raw, locations, labeled, scope_by_location):
    if not isinstance(raw, dict) or not isinstance(raw.get("quote"), str) or not raw["quote"].strip():
        return raw
    quote = raw["quote"].strip()
    location = raw.get("location")
    current = locations.get(location) if isinstance(location, str) else None
    if current and quote in current["text"]:
        return raw
    if current and (excerpt := _source_excerpt(quote, current["text"])):
        return {**raw, "quote": excerpt}
    eligible = {"session_configuration", "session_promotion"} if field in {"theme", "objective", "scene", "schedule"} else {"session_promotion"}
    candidates = []
    for key, row in locations.items():
        source_role = labeled.get(key)
        reasons = scope_by_location.get(key)
        if source_role != field and (source_role or not reasons or not reasons <= eligible):
            continue
        if excerpt := _source_excerpt(quote, row["text"]):
            candidates.append((key, excerpt))
    if len(candidates) == 1:
        location, excerpt = candidates[0]
        return {**raw, "location": location, "quote": excerpt}
    return raw


def _project_default_session(raw, locations, labeled, scope_by_location):
    result = dict(raw or {})
    for field in ("theme", "objective", "scene", "schedule"):
        if result.get(field) is not None and not (field == "scene" and result[field] == []):
            continue
        matches = [key for key, role in labeled.items() if role == field]
        if len(matches) != 1:
            continue
        location, = matches
        reasons = scope_by_location.get(location)
        if not reasons or not reasons <= {"session_configuration", "session_promotion"}:
            continue
        quote = locations[location]["text"].strip()
        if len(quote) > 1000:
            continue
        evidence = {"location": location, "quote": quote}
        if field == "schedule":
            bounds = _explicit_time_bounds(quote)
            offsets = _time_offsets(quote, quote)
            if bounds and all(day is not None for day, _ in bounds) and len(offsets) == 1:
                zone = timezone(next(iter(offsets)))
                start, end = [datetime.combine(day, clock, zone) for day, clock in bounds]
                if start < end:
                    evidence.update(start=start.isoformat(), end=end.isoformat())
        result[field] = [evidence] if field == "scene" else evidence
    return result or None


def _coupon_sku_scopes(quote, source_text):
    matches = list(_COUPON_SKUS.finditer(quote))
    if not matches:
        continuation = source_text.split(quote, 1)[1].lstrip("。；;，, \t")
        if match := _COUPON_SKUS.match(continuation):
            matches.append(match)
    return {frozenset(token.casefold() for token in re.findall(_SKU_TOKEN, match[1])) for match in matches}


def _named_coupon_sku_scope(quote, source_text, source_ref, prepared):
    matches = list(_NAMED_COUPON_SCOPE.finditer(quote))
    if not matches:
        continuation = source_text.split(quote, 1)[1].lstrip("。；;，, \t")
        if match := _NAMED_COUPON_SCOPE.match(continuation):
            matches.append(match)
    labels = {match[1].strip() for match in matches if re.search(r"款|型号|规格|套装|版本", match[1])}
    if not labels:
        return False, []
    if len(labels) != 1:
        return True, []
    known = {}
    for unit in prepared.get("source_units", []):
        if (unit.get("source_ref") == source_ref and unit.get("column") in {"款式", "型号", "规格", "版本"}
                and unit.get("sku_id") and not unit.get("excluded_reason")):
            known.setdefault(unit["text"].strip(), set()).add(unit["sku_id"])
    names = re.split(r"\s*(?:、|[，,／/]|或|及|和)\s*", next(iter(labels)))
    if any(len(known.get(name, ())) != 1 for name in names):
        return True, []
    return True, sorted({next(iter(known[name])) for name in names})


def _session_issue(path, reason, location=None):
    return {"path": list(path), "field": path[0] if path else None, "reason": reason,
            **({"location": location} if location else {})}


def _session_error(issues, correction):
    fields = {issue["field"] for issue in issues}
    repair_fields = sorted(fields) if fields <= SessionImport.model_fields.keys() else []
    return ContractError("DOCUMENT_SESSION_EVIDENCE_INVALID",
        "本场活动或设置的内容与所引原文未能核对，请仅修正本场活动及对应引文。", status=422,
        details={"correction": correction, "issues": issues[:48], "issue_count": len(issues),
                 "repair_fields": repair_fields})


def _table_activity_prices(prepared, locations):
    """Take explicit per-SKU live prices from the actual table cell."""
    from .catalog_extract import _explicit_currencies, _price_amounts
    from .catalog_coverage import _PRICE_VALUE_ONLY

    currencies = {}
    for row in locations.values():
        currencies.setdefault(row["source_ref"]["id"], set()).update(_explicit_currencies(row["text"]))
    prices, gaps, conflicting = {}, [], set()
    for unit in prepared.get("source_units", []):
        sku, column = unit.get("sku_id"), unit.get("column", "")
        row = locations.get(unit["location"])
        if not sku or not row or row["kind"] != "table_row" or not _SESSION_PRICE_COLUMN.fullmatch(column.strip()):
            continue
        text = unit["text"]
        amounts = _price_amounts(text)
        currency = _explicit_currencies(text) or currencies[row["source_ref"]["id"]]
        if not _PRICE_VALUE_ONLY.fullmatch(text) or len(amounts) != 1 or len(currency) != 1:
            gaps.append(f"{sku} 的直播售价或币种待核对。")
            continue
        quote = row["text"].strip()
        if not quote or len(quote) > 1000 or text not in quote:
            continue
        candidate = Price(sku=sku, amount=float(next(iter(amounts))), location=row["location"], quote=quote)
        key = sku.casefold()
        if key in prices and prices[key].amount != candidate.amount:
            gaps.append(f"{sku} 在原文有不同的直播售价，待确认。")
            conflicting.add(key)
        prices[key] = candidate
    return {key: price for key, price in prices.items() if key not in conflicting}, gaps, conflicting


def _session_source_context(prepared):
    locations = {row["location"]: row for row in prepared["locations"] if row["kind"] != "image"}
    labeled = {key: role for key, row in locations.items() if (role := _session_source_role(row["text"]))}
    return locations, labeled, _session_scope_by_location(prepared)


def checked_session(raw, prepared, *, require_complete=True, allow_default_projection=False):
    row_indexes = {}
    try:
        locations, labeled, scope_by_location = _session_source_context(prepared)
        if allow_default_projection and (raw is None or isinstance(raw, dict)):
            raw = _project_default_session(raw, locations, labeled, scope_by_location)
        table_prices, table_gaps, conflicting = _table_activity_prices(prepared, locations)
        if raw is None and not (table_prices or table_gaps or require_complete and labeled):
            return None
        if raw is None:
            raw = {}
        # Prefer the parsed cell over the model's duplicate projection of the same row.
        if isinstance(raw, dict):
            raw = {**raw, **{key: [] for key in ("scene", "rules", "prices", "gaps")
                             if key in raw and raw[key] is None}}
            for field in ("theme", "objective", "schedule", "window", "coupon"):
                if field in raw:
                    raw[field] = _session_reference(field, raw[field], locations, labeled, scope_by_location)
            for field in ("scene", "rules", "prices"):
                if isinstance(raw.get(field), list):
                    raw[field] = [_session_reference(field, row, locations, labeled, scope_by_location)
                                  for row in raw[field]]
            if isinstance(raw.get("prices"), list):
                rows = [(index, row) for index, row in enumerate(raw["prices"]) if not (
                    isinstance(row, dict) and isinstance(row.get("sku"), str)
                    and (proven := table_prices.get(row["sku"].casefold())) is not None
                    and row.get("location") == proven.location)]
                row_indexes["prices"] = [index for index, _ in rows]
                raw["prices"] = [row for _, row in rows]
            if isinstance(raw.get("rules"), list):
                raw["rules"] = [({key: row[key] for key in ("location", "quote")}
                                 if isinstance(row, dict) and set(row) == {"location", "quote", "fields"}
                                 and row["fields"] == [] else row) for row in raw["rules"]]
                rows = [(index, row) for index, row in enumerate(raw["rules"]) if not (
                    isinstance(row, dict) and isinstance(row.get("quote"), str)
                    and re.fullmatch(r"库存(?:量)?为开播前快照[。.]?", row["quote"].strip()))]
                row_indexes["rules"] = [index for index, _ in rows]
                raw["rules"] = [row for _, row in rows]
        value = SessionImport.model_validate(raw)
        configuration_ids = {unit["location"] for unit in prepared.get("source_units", [])
                             if unit.get("excluded_reason") == "session_configuration"}
        parts_by_path = [((field,), getattr(value, field)) for field in ("theme", "objective", "schedule", "window", "coupon")]
        for field in ("scene", "rules", "prices"):
            indexes = row_indexes.get(field)
            parts_by_path.extend(((field, indexes[index] if indexes is not None else index), row)
                                 for index, row in enumerate(getattr(value, field)))
        valid = {}
        issues = []
        if require_complete:
            for field in _SESSION_LABELS:
                matches = [location for location, role in labeled.items() if role == field]
                if len(matches) == 1 and not getattr(value, field):
                    issues.append(_session_issue((field,), "explicit_session_field_missing", matches[0]))
        for path, row in parts_by_path:
            if row is None or path[0] == "rules" and row.location in configuration_ids:
                continue
            if row.location not in locations:
                issues.append(_session_issue((*path, "location"), "location_not_found", row.location))
            elif row.quote not in locations[row.location]["text"]:
                issues.append(_session_issue((*path, "quote"), "quote_not_in_location", row.location))
            elif path[0] in {"window", "coupon", "prices"} and row.location in configuration_ids:
                issues.append(_session_issue((*path, "location"), "internal_configuration_as_promotion", row.location))
            elif (path[0] in {"schedule", "window"} and labeled.get(row.location) in {"schedule", "window"}
                  and path[0] != labeled[row.location]):
                issues.append(_session_issue((*path, "location"), "schedule_activity_window_mismatch", row.location))
            elif (path[0] in {"theme", "objective", "scene"} and labeled.get(row.location) in {"theme", "objective", "scene"}
                  and path[0] != labeled[row.location]):
                issues.append(_session_issue((*path, "location"), "session_field_source_mismatch", row.location))
            else:
                valid[path] = row
        for field in ("schedule", "window"):
            window = valid.get((field,))
            if window is None:
                continue
            source = locations[window.location]["text"]
            invalid_times = False
            explicit_range = _explicit_time_bounds(source)
            offsets = _time_offsets(source, window.quote)
            for index, bound in enumerate(("start", "end")):
                supplied = getattr(window, bound)
                try:
                    expected_date, expected_clock = explicit_range[index] if explicit_range else (None, None)
                    if supplied is None:
                        if require_complete and expected_date and len(offsets) == 1:
                            raise ValueError("明确时间区间需逐项填写起止")
                        continue
                    actual = _check_time(supplied, source, window.quote, offsets)
                    if (expected_clock and actual.time() != expected_clock
                            or expected_date and actual.date() != expected_date):
                        raise ValueError("明确时间区间的起止需逐项匹配")
                except ValueError:
                    invalid_times = True
                    issues.append(_session_issue((field, bound), "time_not_grounded", window.location))
            if (not invalid_times and window.start and window.end
                    and datetime.fromisoformat(window.end) <= datetime.fromisoformat(window.start)):
                issues.append(_session_issue((field, "end"), "end_not_after_start", window.location))
        from .catalog_extract import _price_amounts, _explicit_currencies
        verified_prices = {}
        for path, row in valid.items():
            if path[0] != "prices":
                continue
            sku = row.sku.casefold()
            from_table = table_prices.get(sku)
            if sku in conflicting and locations[row.location]["kind"] == "table_row":
                continue
            if from_table and from_table.location == row.location:
                # The parsed cell is the authority for this table location.
                continue
            text = locations[row.location]["text"]
            if any(_PERMANENT_PRICE_LABEL.search(part) and not _PROMOTIONAL_PRICE_LABEL.search(part)
                   for part in (row.quote, text)):
                issues.append(_session_issue((*path, "location"), "permanent_price_as_promotion", row.location))
                continue
            match = re.search(r"(?<![\w-])" + re.escape(row.sku) + r"(?![\w-])", row.quote, re.I)
            own = re.split(r"[，,。；;\n]", row.quote[match.end():], maxsplit=1)[0] if match else ""
            if not match or _price_amounts(own) != {Decimal(str(row.amount))}:
                issues.append(_session_issue((*path, "amount" if match else "sku"), "sku_amount_not_grounded", row.location))
                continue
            if prior := verified_prices.get(sku):
                if (prior.amount, prior.location, prior.quote) != (row.amount, row.location, row.quote):
                    issues.append(_session_issue((*path, "sku"), "duplicate_activity_sku", row.location))
                continue
            verified_prices[sku] = row
        if row := valid.get(("coupon",)):
            source = locations[row.location]["source_ref"]
            source_text = locations[row.location]["text"]
            if "券" not in source_text and not _COUPON_TERMS.search(source_text):
                issues.append(_session_issue(("coupon", "location"), "coupon_terms_not_in_source", row.location))
            source_currencies = _explicit_currencies(" ".join(
                entry["text"] for entry in locations.values() if entry["source_ref"] == source))
            if source_currencies != {row.currency} and _explicit_currencies(row.quote) != {row.currency}:
                issues.append(_session_issue(("coupon", "currency"), "coupon_currency_not_grounded", row.location))
            threshold, amount = Decimal(str(row.minimum_spend)), Decimal(str(row.amount))
            amounts = _price_amounts(row.quote)
            terms = {(Decimal(match[1].replace(",", "")), Decimal(match[2].replace(",", "")))
                     for match in _COUPON_TERMS.finditer(row.quote)
                     if re.fullmatch(r"\d+(?:\.\d+)?", match[1].replace(",", ""))
                     and re.fullmatch(r"\d+(?:\.\d+)?", match[2].replace(",", ""))}
            if threshold not in amounts or terms and not any(pair[0] == threshold for pair in terms):
                issues.append(_session_issue(("coupon", "minimum_spend"), "coupon_threshold_not_grounded", row.location))
            if amount not in amounts or terms and (threshold, amount) not in terms:
                issues.append(_session_issue(("coupon", "amount"), "coupon_discount_not_grounded", row.location))
            scopes = _coupon_sku_scopes(row.quote, locations[row.location]["text"])
            if len(scopes) == 1 and {sku.casefold() for sku in row.skus} != next(iter(scopes)):
                issues.append(_session_issue(("coupon", "skus"), "coupon_sku_scope_not_grounded", row.location))
            if not scopes:
                restricted, matched_skus = _named_coupon_sku_scope(row.quote, source_text, source, prepared)
                if restricted and (not matched_skus or row.skus and
                        {sku.casefold() for sku in row.skus} != {sku.casefold() for sku in matched_skus}):
                    issues.append(_session_issue(("coupon", "skus"), "coupon_named_scope_not_grounded", row.location))
                elif restricted and not row.skus:
                    row = row.model_copy(update={"skus": matched_skus})
                    valid[("coupon",)] = row
                    value = value.model_copy(update={"coupon": row})
        if issues:
            raise _session_error(issues, "只核对列出的本场字段和引文，保留其他已核对设置。")
        for sku, row in table_prices.items():
            if sku in verified_prices and verified_prices[sku].amount != row.amount:
                table_gaps.append(f"{row.sku} 在表格和正文中有不同的直播售价，待确认。")
                conflicting.add(sku)
            verified_prices[sku] = row
        verified_prices = {sku: row for sku, row in verified_prices.items() if sku not in conflicting}
        gaps = list(value.gaps)
        if verified_prices and all(row == table_prices.get(sku) for sku, row in verified_prices.items()):
            # A proved table currency takes priority over the model's missing-currency note.
            # Any actual unresolved row still contributes its own program-derived gap.
            gaps = [gap for gap in gaps if not _MISSING_PRICE_CURRENCY.fullmatch(gap.strip())]
        if table_prices and not (value.window and value.window.start and value.window.end):
            table_gaps.append("已识别单品直播售价，活动开始和截止时间待补充。")
        parts = [row for row in valid.values() if not isinstance(row, Price)] + list(verified_prices.values())
        value = value.model_copy(update={"prices": list(verified_prices.values()),
                                         "rules": [row for path, row in valid.items() if path[0] == "rules"],
                                         "gaps": list(dict.fromkeys([*gaps, *table_gaps]))})
        out = value.model_dump(mode="json")
        # Reuse the catalog reader's explicit activity boundaries. A verified
        # rule within one unambiguous section anchors its complete original text.
        from .catalog_coverage import _PROMOTION_SECTION
        activity_ids = {unit["location"] for unit in prepared.get("source_units", [])
                        if unit.get("excluded_reason") == "session_promotion"}
        sections = []
        for location in locations.values():
            if location["location"] not in activity_ids:
                continue
            if _PROMOTION_SECTION.fullmatch(location["text"].strip()):
                sections.append([location])
            elif sections and sections[-1][0]["source_ref"] == location["source_ref"]:
                sections[-1].append(location)
        used = {row.location for row in [value.window, value.coupon, *value.prices, *value.rules] if row is not None}
        anchored = [section for section in sections if len(section) > 1
                    and used & {row["location"] for row in section[1:]}]
        if len(anchored) == 1:
            heading, *paragraphs = anchored[0]
            canonical = [{"location": row["location"], "quote": row["text"].strip()} for row in paragraphs]
            # Preserve rules from outside the known section as well.
            outside = [row for row in out["rules"] if row["location"] not in {p["location"] for p in paragraphs}]
            out["rules"] = canonical + outside
            if not out["theme"] and "直播" in heading["text"]:
                out["theme"] = {"location": heading["location"], "quote": heading["text"].strip()}
            known_text = "".join(row["quote"] for row in canonical)
            out["gaps"] = [gap for gap in out["gaps"] if not any(
                phrase and phrase in known_text for phrase in re.split(r"[。；;]", gap) if len(phrase) > 10)]
            parts.extend(Evidence.model_construct(**row) for row in canonical)
            if out["theme"]:
                parts.append(Evidence.model_construct(**out["theme"]))
        if value.coupon:
            # The coupon and the product-price override are one business rule.
            # Distinguish the expressly allowed combination from exclusions
            # concerning unrelated coupons in the same sentence.
            allowed = denied = False
            for rule in out["rules"]:
                for clause in re.split(r"[，,；;。\n]", rule["quote"]):
                    if not re.search(r"活动价|促销价|直播价|优惠价|商品价", clause):
                        continue
                    if re.search(r"(?:不可|不得|不能|无法|禁止|不支持|不与).{0,45}(?:叠加|一起使用|同享|同时使用)", clause):
                        denied = True
                    elif re.search(r"(?:可|可以|能|支持).{0,45}(?:叠加|一起使用|同享|同时使用)", clause):
                        allowed = True
            out["stacks_with_coupon"] = allowed and not denied
            if allowed and denied:
                out["gaps"].append("原文对优惠券能否与商品活动价同用存在冲突，需核对后再开启叠加。")
            elif value.stacks_with_coupon and not allowed and not denied:
                out["gaps"].append("原文未能明确核实优惠券是否可与商品活动价同用，待补充。")
        out["sources"] = {row.location: {"ref": locations[row.location]["source_ref"],
                                          "locator": locations[row.location]["locator"]}
                          for row in parts if row is not None}
        return out if any(row is not None for row in parts) or value.gaps else None
    except ValidationError as error:
        issues = []
        for issue in error.errors(include_input=False, include_context=False, include_url=False):
            path = list(issue["loc"])
            if len(path) > 1 and path[0] in row_indexes and isinstance(path[1], int):
                path[1] = row_indexes[path[0]][path[1]]
            issues.append(_session_issue(path, "schema_" + issue["type"]))
        raise _session_error(issues, "按 session 结构修正列出的字段。") from None


def _omit_unverified_session_values(candidate, issues):
    """Keep verified siblings and remove only values that fail the strict gate."""
    if not isinstance(candidate, dict):
        return None
    result = deepcopy(candidate)
    rejected_rows = {}
    for issue in issues:
        path = issue.get("path") or []
        if not path:
            continue
        field = path[0]
        value = result.get(field)
        if field in {"scene", "prices", "rules", "gaps"} and isinstance(value, list) and len(path) > 1 and type(path[1]) is int:
            indexes = rejected_rows.setdefault(field, set())
            indexes.add(path[1])
            if (field == "prices" and issue.get("reason") == "duplicate_activity_sku" and 0 <= path[1] < len(value)
                    and isinstance(value[path[1]], dict) and isinstance(sku := value[path[1]].get("sku"), str)):
                indexes.update(index for index, row in enumerate(value) if isinstance(row, dict)
                               and isinstance(row.get("sku"), str) and row["sku"].casefold() == sku.casefold())
        elif field in {"schedule", "window"} and isinstance(value, dict) and len(path) == 2 and path[1] in {"start", "end"}:
            value[path[1]] = None
        else:
            result.pop(field, None)
    for field, indexes in rejected_rows.items():
        if field in result:
            result[field] = [row for index, row in enumerate(result[field]) if index not in indexes]
    return result or None


def checked_session_soft(raw, prepared, *, allow_default_projection=False):
    """Return a source-checked subset and developer diagnostics without a model retry."""
    candidate = raw
    if allow_default_projection and (raw is None or isinstance(raw, dict)):
        candidate = _project_default_session(raw, *_session_source_context(prepared))
    diagnostics = []
    for attempt in range(5):
        try:
            result = checked_session(candidate, prepared, require_complete=attempt == 0)
            return result, diagnostics
        except ContractError as error:
            if error.status != 422 or error.code != "DOCUMENT_SESSION_EVIDENCE_INVALID":
                raise
            issues = error.details.get("issues", [])
            for issue in issues:
                if issue not in diagnostics:
                    diagnostics.append(issue)
            subset = _omit_unverified_session_values(candidate, issues)
            if attempt and subset == candidate:
                break
            candidate = subset
    # A persistent parser/schema issue cannot make unverified commercial data
    # valid. Source-reader and permission errors still take the original path.
    return None, diagnostics


def _import_key(engine, run, *, entry=None):
    hashes = []
    for ref in run["input"]["material_refs"]:
        material = engine.store.get("material", ref["id"])
        if material["conversation_id"] != run["conversation_id"] or material["artifact_ref"] != ref:
            raise ContractError("DOCUMENT_IMPORT_SCOPE", "资料不属于本次导入会话。", status=403)
        hashes.append(material["manifest"]["original_sha256"])
    entry = entry or import_entry(run)
    return "import_" + canonical_hash([entry, sorted(set(hashes)),
        run["scope"].get("preparation_id") if entry == "room" else None,
        run["input"]["text"].strip() if entry == "room" else None])[7:39]


def previous_import(engine, run):
    row = engine.store.get(KIND, _import_key(engine, run), required=False)
    if not row:
        if import_entry(run) == "room" and run["input"]["text"] == ROOM_DEFAULT_REQUEST:
            catalog = engine.store.get(KIND, _import_key(engine, run, entry="catalog"), required=False)
            if (catalog and catalog.get("result") and catalog["product_ids"] and
                    not any("未绑定款式" in gap for gap in catalog["result"]["missing_fields"])):
                return {**catalog, "reused_catalog": True}
        return None
    # Even a partial result can resume at the first missing business step.
    # When an activity was missing required SKUs, a deliberate follow-up
    # import can add them instead of trapping the original partial selection.
    if any("未绑定款式" in gap for gap in (row.get("result") or {}).get("missing_fields", [])):
        return None
    return row


async def existing_product(engine, scope, fields):
    sku, name = fields["sku_id"], fields["name"]
    matches = []
    cursor = 0
    while cursor is not None:
        result = await engine.business_query(scope, "search_products", {"q": sku, "limit": 50, "cursor": cursor})
        matches.extend(row for row in result["items"] if (row.get("sku_id") or "").casefold() == sku.casefold())
        cursor = result.get("next_cursor")
    normalize = lambda text: re.sub(r"[\W_]+", "", text).casefold()
    if len(matches) == 1 and normalize(matches[0]["name"]) == normalize(name):
        match = matches[0]
        if match.get("sale_status") != "unavailable":
            return match["ref"]
    if matches:
        raise ContractError("DOCUMENT_IMPORT_PRODUCT_IDENTITY", f"「{name}」的款式编号与已有商品冲突，请核对商品身份。", status=409)
    return None


def _configuration(session, sku_to_id):
    """Map checked session candidates to the existing draft shape."""
    if not session:
        return {}, []
    config, gaps = {}, list(session["gaps"])
    for key in ("theme", "objective"):
        if session[key]:
            config[key] = session[key]["quote"]
    if session["schedule"]:
        schedule = session["schedule"]
        if schedule["start"] and schedule["end"]:
            config["scheduled_at"] = {"start": schedule["start"], "end": schedule["end"]}
        else:
            gaps.append(f"直播排期仍待补全。原文「{schedule['quote']}」")
    window = session["window"]
    complete_window = bool(window and window["start"] and window["end"])
    if complete_window:
        config.update(campaign_starts_at=window["start"], campaign_ends_at=window["end"])
    elif window:
        gaps.append(f"活动时间仍待补全。原文「{window['quote']}」")
    # An incomplete time window belongs in the draft gap rather than public
    # promotion copy. Preserve separate rules whose content was verified.
    quotes = list(dict.fromkeys(row["quote"] for row in session["rules"] if not (
        window and not complete_window and row["location"] == window["location"] and row["quote"] == window["quote"])))
    if complete_window and not any(window["quote"] in quote for quote in quotes):
        quotes.insert(0, window["quote"])
    commerce = {}
    if session["scene"]:
        commerce["scene"] = "\n".join(row["quote"] for row in session["scene"])
    price_skus = {row["sku"].casefold() for row in session["prices"]}
    coupon = session["coupon"]
    coupon_skus = {sku.casefold() for sku in coupon["skus"]} if coupon else set()
    missing = sorted((price_skus | coupon_skus) - sku_to_id.keys())
    if missing:
        gaps.append("活动尚有未绑定款式 " + "、".join(missing) + "。待补齐后再导入整组优惠。")
    else:
        ids = sorted({sku_to_id[sku] for sku in price_skus | coupon_skus})
        if quotes:
            commerce["promotion"] = "\n".join(quotes)
            commerce["promotion_scope"] = "selected" if ids else "all"
            commerce["promotion_product_ids"] = ids
        if coupon:
            coupon_ids = sorted(sku_to_id[sku] for sku in coupon_skus)
            commerce["coupon"] = {"mode": "coupon", "amount": coupon["amount"], "currency": coupon["currency"],
                "minimum_spend": coupon["minimum_spend"], "condition": "\n".join(dict.fromkeys([coupon["quote"], *quotes]))[:1000],
                "scope": "selected" if coupon_ids else "all", "product_ids": coupon_ids, "discount": None}
        if price_skus:
            commerce["product_overrides"] = {sku_to_id[row["sku"].casefold()]:
                {"mode": "price", "price": row["amount"], "exclusive": not (
                    session["stacks_with_coupon"] is True and coupon is not None and
                    (not coupon_skus or row["sku"].casefold() in coupon_skus))}
                for row in session["prices"]}
    if commerce:
        config["commerce"] = commerce
    return config, list(dict.fromkeys(gaps))


def _same_offer_value(field, current, incoming):
    if current == incoming:
        return True
    if field in {"condition", "promotion"} and isinstance(current, str) and isinstance(incoming, str):
        # Treat line wrapping and spaces around Chinese punctuation as layout.
        # Keep token boundaries within amounts, SKU names, and other prose.
        def layout(text):
            return re.sub(r"(?<=[。！？；，、：]) | (?=[。！？；，、：])", "", " ".join(text.split()))

        return layout(current) == layout(incoming)
    if (field in {"product_ids", "promotion_product_ids"} and isinstance(current, list)
            and isinstance(incoming, list) and all(isinstance(item, str) for item in current + incoming)):
        return set(current) == set(incoming)
    if field == "coupon" and isinstance(current, dict) and isinstance(incoming, dict):
        return current.keys() == incoming.keys() and all(
            _same_offer_value(key, current[key], item) if key in {"condition", "product_ids"}
            else current[key] == item for key, item in incoming.items())
    return False


def _layout_only_offer_conflict(item):
    if item.get("field") not in {"优惠券", "活动说明"}:
        return False
    before, after = item.get("current"), item.get("incoming")
    return (isinstance(before, dict) and isinstance(after, dict) and before.keys() == after.keys()
            and all(_same_offer_value(key, before[key], value) for key, value in after.items()))


def _merge(draft, incoming, *, replace=False):
    """Merge by business group so mode, amount, scope and references stay consistent."""
    merged, conflicts = deepcopy(draft), []

    def apply(dst, values, label, keys):
        before = {key: dst[key] for key in keys if key in dst}
        different = any(dst.get(key) != item for key, item in values.items())
        if (different and label in {"优惠券", "活动说明"}
                and all(_same_offer_value(key, dst.get(key), item) for key, item in values.items())):
            return  # Equivalent reimports preserve the saved spelling and ID order.
        occupied = any(item not in (None, "", [], {}) for item in before.values())
        if label == "优惠券":
            occupied = (dst.get("coupon") or {}).get("mode", "none") != "none"
        elif label == "活动说明":
            occupied = bool(dst.get("promotion"))
        if different and occupied:
            conflicts.append({"field": label, "current": before, "incoming": values})
            if not replace:
                return
        dst.update(values)

    for key in ("theme", "objective", "scheduled_at", "campaign_starts_at", "campaign_ends_at"):
        if key in incoming:
            apply(merged, {key: incoming[key]}, {"theme": "本场主题", "objective": "本场目标",
                "scheduled_at": "直播排期", "campaign_starts_at": "活动开始时间", "campaign_ends_at": "活动截止时间"}[key], (key,))
    if "commerce" in incoming:
        dest = merged.setdefault("commerce", {})
        src = incoming["commerce"]
        for key, label in (("scene", "直播场景"), ("coupon", "优惠券"), ("promotion", "活动说明")):
            if key not in src:
                continue
            keys = (key, "promotion_scope", "promotion_product_ids") if key == "promotion" else (key,)
            values = {k: src[k] for k in keys}
            hidden = dest.get("hidden_sections", [])
            if key in hidden:
                conflicts.append({"field": label, "current": "已停用", "incoming": values})
                if not replace:
                    continue
                dest["hidden_sections"] = [section for section in hidden if section != key]
            apply(dest, values, label, keys)
        if "product_overrides" in src:
            overrides = dest.setdefault("product_overrides", {})
            for identity, row in src["product_overrides"].items():
                override = overrides.setdefault(identity, {})
                apply(override, {**row, "discount": None}, "单品活动价 " + identity,
                    ("mode", "price", "discount", "exclusive") if override.get("mode") != "none" else ())
    if not replace and any(item["field"] not in {"本场主题", "本场目标", "直播排期", "直播场景"}
                           for item in conflicts):
        # The time, thresholds, per-SKU prices and stacking policy form one
        # offer. Keeping an old component while importing the others may
        # silently create a new, unintended offer.
        ordinary = {key: value for key, value in incoming.items() if key in {"theme", "objective", "scheduled_at"}}
        if "scene" in incoming.get("commerce", {}):
            ordinary["commerce"] = {"scene": incoming["commerce"]["scene"]}
        merged, _ = _merge(draft, ordinary)
    return merged, conflicts


async def apply_document_import(engine, scope, *, products, session, unresolved_session=False):
    run = await engine.assert_scope(scope)
    entry = import_entry(run)
    if not entry or not run.get("catalog_intake_enabled"):
        raise ContractError("DOCUMENT_IMPORT_SCOPE", "没有本次资料导入授权。", status=403)
    identity = _import_key(engine, run)

    def progress(step, title, status="RUNNING"):
        engine._emit(run["id"], "workflow.progress", {"workflow": "catalog_intake",
            "step_id": step, "title": title, "status": status}, scope)

    async def write(key, args):
        await engine.assert_scope(scope)
        # Voice owns the mutation receipt. Stable IDs recover a committed
        # operation even if the HTTP response or local checkpoint was lost.
        token = canonical_hash([identity, key, args])[7:39]
        mutation = MutationContext(operation_id="doc_" + token)
        try:
            reply = await engine.client.command(key, args, mutation, run_id=run["id"], controller=True)
        except RemoteOutcomeUnknown:
            await engine.assert_scope(scope)
            state = await engine.client.query("read_operation_receipt", {"operation_id": mutation.operation_id},
                                              controller=True)
            if state["state"] == "committed":
                reply = state["receipt"]
            else:
                # The business API serializes mutations and deduplicates the
                # exact operation ID even if the first request is still in flight.
                reply = await engine.client.command(key, args, mutation, run_id=run["id"], controller=True)
        return reply["data"]

    async def read(key, **args):
        return await engine.client.query(key, args, controller=True)

    async with engine.action_lock(identity):
        row = engine.store.get(KIND, identity, required=False)
        if row is None:
            row = engine.store.put(KIND, {"id": identity, "entry": entry, "session": session,
                "product_ids": [item["ref"]["id"] for item in products], "owner_run_id": run["id"]})
        if row["entry"] != entry:
            raise ContractError("DOCUMENT_IMPORT_SCOPE", "导入回执范围不一致。", status=403)
        # An exact repeat reuses the original recognized activity. It may add
        # a newly approved SKU which the user omitted in the first import.
        session = row["session"]
        product_ids = list(dict.fromkeys([*row["product_ids"], *(item["ref"]["id"] for item in products)]))
        row = engine.store.update(KIND, identity, product_ids=product_ids)
        room_id, prep_id = row.get("room_id"), row.get("preparation_id")
        if entry == "room":
            prep_id = run["scope"]["preparation_id"]
            prep = await read("read_preparation", preparation_id=prep_id)
            room_id = prep["body"]["room_id"]
            if room_id != run["room_id"] or room_id != run["input"]["view_context"]["room_id"]:
                raise ContractError("DOCUMENT_IMPORT_SCOPE", "本场与原上传直播间不一致。", status=403)
        elif not room_id:
            progress("room", "正在为本次入库的商品创建直播间")
            if not products:
                raise ContractError("DOCUMENT_IMPORT_EMPTY_PRODUCTS", "没有可绑定的商品，无法创建直播间。", status=422)
            first = products[0]["body"]["name"]
            # The title may be inferred for display, never for identifying an existing room.
            label = re.split(r"[（(]", first, maxsplit=1)[0].strip()
            if len(products) > 1:
                other = products[1]["body"]["name"]
                common = ""
                for a, b in zip(label, other):
                    if a != b:
                        break
                    common += a
                label = common.rstrip(" -—｜·0123456789") if len(common) >= 4 else label
            quotes = " ".join(r["quote"] for r in (session or {}).get("rules", []))
            if session and session.get("window"):
                quotes += session["window"]["quote"]
            theme = ((session or {}).get("theme") or {}).get("quote", "")
            suffix = theme[:28] if theme else "双十一直播" if re.search(r"双十一|双11", quotes) else "直播间"
            name = (label[:75] + "｜" + suffix)[:100]
            # Freeze initial creation parameters before sending. A later
            # product revision or reload must replay exactly this request.
            args = row.get("room_args") or {"name": name, "initial_config": {}}
            engine.store.update(KIND, identity, room_args=args)
            room_id = (await write("create_room", args))["room_ref"]["id"]
            row = engine.store.update(KIND, identity, room_id=room_id)
        room = await read("read_room", room_id=room_id)
        if not prep_id:
            args = row.get("preparation_args") or {"room_id": room_id,
                "room_revision": room["ref"]["revision"], "goal": room["body"]["name"], "source_refs": []}
            engine.store.update(KIND, identity, preparation_args=args)
            prep_id = (await write("create_preparation", args))["preparation_ref"]["id"]
        row = engine.store.update(KIND, identity, room_id=room_id, preparation_id=prep_id)
        progress("room", "已核对目标直播间", "SUCCEEDED")
        progress("bind", "正在绑定并核对本次导入商品")
        for attempt in range(3):
            prep = await read("read_preparation", preparation_id=prep_id)
            refs = list(prep["body"]["selected_product_refs"])
            selected = {ref["id"] for ref in refs}
            for item in product_ids:
                if item not in selected:
                    refs.append((await read("read_product", product_id=item))["ref"])
            if refs == prep["body"]["selected_product_refs"]:
                break
            try:
                await write("update_preparation_products", {"preparation_id": prep_id,
                    "expected_revision": prep["ref"]["revision"], "selected_product_refs": refs})
            except ContractError as error:
                if error.code != "REVISION_CONFLICT" or attempt == 2:
                    raise
        prep = await read("read_preparation", preparation_id=prep_id)
        refs = prep["body"]["selected_product_refs"]
        if not set(product_ids) <= {ref["id"] for ref in refs}:
            raise ContractError("DOCUMENT_IMPORT_BIND_READBACK", "本场选品回读不一致，请继续核对。", status=409)
        sku_to_id = {}
        for ref in refs:
            record = await read("read_product", ref=ref)
            sku = record["body"].get("sku_id")
            if sku:
                sku_to_id[sku.casefold()] = ref["id"]
        progress("bind", f"已核对本场绑定的 {len(product_ids)} 款导入商品", "SUCCEEDED")
        progress("session", "正在核对并保存本场配置草稿")
        incoming, gaps = _configuration(session, sku_to_id)
        conflicts = []
        if not session and not unresolved_session:
            gaps.append(_NO_ACTIVITY_GAP)
        elif session and not (session.get("schedule") or {}).get("start"):
            gaps.append("文档给出的活动时间不能代替直播排期。计划开播时间待补充。"
                        if (session.get("window") or {}).get("start") else "计划开播时间待补充。")
        if session and not incoming and not gaps:
            gaps.append("资料中未找到可写入的本场配置。")
        prior_outcome = row.get("result") or {}
        already_applied = bool(prior_outcome.get("draft_ref") and prior_outcome.get("incoming_hash") == canonical_hash(incoming))
        for attempt in range(3):
            prep = await read("read_preparation", preparation_id=prep_id)
            ref = prep["body"].get("draft_ref")
            old = (await read("read_context", ref=ref, sections=["draft"]))["body"]["draft"] if ref else {}
            if already_applied and ref:
                # Exact reupload must not revert subsequent human changes.
                conflicts = [item for item in prior_outcome.get("conflicts", [])
                             if not _layout_only_offer_conflict(item)]
                break
            merged, conflicts = _merge(old, incoming)
            if conflicts:
                evidence = ", ".join((session or {}).get("sources", {}).keys())
                def display(value):
                    if not isinstance(value, dict):
                        return str(value) or "未填写"
                    if "coupon" in value:
                        coupon = value["coupon"]
                        return f"满 {coupon.get('minimum_spend') or 0} 减 {coupon.get('amount')}" if coupon.get("mode") == "coupon" else "已设置优惠"
                    if "promotion" in value:
                        return value["promotion"] or "未填写"
                    if "price" in value:
                        return str(value["price"]) if value["price"] is not None else "已设置优惠"
                    return "，".join(str(item) for item in value.values() if item is not None)
                prompt = "\n".join(["本场草稿已有不同配置。活动规则会按整组保留或更新。",
                    *(f"{item['field']}：当前 {display(item['current'])} → 导入 {display(item['incoming'])}" for item in conflicts),
                    "来源位置 " + evidence])
                decision = await engine.request_decision(scope, {"kind": "document_import_conflict", "prompt": prompt,
                    "options": [{"value": "keep", "label": "保留当前冲突设置，保存可独立更新的内容"},
                                {"value": "replace", "label": "采用导入设置"}]},
                    "doc-conflict-" + canonical_hash([ref, conflicts])[7:39])
                if decision.get("answer") == "replace":
                    merged, _ = _merge(old, incoming, replace=True)
            if gaps and (session or entry == "catalog" and not ref):
                prior_gap_labels = {_NO_ACTIVITY_GAP}
                if session and session.get("prices") and not any("币种" in gap for gap in gaps):
                    prior_gap_labels.update(gap for gap in merged.get("unknowns", [])
                                            if _MISSING_PRICE_CURRENCY.fullmatch(gap.strip()))
                carried = [gap for gap in merged.get("unknowns", []) if not session or gap not in prior_gap_labels]
                merged["unknowns"] = list(dict.fromkeys([*carried, *gaps]))
            if merged == old and ref:
                break
            try:
                saved = await write("save_session_draft", {"preparation_id": prep_id,
                    "expected_revision": prep["ref"]["revision"], "draft": merged})
            except ContractError as error:
                if error.code == "REVISION_CONFLICT" and attempt < 2:
                    continue
                raise
            prep = await read("read_preparation", preparation_id=prep_id)
            ref = saved["draft_ref"]
            if prep["body"].get("draft_ref") != ref:
                raise ContractError("DOCUMENT_IMPORT_DRAFT_READBACK", "本场配置草稿回读不一致。", status=409)
            readback = (await read("read_context", ref=ref, sections=["draft"]))["body"]["draft"]
            commercial = merged.get("commerce", {})
            actual = readback["commerce"]
            def matches(expected, value):
                if isinstance(expected, dict):
                    return isinstance(value, dict) and all(matches(item, value.get(key)) for key, item in expected.items())
                return expected == value
            if (any(readback.get(key) != merged.get(key) for key in incoming if key != "commerce")
                    or not matches(commercial, actual)):
                raise ContractError("DOCUMENT_IMPORT_DRAFT_READBACK", "本场配置与保存值不一致。", status=409)
            break
        result = {"room_id": room_id, "room_name": room["body"]["name"], "preparation_id": prep_id,
                  "draft_ref": ref, "product_ids": product_ids, "activity_found": bool(session),
                  "conflicts": conflicts, "missing_fields": gaps, "incoming_hash": canonical_hash(incoming),
                  "source_locations": (session or {}).get("sources", {})}
        engine.store.update(KIND, identity, result=result)
        engine.store.update("run", run["id"], document_import_result=result)
        progress("session", "已保存并回读本场配置草稿", "SUCCEEDED")
        return result


async def verify_import(engine, run):
    result = run.get("document_import_result") or {}
    if not result.get("draft_ref") or not result.get("preparation_id"):
        return {"ok": False, "missing": ["本场配置草稿保存回执"]}
    prep = await engine.client.query("read_preparation", {"preparation_id": result["preparation_id"]}, controller=True)
    body = prep["body"]
    ok = (body["room_id"] == result["room_id"] and body["draft_ref"] == result["draft_ref"]
          and set(result["product_ids"]) <= {ref["id"] for ref in body["selected_product_refs"]})
    return {"ok": ok, "missing": [] if ok else ["房间、本场草稿或已绑定商品与保存回执不一致"]}

