"""Grounded, no-provider like acknowledgements for the current consumer product.

WebcastLikeMessage.count is the current message's count; .total is a room
aggregate and is never attributed to a viewer. Room.lock must protect checking
the existing insertion slot and calling this policy. Each viewer's short burst
is combined before joining a FIFO backlog; no global cooldown or busy drop.
There are no TTS or model calls here.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from decimal import InvalidOperation
import hashlib
import json
import re
import threading
import time
import unicodedata
from uuid import uuid4

from fastapi import HTTPException

from . import commerce_config as commerce
from .welcome import readable_username

LIKE_THANKS_POLICY_REVISION = "like-thanks-v2-per-viewer-quiet-burst-fifo"
QUIET_SECONDS = 3.0
MAX_BURST_SECONDS = 8.0
# Waiting viewers do not expire. Only already-prepared audio expires, and the
# caller can return its unchanged batch to this policy to prepare fresh audio.
MAX_PENDING_SECONDS = 120.0
_CURRENCIES = {"CNY": "元", "TWD": "新台币", "USD": "美元", "AUD": "澳元"}


class LikePreparationError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def event_like_count(event):
    """Accept only a positive, explicit per-message integer, never `total`."""
    value = event.get("count")
    if type(value) is int:
        return value if 0 < value < 10**30 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,30}", value):
        number = int(value)
        return number if number > 0 else None
    return None


def _consumer_offer(current, product, config):
    if (not isinstance(current, dict) or not isinstance(product, dict)
            or not isinstance(config, dict) or not config.get("id")
            or not product.get("id") or current.get("product_id") != product["id"]):
        raise LikePreparationError("product_changed")
    try:
        # The existing commerce calculator owns room overrides and coupon
        # eligibility; legacy promo_price and procurement fields are not prices.
        effective = commerce.effective_product(product, config)
        name = str(effective.get("name") or "")
        name = "".join(c for c in name if unicodedata.category(c)[0] in {"L", "M", "N"}
                       or c.isspace() or c in "·—-")
        name = " ".join(name.split())[:60]
        if not name:
            raise LikePreparationError("product_unavailable")
        quote = effective["_price_quote"]
        currency = quote.get("currency")
        if currency not in _CURRENCIES:
            raise LikePreparationError("price_unavailable")
        price = None
        if quote.get("confirmed") and quote.get("final_price") is not None:
            amount = commerce.money(quote["final_price"])
            if not amount.is_finite() or amount < 0:
                raise LikePreparationError("price_unavailable")
            price = format(amount, "f").rstrip("0").rstrip(".")
        snapshot = {"room_id": config["id"], "product_id": product["id"],
                    "name": name, "currency": currency, "quote": quote}
        fingerprint = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        return name, price, currency, fingerprint
    except LikePreparationError:
        raise
    except (HTTPException, KeyError, TypeError, ValueError, InvalidOperation):
        raise LikePreparationError("product_or_price_unavailable") from None


def prepare_like(event, current, product, config, session_id):
    """Pure text preparation; reading this result does not admit or start audio.

    If a conditional price cannot be confirmed, omit the amount rather than
    promising a discount or narrating internal configuration.
    """
    if (not isinstance(event, dict) or event.get("type") != "like" or not event.get("id")
            or not session_id or event.get("session_id") != session_id):
        raise LikePreparationError("invalid_event_or_session")
    if event.get("source") == "douyin" and event.get("purpose") != "own_room":
        raise LikePreparationError("external_test_read_only")
    if not isinstance(config, dict) or event.get("room_id") != config.get("id"):
        raise LikePreparationError("room_changed")
    name, price, currency, fingerprint = _consumer_offer(current, product, config)
    username = readable_username(event.get("username") or event.get("nickname")) or "这位朋友"
    count = event_like_count(event)
    thanks = f"感谢{username}送来的{count}个赞！" if count is not None else f"感谢{username}的点赞！"
    offer = (f"今天给大家推荐的是{name}，售价{price}{_CURRENCIES[currency]}。"
             if price is not None else f"今天给大家推荐的是{name}。")
    text = thanks + offer + "想了解怎么用，留下来听我接着讲！"
    return {"is_like": True, "like_event_id": event["id"], "like_session_id": session_id,
            "like_product_id": product["id"], "like_room_id": config["id"],
            "like_source_fingerprint": fingerprint, "like_count": count,
            "like_price_status": "confirmed" if price is not None else "unconfirmed_omitted",
            "text": text, "spoken_text": text}


class LikeThanksRuntime:
    """Combine a viewer's burst, then offer ready batches to one Room slot.

    collect/admit always buffers during another insertion. pop_ready freezes
    text only when Room can prepare it; requeue returns an unplayed batch after
    stale prices, a product switch, or preparation failure. Room.lock still
    owns all slot and ACK transitions. Capacity errors must be logged by Room.
    """
    def __init__(self, *, clock=None, wall_clock=None, capacity=512):
        self.clock = clock or time.monotonic
        self.wall_clock = wall_clock or time.time
        if type(capacity) is not int or capacity < 1:
            raise ValueError("点赞待播容量须为正整数")
        self.capacity = capacity
        self.session_id = None
        self.started = set()
        self.seen = OrderedDict()
        self.pending = OrderedDict()
        self.inflight = {}
        self.failed = OrderedDict()
        self.open_by_viewer = {}
        self._diagnostics = []
        self._lock = threading.RLock()

    def session(self, session_id):
        with self._lock:
            if self.session_id != session_id:
                if self.pending or self.inflight:
                    self._diagnostics.append({"reason": "session_changed",
                        "pending_batches": len(self.pending), "inflight_batches": len(self.inflight)})
                self.session_id = session_id
                self.pending.clear()
                self.inflight.clear()
                self.open_by_viewer.clear()
                self.started.clear()
                # Preserve upstream deduplication across stop/start and reconnect.

    def drain_diagnostics(self):
        with self._lock:
            result, self._diagnostics = self._diagnostics, []
            return result

    def clear(self, *, reason="stopped"):
        """Cancel this live session's waiting batches, retaining event dedup."""
        with self._lock:
            record = {"reason": str(reason), "pending_batches": len(self.pending),
                "inflight_batches": len(self.inflight), "open_viewers": len(self.open_by_viewer)}
            self.pending.clear()
            self.inflight.clear()
            self.open_by_viewer.clear()
            self.started.clear()
            self._diagnostics.append(record)
            return deepcopy(record)

    def _viewer_key(self, event):
        for field in ("viewer_hash", "user_id", "viewer_id", "user_id_str"):
            value = event.get(field)
            if value is not None and str(value).strip() not in {"", "0"}:
                return str(event.get("source")), field, str(value)
        raw = unicodedata.normalize("NFKC", str(event.get("username") or event.get("nickname") or "")).strip()
        # Keep numeric distinctions in the grouping key, but never speak them.
        # Missing identity is not evidence that two anonymous messages are one person.
        return (str(event.get("source")), "nickname", raw) if raw else ("anonymous_event", event["id"])

    def _ready_at(self, batch):
        return min(batch["last_at"] + QUIET_SECONDS, batch["first_at"] + MAX_BURST_SECONDS)

    def next_ready_at(self):
        with self._lock:
            # First-arrival order is intentional: later viewers cannot jump an
            # earlier active burst; its hard eight-second boundary is bounded.
            return self._ready_at(next(iter(self.pending.values()))) if self.pending else None

    def remember(self, event):
        with self._lock:
            upstream = event.get("upstream_event_id")
            key = ((event.get("source"), event.get("external_room_id"), event.get("raw_method"), str(upstream))
                   if upstream is not None and str(upstream) not in {"", "0"}
                   else (event.get("source"), event.get("id")))
            if key in self.seen:
                return False
            self.seen[key] = None
            if len(self.seen) > 4096:
                self.seen.popitem(last=False)
            return True

    def collect(self, event, current, product, config, session_id):
        """Collect one proven like; receipt is not prepared playback text."""
        with self._lock:
            if (not isinstance(event, dict) or event.get("type") != "like" or not event.get("id")):
                return None, "invalid_event"
            if not session_id or event.get("session_id") != session_id:
                return None, "session_changed"
            if not isinstance(config, dict) or event.get("room_id") != config.get("id"):
                return None, "room_changed"
            if event.get("source") == "douyin" and event.get("purpose") != "own_room":
                return None, "external_test_read_only"
            self.session(session_id)
            if not self.remember(event):
                return None, "duplicate_event"
            now = self.clock()
            viewer = self._viewer_key(event)
            key = self.open_by_viewer.get(viewer)
            batch = self.pending.get(key)
            # Once its quiet/deadline boundary passed, a later event starts a
            # separate burst even if the insertion slot was occupied meanwhile.
            if batch is not None and now >= self._ready_at(batch):
                batch = None
            merging = batch is not None
            if batch is None:
                if len(self.pending) + len(self.inflight) >= self.capacity:
                    self._diagnostics.append({"reason": "capacity_exceeded", "event_id": event["id"],
                        "capacity": self.capacity, "pending_batches": len(self.pending),
                        "inflight_batches": len(self.inflight)})
                    return None, "capacity_exceeded"
                key = "like_batch_" + uuid4().hex
                batch = {"id": key, "viewer": viewer, "first_at": now, "last_at": now,
                    "received_ms": round(self.wall_clock() * 1000, 3), "count": 0,
                    "count_complete": True, "event_count": 0, "event_ids": [],
                    "event": {k: deepcopy(event.get(k)) for k in
                        ("id", "type", "source", "purpose", "room_id", "session_id", "username", "nickname")}}
                self.pending[key] = batch
                self.open_by_viewer[viewer] = key
            count = event_like_count(event)
            if count is None:
                batch["count_complete"] = False
            else:
                batch["count"] += count
            batch["last_at"] = now
            batch["event_count"] += 1
            if len(batch["event_ids"]) < 128:
                batch["event_ids"].append(event["id"])
            return {"status": "merged" if merging else "buffered", "like_batch_id": key,
                    "event_count": batch["event_count"], "ready_at": self._ready_at(batch),
                    "pending_batches": len(self.pending)}, None

    # Same method name is retained for Room adapters, with the new buffered
    # receipt contract. A receipt must never be passed directly to TTS.
    admit = collect

    def pop_ready(self, current, product, config, session_id):
        with self._lock:
            if session_id != self.session_id:
                return None, "session_changed"
            if not self.pending:
                return None, "empty"
            key, batch = next(iter(self.pending.items()))
            now = self.clock()
            if now < self._ready_at(batch):
                return None, "collecting"
            event = {**batch["event"], "count": batch["count"] if batch["count_complete"] else None}
            try:
                item = prepare_like(event, current, product, config, session_id)
            except LikePreparationError as error:
                return None, error.reason
            self.pending.pop(key)
            if self.open_by_viewer.get(batch["viewer"]) == key:
                self.open_by_viewer.pop(batch["viewer"], None)
            self.inflight[key] = batch
            item.update(like_batch_id=key, like_admitted_at=now,
                like_expires_at=self.wall_clock() + MAX_PENDING_SECONDS,
                like_received_ms=batch["received_ms"], like_event_ids=list(batch["event_ids"]),
                like_event_count=batch["event_count"], like_count_complete=batch["count_complete"])
            return item, None

    def requeue(self, item, *, reason="not_played"):
        """Return an unplayed frozen batch; next pop rebuilds current offer."""
        with self._lock:
            if item.get("like_session_id") != self.session_id:
                return False
            key = item.get("like_batch_id")
            batch = self.inflight.pop(key, None)
            if batch is None or key in self.started:
                return False
            self.pending[key] = batch
            self.pending.move_to_end(key, last=False)
            self._diagnostics.append({"reason": "requeued", "like_batch_id": key, "cause": reason})
            return True

    def fail(self, item, *, reason):
        """Release a failed batch without retrying a possibly charged request.

        The returned diagnostic must be persisted by Room. A started-but-failed
        playback is not claimed to be completed or entirely unheard.
        """
        with self._lock:
            key = item.get("like_batch_id")
            if (item.get("like_session_id") != self.session_id or key in self.failed
                    or key not in self.inflight and key not in self.started):
                return None
            self.inflight.pop(key, None)
            record = {"reason": "failed_unconfirmed", "cause": str(reason),
                "like_batch_id": key, "event_id": item.get("like_event_id"),
                "event_ids": list(item.get("like_event_ids", [])),
                "event_count": item.get("like_event_count"), "count": item.get("like_count"),
                "playback_started": key in self.started, "automatic_retry": False}
            self.failed[key] = record
            self._diagnostics.append(record)
            if len(self.failed) > self.capacity:
                old, _ = self.failed.popitem(last=False)
                self._diagnostics.append({"reason": "failure_history_evicted", "like_batch_id": old,
                    "notice": "in-memory retention limit; persisted Room diagnostics remain authoritative"})
            return deepcopy(record)

    def play_reason(self, item, current, product, config, session_id):
        with self._lock:
            if item.get("like_session_id") != session_id or self.session_id != session_id:
                return "session_changed"
            if not config or item.get("like_room_id") != config.get("id"):
                return "room_changed"
            if not current or item.get("like_product_id") != current.get("product_id"):
                return "product_changed"
            now = self.clock()
            if now - item.get("like_admitted_at", float("-inf")) >= MAX_PENDING_SECONDS:
                return "expired"
            key = item.get("like_batch_id")
            if key in self.started:
                return "already_started"
            if key not in self.inflight:
                return "batch_not_pending"
            try:
                fingerprint = _consumer_offer(current, product, config)[3]
            except LikePreparationError:
                return "source_changed"
            if item.get("like_source_fingerprint") != fingerprint:
                return "source_changed"
            return None

    def mark_started(self, item):
        """Call once after play_reason passes, inside the caller's Room.lock."""
        with self._lock:
            if item.get("like_session_id") != self.session_id:
                return False
            token = item["like_batch_id"]
            if token in self.started or token not in self.inflight:
                return False
            self.started.add(token)
            self.inflight.pop(token)
            return True
