"""Closed source identifiers shared by whole-script and single-module writers.

This describes the request, not a provider-enforced decoder. Local validation
remains authoritative and never guesses which source an invalid ID meant.
"""
from copy import deepcopy

REVISION = "closed-fact-reference-v1"
GUIDANCE = """
fact_ids只能从本次allowed_fact_ids逐字选择，大小写一致；它们是facts条目的id，不是商品字段名或研究标题。不得自行发明target_audience、shipping_time等字段型ID，也不得引用未列出的SELLING或PROMOTION。NAME只支持名称，PRICE只支持当前价格，DETAILS支持其中明列的规格/顾客适用信息，KNOWLEDGE支持其中明列的消费者知识；仅在存在时使用SELLING/PROMOTION。逐句依据实际来源选择一个或多个ID，合法ID本身不证明断言成立，不能用DETAILS掩盖没有依据的效果。纯互动的fact_ids为空数组。
""".strip()


def request_contract(schema, facts):
    ids = [item["id"] for item in facts]
    result = deepcopy(schema)
    sentence = result.get("$defs", {}).get("ModuleSentence") or result.get("$defs", {}).get("ScriptUtterance")
    if not isinstance(sentence, dict):
        raise ValueError("ModuleSentence schema is missing")
    sentence["properties"]["fact_ids"]["items"] = {"type": "string", "enum": ids}
    return {"fact_reference_contract": REVISION, "allowed_fact_ids": ids,
            "fact_reference_instructions": GUIDANCE, "response_schema": result}
