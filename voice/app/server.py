from __future__ import annotations

import asyncio
from contextlib import contextmanager, asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import errno
import json
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
import uuid

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from opencc import OpenCC

from .speech import POOL, render
from . import production, reply_lab, live_source, live_history, commerce_config as commerce
from .welcome import WelcomeRuntime, build_product_templates
from .like_thanks import LikeThanksRuntime
from .prosody import sentence_style, profile_catalog, interaction_style
from .copywriting_quality import check_versions, content_issues, required_features
from .prelive.storage import Store as VersionedStore, BusinessError, canonical as canonical_record
from .consumer_catalog import assert_consumer_product
from .voice_catalog import supported_voice, voice_catalog

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = timezone(timedelta(hours=8))
RUNTIME = Path(os.environ.get("SHENGBO_RUNTIME", str(ROOT / "runtime")))
RUNTIME.mkdir(parents=True, exist_ok=True)
SIMPLIFY = OpenCC("t2s").convert
for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines() if os.environ.get("SHENGBO_SKIP_DOTENV") != "1" and (ROOT / ".env").exists() else []:
    if "=" in line and not line.startswith("#"):
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex[:16]


def now():
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


_VERSIONED_STORES = {}


def _legacy_store():
    key = str(RUNTIME.resolve())
    if key not in _VERSIONED_STORES:
        _VERSIONED_STORES[key] = VersionedStore(RUNTIME)
    return _VERSIONED_STORES[key]


def _active_prelive():
    application = globals().get("app")
    service = getattr(application.state, "prelive_service", None) if application else None
    return service if service and service.runtime.resolve() == RUNTIME.resolve() else None


@contextmanager
def db():
    with _legacy_store().transaction() as connection:
        yield connection


def put(kind, item, *, expected_revision=None):
    if kind == "script" and item.get("prelive_managed"):
        raise HTTPException(409, "此话术由播前助手管理，请在右侧对话修改指定模块，不能保存为独立旧稿")
    value = {k: deepcopy(v) for k, v in item.items() if not k.startswith("_record_")}
    if kind == "product":
        try:
            assert_consumer_product(value)
        except BusinessError as error:
            raise HTTPException(error.status, {"code": error.code, "message": error.message,
                "details": error.details}) from None
    expected_revision = expected_revision if expected_revision is not None else item.get("_record_revision")
    store = _legacy_store()
    try:
        with store.transaction(write=True) as connection:
            existing = connection.execute("SELECT revision FROM records WHERE kind=? AND id=?", (kind, item["id"])).fetchone()
            if kind == "script" and connection.execute("SELECT 1 FROM records WHERE kind='script_revision' AND id=?", (item["id"],)).fetchone():
                raise HTTPException(409, "受管话术不能被旧接口覆盖，请在播前助手中修改")
            if kind == "product":
                sku = value.get("sku_id") or value["id"]
                for record in store.all(connection, "product"):
                    if record["ref"]["id"] != value["id"] and (record["body"].get("sku_id") or record["ref"]["id"]) == sku:
                        raise HTTPException(409, "SKU ID 已存在，请刷新商品库后检查")
                occupied = connection.execute("SELECT product_id FROM prelive_skus WHERE sku_id=?", (sku,)).fetchone()
                if occupied and occupied[0] != value["id"]:
                    raise HTTPException(409, "SKU ID 已存在，请检查商品身份")
                connection.execute("DELETE FROM prelive_skus WHERE product_id=?", (value["id"],))
                connection.execute("INSERT INTO prelive_skus VALUES(?,?)", (sku, value["id"]))
            ref = store.put(connection, kind, value["id"], value,
                            expected=expected_revision if expected_revision is not None else existing[0] if existing else None,
                            create=existing is None)
            if kind in {"product", "config", "script"}:
                store.event(connection, "catalog" if kind == "product" else value["id"], "legacy.record_saved", {"ref": ref, "origin": "legacy_ui"})
    except BusinessError as error:
        raise HTTPException(error.status, {"code": error.code, "message": "资料已变化，请刷新后重试", "details": error.details}) from None
    item["_record_revision"] = ref["revision"]
    return item


def all_of(kind):
    with db() as connection:
        rows = connection.execute("SELECT body,revision FROM records WHERE kind=? ORDER BY rowid", (kind,)).fetchall()
    result = [{**json.loads(row[0]), "_record_revision": row[1]} for row in rows]
    if kind == "script" and _active_prelive():
        service = _active_prelive()
        with service.store.transaction() as connection:
            identities = [row[0] for row in connection.execute("SELECT id FROM records WHERE kind='script_revision'").fetchall()]
        result.extend(service.legacy_script_projection(identity) for identity in identities if not any(r["id"] == identity for r in result))
    return result


def get(kind, identity):
    with db() as connection:
        row = connection.execute("SELECT body,revision FROM records WHERE kind=? AND id=?", (kind, identity)).fetchone()
    if not row:
        if kind == "script" and _active_prelive():
            try:
                return _active_prelive().legacy_script_projection(identity)
            except BusinessError as error:
                if error.code != "NOT_FOUND":
                    raise HTTPException(error.status, error.message) from None
        raise HTTPException(404, "记录不存在")
    return {**json.loads(row[0]), "_record_revision": row[1]}


def _expected_record_revision(request, record):
    if request is None:
        return record.get("_record_revision")
    raw = request.headers.get("x-shengbo-expected-revision")
    if not raw or not raw.isdigit():
        raise HTTPException(428, {"code": "REVISION_REQUIRED", "message": "请刷新页面后重试，避免覆盖其他任务的修改"})
    expected = int(raw)
    if expected != record.get("_record_revision"):
        raise HTTPException(409, {"code": "REVISION_CONFLICT", "message": "资料已更新，请刷新后再修改"})
    return expected


class KnowledgeDocumentInput(BaseModel):
    id: str = Field(default_factory=lambda: uid("knowledge"), pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=15000)


class ProductInput(commerce.ProductFields):
    name: str = Field(min_length=1, max_length=60)
    currency: str = Field(default="CNY", pattern="^(CNY|TWD|USD|AUD)$")
    price: float = Field(ge=0, le=10000000)
    promo_price: float | None = Field(default=None, ge=0, le=10000000)
    selling_points: str = Field(default="", max_length=15000)
    promotion: str = Field(default="", max_length=2000)
    knowledge: str = Field(default="", max_length=15000)
    knowledge_documents: list[KnowledgeDocumentInput] = Field(default_factory=list, max_length=20)
    image: str = Field(default="", max_length=4500000)

    def record(self, identity=None):
        item = self.model_dump()
        prior = next((p for p in all_of("product") if p["id"] == identity), {})
        if prior:
            item = {**item, **prior, **{k: v for k, v in item.items() if k in self.model_fields_set}}
        if "core_selling_points" in self.model_fields_set:
            item["selling_points"] = "\n".join(item["core_selling_points"])
        elif "selling_points" in self.model_fields_set:
            item["core_selling_points"] = []  # legacy clients edit the single canonical prose field
        for key in item.get("hidden_optional_fields", []):
            if key == "shelf_life":
                item.update(shelf_life_value=None, shelf_life_unit="month")
            else:
                item[key] = [] if key in {"specifications", "knowledge_documents"} else ""
        if len({d["id"] for d in item["knowledge_documents"]}) != len(item["knowledge_documents"]):
            raise HTTPException(400, "知识文档编号不能重复")
        if any(not d["title"].strip() or not d["content"].strip() for d in item["knowledge_documents"]):
            raise HTTPException(400, "请填写知识文档标题与内容")
        if item["image"] and not (item["image"].startswith("data:image/png;base64,") or item["image"].startswith("data:image/jpeg;base64,") or item["image"].startswith("data:image/webp;base64,")):
            raise HTTPException(400, "商品图片只支持本地PNG、JPEG、WebP")
        existing = all_of("product")
        prior = next((p for p in existing if p["id"] == identity), {})
        number = prior.get("display_number") or max([p.get("display_number", i + 1) for i, p in enumerate(existing)] or [0]) + 1
        item.update(id=identity or uid("product"), display_number=number, updated_at=now(), archived=False)
        item["sku_id"] = item.get("sku_id") or item["id"]
        if any(p["id"] != item["id"] and (p.get("sku_id") or p["id"]) == item["sku_id"] for p in existing):
            raise HTTPException(400, "SKU ID 已存在")
        return item


def product_facts(product):
    product = commerce.active_product_fields(product)
    price_text = (f"售价 {product['price']} {product['currency']}；没有另设活动价" if product.get("promo_price") is None else
                  f"原价 {product['price']} {product['currency']}；活动价 {product['promo_price']} {product['currency']}")
    facts = [{"id": "NAME", "text": product["name"]},
             {"id": "PRICE", "text": price_text},
             {"id": "SELLING", "text": product["selling_points"]},
             {"id": "PROMOTION", "text": product["promotion"]}]
    details = []
    for field, label in [("material", "材质"), ("ingredients", "成分"), ("target_audience", "适用人群")]:
        if product.get(field):
            details.append(label + "：" + product[field])
    details += [s["name"] + "：" + s["value"] for s in product.get("specifications", [])]
    if details:
        facts.append({"id": "SPECIFICATIONS", "text": "\n".join(details)})
    if product.get("_room_scene"):
        facts.append({"id": "ROOM_SCENE", "text": "本场场景与口播要求（不得据此新增价格、库存、功效或服务承诺）：" + product["_room_scene"]})
    if product.get("_price_quote") and not product["_price_quote"]["confirmed"]:
        facts = [f for f in facts if f["id"] != "PRICE"] + [{"id": "PRICE", "text":
            f"日常售价 {product['price']} {product['currency']}；最终优惠价尚未确认，不能把日常售价称作到手价。" + product["_price_quote"]["reason"]}]
    facts += [{"id": f"KNOWLEDGE_{i+1}", "text": text} for i, text in enumerate(product["knowledge"].splitlines()) if text.strip()]
    return facts


def generation_facts(product):
    # Knowledge remains available to future Q&A, but never enters script generation.
    return [f for f in product_facts(product) if not f["id"].startswith("KNOWLEDGE_")]


def script_product_keys(script):
    keys = ["name", "price", "promo_price", "currency", "selling_points", "promotion"]
    if any(str(f.get("id", "")).startswith("KNOWLEDGE_") for f in script.get("facts", [])):
        keys.append("knowledge")
    return keys


