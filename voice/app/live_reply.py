"""One grounded reply for the product currently playing, without playback effects.

The caller owns event deduplication, session/TTL/concurrency and insertion. This
module does not create reply-lab sessions, re-emit events, call TTS, or retry a
provider. It shares the existing evidence retrieval and quotation validator.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from time import perf_counter_ns

from . import commerce_config as commerce, reply_lab
from .consumer_catalog import consumer_context_view


_PRODUCT_FIELDS = frozenset({"id", "name", "display_number", "sku_id", "price", "promo_price", "currency",
    "selling_points", "core_selling_points", "promotion", "knowledge", "knowledge_documents", "specifications",
    "material", "ingredients", "target_audience", "storage_conditions", "shipping_origin", "shipping_time",
    "return_policy", "usage_instructions", "faq", "shelf_life_value", "shelf_life_unit", "sales_platform",
    "hidden_optional_fields", "archived", "sale_status"})


class LiveReplyError(RuntimeError):
    """Safe stage diagnostics; no raw provider response or transport exception."""
    def __init__(self, code, stage, timings_ms, *, usage=None):
        super().__init__("评论回复未完成，请查看该阶段记录；没有自动重试。")
        self.code, self.stage, self.timings_ms = code, stage, dict(timings_ms)
        self.usage = _usage(usage)
        self.usage_state = "known" if self.usage else "unknown"


def _consumer_product(product):
    safe = commerce.active_product_fields(product)
    return {key: deepcopy(value) for key, value in safe.items() if key in _PRODUCT_FIELDS}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def reply_product_fingerprint(product):
    """Caller may use the same consumer-view identity for its stale-result check."""
    return _digest(_consumer_product(product))


def _usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: number for key, number in value.items() if key in {
        "prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
        and type(number) is int and number >= 0}


async def build_live_answer(event, product, config):
    """Return a grounded short answer or raise LiveReplyError; no side effects on air.

    The complete current consumer product is indexed; only matching evidence and
    applicable structured price/shipping rules enter the one model request. Old
    scripts, other products, prior answers and raw config never become evidence.
    """
    started = perf_counter_ns()
    timings = {}
    metadata = {}
    stage = "input"
    tick = started
    try:
        if (not isinstance(event, dict) or event.get("type") != "comment"
                or not isinstance(event.get("text"), str) or not 1 <= len(event["text"].strip()) <= 2000
                or not isinstance(product, dict) or not product.get("id") or not product.get("name")
                or not isinstance(config, dict) or not config.get("id")
                or (event.get("room_id") and event["room_id"] != config["id"])):
            raise ValueError("invalid live reply scope")
        safe_product = _consumer_product(product)
        safe_config = consumer_context_view({"id": config["id"], "name": config.get("name", "直播间"),
            "commerce": deepcopy(config.get("commerce", commerce.parse_room({})))})
        effective = commerce.effective_product(safe_product, safe_config)
        timings[stage] = round((perf_counter_ns() - tick) / 1_000_000, 3)
        stage, tick = "retrieval", perf_counter_ns()
        retrieval = await asyncio.to_thread(reply_lab.retrieve, [safe_product], event["text"].strip(),
            limit=6, config=safe_config)
        if any(chunk.get("product_id") != safe_product["id"] for chunk in retrieval["chunks"]):
            raise ValueError("cross product retrieval")
        timings[stage] = round((perf_counter_ns() - tick) / 1_000_000, 3)
        context = {"today": datetime.now(timezone.utc).astimezone().date().isoformat(),
            "room": {"id": safe_config["id"], "name": safe_config["name"]},
            "catalog": [{"id": safe_product["id"], "name": safe_product["name"],
                         "number": safe_product.get("display_number")}],
            "current_product_id": safe_product["id"], "evidence": retrieval["chunks"],
            "history_for_reference_only": [], "comment": event["text"].strip(),
            "reply_context": {"mode": "live_comment", "audience": "当前正在听直播的观众",
                "delivery": "先直接回应一条评论，用自然普通话1至3句、通常不超过120字；只说证据支持的信息。"}}
        stage, tick = "model", perf_counter_ns()
        raw, metadata = await asyncio.to_thread(reply_lab.complete_remote, context, max_tokens=600, timeout=20)
        timings[stage] = round((perf_counter_ns() - tick) / 1_000_000, 3)
        stage, tick = "validation", perf_counter_ns()
        answer = reply_lab.validate_answer(raw, retrieval)
        timings[stage] = round((perf_counter_ns() - tick) / 1_000_000, 3)
        timings["total"] = round((perf_counter_ns() - started) / 1_000_000, 3)
        return {**answer, "retrieval": retrieval, "timings_ms": timings,
            "usage": _usage(metadata.get("usage")), "model": metadata.get("model", ""),
            "provider": metadata.get("provider", "DeepSeek"), "product_id": safe_product["id"],
            "product_fingerprint": _digest(safe_product),
            "effective_product_fingerprint": _digest(_consumer_product(effective)),
            "context_hash": _digest(context), "prompt_revision": reply_lab.digest(reply_lab.prompt())}
    except asyncio.CancelledError:
        raise
    except Exception as error:
        timings[stage] = round((perf_counter_ns() - tick) / 1_000_000, 3)
        timings["total"] = round((perf_counter_ns() - started) / 1_000_000, 3)
        code = {"input": "LIVE_REPLY_INPUT_INVALID", "retrieval": "LIVE_REPLY_RETRIEVAL_FAILED",
            "model": "LIVE_REPLY_PROVIDER_FAILED", "validation": "LIVE_REPLY_VALIDATION_FAILED"}[stage]
        raise LiveReplyError(code, stage, timings, usage=metadata.get("usage")) from None
