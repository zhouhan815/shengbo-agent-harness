"""Local, durable Agent event and safe-payload store.

This module records observable actions and explicit decision summaries only. It
does not collect model private reasoning. Browser rendering must treat strings
as text; this store never interprets HTML, Markdown, or script.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any
from uuid import uuid4

from shengbo_contracts.common import EventEnvelope, SCHEMA_VERSION


class ObservabilityUnavailable(RuntimeError):
    pass


_SECRET_KEYS = {"authorization", "proxyauthorization", "cookie", "setcookie", "accesstoken",
                "refreshtoken", "idtoken", "apikey", "clientsecret", "password", "passwd",
                "credential", "credentials", "bearertoken", "privatekey", "secretkey"}
_SECRET_KEYS.update({"servicetoken", "csrftoken", "csrf", "secret", "sessioncookie", "xservicetoken",
                     "xshengboservicetoken", "xagenttoken", "xapikey", "agenttovoice", "controllertovoice", "voicetoagent",
                     "extagenthostid"})
_REASONING_KEYS = {"reasoningcontent", "chainofthought", "thinking", "encryptedcontent"}
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"'<>;,]+")
_SK = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}")
_SERVICE = re.compile(r"\bsvc_[A-Za-z0-9_-]{32,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_QUERY_SECRET = re.compile(r"(?i)([?&](?:access_token|refresh_token|id_token|api_key|client_secret|code)=)[^&#\s]+")
_EMBEDDED_SECRET = re.compile(r'''(?i)(["']?(?:authorization|cookie|set-cookie|access_token|refresh_token|id_token|api[_-]?key|client_secret)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,}\n]+)''')


def _key(value: str):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def sanitize(value: Any, _seen: set[int] | None = None, _depth: int = 0) -> Any:
    """Deterministic safety projection, retaining normal business `token_*` fields."""
    if _depth > 80:
        return "[depth_limit]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        value = _BEARER.sub("Bearer [REDACTED]", value)
        value = _SK.sub("[REDACTED_API_KEY]", value)
        value = _SERVICE.sub("[REDACTED_SERVICE_CREDENTIAL]", value)
        value = _JWT.sub("[REDACTED_JWT]", value)
        value = _QUERY_SECRET.sub(r"\1[REDACTED]", value)
        return _EMBEDDED_SECRET.sub(r"\1[REDACTED]", value)
    if isinstance(value, BaseException):
        return {"error_type": type(value).__name__, "details": "[not_collected]"}
    if isinstance(value, datetime):
        return value.isoformat()
    seen = _seen if _seen is not None else set()
    if id(value) in seen:
        return "[cycle]"
    seen.add(id(value))
    try:
        if isinstance(value, dict):
            if value.get("type") in {"reasoning", "reasoning_summary", "reasoning_text", "thinking", "redacted_thinking"}:
                return {"type": "private_reasoning", "collected": False}
            result = {}
            for raw_key, item in value.items():
                name = str(raw_key)
                normalized = _key(name)
                if normalized in _SECRET_KEYS or normalized.endswith(("accesstoken", "refreshtoken", "idtoken", "servicetoken", "apikey", "clientsecret")):
                    result[name] = "[REDACTED]"
                elif normalized in _REASONING_KEYS:
                    result[name] = "[not_collected]"
                else:
                    result[name] = sanitize(item, seen, _depth + 1)
            return result
        if isinstance(value, (list, tuple, set)):
            return [sanitize(v, seen, _depth + 1) for v in value]
        if hasattr(value, "model_dump"):
            return sanitize(value.model_dump(mode="json"), seen, _depth + 1)
        return {"unsupported_type": type(value).__name__}
    finally:
        seen.discard(id(value))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Observability:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.payload_root = self.root / "payloads"
        self.database = self.root / "observability.sqlite"
        self.spool_path = self.root / "events-spool.jsonl"
        self._mutex = threading.RLock()
        self._healthy = True
        self._spool_count = 0
        self._last_error = None
        try:
            self.payload_root.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS event_counters(run_id TEXT PRIMARY KEY, last_sequence INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS events(
                      run_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
                      envelope_json TEXT NOT NULL, PRIMARY KEY(run_id, sequence));
                    CREATE TABLE IF NOT EXISTS payloads(
                      payload_ref TEXT PRIMARY KEY, run_id TEXT NOT NULL, filename TEXT NOT NULL,
                      sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS ix_payload_run ON payloads(run_id);
                """)
                # Fail startup before any new model/tool side effect if unwritable.
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("INSERT OR IGNORE INTO event_counters VALUES('__startup_probe__',0)")
                connection.rollback()
            probe = self.payload_root / ("probe-" + str(uuid4()))
            probe.write_bytes(b"probe")
            probe.unlink()
            if self.spool_path.exists() and self.spool_path.stat().st_size:
                self._spool_count = sum(1 for line in self.spool_path.read_text(encoding="utf-8").splitlines() if line.strip())
                self._healthy, self._last_error = False, "spool_recovery_required"
        except Exception:
            raise ObservabilityUnavailable("observability_startup_unavailable") from None

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=15, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=15000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _run(run_id):
        if not isinstance(run_id, str) or not run_id or len(run_id) > 256:
            raise ValueError("invalid_run_id")
        return run_id

    def emit(self, run_id, event_type, data, invocation_id=None, branch_task_id=None, parent_event_id=None):
        self._run(run_id)
        if not isinstance(event_type, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", event_type):
            raise ValueError("invalid_event_type")
        envelope = {"schema_version": SCHEMA_VERSION, "event_id": str(uuid4()), "run_id": run_id,
                    "type": event_type, "timestamp": datetime.now(timezone.utc).isoformat(),
                    "invocation_id": invocation_id, "branch_task_id": branch_task_id,
                    "parent_event_id": parent_event_id, "data": sanitize(data if isinstance(data, dict) else {"value": data})}
        # IDs are untrusted strings too; do not accidentally store tokens there.
        envelope = sanitize(envelope)
        try:
            with self._mutex, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("INSERT OR IGNORE INTO event_counters VALUES(?,0)", (run_id,))
                sequence = connection.execute("UPDATE event_counters SET last_sequence=last_sequence+1 WHERE run_id=? RETURNING last_sequence", (run_id,)).fetchone()[0]
                envelope["sequence"] = sequence
                envelope = EventEnvelope.model_validate(envelope).model_dump(mode="json")
                connection.execute("INSERT INTO events VALUES(?,?,?,?)", (run_id, sequence, envelope["event_id"], _json(envelope)))
                connection.commit()
            return envelope
        except Exception:
            self._healthy, self._last_error = False, "event_write_failed"
            raise ObservabilityUnavailable("observability_event_write_failed") from None

    def try_emit(self, run_id, event_type, data, invocation_id=None, branch_task_id=None, parent_event_id=None):
        """Use for completion/tail logging; never hide already committed success."""
        try:
            return self.emit(run_id, event_type, data, invocation_id, branch_task_id, parent_event_id)
        except Exception:
            envelope = sanitize({"schema_version": SCHEMA_VERSION, "event_id": str(uuid4()),
                                 "run_id": run_id, "type": event_type, "data": data,
                                 "invocation_id": invocation_id, "branch_task_id": branch_task_id,
                                 "parent_event_id": parent_event_id, "timestamp": datetime.now(timezone.utc).isoformat(),
                                 "spool_pending": True})
            with self._mutex:
                self._spool_count += 1
                self._healthy, self._last_error = False, "event_spooled"
                try:
                    with self.spool_path.open("a", encoding="utf-8") as handle:
                        handle.write(_json(envelope) + "\n")
                        handle.flush()
                except Exception:
                    self._last_error = "event_and_spool_write_failed"
            return None

    def assert_ready_for_side_effect(self):
        """A degraded tail log is visible, and new side effects fail closed."""
        if not self._healthy:
            raise ObservabilityUnavailable("observability_degraded_no_new_side_effect")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
        except Exception:
            self._healthy = False
            self._last_error = "side_effect_gate_unavailable"
            raise ObservabilityUnavailable("observability_side_effect_gate_unavailable") from None

    def health(self):
        return {"healthy": self._healthy, "spooled_events": self._spool_count, "last_error": self._last_error,
                "new_side_effects_allowed": self._healthy}

    def recover_spool(self):
        """Explicit, idempotent replay. Failed recovery stays degraded across restart."""
        with self._mutex:
            if not self.spool_path.exists() or not self.spool_path.stat().st_size:
                # Probe the actual sink before clearing a prior degraded state.
                try:
                    with self._connect() as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        connection.rollback()
                    self._healthy, self._last_error, self._spool_count = True, None, 0
                    return {"recovered": 0, "healthy": True}
                except Exception:
                    self._healthy, self._last_error = False, "spool_recovery_failed"
                    return {"recovered": 0, "healthy": False}
            count = 0
            try:
                rows = self.spool_path.read_text(encoding="utf-8").splitlines()
                for line in rows:
                    if not line.strip():
                        continue
                    value = sanitize(json.loads(line))
                    value.pop("spool_pending", None)
                    value["sequence"] = 1  # Validate shape, actual sequence is assigned in transaction.
                    value = EventEnvelope.model_validate(value).model_dump(mode="json")
                    self._run(value["run_id"])
                    with self._connect() as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        duplicate = connection.execute("SELECT 1 FROM events WHERE event_id=?", (value["event_id"],)).fetchone()
                        if not duplicate:
                            connection.execute("INSERT OR IGNORE INTO event_counters VALUES(?,0)", (value["run_id"],))
                            value["sequence"] = connection.execute("UPDATE event_counters SET last_sequence=last_sequence+1 WHERE run_id=? RETURNING last_sequence", (value["run_id"],)).fetchone()[0]
                            connection.execute("INSERT INTO events VALUES(?,?,?,?)", (value["run_id"], value["sequence"], value["event_id"], _json(value)))
                        connection.commit()
                    count += 1
                temporary = self.spool_path.with_suffix(".recovered.tmp")
                temporary.write_text("", encoding="utf-8")
                temporary.replace(self.spool_path)
                self._healthy, self._last_error, self._spool_count = True, None, 0
                return {"recovered": count, "healthy": True}
            except Exception:
                self._healthy, self._last_error = False, "spool_recovery_failed"
                return {"recovered": count, "healthy": False}

    def payload(self, run_id, value):
        self._run(run_id)
        safe = sanitize(value)
        content = _json(safe).encode("utf-8")
        ref = "payload:" + str(uuid4())
        name = ref[8:] + ".json"
        path = self.payload_root / name
        temporary = path.with_suffix(".tmp")
        try:
            with self._mutex:
                temporary.write_bytes(content)
                temporary.replace(path)
                with self._connect() as connection:
                    connection.execute("INSERT INTO payloads VALUES(?,?,?,?,?)", (ref, run_id, name, hashlib.sha256(content).hexdigest(), datetime.now(timezone.utc).isoformat()))
            return ref
        except Exception:
            self._healthy, self._last_error = False, "payload_write_failed"
            raise ObservabilityUnavailable("observability_payload_write_failed") from None

    def read_payload(self, run_id, ref):
        self._run(run_id)
        if not isinstance(ref, str) or not re.fullmatch(r"payload:[0-9a-f-]{36}", ref):
            raise ValueError("invalid_payload_ref")
        with self._connect() as connection:
            row = connection.execute("SELECT filename,sha256 FROM payloads WHERE run_id=? AND payload_ref=?", (run_id, ref)).fetchone()
        if row is None:
            raise KeyError("payload_not_found")
        path = (self.payload_root / row[0]).resolve()
        if path.parent != self.payload_root.resolve():
            raise ValueError("payload_path_outside_store")
        try:
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != row[1]:
                raise ValueError()
            return json.loads(content)
        except Exception:
            raise ObservabilityUnavailable("observability_payload_integrity_failed") from None

    def events(self, run_id, after=0, limit=200):
        self._run(run_id)
        if not isinstance(after, int) or after < 0 or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("invalid_event_cursor")
        with self._connect() as connection:
            rows = connection.execute("SELECT envelope_json FROM events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?", (run_id, after, limit)).fetchall()
        events = [json.loads(row[0]) for row in rows]
        return {"events": events, "next_cursor": events[-1]["sequence"] if events else after}
