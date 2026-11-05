"""Local first-time DeepSeek key setup, verified before an atomic .env save."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from shengbo_prelive.providers.deepseek_key import is_configured_api_key, local_api_key_status

from .prelive.storage import BusinessError

_VERIFY_URL = "https://api.deepseek.com/user/balance"
_BODY_LIMIT = 1024


async def _verify_key(key: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0),
                                     follow_redirects=False, trust_env=False) as client:
            response = await client.get(_VERIFY_URL, headers={"Authorization": f"Bearer {key}"})
            if response.status_code != 200:
                return False
            result = response.json()
            return isinstance(result, dict) and isinstance(result.get("is_available"), bool)
    except (httpx.HTTPError, ValueError):
        return False


def _response(status: str, **fields):
    return JSONResponse({"status": status, **fields}, headers={"Cache-Control": "no-store"})


def _error(code: str, message: str, status: int):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _save_verified_key(path: Path, key: str):
    try:
        previous = path.read_bytes()
    except FileNotFoundError:
        previous = b""
    bom = b"\xef\xbb\xbf" if previous.startswith(b"\xef\xbb\xbf") else b""
    text = previous.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines, found = [], False
    for line in text.splitlines(keepends=True):
        if line.partition("=")[0].strip() == "DEEPSEEK_API_KEY":
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines.append("DEEPSEEK_API_KEY=" + key + ending)
            found = True
        else:
            lines.append(line)
    if not found:
        if lines and not lines[-1].endswith(("\r", "\n")):
            lines[-1] += newline
        lines.append("DEEPSEEK_API_KEY=" + key + newline)
    contents = bom + "".join(lines).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".deepseek-key-", suffix=".tmp", delete=False) as stream:
            pending = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    os.environ["DEEPSEEK_API_KEY"] = key


def install_model_key_setup(app, server_module, browser_identity):
    path = Path(os.environ.get("SHENGBO_DEEPSEEK_ENV_FILE") or server_module.ROOT / ".env")
    lock = asyncio.Lock()

    @app.get("/api/assistant/v2/model-key")
    async def read_model_key(request: Request):
        try:
            browser_identity(request)
            return _response(local_api_key_status(path))
        except BusinessError as error:
            return _error(error.code, error.message, error.status)
        except (OSError, UnicodeError):
            return _error("MODEL_KEY_STATUS_UNAVAILABLE", "暂时无法读取本机密钥设置。", 503)

    @app.post("/api/assistant/v2/model-key/connect")
    async def connect_model_key(request: Request):
        try:
            browser_identity(request, write=True)
        except BusinessError as error:
            return _error(error.code, error.message, error.status)
        payload = bytearray()
        try:
            async for chunk in request.stream():
                if len(payload) + len(chunk) > _BODY_LIMIT:
                    return _error("MODEL_KEY_INPUT_INVALID", "请输入有效的 API 密钥。", 422)
                payload.extend(chunk)
            body = json.loads(payload)
            key = body.get("api_key") if isinstance(body, dict) else None
        except (ValueError, UnicodeError):
            key = None
        if not isinstance(key, str) or len(key.strip()) < 12 or not is_configured_api_key(key.strip()):
            return _error("MODEL_KEY_INPUT_INVALID", "请输入有效的 API 密钥。", 422)
        key = key.strip()
        async with lock:
            try:
                if local_api_key_status(path) == "configured":
                    return _response("configured", already_configured=True)
                if not await _verify_key(key):
                    return _error("MODEL_KEY_CONNECTION_FAILED", "密钥连接失败", 422)
                await asyncio.to_thread(_save_verified_key, path, key)
            except (OSError, UnicodeError):
                return _error("MODEL_KEY_SAVE_FAILED", "无法保存到本机，请检查目录权限后重试。", 503)
        return _response("configured")
