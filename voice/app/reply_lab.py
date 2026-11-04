"""Durable, room-scoped comment simulations and a small local retrieval pipeline.

Retrieval is Chinese character-bigram BM25 with explicit topic expansion, not a
vector database. Only current product-library records can enter model context.
No playback action or platform write is available from this module.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
import os
import re
import time
from typing import Annotated, Literal
import urllib.error
import urllib.request

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from .commerce_config import active_product_fields
from .consumer_catalog import consumer_context_view

TASKS = {}
RETRIEVAL_VERSION = "local-bm25-zh-v1"
TOPICS = {
    "price": ("多少钱|价格|售价|便宜|贵不贵|优惠|折扣|活动价|原价|促销", "价格 售价 活动价 原价 优惠 促销"),
    "shipping": ("发货|到货|送到|配送|物流|寄出|备货|多久|几天|包邮|运费", "发货 配送 物流 寄出 备货 工作日 运费 包邮"),
    "size": ("尺码|几码|多大|脚宽|脚背|脚胖|脚型|脚肉|宽脚|尺寸|大一号|小一号", "尺码 尺寸 标准码 脚背 宽 脚型 大一号"),
    "safety": ("防滑|滑倒|湿|水|浴室|洗澡|安全|摔", "防滑 浴室 地面 遇水 小心 行走 奔跑"),
    "care": ("清洗|洗|脏|晒|高温|气味|臭|保养|打理|阴干", "清洗 高温 曝晒 气味 通风 阴干 保养"),
    "color": ("颜色|色差|什么色|粉色|白色|黑色", "颜色 色差 灯光 屏幕 实物"),
    "service": ("退货|退款|售后|退换|质量|保修|不合适|无理由", "退货 退款 售后 退换 保修 条件"),
    "stock": ("库存|现货|还有货|缺货|卖完|剩多少", "库存 现货 缺货"),
    "material": ("材质|材料|什么做|环保|EVA", "材质 材料 EVA 环保"),
    "gifts": ("赠品|加赠|送什么|送不送|有送|赠送", "本场 赠品 加赠 规则 条件"),
    "usage": ("怎么用|如何使用|使用方法|安装|能用|可以用|练习|训练|下课|课后", "使用 说明 适用 练习 训练 课程 自主 入门"),
}


class ReplyCitation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    chunk_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class ReplyOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    status: Literal["answered", "needs_confirmation", "chat"]
    citations: list[ReplyCitation] = Field(max_length=12)
    missing_information: list[str] = Field(default_factory=list, max_length=8)


REPLY_OUTPUT_SCHEMA = json.dumps(ReplyOutput.model_json_schema(), ensure_ascii=False, separators=(",", ":"))


def srv():
    from . import server
    return server


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


def prompt():
    guidance = (srv().ROOT / "prompts/评论回复_当前.txt").read_text(encoding="utf-8")
    return guidance.rstrip() + "\n\n输出结构约束（JSON Schema）：\n" + REPLY_OUTPUT_SCHEMA


def room_products(identity):
    config = srv().settings(identity)
    ids = set(config["product_ids"])
    return config, [p for p in srv().all_of("product") if p["id"] in ids and not p.get("archived")
                    and p.get("sale_status", "available") == "available"]


def chunks(text):
    # Preserve line/paragraph boundaries so a quote can be located in the source.
    for line_no, line in enumerate(str(text).splitlines(), 1):
        line = srv().SIMPLIFY(line.strip())
        if not line:
            continue
        for offset in range(0, len(line), 350):
            yield line_no, line[offset:offset + 400]


def document_specs(product):
    product = active_product_fields(product)
    name = srv().SIMPLIFY(product["name"])
    prefix = f"商品{product.get('display_number', 1):03} · {name}"
    money = lambda value: format(value, '.2f').rstrip('0').rstrip('.')
    price = f"{name}；原价/常规价格：{money(product['price'])} {product['currency']}。"
    price += (f"活动价格：{money(product['promo_price'])} {product['currency']}。" if product.get("promo_price") is not None else "未填写单独的活动价格。")
    core_points = product.get("core_selling_points") or []
    selling = "\n".join(point for point in core_points if isinstance(point, str) and point.strip()) or product.get("selling_points", "")
    result = [
        ("identity", "商品名称", name),
        ("price", "价格", price),
        ("selling", "核心卖点", selling),
        ("promotion", "促销机制", product.get("promotion", "")),
        ("knowledge", "知识库 / 注意事项", product.get("knowledge", "")),
    ]
    platform = {"douyin": "抖音", "tiktok": "TikTok", "kuaishou": "快手", "other": "其他平台"}.get(product.get("sales_platform"))
    if platform:
        result.append(("sales_platform", "售卖平台", platform))
    result += [("doc_" + d["id"], d["title"], d["content"]) for d in product.get("knowledge_documents", [])]
    specifications = "\n".join(s["name"] + "：" + s["value"] for s in product.get("specifications", []))
    result += [("specifications", "规格属性", specifications)]
    for field, title in [("material", "材质"), ("ingredients", "成分"), ("target_audience", "适用人群"),
                         ("storage_conditions", "储存条件"), ("shipping_origin", "发货地"),
                         ("shipping_time", "发货时效"), ("return_policy", "特殊退换货条款"),
                         ("usage_instructions", "官方使用说明/安装指南"), ("faq", "商品常见问答 FAQ")]:
        result.append((field, title, product.get(field, "")))
    if product.get("shelf_life_value"):
        result.append(("shelf_life", "保质期", str(product["shelf_life_value"]) + {"day":"天", "month":"个月", "year":"年"}[product.get("shelf_life_unit", "month")]))
    return [{"id": product["id"] + ":" + key, "product_id": product["id"], "product_name": name,
             "title": prefix + " / " + title, "kind": key, "content": srv().SIMPLIFY(text),
             "updated_at": product.get("updated_at"), "version": digest([title, text])}
            for key, title, text in result if str(text).strip()]


def sync_index(products):
    """Index revision is rebuilt transactionally only when its product changes."""
    with srv().db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS rag_documents(id TEXT PRIMARY KEY, product_id TEXT, body TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS rag_chunks(id TEXT PRIMARY KEY, product_id TEXT, body TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS rag_manifest(product_id TEXT PRIMARY KEY, fingerprint TEXT)")
        conn.execute("CREATE INDEX IF NOT EXISTS rag_product ON rag_chunks(product_id)")
        for product in products:
            docs = document_specs(product)
            fingerprint = digest([RETRIEVAL_VERSION, docs])
            previous = conn.execute("SELECT fingerprint FROM rag_manifest WHERE product_id=?", (product["id"],)).fetchone()
            if previous and previous[0] == fingerprint:
                continue
            conn.execute("DELETE FROM rag_documents WHERE product_id=?", (product["id"],))
            conn.execute("DELETE FROM rag_chunks WHERE product_id=?", (product["id"],))
            for doc in docs:
                conn.execute("INSERT INTO rag_documents VALUES(?,?,?)", (doc["id"], product["id"], json.dumps(doc, ensure_ascii=False)))
                for i, (line, text) in enumerate(chunks(doc["content"]), 1):
                    chunk = {"id": doc["id"] + f":c{i}", "document_id": doc["id"], "title": doc["title"],
                             "product_id": product["id"], "product_name": doc["product_name"], "kind": doc["kind"],
                             "ordinal": i, "line": line, "text": text, "version": doc["version"], "updated_at": doc["updated_at"]}
                    conn.execute("INSERT INTO rag_chunks VALUES(?,?,?)", (chunk["id"], product["id"], json.dumps(chunk, ensure_ascii=False)))
            conn.execute("INSERT OR REPLACE INTO rag_manifest VALUES(?,?)", (product["id"], fingerprint))


def read_index(products, config=None):
    sync_index(products)
    ids = {p["id"] for p in products}
    with srv().db() as conn:
        docs = [json.loads(r[0]) for r in conn.execute("SELECT body FROM rag_documents ORDER BY rowid")]
        entries = [json.loads(r[0]) for r in conn.execute("SELECT body FROM rag_chunks ORDER BY rowid")]
    docs, entries = [d for d in docs if d["product_id"] in ids], [c for c in entries if c["product_id"] in ids]
    if config is None:
        return docs, entries
    # Room evidence is rebuilt from persisted references; never overwrite the
    # shared product index with another room's prices or shipping promises.
    scoped_docs, scoped_entries = [], []
    for product in products:
        effective = srv().commerce.effective_product(product, config)
        q = effective["_price_quote"]
        product_docs = document_specs(effective)
        for doc in product_docs:
            if doc["kind"] == "price":
                final = str(q["final_price"]) if q["confirmed"] else "待确认（不得承诺最终到手价）"
                doc["content"] = f"日常售价：{q['daily_price']} {q['currency']}；本场单件到手价：{final} {q['currency']}。{q['reason']}。"
            is_room = doc["kind"] in {"price", "promotion"} or (effective["_shipping_overridden"] and doc["kind"] == "shipping_time")
            doc.update(scope="room" if is_room else "product", source_level="P1" if is_room else "P2")
            if is_room:
                doc["id"] = config["id"] + ":" + doc["id"]
                doc["title"] += " / 本场生效配置"
            scoped_docs.append(doc)
        c = config["commerce"]
        extra = [("scene", "本场特色场景与口播话术", c["scene"])]
        extra += [("room_doc_" + d["id"], d["title"], d["content"]) for d in c["knowledge_documents"]]
        for key, title, content in extra:
            if content.strip():
                scoped_docs.append({"id": config["id"] + ":" + product["id"] + ":" + key, "product_id": product["id"],
                    "product_name": product["name"], "title": title, "kind": key, "content": content,
                    "scope": "room", "source_level": "P1_context", "version": digest(content), "updated_at": config.get("updated_at")})
        for doc in [d for d in scoped_docs if d["product_id"] == product["id"]]:
            for i, (line, text) in enumerate(chunks(doc["content"]), 1):
                # A prose FAQ cannot override a structured room price/shipping rule.
                prose = doc["kind"] in {"knowledge", "selling", "faq", "scene"} or doc["kind"].startswith(("doc_", "room_doc_"))
                if prose and ((effective.get("shipping_time") and re.search(r"发货|寄出|备货", text)) or
                    re.search(r"(?:活动价|到手价|优惠价|满\s*\d+\s*减)", text)):
                    continue
                scoped_entries.append({"id": doc["id"] + f":c{i}", "document_id": doc["id"], "title": doc["title"],
                    "product_id": product["id"], "product_name": product["name"], "kind": doc["kind"], "ordinal": i,
                    "line": line, "text": text, "version": doc["version"], "updated_at": doc["updated_at"],
                    "scope": doc["scope"], "source_level": doc["source_level"]})
    # Room notes may predate consumer catalog admission too. Apply the same
    # legacy boundary before any snippets can be presented to a reply model.
    scoped_docs = [doc for doc in consumer_context_view(scoped_docs)
                   if doc["content"].strip() and doc["title"].strip()]
    scoped_entries = [entry for entry in consumer_context_view(scoped_entries)
                      if entry["text"].strip() and entry["title"].strip()]
    return scoped_docs, scoped_entries


def tokenize(text):
    text = srv().SIMPLIFY(text).lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens += [run[i:i+2] for i in range(len(run)-1)] if len(run) > 1 else [run]
    return tokens


def retrieve(products, query, limit=6, config=None):
    _, entries = read_index(products, config)
    topics = [k for k, (pattern, _) in TOPICS.items() if re.search(pattern, query, re.I)]
    expanded = query + " " + " ".join(TOPICS[k][1] for k in topics)
    terms = set(tokenize(expanded))
    counters = [Counter(tokenize(c["text"] + " " + c["title"].rsplit("/", 1)[-1])) for c in entries]
    average = sum(sum(c.values()) for c in counters) / max(1, len(counters))
    frequencies = Counter(t for c in counters for t in c)
    scored = []
    for chunk, counter in zip(entries, counters):
        matches = sorted(terms.intersection(counter))
        if not matches:
            continue
        length = sum(counter.values())
        score = sum(math.log(1 + (len(entries) - frequencies[t] + .5)/(frequencies[t] + .5)) *
                    (counter[t]*2.2)/(counter[t] + 1.2*(.25 + .75*length/max(1, average))) for t in matches)
        # Explicit knowledge docs outrank a vague marketing description for FAQ.
        if chunk["kind"] == "knowledge" or chunk["kind"].startswith("doc_"):
            score *= 1.15
        scored.append({**chunk, "score": round(score, 4), "matched_terms": matches, "reason": "评论关键词与主题扩展匹配"})
    scored.sort(key=lambda c: (-c["score"], c["id"]))
    result = scored[:limit]
    authoritative = {"price": "price", "shipping": "shipping_time", "service": "return_policy", "gifts": "promotion"}
    for topic, kind in authoritative.items():
        if topic in topics:
            for chunk in entries:
                if chunk["kind"] == kind and chunk["id"] not in {c["id"] for c in result}:
                    result.append({**chunk, "score": 0, "matched_terms": [], "reason": "当前生效的结构化规则"})
    # Identity is factual context, labelled as such rather than claiming a match.
    for chunk in entries:
        if chunk["kind"] == "identity" and chunk["id"] not in {c["id"] for c in result}:
            result.append({**chunk, "score": 0, "matched_terms": [], "reason": "当前商品基础信息"})
    return {"query": query, "expanded_query": expanded, "topics": topics,
            "method": RETRIEVAL_VERSION, "indexed_chunks": len(entries), "chunks": result}


def choose_product(products, requested, text, history):
    if requested:
        if requested not in {p["id"] for p in products}:
            raise HTTPException(400, "测试商品必须是当前直播间引用的可用商品")
        return requested
    if len(products) == 1:
        return products[0]["id"]
    matches = [p["id"] for p in products if srv().SIMPLIFY(p["name"]) in text or
               (p.get("sku_id") and p["sku_id"].lower() in text.lower()) or
               re.search(r"商品\s*0*" + str(p.get("display_number", 1)) + r"(?!\d)", text)]
    if len(matches) == 1:
        return matches[0]
    # Only an already established product in this test conversation, never another room.
    if not matches and history and history[-1].get("product_id") in {p["id"] for p in products}:
        return history[-1]["product_id"]
    return None


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError("non-finite JSON constant")


def complete_remote(context, *, max_tokens=1800, timeout=65):
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("尚未配置 DeepSeek API Key，请在本地配置后重试。")
    body = {"model": os.environ.get("DEEPSEEK_REPLY_MODEL", os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")),
            "messages": [{"role": "system", "content": prompt()},
                         {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            "response_format": {"type": "json_object"}, "max_tokens": max_tokens, "stream": False,
            "thinking": {"type": "disabled"}}
    request = urllib.request.Request("https://api.deepseek.com/chat/completions", data=json.dumps(body).encode(),
              headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
        raw = json.loads(data["choices"][0]["message"]["content"],
                         object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"DeepSeek 请求失败（HTTP {e.code}）；请检查服务或账户后重试。") from None
    except Exception:
        raise RuntimeError("DeepSeek 连接超时或返回格式异常；评论与检索记录已保留，可以重试。") from None
    return raw, {"model": body["model"], "usage": data.get("usage", {}), "provider": "DeepSeek"}


def compact(text):
    return re.sub(r"\s+", "", srv().SIMPLIFY(text))


def validate_answer(raw, retrieval):
    try:
        output = ReplyOutput.model_validate(raw)
    except ValidationError:
        raise RuntimeError("模型回复结构不符合要求；本次回答未发布，请重试。") from None
    allowed = {c["id"]: c for c in retrieval["chunks"]}
    for entry in output.citations:
        if entry.chunk_id not in allowed:
            raise RuntimeError("模型引用了未检索到的来源；本次回答未发布，请重试。")
        quote = compact(entry.quote)
        if len(quote) < 2 or quote not in compact(allowed[entry.chunk_id]["text"]):
            raise RuntimeError("模型引用原文无法核对；本次回答未发布，请重试。")
    if output.status == "answered" and not output.citations:
        raise RuntimeError("模型给出了商品结论但没有原文依据；请重试。")
    if output.status == "chat" and not output.citations and re.search(r"\d+\s*(元|新台币|台币)|EVA|防滑|保温|包邮|一体成型", output.answer, re.I):
        raise RuntimeError("模型在闲聊回复中加入了无引用的商品特点；请重试。")
    return {"answer": srv().SIMPLIFY(output.answer), "answer_status": output.status,
            "citations": [citation.model_dump() for citation in output.citations],
            "missing_information": output.missing_information}


def list_sessions(identity):
    srv().settings(identity)
    items = [s for s in srv().all_of("reply_session") if s["room_id"] == identity]
    return sorted(items, key=lambda s: (s["updated_at"], s["id"]), reverse=True)


def get_session(identity):
    session = srv().get("reply_session", identity)
    srv().settings(session["room_id"])
    turns = [t for t in srv().all_of("reply_turn") if t["session_id"] == identity]
    for turn in turns:
        if turn["status"] in {"queued", "running"} and turn["id"] not in TASKS:
            turn.update(status="interrupted", error="服务已重启，回复生成已中断；已保存评论与检索依据，可点击重试。", updated_at=srv().now())
            srv().put("reply_turn", turn)
    return {"session": session, "turns": sorted(turns, key=lambda t: t["ordinal"])}


def create_session(identity):
    config, _ = room_products(identity)
    return srv().put("reply_session", {"id": srv().uid("reply_session"), "room_id": identity,
                    "title": "新测试", "room_name_at_creation": config["name"], "created_at": srv().now(), "updated_at": srv().now()})


async def run_turn(turn):
    started = time.perf_counter()
    try:
        turn.update(status="running", updated_at=srv().now(), error=None)
        srv().put("reply_turn", turn)
        session_data = get_session(turn["session_id"])
        config, products = room_products(turn["room_id"])
        history = [t for t in session_data["turns"] if t["ordinal"] < turn["ordinal"] and t["status"] == "completed"
                   and t["username"] == turn["username"]][-6:]
        pid = choose_product(products, turn.get("requested_product_id"), turn["comment"], history)
        turn["product_id"] = pid
        if not products or not pid:
            answer = "本直播间还没有选择商品，请先到直播间配置里从商品库选品。" if not products else "你想问哪件商品呢？请告诉我商品名称或编号。"
            turn.update(answer=answer, answer_status="needs_confirmation", citations=[], missing_information=["本场商品" if not products else "所询问的商品"],
                        retrieval={"method": RETRIEVAL_VERSION, "query": turn["comment"], "chunks": [], "topics": [], "indexed_chunks": 0}, model="规则澄清（未调用大模型）", status="completed")
        else:
            selected = [p for p in products if p["id"] == pid]
            query = turn["comment"]
            # Carry the previous viewer question only for brief/pronominal follow-ups.
            if history and (len(query) < 8 or re.search(r"那|这个|它|刚才|还有", query)) and history[-1].get("product_id") == pid:
                query = history[-1]["comment"] + " " + query
            turn["retrieval"] = retrieve(selected, query, config=config)
            turn["product_name"] = selected[0]["name"]
            turn["prompt_revision"] = digest(prompt())
            srv().put("reply_turn", turn)
            context = {"today": srv().now()[:10], "room": {"id": config["id"], "name": config["name"]},
                       "catalog": [{"id": p["id"], "name": p["name"], "number": p.get("display_number")} for p in products],
                       "current_product_id": pid, "evidence": turn["retrieval"]["chunks"],
                       "history_for_reference_only": consumer_context_view(
                           [{"comment": t["comment"], "answer": t["answer"]} for t in history]),
                       "viewer_name": turn["username"], "comment": turn["comment"]}
            raw, metadata = await asyncio.to_thread(complete_remote, context)
            turn.update(**validate_answer(raw, turn["retrieval"]), **metadata, status="completed")
    except asyncio.CancelledError:
        turn.update(status="interrupted", error="回复生成已中断，记录已保留。")
        raise
    except Exception as e:
        turn.update(status="error", error=str(e) if isinstance(e, (RuntimeError, HTTPException)) else "回复处理失败，记录已保留，可重试。")
    finally:
        turn.update(updated_at=srv().now(), elapsed_seconds=round(time.perf_counter()-started, 2))
        srv().put("reply_turn", turn)
        session = srv().get("reply_session", turn["session_id"])
        session.update(updated_at=srv().now())
        srv().put("reply_session", session)


def start_task(turn):
    task = asyncio.create_task(run_turn(deepcopy(turn)))
    TASKS[turn["id"]] = task
    task.add_done_callback(lambda _: TASKS.pop(turn["id"], None))


async def send_comment(session_id, data):
    current = get_session(session_id)
    session, turns = current["session"], current["turns"]
    text = str(data.get("text", "")).strip()
    username = str(data.get("username", "测试观众")).strip()
    request_id = str(data.get("request_id", "")).strip()
    if not text or len(text) > 500 or not username or len(username) > 40 or not re.fullmatch(r"[\w-]{8,80}", request_id):
        raise HTTPException(400, "评论为1至500字，观众名称不超过40字，请使用有效请求编号")
    prior = next((t for t in turns if t["request_id"] == request_id), None)
    if prior:
        if prior["comment"] != text or prior.get("requested_product_id") != data.get("product_id"):
            raise HTTPException(409, "请求编号已用于其他评论")
        return prior
    if any(t["status"] in {"queued", "running"} for t in turns):
        raise HTTPException(409, "本次会话上一条回复尚未完成，请稍后再发送。")
    _, products = room_products(session["room_id"])
    choose_product(products, data.get("product_id"), text, [])  # Validate scope before saving.
    turn = {"id": srv().uid("reply_turn"), "session_id": session_id, "room_id": session["room_id"],
            "ordinal": len(turns)+1, "request_id": request_id, "requested_product_id": data.get("product_id"),
            "username": username, "comment": text, "status": "queued", "created_at": srv().now(), "updated_at": srv().now(),
            "source": "simulated", "answer": "", "citations": [], "error": None, "attempts": 1}
    srv().put("reply_turn", turn)
    session.update(title=text[:28] if not turns else session["title"], updated_at=srv().now())
    srv().put("reply_session", session)
    # Register before any await: a concurrent poll must not mistake this queued
    # request for a job abandoned by a previous process.
    start_task(turn)
    # Read-only production monitor can observe the simulated comment, never the reply.
    await srv().test_event(session["room_id"], {"type": "comment", "username": username, "text": text,
                                              "test_session_id": session_id, "reply_turn_id": turn["id"]})
    return turn


async def retry_turn(identity):
    turn = srv().get("reply_turn", identity)
    current = get_session(turn["session_id"])
    turn = next(t for t in current["turns"] if t["id"] == identity)
    if turn["status"] not in {"error", "interrupted"}:
        raise HTTPException(409, "仅失败或中断的回复可以重试")
    if any(t["status"] in {"queued", "running"} for t in current["turns"]):
        raise HTTPException(409, "本次会话有回复正在生成，请稍后重试")
    turn.update(status="queued", error=None, attempts=turn.get("attempts", 1)+1, updated_at=srv().now())
    srv().put("reply_turn", turn)
    start_task(turn)
    return turn


def knowledge_view(identity):
    config, products = room_products(identity)
    docs, entries = read_index(products, config)
    effective = [srv().commerce.effective_product(p, config) for p in products]
    return {"room": config, "products": [{"id": p["id"], "name": p["name"], "display_number": p.get("display_number"),
             "price": p["price"], "promo_price": p.get("promo_price"), "currency": p["currency"],
             "sku_id": p.get("sku_id") or p["id"], "price_quote": p["_price_quote"]} for p in effective],
            "documents": [{**d, "chunk_count": sum(c["document_id"] == d["id"] for c in entries)} for d in docs],
            "chunk_count": len(entries), "retrieval_method": RETRIEVAL_VERSION,
            "prompt": prompt(), "prompt_revision": digest(prompt()), "deepseek_configured": bool(os.environ.get("DEEPSEEK_API_KEY"))}
