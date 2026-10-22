"""Single-session OAuth renewal over the existing Windows DPAPI vault.

The connection probe must be stopped before this manager takes ownership. Its old
in-process lock does not participate in our cross-process locking protocol.
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4
from typing import Any, Callable

import httpx

TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class CredentialError(RuntimeError):
    """Only fixed, safe diagnostic codes; never attach a response/request."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@contextmanager
def session_lock(path: Path, timeout: float = 30):
    """OS byte-range lock plus a process-local thread lock, with bounded wait."""
    key = os.path.normcase(str(path.resolve()))
    with _LOCKS_GUARD:
        local = _LOCKS.setdefault(key, threading.RLock())
    if not local.acquire(timeout=timeout):
        raise CredentialError("credential_lock_timeout")
    handle = None
    locked = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # Enables isolated fake-vault tests on non-Windows CI.
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise CredentialError("credential_lock_timeout") from None
                time.sleep(0.05)
        yield
    finally:
        if handle is not None:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        local.release()


def _existing_vault(root: Path, connection_module: Path):
    # Do not create or replace the stable installation ID as a side effect.
    if os.name != "nt":
        raise CredentialError("windows_dpapi_required")
    if not (root / "host.json").is_file() or not (root / "connection.dpapi").is_file():
        raise CredentialError("chatgpt_login_required")
    try:
        spec = importlib.util.spec_from_file_location("_shengbo_authorized_connection", connection_module)
        if spec is None or spec.loader is None:
            raise ValueError()
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.Vault(root)
    except Exception:
        raise CredentialError("credential_vault_unavailable") from None


