"""Consumer-facing catalog admission and legacy generation projections.

Extraction/placement is a model task, using the existing product-facts pass.
This module is a small deterministic backstop, not another classifier call.
Writes are rejected with safe coordinates; approved text is never silently
rewritten. Projections protect old snapshots without mutating saved revisions.
"""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata

from opencc import OpenCC
from shengbo_contracts.business import ProductConsumerContentInvalidDetails, _PRODUCT_DIAGNOSTIC_NAMES

_SIMPLIFY = OpenCC("t2s").convert


POLICY_VERSION = "consumer-catalog-v1"
_RULES = (
    ("supply_channel", re.compile(r"(?:[a-z0-9.-]*\.)?1688\.com\b|alibaba\.com\b|1688\s*(?:平台|网站|商品|店铺|采购|供货|货源)|(?:来源|来自|购于|采购于|平台|货源)\s*[:：]?\s*1688|阿里巴巴(?:采购|货源|供货|店铺|批发)|(?:1688\s*)?offer\s*id|供货渠道|进货渠道|采购链接|货源链接|供货链接|供应链等级|混批设置|mix_num|mix_amount", re.I)),
    ("purchase_cost", re.compile(r"进[货]?价|采购价|供货价|批发价|拿货价|供货报价|最小起订|起订量|价格阶梯|新人供货价")),
    ("supplier_identity", re.compile(r"供应商|供货商|供货店铺|供货店名|供货公司|供货方|供应方|进货店铺|采购店铺")),
    ("supplier_metrics", re.compile(r"供货库存|供应商库存|供货店铺销量|页面销量|页面评价|店铺回头率|综合履约率|品质达标率|入驻年限|跨商品(?:混入|评价)|评价区.*(?:店铺维度|其他型号)|SKU\s*覆盖率|sold_count", re.I)),
    ("task_metadata", re.compile(r"本阶段(?:不|仅|先)|当前任务|不开展顾客研究|不提炼(?:核心)?卖点|暂不生成(?:直播)?话术|schema_hash|field_dispositions|未实测|(?:资料|参数)适用范围|页面原始展示值|原始商品标题|本次采集样本|采集样本(?:情感|主题)|图片绑定\s*100%|价格覆盖率|库存覆盖率")),
)
# These are catalog *placement* mistakes, not forbidden consumer knowledge.
_KNOWLEDGE_SPEC_NAMES = re.compile(r"^(?:上市年份(?:季节)?|上市季节|加工定制|加印\s*logo|是否跨境出口(?:专供货源)?|外贸出口认证|专利类型|是否IP授权)$", re.I)
_UNKNOWN = re.compile(r"^(?:其他|其它|未知|不详|未提供|未确认|待确认|暂无|无资料|未填写|不适用|非已确认品牌名)$")
_KEY_REASON = {
    "supplier": "supplier_identity", "supplier_name": "supplier_identity", "supplier_id": "supplier_identity",
    "supplier_url": "supply_channel", "offer_id": "supply_channel",
    "purchase_price": "purchase_cost", "cost_price": "purchase_cost", "wholesale_price": "purchase_cost",
    "supplier_stock": "supplier_metrics", "supplier_reviews": "supplier_metrics",
}
_OPAQUE_KEYS = {"id", "product_id", "image", "created_at", "updated_at", "content_hash"}
# Placement only: recognise explicit FAQ documents or paired labelled Q/A.
# A topic mentioning a customer's question, or containing a question mark,
# is not by itself a FAQ and must not lose valid narrative knowledge.
_FAQ_TITLE = re.compile(r"(?<![a-z0-9])FAQ(?![a-z0-9])|常见问题|常見問題|(?:服务|服務|商品|购买|購買)?问答|問答", re.I)
_QUESTION_LINE = re.compile(r"^\s*(?:(?:[-*]|\d+[.)、])\s*)?(?:问(?:题)?|問題|Q(?:uestion)?)\s*[:：]", re.I | re.M)
_ANSWER_LINE = re.compile(r"^\s*(?:(?:[-*]|\d+[.)、])\s*)?(?:答(?:案)?|A(?:nswer)?)\s*[:：]", re.I | re.M)


