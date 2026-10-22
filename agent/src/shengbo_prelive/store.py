"""Local runtime authority. Business documents remain in the voice database."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from shengbo_contracts.common import ContractError, canonical_json, new_id, now_iso

TERMINAL = {"COMPLETED", "READY_FOR_REVIEW", "BLOCKED", "FAILED", "CANCELLED", "SUPERSEDED"}
MODEL_CALL_LIMIT = 120


def has_durable_decision_resolution(question):
    """Only persisted human answers or the known stale-target paths can resume."""
    if not isinstance(question, dict):
        return False
    if question.get("status") == "ANSWERED":
        return bool(question.get("answer_event_ref") and question.get("answer_request_id"))
    return question.get("status") == "SUPERSEDED" and question.get("kind") in {
        "text_audio_approval", "selling_points_generation", "selling_points_confirmation"}


class RuntimeStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        with self.tx() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS objects(kind TEXT,id TEXT,body TEXT NOT NULL,revision INTEGER NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS slots(scope_key TEXT PRIMARY KEY,run_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS dedup(owner TEXT,request_id TEXT,result TEXT NOT NULL,request_hash TEXT NOT NULL,
                PRIMARY KEY(owner,request_id));
            CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,body TEXT NOT NULL);
            """)

    @contextmanager
    def tx(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=15000")
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def get(self, kind, identity, *, required=True):
        with self.tx() as db:
            row = db.execute("SELECT body FROM objects WHERE kind=? AND id=?", (kind, identity)).fetchone()
        if not row:
            if not required:
                return None
            raise ContractError("NOT_FOUND", "记录不存在", status=404)
        return json.loads(row[0])

    def list(self, kind):
        with self.tx() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT body FROM objects WHERE kind=? ORDER BY rowid", (kind,))]

    @staticmethod
    def _put(db, kind, body):
        identity = body.get("id") or body.get(f"{kind}_id")
        if not identity:
            raise ValueError("runtime object must have id")
        old = db.execute("SELECT revision FROM objects WHERE kind=? AND id=?", (kind, identity)).fetchone()
        revision = old[0] + 1 if old else 1
        value = {**body, "id": identity, "revision": revision}
        db.execute("INSERT INTO objects VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,revision=excluded.revision",
                   (kind, identity, canonical_json(value), revision))
        return value

    def put(self, kind, body, *, expected_revision=None):
        with self.tx() as db:
            identity = body.get("id") or body.get(f"{kind}_id")
            if expected_revision is not None:
                row = db.execute("SELECT revision FROM objects WHERE kind=? AND id=?", (kind, identity)).fetchone()
                if not row or row[0] != expected_revision:
                    raise ContractError("RUNTIME_STALE", "运行记录已变化，请重新读取")
            return self._put(db, kind, body)

    def update(self, kind, identity, **changes):
        with self.tx() as db:
            row = db.execute("SELECT body FROM objects WHERE kind=? AND id=?", (kind, identity)).fetchone()
            if not row:
                raise ContractError("NOT_FOUND", "记录不存在", status=404)
            return self._put(db, kind, {**json.loads(row[0]), **changes})

    def commit_question_resolution(self, question, run_changes, *, expected_question_revision, expected_run_revision):
        """Commit a verified answer or stale-target result with its resume inbox."""
        with self.tx() as db:
            question_row = db.execute("SELECT body,revision FROM objects WHERE kind='question' AND id=?",
                                      (question["id"],)).fetchone()
            run_row = db.execute("SELECT body,revision FROM objects WHERE kind='run' AND id=?",
                                 (question["run_id"],)).fetchone()
            if (not question_row or question_row["revision"] != expected_question_revision
                    or json.loads(question_row["body"]).get("status") != "OPEN"):
                raise ContractError("RUNTIME_STALE", "运行记录已变化，请重新读取")
            run = json.loads(run_row["body"]) if run_row else None
            if not run or run["status"] in TERMINAL | {"FINALIZING", "CANCELLING"} or run.get("dispatch_frozen"):
                raise ContractError("DECISION_STALE", "任务已关闭，答复不能恢复已撤销的执行权限")
            if run_row["revision"] != expected_run_revision:
                raise ContractError("RUNTIME_STALE", "运行记录已变化，请重新读取")
            saved = self._put(db, "question", question)
            self._put(db, "run", {**run, **run_changes})
            return saved

    def restore_decision_inbox(self, question, run_changes, *, expected_run_revision):
        """Repair only an old resolved/waiting crash gap, never a newer wait."""
        with self.tx() as db:
            row = db.execute("SELECT body,revision FROM objects WHERE kind='run' AND id=?",
                             (question["run_id"],)).fetchone()
            if not row or row["revision"] != expected_run_revision:
                return False
            run = json.loads(row["body"])
            if run.get("status") != "WAITING_USER" or run.get("dispatch_frozen"):
                return False
            questions = [json.loads(value[0]) for value in db.execute(
                "SELECT body FROM objects WHERE kind='question' ORDER BY rowid")]
            questions = [value for value in questions if value.get("run_id") == run["id"]]
            latest = questions[-1] if questions else None
            if (not has_durable_decision_resolution(latest) or latest != question
                    or any(item.get("status") == "OPEN" for item in questions)):
                return False
            self._put(db, "run", {**run, **run_changes})
            return True

    def accept_message(self, conversation_id, request_id, request_hash, message, run):
        with self.tx() as db:
            row = db.execute("SELECT result,request_hash FROM dedup WHERE owner=? AND request_id=?",
                             (conversation_id, request_id)).fetchone()
            if row:
                if row[1] != request_hash:
                    raise ContractError("IDEMPOTENCY_CONFLICT", "同一请求编号的内容不同")
                return json.loads(row[0])
            saved_message = self._put(db, "message", message)
            saved_run = self._put(db, "run", run)
            result = {"message": saved_message, "run": saved_run}
            db.execute("INSERT INTO dedup VALUES(?,?,?,?)", (conversation_id, request_id, canonical_json(result), request_hash))
            return result

    def acquire_scope(self, run):
        with self.tx() as db:
            row = db.execute("SELECT run_id FROM slots WHERE scope_key=?", (run["scope_key"],)).fetchone()
            if row and row[0] != run["id"]:
                return False
            db.execute("INSERT OR IGNORE INTO slots VALUES(?,?)", (run["scope_key"], run["id"]))
            return True

    def close_run(self, run_id, outcome, closure):
        with self.tx() as db:
            row = db.execute("SELECT body FROM objects WHERE kind='run' AND id=?", (run_id,)).fetchone()
            run = json.loads(row[0])
            if run["status"] in TERMINAL:
                return run
            receipt = self._put(db, "closure", {"id": new_id("closure"), **closure,
                                                "run_id": run_id, "actual_outcome": outcome, "closed_at": now_iso()})
            run = self._put(db, "run", {**run, "status": outcome, "closure_ref": receipt["id"],
                                       "closed_at": now_iso(), "dispatch_frozen": True})
            db.execute("DELETE FROM slots WHERE run_id=?", (run_id,))
            return run

    def reserve_attempt(self, run_id, invocation_id, role, limit=MODEL_CALL_LIMIT):
        with self.tx() as db:
            row = db.execute("SELECT body FROM objects WHERE kind='run' AND id=?", (run_id,)).fetchone()
            run = json.loads(row[0]) if row else None
            if not run or run["status"] in TERMINAL | {"FINALIZING", "CANCELLING"}:
                raise ContractError("RUN_FENCED", "任务已关闭或正在收尾")
            count = db.execute("SELECT COUNT(*) FROM attempts WHERE run_id=?", (run_id,)).fetchone()[0]
            if count >= limit:
                raise ContractError("BUDGET_EXHAUSTED", "本次任务模型调用预算已用尽")
            ticket = {"id": new_id("attempt"), "run_id": run_id, "invocation_id": invocation_id,
                      "role": role, "state": "reserved", "reserved_at": now_iso(), "usage": None}
            db.execute("INSERT INTO attempts VALUES(?,?,?)", (ticket["id"], run_id, canonical_json(ticket)))
            return ticket

    def settle_attempt(self, attempt_id, **changes):
        with self.tx() as db:
            row = db.execute("SELECT body FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row:
                value = {**json.loads(row[0]), **changes}
                db.execute("UPDATE attempts SET body=? WHERE id=?", (canonical_json(value), attempt_id))

    def recover(self):
        """Called only after exclusive OS lock proves the old service exited."""
        with self.tx() as db:
            for row in db.execute("SELECT id,body FROM attempts").fetchall():
                value = json.loads(row[1])
                if value["state"] in {"reserved", "sent"}:
                    returned = (value.get("provider_response_received") and value.get("response_status") == "completed"
                                and value.get("response_payload_ref"))
                    value.update(state=("known_failed" if returned else "outcome_unknown") if value["state"] == "sent" else "aborted_before_send",
                                 **({"content_status": "unvalidated_after_restart", "recovery_result_available": True} if returned else {}),
                                 recovery_at=now_iso())
                    db.execute("UPDATE attempts SET body=? WHERE id=?", (canonical_json(value), row[0]))
            for row in db.execute("SELECT id,body FROM objects WHERE kind IN ('run','branch')").fetchall():
                value = json.loads(row[1])
                if value.get("status") == "RUNNING":
                    kind = "branch" if value.get("branch_task_id") else "run"
                    self._put(db, kind, {**value, "status": "RECOVERING"})

