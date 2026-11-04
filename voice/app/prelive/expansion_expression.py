"""Compare expansion text against immutable originals and completed siblings."""
import re
import unicodedata

from .storage import BusinessError


CONTRACT = "expansion-expression-v1"
SPAN_LIMIT = 24
SENTENCE_LIMIT = 18

WRITING_INSTRUCTION = """本任务是在原稿基础上扩写独立可播版本。可以、也应该保留同一商品的已核实卖点、名称、规格、价格和必要条件；区别写法，不强求虚构另一组卖点。按 expansion_expression.expression_direction 组织本版句式和节奏。
原稿用来理解写作任务和识别应避免的表达；同批稿正文由声播留在写作请求之外，保存前自动查重。把本版写法落实到每个所选模块，介绍、卖点和收尾也按本版结构重新组织。跳过可套在任何产品上的通用欢迎语、场景过渡和促单套话。例稿仅供理解口播节奏，不沿用其句型。必要的商品名称、数字和短术语可照用。连续二十四个有效字相同或较长整句重复会被拦截。事实编号只逐字复制 allowed_fact_ids 中的值。"""

EXPRESSION_DIRECTIONS = (
    "动作叙事。开场直接让观众想象一个具体动作；亮品句把商品接进这个动作；卖点按行动先后讲述；收尾邀请观众迈出第一步。",
    "选购判断。开场直接问一个选购难题，不展开使用故事；亮品句先给判断答案再报品名；卖点用两条判断标准和对应事实；收尾提醒观众核对最在意的标准，再做选择。",
    "适用人群。开场直接叫住一种具体人群，用陈述句说出他们的需要；亮品句从这类人的购买理由引出商品；卖点按两种需要分别说明适用之处，不按操作步骤铺陈；收尾落到人群自己的使用计划。",
)

_NUM = r"[0-9一二两三四五六七八九十百千零〇]+"
_LENGTH = re.compile(rf"(?:每版|整版|全稿)\s*(?:约|大约|控制在)?\s*({_NUM})\s*(?:到|至|[-–—])\s*({_NUM})\s*个?\s*(?:中文|有效|口播|字数|\s)*字")


def _count(value):
    if value.isascii():
        return int(value)
    digits = dict(zip("零一二三四五六七八九", range(10))) | {"两": 2, "〇": 0}
    total = digit = 0
    for char in value:
        if char in digits:
            digit = digits[char]
        elif char in "十百千":
            total += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
        else:
            return 0
    return total + digit


def requested_short_range(actions):
    matches = {(_count(low), _count(high)) for action in actions if action.get("action") == "generate"
               for low, high in _LENGTH.findall(action.get("instruction", ""))}
    if len(matches) == 1:
        low, high = next(iter(matches))
        if 120 <= low <= high < 1920:
            return low, high
    return None


def _normalize(text):
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def _rows(modules):
    return {key: [line for row in value.get("sentences", [])
        if (line := _normalize(row.get("spoken_text") or row.get("text") or ""))]
        for key, value in modules.items() if value}


def compare_expression(candidate, reference):
    """Return a copied excerpt when detectable; punctuation isn't a rewrite."""
    old = _rows(reference)
    new = _rows(candidate)
    old_sentences = {line for rows in old.values() for line in rows if len(line) >= SENTENCE_LIMIT}
    old_chunks = ["".join(rows) for rows in old.values()]
    grams = {part[i:i + SPAN_LIMIT] for part in old_chunks for i in range(len(part) - SPAN_LIMIT + 1)}
    for key, rows in new.items():
        for line in rows:
            if line in old_sentences:
                return {"module_key": key, "kind": "same_sentence", "repeated_chars": len(line), "excerpt": line[:72]}
        part = "".join(rows)
        for i in range(len(part) - SPAN_LIMIT + 1):
            excerpt = part[i:i + SPAN_LIMIT]
            if excerpt in grams:
                return {"module_key": key, "kind": "copied_span", "repeated_chars": SPAN_LIMIT, "excerpt": excerpt}
    return None


def references(service, c, job, spec):
    if spec.get("expansion_expression_contract") != CONTRACT:
        return []
    origin = spec["original_script_ref"]
    refs = [origin, *spec.get("expansion_comparison_refs", [])]
    for other in job["items"]:
        if other["input"]["variant_id"] == spec["variant_id"]:
            break
        if other["status"] == "succeeded" and other["input"].get("original_script_ref") == origin:
            refs.append(other["output"]["script_ref"])
    result, seen = [], set()
    for ref in refs:
        if ref["id"] in seen:
            continue
        seen.add(ref["id"])
        record = service._artifact(c, ref, "script_revision", job["preparation_id"])
        if record["body"]["product_id"] != spec["product_ref"]["id"]:
            raise BusinessError("ORIGINAL_SCRIPT_TARGET_MISMATCH")
        result.append({"ref": record["ref"], "version_number": record["body"]["version_number"],
            "modules": {key: service._artifact(c, value, "module")["body"]
                        for key, value in record["body"]["module_refs"].items() if value}})
    return result


def writing_context(job, spec):
    # The writer gets a distinct brief; the save gate compares full sibling text.
    ordinal = len(spec.get("expansion_comparison_refs", []))
    for item in job["items"]:
        if item["input"]["variant_id"] == spec["variant_id"]:
            break
        if item["input"].get("original_script_ref") == spec["original_script_ref"]:
            ordinal += 1
    return {"contract": CONTRACT, "expression_direction": EXPRESSION_DIRECTIONS[ordinal % len(EXPRESSION_DIRECTIONS)]}


def verify_expression(candidate, values):
    for value in values:
        overlap = compare_expression(candidate, value["modules"])
        if overlap:
            raise BusinessError("SCRIPT_EXPANSION_REPEATED_EXPRESSION",
                "这版话术与已有版本有较长的相同表达，尚未保存。请换写标出的段落，原稿和其他已保存版本不受影响。",
                status=422, details={**overlap, "comparison_version": value["version_number"]})