def _is_faq_document(document):
    if not isinstance(document, dict):
        return False
    title = unicodedata.normalize("NFKC", str(document.get("title", "")))
    content = unicodedata.normalize("NFKC", str(document.get("content", "")))
    return bool(_FAQ_TITLE.search(title) or
                (_QUESTION_LINE.search(content) and _ANSWER_LINE.search(content)))


def policy_metadata():
    return {
        "version": POLICY_VERSION,
        "purpose": "商品库只保存本商家向直播顾客可使用的事实、核心卖点和服务知识；原件/来源/采购审计留在Agent材料及证据中。",
        "write_error_code": "PRODUCT_CONSUMER_CONTENT_INVALID",
        "classification": {
            "specifications": "简洁、顾客需要的明确参数；颜色与共同套装构成分行；名称不带页面/来源括号。",
            "knowledge_documents": "按主题保存有依据的叙述型消费者知识，如材质、使用、适用、上市及服务说明；不是FAQ容器，不收录供货审计。",
            "faq": "明确的问题与回答、常见购买/定制问答只保存在faq；标题为FAQ/常见问题/问答或正文成对问答的文档须归入faq。",
            "omit": "供货平台/链接/店名/OfferID/进价/供货库存评价、任务说明、未知占位和无意义重复项。",
        },
        "information_coverage": "保留有依据且对顾客相关的次要信息，按主题放知识文档；不因规格精简就删除已确认资料，不以无关供货字段填充。",
        "deduplication": "一个信息优先一个结构化正本；FAQ只为真实常见问题提供简洁回答，不另建同内容FAQ知识文档。",
        "faq_generation": {
            "questions": "按本商品资料、用户疑问与已有事实识别常见购买问题；允许把已知事实转成顾客口语问句，不按品类常识编答案或固定凑数量。",
            "answers": "问答只存faq，先直接答购买判断，再给本商品已支持事实与必要条件；原件/用户明确补充/已确认字段才可支持答案，文风示例与合成问答不是商品证据。",
            "missing": "缺关键事实时将待补问题留Agent分析缺项，不编答案或把内部补料说明存入消费者字段；未知可选项不阻塞其他有依据内容。",
            "updates": "更新须保留已确认问答并遵守本次精确商品、字段与版本范围；文风变化不授权改价格、其他字段或其他商品。",
        },
        "unknown_facts": "不知道则留空/隐藏；产地不等于发货地。平台默认不构成具体小时承诺。",
        "enforcement": "现有提取模型一次分类；确定性准入拒绝并给字段坐标；不增加模型调用，不静默修改待批准内容。",
    }


def _text_reason(value, protected=()):
    text = _SIMPLIFY(unicodedata.normalize("NFKC", str(value)))
    for reason, pattern in _RULES:
        if pattern.search(text):
            return reason
    if any(literal and _SIMPLIFY(unicodedata.normalize("NFKC", literal)) in text for literal in protected):
        return "supplier_identity"
    return None


def _protected_literals(value):
    """Use explicitly labelled source identities, never guess by company suffix.

    A real retail brand/company name is allowed. Once a value is identified as
    supplier identity in this same input, an unlabelled duplicate cannot leak
    through a different field or historical nested context.
    """
    result = set()
    def visit(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key in {"supplier", "supplier_name", "supplier_id", "offer_id"} and isinstance(child, str) and len(child.strip()) >= 4:
                    result.add(child.strip())
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, str):
            for match in re.finditer(r"(?:供应商名称|供应商|供货商|供货店铺名称|供货店铺|供货店名|供货公司|进货店铺)\s*[:：]\s*([^\n；;，,。|]{4,100})", node):
                result.add(match.group(1).strip())
            result.update(re.findall(r"(?:offer[/\s:=：]|offer\s*id\s*[:：])\s*(\d{8,})", node, re.I))
    visit(value)
    return result