class CredentialManager:
    """No record cache: every token read/refresh is serialized and re-reads disk.

    `vault`, `transport` and `clock` are injectable solely for isolated tests.
    Nothing in this object is LangChain/Pydantic state.
    """

    def __init__(self, root: Path | str | None = None, *, vault: Any = None,
                 connection_module: Path | str = "D:/Codex project/shengbo-chatgpt-connection/connection.py",
                 transport: httpx.BaseTransport | None = None,
                 refresh_margin: float = 90, clock: Callable[[], float] = time.time,
                 lock_timeout: float = 30):
        local_app = os.environ.get("LOCALAPPDATA", "")
        self.root = Path(root) if root else Path(local_app) / "ShengboChatGPT"
        self._vault = vault
        self._connection_module = Path(connection_module)
        self._transport = transport
        self._clock = clock
        self._margin = refresh_margin
        self._lock_timeout = lock_timeout

    def __repr__(self):
        return "CredentialManager(credentials=<protected>)"

    def _load(self):
        try:
            if self._vault is None:
                self._vault = _existing_vault(self.root, self._connection_module)
            value = self._vault.load()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("credential_read_failed") from None

    @staticmethod
    def _validate(record):
        if record.get("terminal_auth_error"):
            raise CredentialError("chatgpt_reauthorization_required")
        if record.get("refresh_in_flight"):
            # A killed process or ambiguous network response may have rotated a
            # one-time refresh token. Do not automatically replay the old value.
            raise CredentialError("credential_refresh_reconciliation_required")
        if not record.get("subject") or not str(record.get("client_id", "")).startswith("oaiapp_"):
            raise CredentialError("chatgpt_login_required")
        if PLAN_SCOPE not in record.get("scopes", []):
            raise CredentialError("chatgpt_plan_permission_missing")

    def status(self) -> dict:
        """A safe status view without IDs, tokens, emails or vault paths."""
        try:
            with session_lock(self.root / "refresh.lock", self._lock_timeout):
                record = self._load()
                self._validate(record)
                return {"provider": "chatgpt_plan", "signed_in": True, "plan_enabled": True,
                        "renewal_due": float(record.get("expires_at", 0)) <= self._clock() + self._margin}
        except CredentialError as error:
            return {"provider": "chatgpt_plan", "signed_in": False, "code": error.code}

    def get_access_token(self, force_refresh: bool = False) -> str:
        with session_lock(self.root / "refresh.lock", self._lock_timeout):
            record = self._load()
            self._validate(record)
            if (not force_refresh and record.get("access_token") and
                    float(record.get("expires_at", 0)) > self._clock() + self._margin):
                return record["access_token"]
            if not record.get("refresh_token"):
                raise CredentialError("chatgpt_reauthorization_required")
            record["refresh_in_flight"] = {"attempt_id": str(uuid4()), "started_at": self._clock()}
            self._save(record)
            with httpx.Client(transport=self._transport, timeout=30, follow_redirects=False) as client:
                # Retry only definite non-success server responses. A connection
                # timeout is an unknown rotation outcome and must not be replayed.
                for attempt in range(2):
                    try:
                        response = client.post(TOKEN_ENDPOINT, data={
                            "grant_type": "refresh_token", "client_id": record["client_id"],
                            "refresh_token": record["refresh_token"], "resource": RESOURCE})
                    except httpx.RequestError:
                        raise CredentialError("credential_refresh_outcome_unknown") from None
                    if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                        time.sleep(0.15)
                        continue
                    break
                if response.status_code in (400, 401, 403):
                    # Persist a terminal marker, not a cleartext error payload.
                    try:
                        body = response.json()
                        terminal = body.get("error") in ("invalid_grant", "invalid_client", "access_denied")
                    except (ValueError, TypeError):
                        terminal = False
                    if terminal:
                        record["terminal_auth_error"] = True
                    record.pop("refresh_in_flight", None)
                    self._save(record)
                    raise CredentialError("chatgpt_reauthorization_required")
                if not response.is_success:
                    record.pop("refresh_in_flight", None)
                    self._save(record)
                    raise CredentialError("credential_refresh_temporarily_unavailable")
                try:
                    tokens = response.json()
                    expiry = float(tokens["expires_in"])
                    if (not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]
                            or expiry <= 0 or str(tokens.get("token_type", "Bearer")).lower() != "bearer"):
                        raise ValueError()
                    if "refresh_token" in tokens and not isinstance(tokens["refresh_token"], str):
                        raise ValueError()
                    if tokens.get("id_token"):
                        self._validate_refresh_identity(tokens["id_token"], record, client)
                    rotated = {**record, "access_token": tokens["access_token"],
                               "refresh_token": tokens.get("refresh_token") or record["refresh_token"],
                               "expires_at": self._clock() + expiry}
                    rotated.pop("refresh_in_flight", None)
                    if tokens.get("id_token"):
                        rotated["id_token"] = tokens["id_token"]
                    if "scope" in tokens:
                        rotated["scopes"] = tokens["scope"].split()
                except CredentialError:
                    raise
                except Exception:
                    raise CredentialError("credential_refresh_response_invalid") from None
                # The existing Vault performs DPAPI encryption + atomic replace.
                self._save(rotated)
                self._validate(rotated)
                return rotated["access_token"]

    def _save(self, record):
        try:
            self._vault.save(record)
        except Exception:
            raise CredentialError("credential_rotation_save_failed") from None

    @staticmethod
    def _validate_refresh_identity(token, record, client):
        import jwt
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ("RS256", "ES256"):
                raise ValueError()
            response = client.get("https://auth.openai.com/.well-known/jwks.json")
            response.raise_for_status()
            keys = [k for k in response.json()["keys"] if k.get("kid") == header.get("kid")]
            if len(keys) != 1:
                raise ValueError()
            key = jwt.PyJWK.from_dict(keys[0], algorithm=header["alg"]).key
            claims = jwt.decode(token, key, algorithms=[header["alg"]], audience=record["client_id"],
                                issuer="https://auth.openai.com", leeway=5,
                                options={"require": ["sub", "exp", "iat", "iss", "aud"]})
            if claims["sub"] != record["subject"]:
                raise ValueError()
        except Exception:
            raise CredentialError("credential_refresh_identity_invalid") from None
