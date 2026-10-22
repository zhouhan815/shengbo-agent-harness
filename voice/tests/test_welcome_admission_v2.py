"""Fast welcome policy checks: fake clocks, no Room, TTS or real waiting."""
from copy import deepcopy

import pytest

from app.welcome import (FALLBACK, WelcomeRuntime, build_product_templates,
                         readable_username, WELCOME_POLICY_REVISION)


class FakeClock:
    def __init__(self): self.value = 0.0
    def __call__(self): return self.value


def event(identity, username="小明123＋×÷✨", **extra):
    return {"id": identity, "source": "simulated", "type": "enter", "username": username, **extra}


def product(identity="p1", name="网球训练器"):
    return {"product_id": identity, "welcome_templates": build_product_templates({"name": name})}


def runtime():
    clock = FakeClock()
    return WelcomeRuntime(clock=clock, choose=lambda choices: choices[0]), clock


def test_t0_a_then_t10_bcd_silent_then_t60_first_enter_welcomed_without_replay():
    r, clock = runtime()
    first, reason = r.admit(event("A"), product(), "s1")
    assert reason is None and "小明" in first["text"] and "123" not in first["text"]
    assert r.mark_started(first)
    clock.value = 10
    for identity in "BCD":
        assert r.admit(event(identity), product(), "s1") == (None, "cooldown")
    clock.value = 60
    second, reason = r.admit(event("E"), product(), "s1")
    assert reason is None and second["welcome_event_id"] == "E"
    # Suppressed arrivals cannot reappear as a backlog after the interval.
    assert r.admit(event("B"), product(), "s1") == (None, "duplicate_event")


def test_delayed_playback_does_not_consume_the_next_valid_window():
    r, clock = runtime()
    first, _ = r.admit(event("A"), product(), "s1")
    clock.value = 20
    assert r.play_reason(first, product(), "s1") is None and r.mark_started(first)
    clock.value = 60
    assert r.admit(event("B"), product(), "s1") == (None, "cooldown")
    assert r.last_admitted == 0
    clock.value = 79.999
    assert r.admit(event("C"), product(), "s1") == (None, "cooldown")
    clock.value = 80
    next_item, reason = r.admit(event("D"), product(), "s1")
    assert reason is None and r.play_reason(next_item, product(), "s1") is None


@pytest.mark.parametrize("username,expected", [
    ("张三012３𝟟＋×÷✨", "张三"), ("Alice_123+-", "Alice"),
    ("José العربية हिन्दी 한국어 Русский", "José العربية हिन्दी 한국어 Русский"),
    ("A1\u0301B", "AB"), ("A÷\u0301B", "AB"),
    ("A\u0338B", "AB"), ("A\ufe0f\u20e3B", "AB"),
    ("123＋÷✨\u0301", ""), ("李\u034f三", "李三"),
])
def test_only_readable_letters_and_contiguous_language_marks(username, expected):
    assert readable_username(username) == expected


def test_empty_nickname_fallback_still_uses_cooldown_and_current_product_rotation():
    r, clock = runtime()
    a, _ = r.admit(event("A", "123✨"), product(), "s1")
    assert a["text"] == FALLBACK
    clock.value = 59.999
    assert r.admit(event("B"), product("p2", "口红"), "s1")[1] == "cooldown"
    clock.value = 60
    b, _ = r.admit(event("C"), product("p2", "口红"), "s1")
    assert "口红" in b["text"] and "网球训练器" not in b["text"]
    clock.value = 120
    c, _ = r.admit(event("D"), product("p2", "口红"), "s1")
    assert b["text"] != c["text"]


@pytest.mark.parametrize("mode,expected", [("expired", "expired"), ("product", "product_changed"),
    ("session", "session_changed"), ("replay", "already_started")])
def test_play_time_revalidation_remains_strict(mode, expected):
    r, clock = runtime()
    a, _ = r.admit(event("A"), product(), "s1")
    current, session = product(), "s1"
    if mode == "expired": clock.value = 30
    if mode == "product": current = product("p2", "口红")
    if mode == "session": session = "s2"
    if mode == "replay": assert r.mark_started(a)
    assert r.play_reason(a, current, session) == expected


def test_upstream_reconnect_and_session_replay_remain_deduplicated():
    r, clock = runtime()
    source = {"source": "douyin", "external_room_id": "123", "raw_method": "WebcastMemberMessage",
              "upstream_event_id": "1001"}
    assert r.admit(event("local-a", **source), product(), "s1")[0]
    clock.value = 60
    assert r.admit(event("local-b", **source), product(), "s1") == (None, "duplicate_event")
    assert r.admit(event("local-c", **source), product(), "s2") == (None, "duplicate_event")
    assert WELCOME_POLICY_REVISION.endswith("admission-and-playback-cooldown")