def structure_script(product, raw, source, label, facts=None):
    identity = uid("script")
    sentences = []
    blocks = []
    for index, block in enumerate(raw.get("blocks", [])):
        block_id = f"{identity}_b{index+1:03}"
        if not isinstance(block, dict) or not isinstance(block.get("sentences"), list):
            raise ValueError("话术段落结构错误")
        blocks.append({"id": block_id, "stage": str(block.get("stage", "benefit")), "focus": str(block.get("focus", "")), "ordinal": index+1})
        for entry in block["sentences"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("display_text"), str):
                raise ValueError("话术句子结构错误")
            text = SIMPLIFY(entry["display_text"].strip())
            # Exact sentence-level playback, even if a model merges sentences.
            parts = [part.strip() for part in re.findall(r"[^。！？!?\n]+[。！？!?]?", text) if part.strip()]
            for part in parts:
                sentences.append({"id": f"{identity}_s{len(sentences)+1:03}", "block_id": block_id,
                                  "block_ordinal": index+1, "stage": str(block.get("stage", "benefit")),
                                  "ordinal": len(sentences)+1, "text": part,
                                  "spoken_text": SIMPLIFY(str(entry.get("spoken_text") or part)) if len(parts) == 1 else part,
                                  "kind": entry.get("kind", "product_claim"),
                                  "fact_ids": entry.get("fact_ids", [])})
    if not sentences or len(sentences) > 300:
        raise ValueError("稿件须有1至300句")
    facts = product_facts(product) if facts is None else facts
    valid_ids = {fact["id"] for fact in facts}
    issues = []
    for sentence in sentences:
        refs = sentence["fact_ids"]
        if not isinstance(refs, list) or any(ref not in valid_ids for ref in refs):
            issues.append(f"第{sentence['ordinal']}句事实引用不正确")
        elif sentence["kind"] != "engagement" and not refs:
            issues.append(f"第{sentence['ordinal']}句缺少商品事实依据")
        if re.search(r"零风险|绝对不会|站多久都不|不会滑倒|我(自己)?(穿了|用过)|库存(紧张|不多)|下播.*恢复", sentence["text"]):
            issues.append(f"第{sentence['ordinal']}句有待核实的保证、体验或促销说法")
        if re.search(r"没有接入|未接入|API|接口|系统(未|没有)|页面.*未提供", sentence["text"], re.I):
            issues.append(f"第{sentence['ordinal']}句包含面向消费者不需要的实现细节，请修改")
    return {"id": identity, "product_id": product["id"], "product_snapshot": deepcopy(product),
            **({"commerce_signature": commerce.generation_signature(product)} if "_price_quote" in product else {}),
            "revision": 1, "label": label, "angle": str(raw.get("angle", "")), "locale": "zh-CN",
            "source": source, "status": "draft", "blocks": blocks, "sentences": sentences,
            "welcome_templates": build_product_templates(product),
            "facts": facts, "issues": list(dict.fromkeys(issues)), "missing_information": [], "created_at": now()}


GENERATOR_SYSTEM = (ROOT / "prompts/直播口播_当前.txt").read_text(encoding="utf-8")


