"""Prepared welcome templates and one-room admission/playback policy.

No provider calls happen here. Callers prepare exactly five templates once and
carry them with the selected script into the playback queue. Room.lock owns all
runtime mutations, including admission and playback checks.
"""
from __future__ import annotations

from collections import OrderedDict
import random
import time
import unicodedata

WELCOME_PLACEHOLDER = "{username}"
COOLDOWN_SECONDS = 60.0
MAX_PENDING_SECONDS = 30.0
FALLBACK = "欢迎新来的朋友"
WELCOME_POLICY_REVISION = "entry-welcome-v2-admission-and-playback-cooldown"


def readable_username(value):
    """Keep Unicode letters and attached vowel/diacritic marks, never digits."""
    output = []
    attached_to_letter = False
    for char in str(value or "")[:200]:
        category = unicodedata.category(char)
        if category.startswith("L"):
            output.append(char)
            attached_to_letter = True
        elif (category in {"Mn", "Mc"} and attached_to_letter
                and not any(token in unicodedata.name(char, "")
                            for token in ("VARIATION SELECTOR", "GRAPHEME JOINER", "OVERLAY", "ARROW"))):
            output.append(char)
        elif char.isspace() and output and output[-1] != " ":
            output.append(" ")
            attached_to_letter = False
        else:
            # A removed number/symbol breaks attachment. Its following mark
            # must not migrate onto an earlier readable letter (A1◌́ -> Á).
            attached_to_letter = False
        if len(output) >= 40:
            break
    return "".join(output).strip()


def validate_templates(value):
    """Public preparation contract: five distinct, plain templates, one token."""
    if not isinstance(value, list) or len(value) != 5:
        raise ValueError("欢迎语须预先准备五条")
    result = []
    for template in value:
        if not isinstance(template, str):
            raise ValueError("欢迎语模板须为文本")
        template = template.strip()
        remainder = template.replace(WELCOME_PLACEHOLDER, "")
        if (not 1 <= len(template) <= 160 or template.count(WELCOME_PLACEHOLDER) != 1
                or any(char in remainder for char in "{}<>[]#*()")
                or any(unicodedata.category(char).startswith("C") for char in template)):
            raise ValueError("每条欢迎语须含一个 {username}，且不能含语音控制标记")
        result.append(template)
    if len(set(result)) != 5:
        raise ValueError("五条欢迎语不能重复")
    return result


def build_product_templates(product):
    """Prepare safe product-bound wording once; no claims or provider requests."""
    raw = str(product.get("name") or "") if isinstance(product, dict) else ""
    name = "".join(char for char in raw if (unicodedata.category(char)[0] in {"L", "M", "N"} or char.isspace() or char in "·—-"))
    name = " ".join(name.split())[:60] or "这款商品"
    return validate_templates([
        f"欢迎{{username}}来到直播间，我们正在介绍{name}。",
        f"{{username}}，欢迎来看看{name}，有想了解的可以留言。",
        f"欢迎{{username}}，现在讲的是{name}，一起听听介绍。",
        f"{{username}}你好，欢迎进来了解{name}，按自己的需要选择。",
        f"欢迎{{username}}加入，关于{name}想问什么可以留言。",
    ])


class WelcomeRuntime:
    def __init__(self, *, clock=None, choose=None):
        self.clock = clock or time.monotonic
        self.choose = choose or random.choice
        self.session_id = None
        self.last_admitted = None
        self.last_started = None
        self.last_template = {}
        self.started = set()
        self.seen = OrderedDict()

    def session(self, session_id):
        if self.session_id != session_id:
            self.session_id = session_id
            self.last_admitted = self.last_started = None
            self.last_template.clear()
            self.started.clear()
            # Keep event IDs across stop/start: an old enter is never a new one.

    def remember(self, event):
        upstream = event.get("upstream_event_id")
        key = (event.get("source"), event.get("external_room_id"), event.get("raw_method"), upstream) if upstream and str(upstream) != "0" else (event.get("source"), event.get("id"))
        if key in self.seen:
            return False
        self.seen[key] = None
        if len(self.seen) > 4096:
            self.seen.popitem(last=False)
        return True

    def admit(self, event, current, session_id):
        self.session(session_id)
        if not self.remember(event):
            return None, "duplicate_event"
        now = self.clock()
        anchors = [value for value in (self.last_admitted, self.last_started) if value is not None]
        if anchors and now - max(anchors) < COOLDOWN_SECONDS:
            return None, "cooldown"
        try:
            templates = validate_templates(current.get("welcome_templates"))
        except ValueError:
            return None, "templates_unavailable"
        product_id = current["product_id"]
        choices = [template for template in templates if template != self.last_template.get(product_id)]
        template = self.choose(choices)
        username = readable_username(event.get("username") or event.get("nickname"))
        text = template.replace(WELCOME_PLACEHOLDER, username) if username else FALLBACK
        self.last_template[product_id] = template
        self.last_admitted = now
        return {"welcome_event_id": event["id"], "welcome_session_id": session_id,
                "welcome_product_id": product_id, "welcome_admitted_at": now,
                "welcome_expires_at": time.time() + MAX_PENDING_SECONDS,
                "text": text, "spoken_text": text}, None

    def play_reason(self, item, current, session_id):
        self.session(session_id)
        if item.get("welcome_session_id") != session_id:
            return "session_changed"
        if not current or item.get("welcome_product_id") != current.get("product_id"):
            return "product_changed"
        now = self.clock()
        if now - item["welcome_admitted_at"] >= MAX_PENDING_SECONDS:
            return "expired"
        if item["welcome_event_id"] in self.started:
            return "already_started"
        if self.last_started is not None and now - self.last_started < COOLDOWN_SECONDS:
            return "playback_cooldown"
        return None

    def mark_started(self, item):
        token = item["welcome_event_id"]
        if token in self.started:
            return False
        self.started.add(token)
        self.last_started = self.clock()
        return True
