"""Short SQLite transactions over the voice application's existing records."""
from __future__ import annotations
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import uuid

ARTIFACT_KINDS = {"analysis", "draft", "context", "script_plan", "module", "script_revision", "playlist", "compiled_plan", "requirements", "approval", "audio_manifest", "publication", "recap", "asset_checklist", "blob", "revision_choice", "adoption", "product_assembly", "human_decision", "quarantined_candidate", "source_manifest", "script_audio_preview_approval", "audio_reissue_approval"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class BusinessError(Exception):
    def __init__(self, code, message=None, *, status=409, details=None):
        self.code, self.message, self.status, self.details = code, message or code, status, details or {}
        super().__init__(self.message)

    def payload(self):
        return {"code": self.code, "message": self.message, "details": self.details, "retryable": False}


class Store:
    def __init__(self, runtime):
        self.runtime = Path(runtime)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.path = self.runtime / "library.sqlite3"
        self.migrate()

    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=15000")
        return c

    def migrate(self):
        with closing(self.connect()) as c, c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT,id TEXT,body TEXT,PRIMARY KEY(kind,id))")
            columns = {r[1] for r in c.execute("PRAGMA table_info(records)")}
            if "revision" not in columns:
                c.execute("ALTER TABLE records ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            c.executescript("""
            CREATE TABLE IF NOT EXISTS record_revisions(kind TEXT,id TEXT,revision INTEGER,body TEXT,content_hash TEXT,created_at TEXT,PRIMARY KEY(kind,id,revision));
            CREATE TABLE IF NOT EXISTS prelive_operations(principal_id TEXT,operation_id TEXT,command TEXT,request_hash TEXT,response_json TEXT,created_at TEXT,PRIMARY KEY(principal_id,operation_id));
            CREATE TABLE IF NOT EXISTS prelive_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE,stream_id TEXT,event_type TEXT,payload TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS prelive_grants(grant_id TEXT PRIMARY KEY,run_id TEXT UNIQUE,write_generation INTEGER,status TEXT,scope_json TEXT,capabilities_json TEXT,constraints_json TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS prelive_revocations(run_id TEXT PRIMARY KEY,operation_id TEXT,reason TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS prelive_skus(sku_id TEXT PRIMARY KEY,product_id TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS prelive_sequences(name TEXT PRIMARY KEY,next_value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS prelive_changes(id TEXT PRIMARY KEY,kind TEXT,object_id TEXT,before_json TEXT,after_ref TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS prelive_research(product_id TEXT,artifact_id TEXT,context_hash TEXT,PRIMARY KEY(product_id,artifact_id));
            CREATE TABLE IF NOT EXISTS prelive_revision_choices(id TEXT PRIMARY KEY,preparation_id TEXT,pinned_hash TEXT,head_hash TEXT,diff_hash TEXT,choice TEXT,receipt_json TEXT);
            CREATE TABLE IF NOT EXISTS prelive_live_slot(slot INTEGER PRIMARY KEY CHECK(slot=1),room_id TEXT,publication_id TEXT,state TEXT,start_operation_id TEXT);
            """)
            for r in c.execute("SELECT kind,id,body,revision FROM records").fetchall():
                body = json.loads(r["body"])
                c.execute("INSERT OR IGNORE INTO record_revisions VALUES(?,?,?,?,?,?)",
                          (r["kind"], r["id"], r["revision"], canonical(body), digest(body), now()))
                if r["kind"] == "product":
                    sku = body.get("sku_id") or r["id"]
                    old = c.execute("SELECT product_id FROM prelive_skus WHERE sku_id=?", (sku,)).fetchone()
                    if old and old[0] != r["id"]:
                        raise BusinessError("SKU_MIGRATION_CONFLICT", "现有商品SKU冲突，未合并商品", details={"product_ids": [old[0], r["id"]]})
                    c.execute("INSERT OR IGNORE INTO prelive_skus VALUES(?,?)", (sku, r["id"]))

    @contextmanager
    def transaction(self, *, write=False):
        c = self.connect()
        try:
            c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def get(self, c, kind, identity, *, revision=None):
        if revision is None:
            r = c.execute("SELECT body,revision FROM records WHERE kind=? AND id=?", (kind, identity)).fetchone()
        else:
            r = c.execute("SELECT body,revision FROM record_revisions WHERE kind=? AND id=? AND revision=?", (kind, identity, revision)).fetchone()
        if not r:
            raise BusinessError("NOT_FOUND", "记录不存在", status=404, details={"kind": kind, "id": identity})
        body = json.loads(r["body"])
        ref = {"kind": kind, "id": identity, "revision": r["revision"], "content_hash": digest(body)}
        if kind in ARTIFACT_KINDS:
            ref["schema_version"] = "PRELIVE-2.3"
        return {"ref": ref, "body": body}

    def exact(self, c, ref):
        ref = ref.model_dump(exclude_none=True) if hasattr(ref, "model_dump") else ref
        result = self.get(c, ref["kind"], ref["id"], revision=ref["revision"])
        if "schema_version" in ref and ref["schema_version"] != result["ref"].get("schema_version"):
            raise BusinessError("REF_SCHEMA_MISMATCH")
        if result["ref"]["content_hash"] != ref["content_hash"]:
            raise BusinessError("REF_HASH_MISMATCH", "引用内容校验失败")
        return result

    def all(self, c, kind):
        return [self.get(c, kind, r[0]) for r in c.execute("SELECT id FROM records WHERE kind=? ORDER BY rowid", (kind,)).fetchall()]

    def put(self, c, kind, identity, body, *, expected=None, create=False):
        old = c.execute("SELECT body,revision FROM records WHERE kind=? AND id=?", (kind, identity)).fetchone()
        if create:
            if old:
                raise BusinessError("ALREADY_EXISTS")
            rev = 1
            c.execute("INSERT INTO records(kind,id,body,revision) VALUES(?,?,?,?)", (kind, identity, canonical(body), rev))
        else:
            if not old:
                raise BusinessError("NOT_FOUND", status=404)
            if expected is None or expected != old["revision"]:
                raise BusinessError("REVISION_CONFLICT", details={"current_revision": old["revision"]})
            if json.loads(old["body"]) == body:
                return self.get(c, kind, identity)["ref"]
            rev = expected + 1
            changed = c.execute("UPDATE records SET body=?,revision=? WHERE kind=? AND id=? AND revision=?", (canonical(body), rev, kind, identity, expected)).rowcount
            if changed != 1:
                raise BusinessError("REVISION_CONFLICT")
        ref = {"kind": kind, "id": identity, "revision": rev, "content_hash": digest(body)}
        if kind in ARTIFACT_KINDS:
            ref["schema_version"] = "PRELIVE-2.3"
        c.execute("INSERT INTO record_revisions VALUES(?,?,?,?,?,?)", (kind, identity, rev, canonical(body), ref["content_hash"], now()))
        if old:
            c.execute("INSERT INTO prelive_changes VALUES(?,?,?,?,?,?)", (uid("change"), kind, identity, old["body"], canonical(ref), now()))
        return ref

    def artifact(self, c, kind, body):
        identity = kind + "_" + digest(body).split(":")[-1]
        existing = c.execute("SELECT id FROM records WHERE kind=? AND id=?", (kind, identity)).fetchone()
        if existing:
            return self.get(c, kind, identity)["ref"]
        return self.put(c, kind, identity, body, create=True)

    def event(self, c, stream, event_type, payload):
        event_id = uid("event")
        cur = c.execute("INSERT INTO prelive_events(event_id,stream_id,event_type,payload,created_at) VALUES(?,?,?,?,?)", (event_id, stream, event_type, canonical(payload), now()))
        return cur.lastrowid