def _generate_remote_once(product, count, prior_versions=None, revision_feedback=None):
    product = commerce.active_product_fields(product)
    prior_versions = commerce.active_context_products(prior_versions or [])
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("尚未配置DeepSeek API Key")
    # Full previous prose primes verbatim copying. Supply route/topic summaries instead.
    variation_context = [{"angle": str(v.get("angle", ""))[:180], "opening": v.get("opening", [])[:1],
                          "benefit_order": v.get("benefit_order", [])[:12],
                          "topics": [str(line)[:24] for line in v.get("outline", [])[:10]]}
                         for v in (prior_versions or [])[-3:]]
    body = {"model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
            "messages": [{"role": "system", "content": GENERATOR_SYSTEM},
                         {"role": "user", "content": json.dumps({"facts": generation_facts(product), "version_count": count,
                            "locale": "zh-CN", "date": datetime.now(SHANGHAI).strftime("%Y-%m-%d"),
                            "prior_versions_for_variation_only": variation_context,
                            "must_mention_each_version": [label for label, _ in required_features(product)],
                            "writing_route": ("先讲核心设计和使用体验，再讲打理、携带和选择。" if not prior_versions else
                                "先从不同生活问题切入，先讲已有稿后置的卖点。维护携带或外观选择在前，核心设计体验在后；稀疏商品可按颜色/容量/使用场景→把手或结构→材质展开。全部必要事实仍要覆盖。"),
                            "revision_feedback": revision_feedback or "",
                            "variation_instruction": "参考已有版本只用于变化开场和卖点展开顺序，不从已有话术新增商品事实。"}, ensure_ascii=False)}],
            "stream": False, "max_tokens": min(26000, count*6000), "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"}}
    request = urllib.request.Request("https://api.deepseek.com/chat/completions", data=json.dumps(body).encode(),
              headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        code = error.code
        raise RuntimeError(f"DeepSeek请求失败（HTTP {code}），请检查模型权限、余额或服务状态") from None
    except Exception:
        raise RuntimeError("DeepSeek连接失败或超时，现有稿件已保留") from None
    content = result["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    raw = json.loads(content)
    versions = raw.get("versions", [])
    if not isinstance(versions, list) or len(versions) != count:
        actual = len(versions) if isinstance(versions, list) else "格式异常"
        raise RuntimeError(f"DeepSeek返回{actual}版，要求{count}版；未覆盖已有稿件，可重试生成")
    scripts = [structure_script(product, item, "DeepSeek", str(item.get("label", i+1)), generation_facts(product)) for i, item in enumerate(versions)]
    for script in scripts:
        script["missing_information"] = raw.get("missing_information", [])
        script["usage"] = result.get("usage", {})
        script["model"] = body["model"]
        script["prompt_revision"] = hashlib.sha256(GENERATOR_SYSTEM.encode()).hexdigest()[:12]
    return scripts


def generate_remote(product, count, prior_versions=None):
    scripts = _generate_remote_once(product, count, prior_versions)
    failures = check_versions(scripts, prior_versions)
    missing = content_issues(product, scripts)
    if failures or missing:
        feedback = {
            'issues': missing + (['跨版本展开雷同，请重写下面列出的场景句；改生活情境、解释方式和节奏，不能只换同义词。'] if failures else []),
            'near_copy_sentences_to_replace': list(dict.fromkeys(line['current'] for check in failures for line in check['near'])),
            'drafts_to_revise': [{'blocks': [{**block, 'sentences': [
                {'display_text': entry['text'], 'spoken_text': entry.get('spoken_text', entry['text']),
                 'kind': entry.get('kind', 'product_claim'), 'fact_ids': entry.get('fact_ids', [])}
                for entry in script['sentences'] if entry.get('block_id') == block['id']]}
                for block in script['blocks']]} for script in scripts],
            'instruction': '在本次草稿上修订，不要把完整讲解缩成概括。替换雷同句、补齐遗漏，保留准确的价格规格和其他有效展开，输出完整versions JSON。丰富商品至少700个汉字，目标900至1300字；补具体情境解释，不凑空话。',
        }
        scripts = _generate_remote_once(product, count, prior_versions,
            feedback)
        remaining = content_issues(product, scripts)
        if check_versions(scripts, prior_versions) or remaining:
            detail = '；'.join(remaining) if remaining else '与已有版本过于相似'
            raise RuntimeError("话术自动重写一次后仍未通过检查：" + detail + "；已有话术未改动，请重试。")
        for script in scripts: script['quality_rewrite_attempts'] = 1
    else:
        for script in scripts: script['quality_rewrite_attempts'] = 0
    return scripts


def seed():
    if all_of("product"):
        return
    sample = json.loads((ROOT / "data/samples/slippers.product.json").read_text(encoding="utf-8"))
    facts = sample["facts"]
    selling = "\n".join(str(f["value"]) if not isinstance(f["value"], list) else "、".join(f["value"]) for f in facts[:15])
    product = ProductInput(name=sample["name"].replace("撐", "撑").replace("靜", "静").replace("涼", "凉"),
        currency="TWD", price=178, promo_price=107, selling_points=selling,
        promotion="原价178新台币，活动价107新台币；限时优惠，截止时间未提供。",
        knowledge="浴室地面遇水仍须小心行走，不要奔跑。\n新鞋可能有材质气味，可通风阴干。\n避免长时间曝晒或高温。\n颜色可能因灯光和屏幕略有差异，以实物为准。\n标准尺码，脚背宽或脚型偏肉可斟酌大一号。\n备货2–8工作日，正式播出前请重新确认；原配送日期缺少年份，不作当前承诺。") .record("product_slippers_001")
    put("product", product)
    for label, angle, entries in [
        ("测试A", "居家讲解 · 非模型生成的测试短稿", [
            ("welcome", "欢迎来到声音直播测试。", [], "engagement"),
            ("identity", "接下来介绍这款足弓支撑静音防滑厚底凉拖鞋，小蛮腰女款。", ["NAME"], "product_claim"),
            ("benefit", "这款凉拖鞋采用EVA材质一体成型，设计包含足弓支撑和厚底软弹。", ["SELLING"], "product_claim"),
            ("care", "浴室地面有水时仍要小心行走，不要奔跑。", ["KNOWLEDGE_1"], "product_claim"),
            ("promotion", "资料中的原价为一百七十八新台币，活动价为一百零七新台币。", ["PRICE", "PROMOTION"], "product_claim"),
            ("cta", "你可以根据自己的需求了解颜色和尺码。", [], "engagement")]),
        ("测试B", "旅行讲解 · 非模型生成的测试短稿", [
            ("welcome", "接下来换一个角度，看看这款凉拖鞋的旅行使用场景。", ["SELLING"], "product_claim"),
            ("benefit", "轻便的设计，适合旅行携带，也可以用于日常居家。", ["SELLING"], "product_claim"),
            ("care", "鞋子清洁后可以通风阴干，避免长时间曝晒或高温。", ["KNOWLEDGE_2", "KNOWLEDGE_3"], "product_claim"),
            ("specification", "脚背宽或者脚型偏肉，可以结合实际情况斟酌大一号。", ["KNOWLEDGE_5"], "product_claim"),
            ("promotion", "本次资料列出的活动价是一百零七新台币，具体优惠条件以确认的信息为准。", ["PRICE", "PROMOTION"], "product_claim")])]:
        blocks = [{"stage": stage, "sentences": [{"display_text": text, "spoken_text": text, "fact_ids": refs, "kind": kind}]} for stage, text, refs, kind in entries]
        script = structure_script(product, {"angle": angle, "blocks": blocks}, "测试短稿（非模型生成）", label)
        put("script", script)


seed()

# Assign stable human-facing numbers once; edits keep these identities.
for index, product in enumerate(all_of("product"), 1):
    if "display_number" not in product:
        product["display_number"] = index
        put("product", product)
_version_numbers = {}
for script in all_of("script"):
    pid = script["product_id"]
    _version_numbers[pid] = max(_version_numbers.get(pid, 0), script.get("version_number", 0))
    if "version_number" not in script:
        _version_numbers[pid] += 1
        script["version_number"] = _version_numbers[pid]
        put("script", script)


def settings(identity="default"):
    existing = all_of("config")
    previous = next((c for c in existing if c["id"] == identity), None)
    if identity != "default" and previous is None:
        raise HTTPException(404, "直播间不存在")
    result = {"id": identity, "name": "声音直播测试", "version_count": 2, "loop": True,
            "tts_provider": "system", "tts_voice": "Huihui", "tts_model": "speech-2.8-turbo",
            "script_order": [], "selected_script_ids": [], "product_ids": [],
            **(previous or {})}
    if not previous or "product_ids" not in previous:
        selected = set(result["selected_script_ids"])
        result["product_ids"] = list(dict.fromkeys(s["product_id"] for s in all_of("script") if s["id"] in selected))
        put("config", result)
    if "commerce" not in result:
        result["commerce"] = commerce.parse_room({})
        # Preserve offers that were previously stored globally as per-room overrides.
        for pid in result["product_ids"]:
            product = get("product", pid)
            override = commerce.ProductOverride().model_dump()
            if product.get("promo_price") is not None and product["promo_price"] <= product["price"]:
                override.update(mode="price", price=product["promo_price"])
            override["promotion"] = product.get("promotion", "")
            if override["mode"] != "none" or override["promotion"]:
                result["commerce"]["product_overrides"][pid] = override
        result["commerce_schema_version"] = 1
        put("config", result)
    return result


def voice_options(data):
    provider = data.get("tts_provider", "system")
    voice = data.get("tts_voice", "Huihui" if provider == "system" else "Wise_Woman")
    model = data.get("tts_model", "speech-2.8-turbo")
    if not supported_voice(provider, voice, model):
        raise HTTPException(400, "声音服务、音色或模型不符合支持范围")
    return {"provider": provider, "voice": voice, "model": model}


class Room:
    def __init__(self, identity="default"):
        self.clients = {}
        self.owner = None
        self.state = {"room_id": identity, "session_id": uid("session"), "epoch": 0, "status": "stopped",
                      "queue": [], "index": 0, "current": None, "audio": None, "position": 0,
                      "played": [], "insertion": None, "loop": True, "connected_player": False,
                      "logs": [], "audience_events": [], "error": None}
        self.state["tts"] = voice_options(settings(identity))
        self.state["audience_events"] = [e for e in all_of("viewer_event") if e["room_id"] == identity][-120:]
        self.state["logs"] = [e for e in all_of("room_log") if e["room_id"] == identity][-120:]
        self.player_sources = {}
        self.resume_item = None
        self.actions = {}
        self.last_completed_session = None
        self.lock = asyncio.Lock()
        self.welcomes = WelcomeRuntime()
        self.likes = LikeThanksRuntime()
        self.like_worker = None
        self.like_worker_session = None
        self.comment_seen = {}
        self.interaction_tasks = set()

    def log(self, event, detail):
        entry = {"id": uid("room_log"), "room_id": self.state["room_id"], "session_id": self.state["session_id"],
                 "time": now(), "observed_at_ms": round(time.time() * 1000, 3),
                 "monotonic_ms": round(time.monotonic() * 1000, 3),
                 "event": event, "detail": detail, "epoch": self.state["epoch"]}
        put("room_log", entry)
        self.state["logs"].append(entry)
        self.state["logs"] = self.state["logs"][-120:]
        with (RUNTIME / "events.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def snapshot(self, *, include_queue=True):
        result = deepcopy({k: v for k, v in self.state.items() if include_queue or k != "queue"})
        result["connected_player"] = self.owner is not None
        result["player_id"] = self.owner
        result["output_source"] = self.player_sources.get(self.owner)
        return result

    async def broadcast(self):
        # Full queue only on first delivery/change for upgraded clients. Legacy
        # clients retain their old contract. Metadata is never a per-tick blob.
        queue_key = tuple(item.get("queue_id") for item in self.state["queue"])
        versions = getattr(self, "_sent_queue_versions", {})
        self._sent_queue_versions = versions
        full = small = None
        for identity, (socket, role) in list(self.clients.items()):
            try:
                thin = socket.query_params.get("delta") == "1" if hasattr(socket, "query_params") else False
                delta = thin and versions.get(identity) == queue_key
                if delta:
                    if small is None:
                        small = {"type": "state_delta", "state": self.snapshot(include_queue=False)}
                    message = small
                else:
                    if full is None:
                        full = {"type": "state", "state": self.snapshot()}
                    message = full
                await socket.send_json(message)
                versions[identity] = queue_key
            except Exception:
                self.clients.pop(identity, None)
                versions.pop(identity, None)

    def current_item(self):
        return self.state["queue"][self.state["index"]] if self.state["index"] < len(self.state["queue"]) else None

    def welcome_log(self, stage, event_id, **detail):
        # Do not copy nickname, raw platform messages or provider responses here.
        self.log("welcome_" + stage, {"event_id": event_id, **detail})

    def interaction_log(self, stage, item, **detail):
        self.log("interaction_" + stage, {"event_id": item.get("interaction_event_id") or item.get("welcome_event_id"),
            "kind": "welcome" if item.get("is_welcome") else "like" if item.get("is_like") else "reply", "queue_id": item.get("queue_id"),
            "observed_at_ms": round(time.time() * 1000, 3),
            "monotonic_ms": round(time.monotonic() * 1000, 3), **detail})

    def spawn_interaction(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.interaction_tasks.add(task)
        task.add_done_callback(self.interaction_tasks.discard)

    def queue_interaction(self, kind, item):
        item.update(requires_ready=True, interaction_event_id=item.get("welcome_event_id") or item.get("like_event_id") or item.get("interaction_event_id"),
                    interaction_received_ms=item.get("like_received_ms") or round(time.time() * 1000, 3))
        self.state["insertion"] = {"text": item["text"], "mode": "after_sentence", "kind": kind, "pending": item}
        self.interaction_log("admitted", item)
        if kind in {"welcome", "like"}:
            self.spawn_interaction(self.prepare_pending_insertion(self.state["session_id"], deepcopy(item)))

    def pending_matches(self, session_id, item):
        slot = self.state.get("insertion") or {}
        pending = slot.get("pending")
        return (self.state["session_id"] == session_id and pending
                and pending.get("queue_id") == item.get("queue_id"))

    def interaction_reason(self, item, current):
        if item.get("is_welcome"):
            return self.welcomes.play_reason(item, current, self.state["session_id"])
        if item.get("is_like"):
            product = get("product", current["product_id"]) if current else None
            return self.likes.play_reason(item, current, product, settings(self.state["room_id"]), self.state["session_id"])
        if item.get("interaction_session_id") != self.state["session_id"]:
            return "session_changed"
        if not current or current.get("product_id") != item.get("product_id"):
            return "product_changed"
        if time.time() >= item.get("interaction_expires_at", 0):
            return "expired"
        for kind, field in (("product", "reply_product_revision"), ("config", "reply_config_revision")):
            if item.get(field) is not None:
                value = get("product", item["product_id"]) if kind == "product" else settings(self.state["room_id"])
                if value.get("_record_revision") != item[field]:
                    return "source_changed"
        return None

    def like_diagnostics(self):
        for detail in self.likes.drain_diagnostics():
            self.log("like_thanks_diagnostic", detail)

    def release_unplayed_like(self, item, reason):
        if not item.get("is_like"):
            return
        if reason in {"source_changed", "product_changed", "expired", "manual_control", "superseded"}:
            if self.likes.requeue(item, reason=reason):
                self.ensure_like_worker()
        else:
            self.likes.fail(item, reason=reason)
        self.like_diagnostics()

    def ensure_like_worker(self):
        session_id = self.state["session_id"]
        if not self.like_worker or self.like_worker.done() or self.like_worker_session != session_id:
            self.like_worker_session = session_id
            self.like_worker = asyncio.create_task(self.prepare_like_batches(session_id))

    def like_entry(self, event):
        """Collect per-viewer bursts even while the shared insertion slot is busy."""
        if event.get("type") != "like":
            return {"status": "ignored"}
        if (event.get("session_id") != self.state["session_id"] or self.state["status"] not in {"playing", "ready", "buffering"}
                or not self.state["current"]):
            return {"status": "skipped", "reason": "not_playing"}
        current = self.current_item()
        product = get("product", current["product_id"]) if current else None
        receipt, reason = self.likes.collect(event, current, product, settings(self.state["room_id"]), self.state["session_id"])
        self.like_diagnostics()
        if reason:
            self.log("like_thanks_skipped", {"event_id": event.get("id"), "reason": reason})
            return {"status": "skipped", "reason": reason}
        self.log("like_thanks_collected", {"event_id": event["id"], **receipt})
        self.ensure_like_worker()
        return {**receipt, "status": "collecting"}

    async def prepare_like_batches(self, session_id):
        """Offer ready text batches to the existing slot; never interrupt a sentence."""
        while True:
            async with self.lock:
                if self.state["session_id"] != session_id or self.state["status"] in {"stopped", "completed"}:
                    if self.state["session_id"] == session_id:
                        self.likes.clear(reason=self.state["status"])
                    else:
                        self.likes.session(self.state["session_id"])
                    self.like_diagnostics()
                    return
                ready_at = self.likes.next_ready_at()
                if ready_at is None:
                    return
                if (self.state["status"] in {"playing", "ready", "buffering"}
                        and not self.state["insertion"] and self.likes.clock() >= ready_at):
                    current = self.current_item()
                    product = get("product", current["product_id"]) if current else None
                    prepared, reason = self.likes.pop_ready(current, product, settings(self.state["room_id"]), session_id)
                    if prepared:
                        item = {**deepcopy(current), **prepared, "id": uid("like"), "queue_id": uid("queued"),
                                "stage": "welcome", "is_insertion": True, "ordinal": 0, "total_sentences": 0}
                        self.queue_interaction("like", item)
                        await self.broadcast()
                    elif reason not in {"empty", "collecting"}:
                        # Missing or withdrawn product facts must never trigger invented pricing.
                        if reason != getattr(self, "like_wait_reason", None):
                            self.log("like_thanks_waiting", {"reason": reason})
                        self.like_wait_reason = reason
            await asyncio.sleep(.25)

    async def prepare_pending_insertion(self, session_id, item):
        """Prepare short interactions while the current script keeps playing."""
        started = time.perf_counter()
        try:
            async with self.lock:
                if not self.pending_matches(session_id, item):
                    return
                reason = self.interaction_reason(item, self.current_item())
                if reason:
                    self.state["insertion"] = None
                    self.interaction_log("discarded", item, reason=reason)
                    self.release_unplayed_like(item, reason)
                    if item.get("is_welcome"):
                        self.welcome_log("skipped", item["welcome_event_id"], reason=reason)
                    await self.broadcast()
                    return
            self.interaction_log("tts_started", item)
            tts = deepcopy(self.state["tts"])
            # Live reactions must not queue behind the prerecorded-script worker.
            audio = await asyncio.to_thread(render, item.get("spoken_text") or item["text"], RUNTIME / "audio",
                tts["voice"], tts["provider"], tts["model"], interaction_style(item, tts["provider"]))
            async with self.lock:
                if not self.pending_matches(session_id, item):
                    self.interaction_log("discarded", item, reason="superseded")
                    self.release_unplayed_like(item, "superseded")
                    return
                reason = self.interaction_reason(item, self.current_item())
                if reason:
                    self.state["insertion"] = None
                    self.interaction_log("discarded", item, reason=reason)
                    self.release_unplayed_like(item, reason)
                    if item.get("is_welcome"):
                        self.welcome_log("skipped", item["welcome_event_id"], reason=reason)
                else:
                    pending = self.state["insertion"]["pending"]
                    pending.update(prepared_audio=audio, interaction_ready_ms=round(time.time() * 1000, 3))
                    self.interaction_log("audio_ready", pending,
                        tts_ms=round((time.perf_counter() - started) * 1000, 3),
                        cache_hit=bool(audio.get("cache_hit")), duration_seconds=audio.get("duration"),
                        usage_characters=audio.get("usage_characters"))
                await self.broadcast()
        except Exception:
            async with self.lock:
                if self.pending_matches(session_id, item):
                    self.state["insertion"] = None
                    if item.get("is_welcome"):
                        self.welcome_log("skipped", item["welcome_event_id"], reason="synthesis_failed")
                    self.interaction_log("discarded", item, reason="synthesis_failed",
                        tts_ms=round((time.perf_counter() - started) * 1000, 3))
                    self.release_unplayed_like(item, "synthesis_failed")
                    await self.broadcast()

    def comment_entry(self, event):
        """One bounded voice reply for an active own-room comment; no backlog."""
        if event.get("type") != "comment" or event.get("reply_turn_id"):
            return {"status": "ignored", "reason": "text_lab_only"}
        key = (event.get("source"), event.get("external_room_id"), event.get("upstream_event_id") or event.get("id"))
        if key in self.comment_seen:
            return {"status": "skipped", "reason": "duplicate_event"}
        self.comment_seen[key] = True
        if len(self.comment_seen) > 4096:
            self.comment_seen.pop(next(iter(self.comment_seen)))
        if event.get("source") == "douyin" and event.get("purpose") != "own_room":
            return {"status": "skipped", "reason": "external_test_read_only"}
        if (event.get("session_id") != self.state["session_id"] or self.state["status"] not in {"playing", "ready", "buffering"}
                or not self.state["current"]):
            return {"status": "skipped", "reason": "not_playing"}
        if self.state["insertion"]:
            return {"status": "skipped", "reason": "insertion_busy"}
        current = deepcopy(self.state["current"])
        item = {**current, "id": uid("reply"), "queue_id": uid("queued"), "text": "", "spoken_text": "",
                "stage": "neutral", "is_insertion": True, "is_reply": True, "ordinal": 0, "total_sentences": 0,
                "interaction_event_id": event["id"], "interaction_session_id": self.state["session_id"],
                "interaction_expires_at": time.time() + 60}
        self.queue_interaction("reply", item)
        self.spawn_interaction(self.prepare_comment(self.state["session_id"], deepcopy(item), deepcopy(event)))
        return {"status": "preparing", "event_id": event["id"]}

    async def prepare_comment(self, session_id, item, event):
        try:
            from .live_reply import build_live_answer
            async with self.lock:
                if not self.pending_matches(session_id, item):
                    return
                reason = self.interaction_reason(item, self.current_item())
                if reason:
                    self.state["insertion"] = None
                    self.interaction_log("discarded", item, reason=reason)
                    return
            product = deepcopy(get("product", item["product_id"]))
            config = deepcopy(settings(self.state["room_id"]))
            item.update(reply_product_revision=product.get("_record_revision"), reply_config_revision=config.get("_record_revision"))
            result = await build_live_answer(event, product, config)
            async with self.lock:
                if not self.pending_matches(session_id, item):
                    return
                reason = self.interaction_reason(item, self.current_item())
                if reason:
                    self.state["insertion"] = None
                    self.interaction_log("discarded", item, reason=reason)
                    await self.broadcast()
                    return
                item.update(text=result["answer"], spoken_text=result["answer"])
                self.state["insertion"]["text"] = item["text"]
                self.state["insertion"]["pending"].update(text=item["text"], spoken_text=item["text"],
                    reply_product_revision=item["reply_product_revision"], reply_config_revision=item["reply_config_revision"])
                self.interaction_log("reply_ready", item, answer_status=result["answer_status"],
                    timings_ms=result.get("timings_ms"), citations=result.get("citations"),
                    usage=result.get("usage"), model=result.get("model"))
                put("live_reply", {"id": item["id"], "room_id": self.state["room_id"], "session_id": session_id,
                    "event_id": event["id"], "time": now(), **result})
            await self.prepare_pending_insertion(session_id, item)
        except Exception as error:
            async with self.lock:
                if self.pending_matches(session_id, item):
                    self.state["insertion"] = None
                    self.interaction_log("discarded", item, reason="reply_failed", failure_stage=getattr(error, "stage", "reply"),
                        failure_code=getattr(error, "code", "LIVE_REPLY_FAILED"), timings_ms=getattr(error, "timings_ms", {}),
                        usage=getattr(error, "usage", {}), usage_state=getattr(error, "usage_state", "unknown"))
                    await self.broadcast()

    def welcome_entry(self, event):
        """Handle a freshly deduplicated enter while the caller holds Room.lock."""
        if event.get("type") != "enter":
            return {"status": "ignored"}
        reason = None
        if event.get("source") == "douyin" and event.get("purpose") != "own_room":
            reason = "external_test_read_only"
        elif event.get("session_id") != self.state["session_id"]:
            reason = "session_changed"
        elif self.state["status"] not in {"playing", "ready", "buffering"} or not self.state["current"]:
            reason = "not_playing"
        elif self.state["insertion"]:
            reason = "insertion_busy"
        current = self.state["current"]
        if reason:
            self.welcomes.remember(event)
            self.welcome_log("skipped", event.get("id"), reason=reason)
            return {"status": "skipped", "reason": reason}
        prepared, reason = self.welcomes.admit(event, current, self.state["session_id"])
        if reason:
            self.welcome_log("skipped", event.get("id"), reason=reason)
            return {"status": "skipped", "reason": reason}
        self.welcome_log("admitted", event["id"], product_id=current["product_id"])
        # One existing insertion slot, no delayed welcome backlog. Manual inserts
        # keep their original scheduling and can supersede a pending welcome.
        insertion = {**deepcopy(current), **prepared, "id": uid("welcome"), "queue_id": uid("queued"),
                     "stage": "welcome", "is_insertion": True, "is_welcome": True, "ordinal": 0,
                     "total_sentences": 0}
        self.queue_interaction("welcome", insertion)
        self.welcome_log("queued", event["id"], product_id=current["product_id"])
        return {"status": "queued"}

    async def drop_welcome(self, item, reason):
        await self.drop_interaction(item, reason)

    async def drop_interaction(self, item, reason):
        if item.get("is_welcome"):
            self.welcome_log("skipped", item["welcome_event_id"], reason=reason)
        self.interaction_log("discarded", item, reason=reason)
        self.release_unplayed_like(item, reason)
        await self.advance()

    def set_current(self, item):
        self.state.update(epoch=self.state["epoch"] + 1, current=deepcopy(item), audio=None,
                          status="preparing", position=0, error=None)
        if item:
            self.state["current"]["attempt_id"] = uid("attempt")
        self.log("prepare_requested", item["text"] if item else "")
        if item and item.get("prepared_audio"):
            self.state.update(audio=deepcopy(item["prepared_audio"]), status="ready")
            self.interaction_log("selected_at_boundary", item)
            return
        asyncio.create_task(self.prepare(self.state["epoch"], deepcopy(item)))

    async def prepare(self, epoch, item):
        if not item:
            return
        try:
            if item.get("prelive_audio") and not item.get("is_insertion"):
                service = _active_prelive()
                if service:
                    try:
                        await asyncio.to_thread(service.assert_live_product_control, item["product_id"], item.get("pinned_product_ref"))
                    except BusinessError as error:
                        raise RuntimeError(error.message) from None
                from .prelive_bridge import prepare_precompiled
                return await prepare_precompiled(self, epoch, item, sys.modules[__name__])
            loop = asyncio.get_running_loop()
            tts = deepcopy(self.state["tts"])
            if item.get("is_insertion"):
                audio = await loop.run_in_executor(POOL, render, item.get("spoken_text") or item["text"], RUNTIME / "audio", tts["voice"], tts["provider"], tts["model"], interaction_style(item, tts["provider"]))
            else:
                product = get("product", item["product_id"])
                if product.get("archived") or product.get("sale_status", "available") != "available":
                    raise RuntimeError("当前商品已不可售，已停止继续播放该商品，请结束本场并调整选品。")
                if commerce.script_is_stale(get("script", item["script_id"]), product, settings(self.state["room_id"])):
                    raise RuntimeError("商品或本场配置已变化，已停止继续播放旧话术，请结束本场并重新生成话术与语音。")
                audio = production.prerecorded(self.state["room_id"], item, tts)
                if not audio:
                    raise RuntimeError("本地语音缺失或损坏，请结束本场并返回配置生成语音；直播不会在线补生成常规话术。")
            async with self.lock:
                if epoch != self.state["epoch"]:
                    return
                if item.get("is_welcome"):
                    reason = self.welcomes.play_reason(item, self.current_item(), self.state["session_id"])
                    if reason:
                        await self.drop_welcome(item, reason)
                        await self.broadcast()
                        return
                self.state["audio"] = audio
                if self.state["status"] not in {"paused", "blocked"}:
                    self.state["status"] = "ready"
                self.log("audio_ready", audio["audio_id"])
                await self.broadcast()
        except Exception as error:
            async with self.lock:
                if epoch == self.state["epoch"]:
                    if item.get("is_welcome"):
                        await self.drop_welcome(item, "synthesis_failed")
                        await self.broadcast()
                        return
                    message = str(error) if isinstance(error, RuntimeError) else "语音合成失败，可重试当前句或跳过。"
                    self.state.update(status="error", error=message)
                    self.log("synthesis_failed", item["id"])
                    await self.broadcast()

    async def advance(self, *, preserve_pending=False):
        if self.state["insertion"] and not preserve_pending:
            self.state["insertion"] = None
            if self.resume_item:
                self.set_current(self.resume_item)
            else:
                self.state.update(epoch=self.state["epoch"]+1, status="completed", current=None, audio=None, position=0)
                self.release_publication_slot()
            self.resume_item = None
            return
        self.state["index"] += 1
        if self.state["index"] >= len(self.state["queue"]):
            if self.state["loop"] and self.state["queue"]:
                self.state["index"] = 0
            else:
                self.state.update(status="completed", current=None, audio=None, position=0)
                self.release_publication_slot()
                return
        self.set_current(self.current_item())

    def release_publication_slot(self):
        service = _active_prelive()
        if service:
            service.release_live_slot(self.state["room_id"])
        if self.state["status"] == "completed" and self.last_completed_session != self.state["session_id"]:
            self.log("playback_completed", {})
            self.last_completed_session = self.state["session_id"]

    async def action(self, data):
        action_id = data.get("action_id") or uid("action")
        async with self.lock:
            if action_id in self.actions:
                return self.actions[action_id]
            if data.get("expected_epoch") is not None and data["expected_epoch"] != self.state["epoch"]:
                raise HTTPException(409, "播出位置已变化，请读取最新状态后重试")
            if data.get("expected_session_id") is not None and data["expected_session_id"] != self.state["session_id"]:
                raise HTTPException(409, "场次已变化，请读取最新状态后重试")
            name = data.get("action")
            insertion = self.state["insertion"]
            if insertion and insertion.get("kind") == "welcome" and name in {"insert", "switch", "cancel_product", "reorder", "replace_pending"}:
                pending = insertion.get("pending")
                if pending:
                    self.welcome_log("skipped", pending["welcome_event_id"], reason="manual_control")
                    self.state["insertion"] = None
                elif self.state["current"].get("is_welcome"):
                    await self.drop_welcome(self.state["current"], "manual_control")
            if name == "start":
                if self.state["status"] not in {"stopped", "completed"}:
                    raise HTTPException(409, "直播间已经开播，请进入监控或先结束本场")
                selections = data.get("script_ids", [])
                queue = []
                for identity in selections:
                    script = get("script", identity)
                    product = get("product", script["product_id"])
                    if product.get("archived"):
                        raise HTTPException(400, "商品已归档，请重新选择")
                    welcome_templates = deepcopy(script.get("welcome_templates")) or build_product_templates(product)
                    for sentence in script["sentences"]:
                        queue.append({**deepcopy(sentence), "queue_id": uid("queued"), "product_id": product["id"],
                                      "product_name": script["product_snapshot"]["name"], "image": product.get("image", ""),
                                      "script_id": identity, "version": f"第{script.get('version_number', 1)}版", "revision": script["revision"],
                                      "product_number": product.get("display_number"), "version_number": script.get("version_number", 1),
                                      "total_sentences": len(script["sentences"]), "source": script["source"],
                                      "welcome_templates": deepcopy(welcome_templates)})
                if not queue:
                    raise HTTPException(400, "本场至少需要一版话术")
                self.state.update(session_id=uid("session"), queue=queue, index=0, played=[], insertion=None, loop=True, tts=voice_options(settings(self.state["room_id"])))
                self.resume_item = None
                self.set_current(queue[0])
            elif name == "pause":
                if self.state["current"]:
                    self.state["status"] = "paused"
            elif name == "resume":
                if not self.owner:
                    raise HTTPException(409, "播出窗口尚未连接")
                if self.state["current"]:
                    current = self.state["current"]
                    already_started_like = current.get("is_like") and current.get("interaction_started")
                    reason = self.interaction_reason(current, self.current_item()) if not already_started_like and (current.get("requires_ready") or current.get("is_welcome")) else None
                    if reason:
                        await self.drop_interaction(current, reason)
                    else:
                        self.state.update(status="ready" if self.state["audio"] else "preparing", error=None)
            elif name == "stop":
                insertion = self.state["insertion"]
                if insertion and insertion.get("kind") == "welcome":
                    welcome_item = insertion.get("pending") or self.state["current"]
                    self.welcome_log("skipped", welcome_item["welcome_event_id"], reason="stopped")
                self.state.update(epoch=self.state["epoch"]+1, status="stopped", current=None, audio=None, insertion=None, position=0, recovery_required=False, error=None)
                self.resume_item = None
                self.likes.clear(reason="stopped")
                self.like_diagnostics()
                self.release_publication_slot()
            elif name in {"next", "next_product"}:
                if self.state["insertion"]:
                    like_item = self.state["insertion"].get("pending") or self.state["current"]
                    self.release_unplayed_like(like_item, "manual_control")
                    if self.state["insertion"].get("kind") == "welcome":
                        welcome_item = self.state["insertion"].get("pending") or self.state["current"]
                        self.welcome_log("skipped", welcome_item["welcome_event_id"], reason="manual_skip")
                    self.state["insertion"] = None
                    self.resume_item = None
                if name == "next_product" and self.current_item():
                    product_id = self.current_item()["product_id"]
                    while self.state["index"]+1 < len(self.state["queue"]) and self.state["queue"][self.state["index"]+1]["product_id"] == product_id:
                        self.state["index"] += 1
                await self.advance()
            elif name == "restart_version":
                item = self.current_item()
                if not item or not self.state["current"] or self.state["status"] in {"stopped", "completed"}:
                    raise HTTPException(409, "本场已结束，请重新开始播出")
                for key in ("occurrence_id", "script_id"):
                    expected = data.get("expected_" + key)
                    if expected is not None and expected != item.get(key):
                        raise HTTPException(409, "当前话术版本已变化，请刷新后再从头播放")
                def version_key(value):
                    return (value.get("product_id"), value.get("occurrence_id") or value.get("script_id"))
                index = self.state["index"]
                while index > 0 and version_key(self.state["queue"][index - 1]) == version_key(item):
                    index -= 1
                insertion = self.state["insertion"]
                if insertion:
                    pending = insertion.get("pending") or self.state["current"]
                    self.release_unplayed_like(pending, "manual_restart")
                    if pending.get("is_welcome"):
                        self.welcome_log("skipped", pending["welcome_event_id"], reason="manual_restart")
                self.state.update(index=index, insertion=None,
                                  playback_seek_revision=self.state.get("playback_seek_revision", 0) + 1)
                self.resume_item = None
                self.set_current(self.current_item())
                self.log("version_restarted", {"index": index, "script_id": item.get("script_id"), "occurrence_id": item.get("occurrence_id")})
            elif name == "retry":
                if self.state["current"]:
                    self.state["playback_seek_revision"] = self.state.get("playback_seek_revision", 0) + 1
                    self.set_current(self.state["current"])
            elif name == "insert":
                text = str(data.get("text", "")).strip()
                if not text or len(text) > 500:
                    raise HTTPException(400, "插播内容须为1至500字")
                if not self.state["current"] or self.state["insertion"]:
                    raise HTTPException(409, "请先开始播放，且等待当前插播完成")
                self.resume_item = deepcopy(self.current_item())
                insertion = {**deepcopy(self.state["current"]), "id": uid("insert"), "queue_id": uid("queued"),
                             "text": text, "spoken_text": text, "stage": "插播", "is_insertion": True, "ordinal": 0, "total_sentences": 0}
                self.state["insertion"] = {"text": text, "mode": data.get("mode", "immediate")}
                if data.get("mode") == "after_sentence":
                    self.state["insertion"]["pending"] = insertion
                else:
                    self.set_current(insertion)
            elif name == "reorder":
                if self.state["insertion"]:
                    raise HTTPException(409, "请等待插播完成后调整顺序")
                pending = self.state["queue"][self.state["index"]+1:]
                wanted = data.get("queue_ids", [])
                if len(wanted) != len(pending) or set(wanted) != {item["queue_id"] for item in pending}:
                    raise HTTPException(400, "只可重排全部未播内容；列表已变化，请刷新")
                by_id = {item["queue_id"]: item for item in pending}
                self.state["queue"] = self.state["queue"][:self.state["index"]+1] + [by_id[item] for item in wanted]
            elif name == "replace_pending":
                if self.state.get("publication_ref"):
                    raise HTTPException(409, "此场正在播放已确认整包，不能用旧稿替换；请结束后在播前助手中修改并重新确认")
                if self.state["insertion"]:
                    raise HTTPException(409, "请等待插播完成后替换话术")
                script = get("script", data.get("script_id", ""))
                product = get("product", script["product_id"])
                if product.get("archived"):
                    raise HTTPException(400, "只能使用未归档商品的话术")
                validate_plan(settings(self.state["room_id"]), [script["id"]])
                production.require_audio(self.state["room_id"], [script["id"]], self.state["tts"])
                old_id = data.get("target_script_id")
                pending = self.state["queue"][self.state["index"]+1:]
                found = [i for i, item in enumerate(pending) if item["script_id"] == old_id]
                if not found or any(pending[i]["product_id"] != script["product_id"] for i in found):
                    raise HTTPException(400, "只能替换待播列表中同一商品的版本")
                welcome_templates = deepcopy(script.get("welcome_templates")) or build_product_templates(product)
                replacement = [{**deepcopy(sentence), "queue_id": uid("queued"), "product_id": script["product_id"],
                                "product_name": script["product_snapshot"]["name"], "image": script["product_snapshot"].get("image", ""),
                                "script_id": script["id"], "version": f"第{script.get('version_number', 1)}版", "revision": script["revision"],
                                "product_number": get("product", script["product_id"]).get("display_number"), "version_number": script.get("version_number", 1),
                                "total_sentences": len(script["sentences"]), "source": script["source"],
                                "welcome_templates": deepcopy(welcome_templates)} for sentence in script["sentences"]]
                kept = [item for item in pending if item["script_id"] != old_id]
                self.state["queue"] = self.state["queue"][:self.state["index"]+1] + kept[:found[0]] + replacement + kept[found[0]:]
            elif name in {"switch", "cancel_product"}:
                identity = data.get("product_id")
                if self.state["insertion"]:
                    raise HTTPException(409, "请等待插播完成后切换或取消商品")
                if name == "cancel_product":
                    past = self.state["queue"][:self.state["index"]]
                    pending = [item for item in self.state["queue"][self.state["index"]:] if item["product_id"] != identity]
                    changed_current = bool(self.state["current"] and self.state["current"]["product_id"] == identity)
                    self.state["queue"] = past + pending
                    if changed_current:
                        self.state["epoch"] += 1
                        if pending:
                            self.set_current(pending[0])
                        else:
                            self.state.update(status="completed", current=None, audio=None)
                else:
                    found = next((i for i, item in enumerate(self.state["queue"][self.state["index"]+1:], self.state["index"]+1) if item["product_id"] == identity), None)
                    if found is None:
                        raise HTTPException(400, "待播列表中没有该商品")
                    self.state["index"] = found
                    self.set_current(self.current_item())
            else:
                raise HTTPException(400, "不支持的操作")
            self.log("control_accepted", name)
            if self.state["status"] == "completed":
                self.release_publication_slot()
            result = {"ok": True, "action_id": action_id}
            self.actions[action_id] = result
            if len(self.actions) > 500:
                self.actions.pop(next(iter(self.actions)))
            await self.broadcast()
            return result

    async def acknowledge(self, identity, data):
        async with self.lock:
            current = self.state["current"]
            if identity != self.owner or not current or data.get("epoch") != self.state["epoch"] or data.get("attempt_id") != current["attempt_id"]:
                return
            event = data.get("event")
            if event == "playing" and self.state["status"] in {"ready", "buffering", "playing"}:
                if current.get("is_like") and not current.get("interaction_started"):
                    reason = self.interaction_reason(current, self.current_item())
                    if reason:
                        await self.drop_interaction(current, reason)
                        await self.broadcast()
                        return
                    self.likes.mark_started(current)
                if current.get("is_reply") and not current.get("interaction_started"):
                    reason = self.interaction_reason(current, self.current_item())
                    if reason:
                        await self.drop_interaction(current, reason)
                        await self.broadcast()
                        return
                if current.get("is_welcome") and current["welcome_event_id"] not in self.welcomes.started:
                    reason = self.welcomes.play_reason(current, self.current_item(), self.state["session_id"])
                    if reason:
                        await self.drop_welcome(current, reason)
                        await self.broadcast()
                        return
                    self.welcomes.mark_started(current)
                    current["welcome_started"] = True
                    self.welcome_log("play_started", current["welcome_event_id"], product_id=current["product_id"])
                if current.get("requires_ready") and not current.get("interaction_started"):
                    current["interaction_started"] = True
                    self.interaction_log("play_started", current,
                        signal_to_play_ms=round(time.time() * 1000 - current["interaction_received_ms"], 3),
                        ready_to_play_ms=round(time.time() * 1000 - current.get("interaction_ready_ms", time.time() * 1000), 3))
                self.state["status"] = "playing"
                self.log("client_playing", current["id"])
            elif event == "progress":
                self.state["position"] = max(0, float(data.get("position", 0)))
            elif event == "ended" and self.state["status"] == "playing":
                self.log("client_ended", current["id"])
                if current.get("requires_ready"):
                    self.interaction_log("play_ended", current)
                self.state["played"].append({"sentence_id": current["id"], "attempt_id": current["attempt_id"], "text": current["text"], "time": now()})
                self.state["played"] = self.state["played"][-1000:]
                insertion = self.state["insertion"]
                # Continuous audio already crossed this boundary if no cut was
                # scheduled. A newly ready interaction must wait another one.
                if insertion and insertion.get("pending") and "boundary_choice" in data:
                    pending = insertion["pending"]
                    if data["boundary_choice"] != pending.get("queue_id"):
                        await self.advance(preserve_pending=True)
                        await self.broadcast()
                        return
                if insertion and insertion.get("pending"):
                    pending = insertion["pending"]
                    if pending.get("requires_ready"):
                        next_index = self.state["index"] + 1
                        if self.state["loop"] and next_index >= len(self.state["queue"]):
                            next_index = 0
                        next_item = self.state["queue"][next_index] if next_index < len(self.state["queue"]) else None
                        reason = self.interaction_reason(pending, next_item)
                        if reason:
                            self.state["insertion"] = None
                            if pending.get("is_welcome"):
                                self.welcome_log("skipped", pending["welcome_event_id"], reason=reason)
                            self.interaction_log("discarded", pending, reason=reason)
                            self.release_unplayed_like(pending, reason)
                            await self.advance()
                            await self.broadcast()
                            return
                        if not pending.get("prepared_audio"):
                            self.interaction_log("waiting_for_next_boundary", pending)
                            await self.advance(preserve_pending=True)
                            await self.broadcast()
                            return
                    self.state["index"] += 1
                    if self.state["loop"] and self.state["index"] >= len(self.state["queue"]):
                        self.state["index"] = 0
                    self.resume_item = deepcopy(self.current_item())
                    pending = insertion.pop("pending")
                    reason = self.welcomes.play_reason(pending, self.current_item(), self.state["session_id"]) if pending.get("is_welcome") else None
                    if reason:
                        await self.drop_welcome(pending, reason)
                    else:
                        self.set_current(pending)
                else:
                    await self.advance()
            elif event == "welcome_expired" and current.get("is_welcome"):
                await self.drop_welcome(current, "expired")
            elif event in {"blocked", "failed", "waiting"}:
                if self.state["status"] == "paused":
                    return
                if (current.get("is_welcome") or current.get("is_reply") or current.get("is_like")) and event != "waiting":
                    await self.drop_interaction(current, "playback_" + event)
                    await self.broadcast()
                    return
                self.state.update(status={"blocked": "blocked", "failed": "error", "waiting": "buffering"}[event],
                                  error="声音被浏览器拦截，请点击监控页的开启本机声音；OBS请在交互窗口启用声音" if event == "blocked" else "音频播放失败，可重试当前句" if event == "failed" else None)
                self.log("client_" + event, current["id"])
            await self.broadcast()


room = Room()
rooms = {"default": room}
start_lock = asyncio.Lock()


def room_instance(identity):
    settings(identity)
    if identity not in rooms:
        rooms[identity] = Room(identity)
    return rooms[identity]


def validate_plan(config, identities):
    if not isinstance(identities, list) or not identities or len(identities) > 1000:
        raise HTTPException(400, "本场至少需要一版话术")
    for identity in identities:
        script = get("script", identity)
        if script["product_id"] not in config["product_ids"]:
            raise HTTPException(400, "话术所属商品尚未从商品库添加到本直播间")
        product = get("product", script["product_id"])
        if product.get("archived"):
            raise HTTPException(400, "请选择可用商品")
        if commerce.script_is_stale(script, product, config):
            raise HTTPException(409, "商品或本场配置已更新，已有话术仍引用旧资料，请重新生成话术与语音后再开播")


async def room_control(identity, data):
    target = room_instance(identity)
    if data.get("action") == "start":
        async with start_lock:
            if data.get("action_id") in target.actions:
                return target.actions[data["action_id"]]
            active = next((r for r in rooms.values() if r is not target and r.state["status"] not in {"stopped", "completed"}), None)
            if active:
                raise HTTPException(409, "已有其他直播间正在播出，请先结束该场直播")
            config = settings(identity)
            if config.get("adopted_publication_ref"):
                raise HTTPException(409, "此场使用播前助手的确认整包，请使用助手中的人工开播操作，以核验当前稿件、声音与商品版本")
            service = _active_prelive()
            if service:
                with service.store.transaction() as connection:
                    slot = connection.execute("SELECT state FROM prelive_live_slot WHERE slot=1").fetchone()
                if slot and slot["state"] in {"starting", "live", "unknown"}:
                    raise HTTPException(409, "已有已确认直播或待核实的启动，请先结束对应直播间后再开播")
            planned = production.plan_ids(config)
            identities = data.get("script_ids", planned)
            if config.get("plan_mode") == "all" and identities != planned:
                raise HTTPException(409, "开播必须使用完整本场列表，请先保存话术顺序")
            validate_plan(config, identities)
            production.require_audio(identity, identities, voice_options(config))
            return await target.action({**data, "script_ids": identities, "loop": config["loop"]})
    return await target.action(data)
warmup_state = {"status": "idle", "done": 0, "total": 0, "duration": 0, "error": None}


def _acquire_windows_playback_lock(fd):
    """Allow a short OS cleanup race without ever bypassing exclusive locking."""
    import msvcrt
    deadline = time.monotonic() + .5
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            remaining = deadline - time.monotonic()
            if error.errno != errno.EACCES or remaining <= 0:
                raise
            # Windows may still reject the byte lock just after the previous
            # process has exited. A live owner remains protected: success is
            # possible only when the OS actually grants this same lock.
            time.sleep(min(.02, remaining))


@contextmanager
def playback_owner_lease():
    """One lifetime owner for a runtime, released by the OS even after a crash."""
    path = RUNTIME / "playback-owner.lock"
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                _acquire_windows_playback_lock(handle.fileno())
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("该数据目录已有声播播放服务运行，未接管或清除直播占用。") from None
        try:
            yield uid("playback_owner")
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def app_lifespan(application):
    with playback_owner_lease() as owner_id:
        service = _active_prelive()
        recovery = service.begin_playback_owner(owner_id, exclusive_lease=True) if service else None
        if recovery:
            target = room_instance(recovery["room_id"])
            target.state.update(status="error", recovery_required=True,
                                error="上次开播结果待核实。请确认旧播出窗口和 OBS 已停止声音，再点击结束本场解除占用；系统不会自动重播。")
        try:
            yield
        finally:
            # Stop only this process's in-memory playback. A foreign/legacy
            # ambiguous slot remains blocked until the human explicitly stops it.
            for target in list(rooms.values()):
                if target.state["status"] not in {"stopped", "completed"} and not target.state.get("recovery_required"):
                    await target.action({"action": "stop"})
            await live_source.shutdown()


app = FastAPI(title="电商声音直播", docs_url=None, redoc_url=None, lifespan=app_lifespan)


@app.middleware("http")
async def local_guard(request: Request, call_next):
    if request.url.hostname not in {"127.0.0.1", "localhost"}:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "仅支持本机访问"}, status_code=403)
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
        parsed = urlparse(origin)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != request.url.port:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
    result = await call_next(request)
    result.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "no-cache"
    return result


@app.get("/api/library")
async def library():
    return {"products": [p for p in all_of("product") if not p.get("archived")], "scripts": all_of("script"),
            "config": settings(),
            "rooms": [settings(c["id"]) for c in all_of("config")],
            "service": {"deepseek_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
                        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
                        "minimax_configured": bool(os.environ.get("MINIMAX_API_KEY")),
                        "voice_options": voice_catalog(),
                        "tts": "本机普通话 / MiniMax 可选"}}


@app.post("/api/products")
async def add_product(body: ProductInput):
    if not body.selling_points.strip() and not body.core_selling_points:
        raise HTTPException(400, "请至少填写一条商品核心卖点")
    return put("product", body.record())


@app.put("/api/products/{identity}")
async def update_product(identity: str, body: ProductInput, request: Request = None):
    prior = get("product", identity)
    expected = _expected_record_revision(request, prior)
    result = body.record(identity)
    if not result.get("selling_points", "").strip():
        raise HTTPException(400, "请至少填写一条商品核心卖点")
    return put("product", result, expected_revision=expected)


@app.post("/api/products/{identity}/archive")
async def archive(identity: str, request: Request = None):
    item = get("product", identity)
    expected = _expected_record_revision(request, item)
    item["archived"] = True
    return put("product", item, expected_revision=expected)


@app.post("/api/generate/{identity}")
async def generate(identity: str, data: dict):
    count = int(data.get("version_count", 2))
    if not 1 <= count <= 6:
        raise HTTPException(400, "版本数量应为1至6")
    room_id = data.get("room_id")
    if not isinstance(room_id, str) or identity not in settings(room_id)["product_ids"]:
        raise HTTPException(400, "请在直播间内选择商品后生成话术")
    try:
        product = commerce.effective_product(get("product", identity), settings(room_id))
        results = await asyncio.to_thread(generate_remote, product, count, production.variation_context(room_id, identity))
        for script in results:
            production.number_script(script, room_id)
            put("script", script)
        production.attach_scripts(room_id, results)
        return {"scripts": results}
    except HTTPException:
        raise
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else "生成结果格式有误，原有稿件已保留"
        raise HTTPException(502, message) from None


def script_view(script, provider="minimax"):
    result = deepcopy(script)
    result["paragraphs"] = []
    for block in result.get("blocks", []):
        entries = [v for v in result["sentences"] if v.get("block_id") == block["id"]]
        block["speech_style"] = sentence_style({"stage": block.get("stage", "benefit")}, provider)
        result["paragraphs"].append({**block, "sentences": entries})
    return result


@app.get("/api/scripts/{identity}")
async def get_script(identity: str):
    return script_view(get("script", identity))


@app.get("/api/tts/profiles")
async def tts_profiles(provider: str = "minimax"):
    if provider not in {"system", "minimax"}:
        raise HTTPException(400, "未知语音来源")
    return {"provider": provider, "profiles": profile_catalog(provider), "limitations": [
        "语气说明是本地配置解释，不是传给MiniMax的自然语言prompt。",
        "音高是整句偏移，不能保证逐字升降调；听感、唱腔和杂音需试听验收。",
        "本机音色只提供有限的语速音量控制，不能保证情绪效果。"]}


@app.get("/api/copywriting/evaluation")
async def copywriting_evaluation():
    from .evaluation_view import evaluation_report
    return evaluation_report(ROOT)


@app.put("/api/scripts/{identity}")
async def edit_script(identity: str, data: dict, request: Request = None):
    original = get("script", identity)
    if original.get("prelive_managed"):
        raise HTTPException(409, "此稿由播前助手管理，请在右侧对话指定模块或句子修改，之后重新审核整包")
    _expected_record_revision(request, original)
    if "blocks" in data or "sentences" in data:
        blocks, entries = data.get("blocks"), data.get("sentences")
        if not isinstance(blocks, list) or not isinstance(entries, list) or not blocks or len(blocks) > 100 or not entries or len(entries) > 300:
            raise HTTPException(400, "分段话术格式不正确")
        ids = [b.get("id") for b in blocks if isinstance(b, dict)]
        if len(ids) != len(blocks) or any(not isinstance(v, str) for v in ids) or len(set(ids)) != len(ids):
            raise HTTPException(400, "段落编号无效或重复")
        raw_blocks = []
        for block in blocks:
            group = [v for v in entries if isinstance(v, dict) and v.get("block_id") == block["id"]]
            if not group:
                continue
            raw = {"stage": str(block.get("stage", "benefit")), "focus": str(block.get("focus", "")), "sentences": []}
            for entry in group:
                value = str(entry.get("text", entry.get("display_text", ""))).strip()
                if not value or len(value) > 1500:
                    raise HTTPException(400, "每句话术须为1至1500字")
                raw["sentences"].append({"display_text": value, "spoken_text": str(entry.get("spoken_text") or value),
                                        "kind": entry.get("kind", "product_claim"), "fact_ids": entry.get("fact_ids", [])})
            raw_blocks.append(raw)
        if any(not isinstance(v, dict) or v.get("block_id") not in ids for v in entries):
            raise HTTPException(400, "句子引用了未知段落")
        try:
            edited = structure_script(original["product_snapshot"], {"blocks": raw_blocks, "angle": original.get("angle", "")},
                                      original["source"], original["label"], original.get("facts"))
        except (ValueError, TypeError):
            raise HTTPException(400, "分段话术内容无效") from None
        edited.update(revision=original["revision"] + 1, parent_id=identity,
                      version_number=original.get("version_number", 1))
        if original.get("room_id"):
            edited["room_id"] = original["room_id"]
        return put("script", edited)
    lines = [part.strip() for line in data.get("lines", []) for part in re.findall(r"[^。！？!?\n]+[。！？!?]?", str(line)) if part.strip()]
    if not lines or len(lines) > 300 or any(len(line) > 1500 for line in lines):
        raise HTTPException(400, "请填写有效的分句话术")
    # Never mutate an approved snapshot. This creates a new draft revision.
    edited = deepcopy(original)
    edited.update(id=uid("script"), revision=original["revision"]+1, parent_id=identity, status="draft", created_at=now())
    edited["sentences"] = []
    edited["blocks"] = []
    for index, text in enumerate(lines):
        previous = original["sentences"][index] if index < len(original["sentences"]) else {}
        edited["blocks"].append({"id": f"{edited['id']}_b{index+1:03}", "stage": previous.get("stage", "手动编辑"), "ordinal": index+1})
        edited["sentences"].append({**previous, "id": f"{edited['id']}_s{index+1:03}", "ordinal": index+1,
                                   "text": text, "spoken_text": text, "stage": previous.get("stage", "手动编辑"),
                                   "block_ordinal": index+1, "block_id": f"{edited['id']}_b{index+1:03}",
                                   "fact_ids": previous.get("fact_ids", []), "edited": True})
    edited["issues"] = ["人工修改稿：请重新核对全部事实、价格和优惠，原引用仅供参考。"]
    return put("script", edited)


@app.post("/api/scripts/{identity}/approve")
async def approve(identity: str, request: Request = None):
    script = get("script", identity)
    if script.get("prelive_managed"):
        raise HTTPException(409, "此稿须在播前助手中审核整包；旧单稿批准不能代替文本到语音批准")
    expected = _expected_record_revision(request, script)
    for sentence in script["sentences"]:
        if re.search(r"零风险|绝对不会|站多久都不|不会滑倒|我(自己)?(穿了|用过)|库存(紧张|不多)|下播.*恢复", sentence["text"]):
            raise HTTPException(400, f"第{sentence['ordinal']}句包含未核实的体验、库存或保证，请先修改后确认")
    script.update(status="approved", approved_at=now())
    return put("script", script, expected_revision=expected)


@app.post("/api/config")
async def config(data: dict, request: Request = None):
    expected = _expected_record_revision(request, settings())
    return save_room_config("default", data, expected_revision=expected)


def save_room_config(identity, data, creating=False, expected_revision=None):
    previous = {} if creating else settings(identity)
    if previous.get("adopted_publication_ref"):
        comparable = {"name", "product_ids", "version_count", "loop", "plan_mode", "script_order", "selected_script_ids", "tts_provider", "tts_voice", "tts_model", "commerce"}
        proposed = {k: v for k, v in data.items() if k in comparable}
        if "commerce" in proposed:
            proposed["commerce"] = commerce.parse_room({**previous.get("commerce", {}), **proposed["commerce"]})
        if any(proposed[k] != previous.get(k) for k in proposed):
            raise HTTPException(409, "此直播间已采用助手整包，请在右侧对话修改准备草案并重新确认；不能通过旧配置覆盖")
        if expected_revision is not None and expected_revision != previous.get("_record_revision"):
            raise HTTPException(409, "直播间已变化，请刷新后重试")
        return previous
    # A background text job may append versions while a user is sorting the plan.
    # Preserve only those concurrent additions, never resurrect explicitly removed rows.
    if data.get("plan_mode") == "all" and "base_script_ids" in data:
        base = data["base_script_ids"]
        removed = data.get("removed_script_ids", [])
        if not isinstance(base, list) or not isinstance(removed, list):
            raise HTTPException(400, "本场话术基线格式不正确")
        requested = data.get("script_order", [])
        if not isinstance(requested, list):
            raise HTTPException(400, "话术顺序格式不正确")
        data["script_order"] = requested + [sid for sid in production.plan_ids(previous) if sid not in base
                and sid not in requested and sid not in removed
                and get("script", sid)["product_id"] in data.get("product_ids", previous.get("product_ids", []))]
    if "commerce" in data:
        if not isinstance(data["commerce"], dict):
            raise HTTPException(400, "直播间业务配置必须是对象")
        data = {**data, "commerce": {**previous.get("commerce", {}), **data["commerce"]}}
    data = {**previous, **data}
    if data.get("plan_mode") == "all" and isinstance(data.get("script_order", []), list):
        # Compatibility field is derived, never a second independent playback plan.
        data["selected_script_ids"] = [sid for sid in data.get("script_order", []) if isinstance(sid, str)]
    tts = voice_options(data)
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 80:
        raise HTTPException(400, "请填写1至80字直播间名称")
    for field in ["product_ids", "script_order", "selected_script_ids"]:
        values = data.get(field, [])
        if not isinstance(values, list) or len(values) > 1000 or any(not isinstance(v, str) for v in values):
            raise HTTPException(400, "商品与话术选择格式不正确")
    product_ids = list(dict.fromkeys(data.get("product_ids", [])))
    for pid in product_ids:
        product = get("product", pid)
        if (product.get("archived") or product.get("sale_status", "available") != "available") and pid not in previous.get("product_ids", []):
            raise HTTPException(400, "不可售或已归档商品不能加入直播间")
    business = commerce.parse_room(data.get("commerce", {}))
    # Removing a reference also removes only that room's corresponding overrides.
    business["product_overrides"] = {pid: v for pid, v in business.get("product_overrides", {}).items()
        if pid in product_ids or pid not in previous.get("product_ids", [])}
    for container, field in [(business.get("coupon", {}), "product_ids"), (business, "promotion_product_ids")]:
        if isinstance(container, dict) and isinstance(container.get(field), list):
            container[field] = [pid for pid in container[field] if pid in product_ids or pid not in previous.get("product_ids", [])]
    if not business.get("coupon", {}).get("product_ids") and business.get("coupon", {}).get("scope") == "selected":
        business["coupon"] = commerce.Coupon().model_dump()
    if business.get("promotion_scope") == "selected" and not business.get("promotion_product_ids"):
        business.update(promotion="", promotion_scope="all")
    business = commerce.validate_room(business, {pid: get("product", pid) for pid in product_ids})
    for sid in data.get("selected_script_ids", []):
        script = get("script", sid)
        if script["product_id"] not in product_ids:
            raise HTTPException(400, "话术必须属于本直播间引用的商品")
    for sid in data.get("script_order", []):
        if get("script", sid)["product_id"] not in product_ids:
            raise HTTPException(400, "本场话术必须属于本场引用的商品")
    item = {"id": identity, "name": name, "product_ids": product_ids,
            "commerce": business, "commerce_schema_version": 1,
            "version_count": max(1, min(6, int(data.get("version_count", 2)))), "loop": True,
            "tts_provider": tts["provider"], "tts_voice": tts["voice"], "tts_model": tts["model"],
            "plan_mode": "all" if data.get("plan_mode") == "all" else "legacy",
            "script_order": list(dict.fromkeys(data.get("script_order", []))),
            "selected_script_ids": list(dict.fromkeys(data.get("selected_script_ids", []))), "updated_at": now()}
    return put("config", item, expected_revision=expected_revision if expected_revision is not None else previous.get("_record_revision"))


@app.post("/api/rooms")
async def create_room(data: dict):
    return save_room_config(uid("room"), data, creating=True)


@app.get("/api/commerce/schema")
async def commerce_schema():
    return {"schema_version": 1, "product": ProductInput.model_json_schema(),
            "room_commerce": commerce.RoomCommerce.model_json_schema(),
            "ownership": "商品库保存长期事实；commerce 保存本直播间优惠与知识；product_overrides 仅按商品ID引用。"}


@app.put("/api/rooms/{identity}")
async def update_room(identity: str, data: dict, request: Request = None):
    expected = _expected_record_revision(request, settings(identity))
    return save_room_config(identity, data, expected_revision=expected)


@app.get("/api/rooms/{identity}/commerce")
async def room_commerce(identity: str):
    config = settings(identity)
    products = [get("product", pid) for pid in config["product_ids"]]
    return {"room_id": identity, "commerce": config["commerce"], "products": [
        {"product_id": p["id"], "sku_id": p.get("sku_id") or p["id"], "name": p["name"],
         "available": not p.get("archived") and p.get("sale_status", "available") == "available",
         "quote": commerce.quote(p, config), "shipping_time": config["commerce"]["product_overrides"].get(p["id"], {}).get("shipping_time")
             if config["commerce"]["product_overrides"].get(p["id"], {}).get("shipping_override") else p.get("shipping_time", "")}
        for p in products]}


@app.post("/api/rooms/{identity}/price-preview")
async def price_preview(identity: str, data: dict):
    config = settings(identity)
    if data.get("product_id") not in config["product_ids"]:
        raise HTTPException(400, "请选择本直播间已引用的商品")
    if "commerce" in data:
        config = {**config, "commerce": commerce.validate_room(data["commerce"], {pid: get("product", pid) for pid in config["product_ids"]})}
    return commerce.quote(get("product", data["product_id"]), config)


@app.get("/api/rooms/{identity}/state")
async def room_state(identity: str):
    return room_instance(identity).snapshot()


@app.post("/api/rooms/{identity}/control")
async def control_room(identity: str, data: dict):
    return await room_control(identity, data)


@app.get("/api/state")
async def state():
    return room.snapshot()


@app.get("/api/audio/output-status")
async def audio_output_status():
    from .audio_output import output_status
    return await asyncio.to_thread(output_status)


@app.post("/api/control")
async def control(data: dict):
    return await room_control("default", data)


@app.post("/api/preview")
async def preview(data: dict):
    text = str(data.get("text", "")).strip()
    if not text or len(text) > 500:
        raise HTTPException(400, "试听文本须为1至500字")
    try:
        tts = voice_options({**settings(), **{k: v for k, v in data.items() if k.startswith("tts_")}})
        style = sentence_style({"stage": str(data.get("stage", "identity"))}, tts["provider"])
        return await asyncio.get_running_loop().run_in_executor(POOL, render, text, RUNTIME / "audio", tts["voice"], tts["provider"], tts["model"], style)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(502, str(error) if isinstance(error, RuntimeError) else "试听合成失败") from None


@app.get("/api/warmup")
async def warmup_status():
    return deepcopy(warmup_state)


@app.post("/api/warmup")
async def warmup(data: dict):
    return await production.create_job(data.get("room_id", "default"), {"kind": "audio"})


@app.get("/api/jobs")
async def jobs(room_id: str | None = None):
    if room_id:
        settings(room_id)
    return {"jobs": production.list_jobs(room_id)}


@app.post("/api/rooms/{identity}/jobs")
async def create_preparation_job(identity: str, data: dict):
    return await production.create_job(identity, data)


@app.get("/api/rooms/{identity}/audio-status")
async def prepared_audio_status(identity: str):
    return production.audio_status(identity)


@app.post("/api/rooms/{identity}/test-events")
async def test_event(identity: str, data: dict):
    target = room_instance(identity)
    kind = data.get("type")
    if kind not in {"like", "enter", "comment"}:
        raise HTTPException(400, "请选择点赞、进入直播间或发布评论")
    username = str(data.get("username", "测试观众")).strip()
    text = str(data.get("text", "")).strip()
    if not username or len(username) > 40 or (kind == "comment" and (not text or len(text) > 500)):
        raise HTTPException(400, "请填写观众名称，评论为1至500字")
    like_count = data.get("like_count", 1)
    if kind == "like" and (type(like_count) is not int or not 0 < like_count <= 9007199254740991):
        raise HTTPException(400, "本次点赞数须为安全范围内的正整数")
    async with target.lock:
        event = {"id": uid("viewer_event"), "room_id": identity, "session_id": target.state["session_id"],
                 "type": kind, "username": username, "text": text if kind == "comment" else "",
                 "source": "simulated", "time": now(), "test_session_id": data.get("test_session_id"),
                 "reply_turn_id": data.get("reply_turn_id")}
        if kind == "like":
            event["count"] = like_count
        put("viewer_event", event)
        target.state["audience_events"] = (target.state["audience_events"] + [event])[-120:]
        target.log("simulated_" + kind, username + ("：" + text if kind == "comment" else ""))
        welcome = target.welcome_entry(event) if kind == "enter" else None
        reply = target.comment_entry(event) if kind == "comment" else None
        like = target.like_entry(event) if kind == "like" else None
        await target.broadcast()
    return {"event": event, "automatic_reply": bool(reply and reply.get("status") == "preparing"), "welcome": welcome, "reply": reply, "like_thanks": like}


@app.get("/api/rooms/{identity}/reply-knowledge")
async def reply_knowledge(identity: str):
    return reply_lab.knowledge_view(identity)


@app.get("/api/rooms/{identity}/live-source")
async def get_live_source(identity: str, summary_only: bool = False):
    return live_source.get_view(identity, summary_only=summary_only)


@app.get("/api/rooms/{identity}/live-history")
async def get_live_history(identity: str, cursor: int = 0):
    return live_history.list_sessions(identity, cursor)


@app.get("/api/rooms/{identity}/live-history/{session_id}")
async def get_live_history_session(identity: str, session_id: str, before: int | None = None):
    return live_history.read_session(identity, session_id, before)


@app.put("/api/rooms/{identity}/live-source")
async def save_live_source(identity: str, data: dict):
    return live_source.save_config(identity, data)


@app.post("/api/rooms/{identity}/live-source/connect")
async def connect_live_source(identity: str, data: dict | None = None):
    return await live_source.connect_source(identity, data)


@app.post("/api/rooms/{identity}/live-source/disconnect")
async def disconnect_live_source(identity: str):
    return await live_source.disconnect_source(identity)


@app.get("/api/rooms/{identity}/live-source/events")
async def live_source_events(identity: str, after: int = 0):
    return live_source.get_view(identity, after)


@app.get("/api/rooms/{identity}/reply-sessions")
async def reply_sessions(identity: str):
    return {"sessions": reply_lab.list_sessions(identity)}


@app.post("/api/rooms/{identity}/reply-sessions")
async def create_reply_session(identity: str):
    return reply_lab.create_session(identity)


@app.get("/api/reply-sessions/{identity}")
async def reply_history(identity: str):
    return reply_lab.get_session(identity)


@app.post("/api/reply-sessions/{identity}/comments")
async def reply_comment(identity: str, data: dict):
    return await reply_lab.send_comment(identity, data)


@app.post("/api/reply-turns/{identity}/retry")
async def retry_reply(identity: str):
    return await reply_lab.retry_turn(identity)


@app.get("/api/export")
async def export():
    return {"config": all_of("config"), "playback": [r.snapshot() for r in rooms.values()], "products": all_of("product"), "scripts": all_of("script"),
            "viewer_events": all_of("viewer_event"), "preparation_jobs": production.list_jobs(),
            "room_logs": all_of("room_log"), "reply_sessions": all_of("reply_session"), "reply_turns": all_of("reply_turn"),
            "live_replies": all_of("live_reply"),
            "live_sources": all_of("live_source_config"), "live_metrics": all_of("live_metric")}


@app.websocket("/ws")
async def socket(ws: WebSocket):
    host = (ws.headers.get("host") or "").split(":")[0]
    origin = ws.headers.get("origin")
    if host not in {"localhost", "127.0.0.1"} or (origin and urlparse(origin).netloc != ws.headers.get("host")):
        await ws.close(code=1008)
        return
    try:
        room = room_instance(ws.query_params.get("room", "default"))
    except HTTPException:
        await ws.close(code=1008)
        return
    await ws.accept()
    identity = uid("client")
    role = ws.query_params.get("role", "monitor")
    source = ws.query_params.get("source", "browser")
    room.clients[identity] = (ws, role)
    if role == "player":
        room.player_sources[identity] = "obs" if source == "obs" else "browser"
        async with room.lock:
            if room.owner is None or (source == "obs" and room.player_sources.get(room.owner) != "obs"):
                room.owner = identity
                if room.state["current"]:
                    paused = room.state["status"] == "paused"
                    room.set_current(room.state["current"])
                    if paused:
                        room.state["status"] = "paused"
    await ws.send_json({"type": "welcome", "id": identity, "role": role})
    await room.broadcast()
    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "claim" and role == "player":
                async with room.lock:
                    room.owner = identity
                    if room.state["current"]:
                        paused = room.state["status"] == "paused"
                        room.set_current(room.state["current"])
                        if paused:
                            room.state["status"] = "paused"
                    room.log("player_claimed", identity)
                    await room.broadcast()
            elif data.get("type") == "ack" and role == "player":
                await room.acknowledge(identity, data)
            elif data.get("type") == "playback_diagnostic" and role == "player" and identity == room.owner:
                allowed = {"continuous_source_started", "continuous_source_retained", "continuous_sentence_boundary", "continuous_interaction_scheduled", "continuous_play_failed", "continuous_clock_confirmed", "continuous_clock_stalled"}
                if data.get("event") in allowed and data.get("epoch") == room.state["epoch"]:
                    offset = data.get("offset_seconds")
                    import math
                    if offset is None or (type(offset) in {int, float} and math.isfinite(offset) and 0 <= offset <= 1800):
                        detail = {"offset_seconds": offset, "sentence_id": (room.state.get("current") or {}).get("id")}
                        if data["event"] in {"continuous_source_started", "continuous_clock_confirmed", "continuous_clock_stalled"}:
                            if data.get("context_state") in {"running", "suspended", "interrupted", "closed", "unknown"}:
                                detail["context_state"] = data["context_state"]
                            for key in ("context_time", "media_time", "graph_peak", "media_volume"):
                                value = data.get(key)
                                if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 86400:
                                    detail[key] = round(value, 4)
                            for key in ("media_paused", "silent_clock", "media_muted"):
                                if type(data.get(key)) is bool: detail[key] = data[key]
                            if data.get("output_mode") in {"web_audio_direct", "media_stream"}:
                                detail["output_mode"] = data["output_mode"]
                            if type(data.get("media_ready_state")) is int and 0 <= data["media_ready_state"] <= 4:
                                detail["media_ready_state"] = data["media_ready_state"]
                        room.log("playback_" + data["event"], detail)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        room.clients.pop(identity, None)
        room.player_sources.pop(identity, None)
        if room.owner == identity:
            room.owner = None
            async with room.lock:
                if room.state["current"]:
                    room.state.update(epoch=room.state["epoch"]+1, status="paused", error="播出窗口已断开，重新连接后恢复")
                room.log("player_disconnected", identity)
                await room.broadcast()


@app.get("/audio/{filename}")
async def audio_file(filename: str):
    if not re.fullmatch(r"[a-f0-9]{64}\.wav", filename):
        raise HTTPException(404)
    path = RUNTIME / "audio" / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/wav")


@app.get("/room-audio/{identity}/{filename}")
async def room_audio_file(identity: str, filename: str):
    if not re.fullmatch(r"[a-f0-9]{64}\.wav", filename):
        raise HTTPException(404)
    path = production.room_directory(identity) / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/wav")


@app.get("/")
async def index():
    return FileResponse(ROOT / "web/agent-workbench.html")


@app.get("/studio")
async def studio():
    """Secondary playback console; the primary app is the Agent workbench."""
    return FileResponse(ROOT / "web/index.html")


@app.get("/test")
async def test_console():
    return FileResponse(ROOT / "web/test.html")


@app.get("/tts-samples/{filename}")
async def tts_sample(filename: str):
    if not re.fullmatch(r"[a-f0-9]{64}\.wav", filename):
        raise HTTPException(404)
    path = ROOT / "docs/语音试听_v1.4" / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/wav")


@app.get("/live/{identity}")
async def stage(identity: str):
    settings(identity)
    return FileResponse(ROOT / "web/live.html")


app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")

# Tests set this before import and use an isolated runtime. Production mounts
# the authenticated bridge after every legacy route and type is defined.
if os.environ.get("SHENGBO_DISABLE_AGENT_BRIDGE") != "1":
    from .prelive_bridge import install as install_prelive_bridge
    install_prelive_bridge(app, sys.modules[__name__])
