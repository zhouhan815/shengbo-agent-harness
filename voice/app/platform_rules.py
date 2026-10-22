"""Versioned, locally reviewed platform templates shared by schema and UI.

This registry never fetches the network, mutates a product, or overrides a
merchant promise. Rules are consumer templates with explicit applicability;
source metadata stays separate from the product's consumer facts.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
from urllib.parse import urlsplit


RULES_PATH = Path(__file__).with_name("data") / "platform_rules" / "douyin-20261020.json"


@lru_cache(maxsize=1)
def _reviewed_catalog():
    value = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    if value.get("platform") != "douyin" or not value.get("version") or not value.get("checked_at"):
        raise ValueError("Invalid reviewed platform policy identity")
    if set(value.get("defaults", {})) != {"shipping_time", "return_policy"}:
        raise ValueError("Invalid platform default field contract")
    if not all(isinstance(text, str) and text.strip() for text in value["defaults"].values()):
        raise ValueError("Platform defaults must be nonempty consumer text")
    sources = value.get("sources", [])
    if not sources:
        raise ValueError("Reviewed platform rules require official sources")
    for source in sources:
        url = urlsplit(source.get("url", ""))
        if url.scheme != "https" or not (url.hostname or "").endswith(".jinritemai.com"):
            raise ValueError("Platform sources must be official HTTPS documents")
    return {"douyin": value}


def platform_policy_catalog():
    """Fresh copy of the one reviewed registry; other platforms are absent."""
    return deepcopy(_reviewed_catalog())


def platform_policy(platform: str):
    return platform_policy_catalog().get(platform)