def consumer_content_issues(value):
    """Return stable safe coordinates, never input values or sensitive labels."""
    protected = _protected_literals(value)
    issues = []
    def add(path, reason, action="remove_from_product"):
        if len(issues) < 60:
            pieces = path.removeprefix("/").split("/")
            safe = "/" + "/".join(piece if piece in _PRODUCT_DIAGNOSTIC_NAMES or piece.isdecimal()
                                   else "unknown_field" for piece in pieces)
            issues.append({"path": safe, "reason": reason, "action": action})
    def visit(node, path=""):
        if isinstance(node, dict):
            for key, child in node.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                child_path = path + "/" + escaped
                if key in _KEY_REASON and child not in (None, "", [], {}):
                    add(child_path, _KEY_REASON[key])
                elif key not in _OPAQUE_KEYS:
                    visit(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, path + "/" + str(index))
        elif isinstance(node, str) and (reason := _text_reason(node, protected)):
            add(path, reason)
    visit(value)
    for index, spec in enumerate(value.get("specifications", [])):
        name, content = str(spec.get("name", "")), str(spec.get("value", ""))
        if _KNOWLEDGE_SPEC_NAMES.fullmatch(name.strip()):
            add(f"/specifications/{index}", "specification_placement", "move_to_knowledge")
        elif _UNKNOWN.fullmatch(content.strip()):
            add(f"/specifications/{index}", "specification_placement", "omit_unknown")
        elif re.search(r"[（(]\s*页面\s*[）)]", name):
            add(f"/specifications/{index}/name", "specification_placement")
    for index, document in enumerate(value.get("knowledge_documents", [])):
        if _is_faq_document(document):
            add(f"/knowledge_documents/{index}", "knowledge_placement", "move_to_faq")
    return issues


def assert_consumer_product(value):
    from .prelive.storage import BusinessError
    issues = consumer_content_issues(value)
    if issues:
        details = ProductConsumerContentInvalidDetails.model_validate({"policy_version": POLICY_VERSION,
            "commit_state": "not_committed", "issues": issues,
            "retryable_after_correction": True}, strict=True).model_dump(mode="json")
        raise BusinessError("PRODUCT_CONSUMER_CONTENT_INVALID",
            "商品字段包含供货信息、任务说明或内容归类错误，请按字段位置整理消费者可用内容后重试",
            status=422, details=details)


def consumer_context_view(value):
    """Remove recognised legacy contamination from generation-only copies.

    This does not migrate data, change approvals, or silently commit anything.
    Whole sensitive lines/specifications are excluded rather than redacting a
    few characters that might leave a supplier price or identity behind.
    """
    protected = _protected_literals(value)
    def clean(node):
        if isinstance(node, str):
            return "\n".join(line for line in node.splitlines() if not _text_reason(line, protected))
        if isinstance(node, list):
            return [clean(child) for child in node]
        if not isinstance(node, dict):
            return deepcopy(node)
        result = {}
        for key, child in node.items():
            if key in _KEY_REASON:
                continue
            if key == "specifications" and isinstance(child, list):
                result[key] = [clean(spec) for spec in child if isinstance(spec, dict)
                    and not _text_reason(str(spec.get("name", "")) + "：" + str(spec.get("value", "")), protected)
                    and not _KNOWLEDGE_SPEC_NAMES.fullmatch(str(spec.get("name", "")).strip())
                    and not _UNKNOWN.fullmatch(str(spec.get("value", "")).strip())]
            elif key == "knowledge_documents" and isinstance(child, list):
                documents = []
                for document in child:
                    if not isinstance(document, dict) or _text_reason(document.get("title", ""), protected):
                        continue
                    projected = clean(document)
                    if projected.get("content", "").strip():
                        documents.append(projected)
                result[key] = documents
            elif key in _OPAQUE_KEYS:
                result[key] = deepcopy(child)
            else:
                result[key] = clean(child)
        return result
    return clean(value)
