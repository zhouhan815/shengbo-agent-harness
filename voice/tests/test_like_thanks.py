"""Like policy checks use fake clocks and local calculator only; no TTS/API."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app import commerce_config as commerce, live_source
from app.like_thanks import (LikeThanksRuntime, event_like_count, prepare_like,
                             LikePreparationError)


class Clock:
    def __init__(self): self.value = 0.0
    def __call__(self): return self.value


def product(**extra):
    return {"id": "p1", "name": "网球训练器", "price": 23.58, "currency": "CNY",
            "promo_price": 9.9, "purchase_price": 9.23, **extra}


def config():
    return {"id": "r1", "commerce": commerce.parse_room({})}


def event(identity="e1", **extra):
    return {"id": identity, "type": "like", "source": "simulated", "room_id": "r1",
            "session_id": "s1", "username": "王教练88＋🎾", "count": 8,
            "total": 99999, **extra}


def current(): return {"product_id": "p1"}


def policy(**options):
    c = Clock()
    return LikeThanksRuntime(clock=c, wall_clock=lambda: 1000 + c.value, **options), c


def test_real_normalizer_keeps_per_message_count_separate_from_room_total(monkeypatch):
    monkeypatch.setattr(live_source, "srv", lambda: SimpleNamespace(uid=lambda _: "douyin-e1", now=lambda: "now"))
    normalized, key = live_source.normalize({"method": "WebcastLikeMessage",
        "common": {"msgId": "9007199254740993"}, "user": {"nickname": "教练99✨"},
        "count": "12", "total": "999999"},
        {"id": "r1", "external_room_id": "external1", "purpose": "own_room"}, "capture1")
    assert normalized["count"] == 12 and normalized["total"] == 999999
    assert key == "WebcastLikeMessage:9007199254740993"
    assert event_like_count(normalized) == 12
    normalized["count"] = None
    assert event_like_count(normalized) is None


@pytest.mark.parametrize("count,expected", [(8, 8), ("12", 12), ("0002", 2),
    (None, None), (True, None), (0, None), (-3, None), (1.5, None),
    ("1.0", None), ("１２", None), ("+3", None), (10**30, None)])
def test_only_explicit_positive_integer_count_is_read(count, expected):
    assert event_like_count({"count": count, "total": 987654321}) == expected


def test_text_uses_consumer_sale_price_not_procurement_or_legacy_promo():
    p, cfg, e = product(), config(), event()
    before = deepcopy((p, cfg, e))
    item = prepare_like(e, current(), p, cfg, "s1")
    assert "感谢王教练送来的8个赞" in item["text"]
    assert "网球训练器，售价23.58元" in item["text"]
    assert all(x not in item["text"] for x in ["99999", "9.9", "9.23", "配置", "优惠"])
    assert "留下来听我接着讲" in item["text"]
    assert (p, cfg, e) == before


def test_confirmed_room_price_and_selected_coupon_use_existing_calculator():
    cfg = config()
    cfg["commerce"] = commerce.parse_room({"product_overrides": {"p1": {"mode": "price", "price": 20,
        "exclusive": False}}, "coupon": {"mode": "coupon", "amount": 3, "minimum_spend": 20,
        "scope": "selected", "product_ids": ["p1"]}})
    item = prepare_like(event(), current(), product(), cfg, "s1")
    assert "售价17元" in item["text"] and item["like_price_status"] == "confirmed"


def test_unconfirmed_conditional_price_omits_amount_and_internal_rules():
    cfg = config()
    cfg["commerce"] = commerce.parse_room({"coupon": {"mode": "coupon", "amount": 3,
        "condition": "会员专享"}})
    item = prepare_like(event(count=None, username="123＋🎾"), current(), product(), cfg, "s1")
    assert "感谢这位朋友的点赞" in item["text"]
    assert "网球训练器" in item["text"] and item["like_price_status"] == "unconfirmed_omitted"
    assert all(x not in item["text"] for x in ["99999", "23.58", "会员", "配置", "个赞"])


def test_same_person_burst_merges_after_three_seconds_of_silence():
    r, clock = policy()
    receipt, reason = r.collect(event("a", count=2, viewer_hash="u1"), current(), product(), config(), "s1")
    assert reason is None and receipt["status"] == "buffered"
    clock.value = 1
    receipt, _ = r.collect(event("b", count=3, viewer_hash="u1"), current(), product(), config(), "s1")
    assert receipt["status"] == "merged"
    assert r.next_ready_at() == 4
    clock.value = 3.999
    assert r.pop_ready(current(), product(), config(), "s1") == (None, "collecting")
    clock.value = 4
    item, reason = r.pop_ready(current(), product(), config(), "s1")
    assert reason is None and item["like_count"] == 5
    assert item["like_event_count"] == 2 and "5个赞" in item["text"]
    assert r.mark_started(item) and not r.mark_started(item)
    # A later burst is thanked again immediately after its own quiet window,
    # without any global or per-person sixty-second cooldown.
    r.collect(event("c", count=1, viewer_hash="u1"), current(), product(), config(), "s1")
    clock.value = 7
    other, _ = r.pop_ready(current(), product(), config(), "s1")
    assert other["like_count"] == 1 and other["like_batch_id"] != item["like_batch_id"]


def test_continuous_likes_seal_at_eight_seconds_instead_of_waiting_forever():
    r, clock = policy()
    for i, at in enumerate([0, 2, 4, 6, 7.9]):
        clock.value = at
        r.collect(event(str(i), count=1), current(), product(), config(), "s1")
    assert r.next_ready_at() == 8
    clock.value = 8
    item, reason = r.pop_ready(current(), product(), config(), "s1")
    assert reason is None and item["like_count"] == 5


def test_upstream_reconnect_replay_keeps_event_identity_across_session():
    r, clock = policy()
    source = {"source": "douyin", "purpose": "own_room", "raw_method": "WebcastLikeMessage",
              "external_room_id": "external1", "upstream_event_id": "9007199254740993"}
    assert r.collect(event("local1", **source), current(), product(), config(), "s1")[0]
    clock.value = 60
    assert r.collect(event("local2", **source), current(), product(), config(), "s1")[1] == "duplicate_event"
    assert r.collect(event("local3", session_id="s2", **source), current(), product(), config(), "s2")[1] == "duplicate_event"


@pytest.mark.parametrize("change,expected", [("expired", "expired"), ("product", "product_changed"),
    ("session", "session_changed"), ("room", "room_changed"), ("price", "source_changed"),
    ("name", "source_changed"), ("withdrawn", "source_changed"), ("started", "already_started")])
def test_play_time_rechecks_session_ttl_product_current_offer(change, expected):
    r, clock = policy()
    r.collect(event(), current(), product(), config(), "s1")
    clock.value = 3
    item, _ = r.pop_ready(current(), product(), config(), "s1")
    c, p, cfg, sid = current(), product(), config(), "s1"
    if change == "expired": clock.value = 123
    if change == "product": c["product_id"] = "p2"
    if change == "session": sid = "s2"
    if change == "room": cfg["id"] = "r2"
    if change == "price": p["price"] = 25
    if change == "name": p["name"] = "新网球训练器"
    if change == "withdrawn": p["sale_status"] = "unavailable"
    if change == "started": assert r.mark_started(item)
    assert r.play_reason(item, c, p, cfg, sid) == expected


def test_procurement_change_is_not_part_of_spoken_offer_fingerprint():
    r, clock = policy()
    r.collect(event(), current(), product(), config(), "s1")
    clock.value = 3
    item, _ = r.pop_ready(current(), product(), config(), "s1")
    assert r.play_reason(item, current(), product(purchase_price=1), config(), "s1") is None


def test_different_people_all_queue_during_busy_no_global_cooldown():
    r, clock = policy()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: r.collect(event(str(i), viewer_hash=str(i + 1)), current(), product(), config(), "s1"), range(24)))
    assert all(receipt and reason is None for receipt, reason in results)
    assert len(r.pending) == 24
    clock.value = 150  # Even waiting beyond the old TTL must not drop people.
    batches = []
    for _ in range(24):
        item, reason = r.pop_ready(current(), product(), config(), "s1")
        assert reason is None and r.mark_started(item)
        batches.append(item["like_batch_id"])
    assert len(set(batches)) == 24 and not r.pending


def test_fifo_and_stable_user_id_win_over_nickname_then_nickname_is_fallback():
    r, clock = policy()
    r.collect(event("a", viewer_hash="u1", username="Alice1"), current(), product(), config(), "s1")
    r.collect(event("b", viewer_hash="u2", username="Alice1"), current(), product(), config(), "s1")
    clock.value = 1
    r.collect(event("c", viewer_hash="u1", username="ChangedNickname"), current(), product(), config(), "s1")
    r.collect(event("d", username="王123"), current(), product(), config(), "s1")
    r.collect(event("e", username="王456"), current(), product(), config(), "s1")
    assert len(r.pending) == 4
    clock.value = 4
    a, _ = r.pop_ready(current(), product(), config(), "s1")
    b, _ = r.pop_ready(current(), product(), config(), "s1")
    assert a["like_event_ids"] == ["a", "c"] and b["like_event_ids"] == ["b"]


def test_unknown_count_in_a_burst_never_claims_room_total_or_partial_sum():
    r, clock = policy()
    r.collect(event("a", count=2), current(), product(), config(), "s1")
    r.collect(event("b", count=None, total=99999), current(), product(), config(), "s1")
    clock.value = 3
    item, _ = r.pop_ready(current(), product(), config(), "s1")
    assert item["like_count"] is None and not item["like_count_complete"]
    assert "个赞" not in item["text"] and "99999" not in item["text"]


def test_freeze_does_not_mutate_tts_text_and_requeue_rebuilds_latest_offer():
    r, clock = policy()
    r.collect(event("a", count=2), current(), product(), config(), "s1")
    clock.value = 3
    frozen, _ = r.pop_ready(current(), product(), config(), "s1")
    r.collect(event("b", count=3), current(), product(), config(), "s1")
    assert frozen["like_count"] == 2 and "2个赞" in frozen["text"]
    assert r.play_reason(frozen, current(), product(price=25), config(), "s1") == "source_changed"
    assert r.requeue(frozen, reason="source_changed")
    rebound, _ = r.pop_ready({"product_id": "p2"}, product(id="p2", name="口红", price=30), config(), "s1")
    assert "口红" in rebound["text"] and "30元" in rebound["text"]
    assert r.mark_started(rebound) and not r.requeue(rebound)
    clock.value = 6
    followup, _ = r.pop_ready(current(), product(), config(), "s1")
    assert followup["like_count"] == 3
    assert r.drain_diagnostics()[0]["cause"] == "source_changed"


def test_capacity_is_explicit_and_existing_viewer_can_still_merge():
    r, _ = policy(capacity=1)
    assert r.collect(event("a", viewer_hash="u1"), current(), product(), config(), "s1")[0]
    assert r.collect(event("b", viewer_hash="u1"), current(), product(), config(), "s1")[0]
    assert r.collect(event("c", viewer_hash="u2"), current(), product(), config(), "s1") == (None, "capacity_exceeded")
    diagnostic = r.drain_diagnostics()
    assert diagnostic[0]["reason"] == "capacity_exceeded" and diagnostic[0]["event_id"] == "c"
    assert not r.drain_diagnostics()


@pytest.mark.parametrize("price,spoken", [(100, "100元"), (1000, "1000元"),
    (0.01, "0.01元"), (0, "0元")])
def test_money_quantizes_before_removing_fractional_zeroes(price, spoken):
    assert "售价" + spoken in prepare_like(event(), current(), product(price=price), config(), "s1")["text"]


def test_failed_batch_is_visible_not_auto_retried_and_does_not_block_next_person():
    r, clock = policy()
    r.collect(event("a", viewer_hash="u1"), current(), product(), config(), "s1")
    r.collect(event("b", viewer_hash="u2"), current(), product(), config(), "s1")
    clock.value = 3
    first, _ = r.pop_ready(current(), product(), config(), "s1")
    failure = r.fail(first, reason="tts_result_unknown")
    assert failure["automatic_retry"] is False and not failure["playback_started"]
    assert first["like_batch_id"] in r.failed and first["like_batch_id"] not in r.inflight
    assert not r.requeue(first) and r.fail(first, reason="again") is None
    second, _ = r.pop_ready(current(), product(), config(), "s1")
    assert second["like_event_id"] == "b" and r.mark_started(second)
    assert r.fail(second, reason="player_failed")["playback_started"] is True
    assert len(r.drain_diagnostics()) == 2


def test_clear_on_stop_cancels_pending_and_inflight_but_keeps_seen():
    r, clock = policy()
    r.collect(event("a", viewer_hash="u1"), current(), product(), config(), "s1")
    r.collect(event("b", viewer_hash="u2"), current(), product(), config(), "s1")
    clock.value = 3
    first, _ = r.pop_ready(current(), product(), config(), "s1")
    result = r.clear(reason="stopped")
    assert result["pending_batches"] == 1 and result["inflight_batches"] == 1
    assert not r.pending and not r.inflight and not r.open_by_viewer
    assert r.next_ready_at() is None and not r.requeue(first)
    assert r.collect(event("a", viewer_hash="u1"), current(), product(), config(), "s1")[1] == "duplicate_event"
    assert r.drain_diagnostics()[-1] == result


@pytest.mark.parametrize("withdrawal", [{"archived": True}, {"sale_status": "unavailable"}])
def test_archived_or_withdrawn_product_is_refused_at_prepare_and_play(withdrawal):
    r, clock = policy()
    r.collect(event(), current(), product(), config(), "s1")
    clock.value = 3
    item, _ = r.pop_ready(current(), product(), config(), "s1")
    assert r.play_reason(item, current(), product(**withdrawal), config(), "s1") == "source_changed"
    with pytest.raises(LikePreparationError):
        prepare_like(event(), current(), product(**withdrawal), config(), "s1")


def test_read_only_external_room_and_wrong_session_cannot_admit():
    r, _ = policy()
    assert r.admit(event("x", source="douyin", purpose="external_test"), current(), product(), config(), "s1")[1] == "external_test_read_only"
    assert r.admit(event("y", session_id="old"), current(), product(), config(), "s1")[1] == "session_changed"
    with pytest.raises(LikePreparationError, match="room_changed"):
        prepare_like(event("z", room_id="r2"), current(), product(), config(), "s1")
