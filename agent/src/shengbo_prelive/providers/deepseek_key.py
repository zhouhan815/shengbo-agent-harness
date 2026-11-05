"""Lightweight DeepSeek key resolution shared by Voice and the Agent."""
from __future__ import annotations

import os
from pathlib import Path
import re

from .credentials import CredentialError


def _voice_env_path() -> Path:
    override = os.environ.get("SHENGBO_DEEPSEEK_ENV_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[4] / "ecommerce-voice-agent" / ".env"


def _api_key_from_file(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "DEEPSEEK_API_KEY":
                return value.strip().strip('"').strip("'")
    except FileNotFoundError:
        return ""
    return ""


def is_configured_api_key(value: str) -> bool:
    """Recognize local samples and dummy values before making any request."""
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,512}", value):
        return False
    suffix = value.lower().removeprefix("sk-")
    compact = re.sub(r"[^a-z0-9]", "", suffix)
    if compact in {"", "none", "null", "undefined", "unset", "notset", "notconfigured", "apikey", "deepseekapikey", "todo", "tbd"} or re.fullmatch(r"(.{1,4})\1{2,}", compact):
        return False
    if re.search(r"(?:^|[._-])(?:example|sample|placeholder|dummy|test)(?:$|[._-])", suffix):
        return False
    return not re.match(
        r"^(?:example|sample|placeholder|your(?:deepseek|api|key)|changeme|replace|dummy|fake|"
        r"test(?:api|key|token|only|[0-9])|insert|paste|1234567890|0123456789|abcdefgh)",
        compact,
    )


def local_api_key_status(path: Path | None = None) -> str:
    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if is_configured_api_key(value):
        return "configured"
    local = _api_key_from_file(path or _voice_env_path())
    if is_configured_api_key(local):
        return "configured"
    return "placeholder" if value or local else "missing"


def read_api_key() -> str:
    """Read the effective local key at send time. Ignore configured samples."""
    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if is_configured_api_key(value):
        return value
    try:
        value = _api_key_from_file(_voice_env_path())
    except OSError:
        raise CredentialError("deepseek_api_key_unavailable") from None
    if not is_configured_api_key(value):
        raise CredentialError("deepseek_api_key_missing")
    return value


