"""Independent local service credentials; never reuse ChatGPT OAuth material."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

from .providers.credentials import session_lock


class ServiceSecurityError(RuntimeError):
    pass


class ServiceTokens(dict):
    def __repr__(self):
        return "ServiceTokens(agent_to_voice=<protected>,controller_to_voice=<protected>,voice_to_agent=<protected>)"


def token_bytes(value):
    if not isinstance(value, str) or len(value) < 32 or len(value) > 512:
        raise ServiceSecurityError("service_credential_invalid")
    return value.encode("utf-8")


def _dpapi(data, decrypt=False):
    if os.name != "nt":
        raise ServiceSecurityError("windows_dpapi_required")
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypto, kernel = ctypes.WinDLL("crypt32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    if decrypt:
        function = crypto.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        function = crypto.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = function(ctypes.byref(source), "Shengbo local services", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ServiceSecurityError("service_credential_dpapi_failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def service_tokens(root=None):
    """Read/create one encrypted trio under a cross-process lock, no .env use."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2] / ".runtime"
    path = root / "service.dpapi"
    keys = {"agent_to_voice", "controller_to_voice", "voice_to_agent"}
    try:
        with session_lock(root / "service-token.lock"):
            if path.exists():
                value = json.loads(_dpapi(path.read_bytes(), decrypt=True))
                if not isinstance(value, dict) or set(value) != keys:
                    raise ValueError()
                for secret in value.values():
                    token_bytes(secret)
            else:
                value = {key: "svc_" + secrets.token_urlsafe(48) for key in sorted(keys)}
                encrypted = _dpapi(json.dumps(value, sort_keys=True).encode())
                temporary = path.with_suffix("." + uuid4().hex + ".tmp")
                temporary.write_bytes(encrypted)
                temporary.replace(path)
            return ServiceTokens(value)
    except ServiceSecurityError:
        raise
    except Exception:
        raise ServiceSecurityError("service_credential_store_unavailable") from None
