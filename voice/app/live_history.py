"""Read playback sessions and the platform events attributable to each one."""
from __future__ import annotations

from collections import defaultdict
import json

from fastapi import HTTPException

from .live_source import _metrics, srv


SESSION_PAGE = 12
EVENT_PAGE = 30
SQLITE_MAX_INT = 9223372036854775807


def _room_exists(connection, room_id):
    if room_id != "default" and not connection.execute(
            "SELECT 1 FROM records WHERE kind='config' AND id=?", (room_id,)).fetchone():
        raise HTTPException(404, "直播间不存在")


def _session_starts(connection, room_id, *, session_id=None, offset=0, limit=SESSION_PAGE):
    # Prefer the durable start receipt. Older accepted player starts also carry
    # an exact session ID. Idle logs and unbound platform captures are no starts.
    return connection.execute("""WITH starts AS (
        SELECT id AS session_id, body, json_extract(body,'$.started_at') AS started_at, 0 AS priority
        FROM records WHERE kind='prelive_session' AND json_extract(body,'$.room_id')=?
        UNION ALL
        SELECT json_extract(body,'$.session_id'), body, json_extract(body,'$.time'), 1
        FROM records WHERE kind='room_log' AND json_extract(body,'$.room_id')=?
            AND (json_extract(body,'$.event')='prelive_publication_started'
                OR (json_extract(body,'$.event')='control_accepted' AND json_extract(body,'$.detail')='start'))
    ), ranked AS (
        SELECT *, ROW_NUMBER() OVER(PARTITION BY session_id ORDER BY priority,julianday(started_at)) AS rank
        FROM starts WHERE session_id IS NOT NULL AND julianday(started_at) IS NOT NULL
        AND (? IS NULL OR session_id=?)
    )
    SELECT * FROM ranked WHERE rank=1 ORDER BY julianday(started_at) DESC, session_id DESC LIMIT ? OFFSET ?
    """, (room_id, room_id, session_id, session_id, limit, offset)).fetchall()


def _ends_by_session(connection, room_id, rows):
    pending = [(row["session_id"], row["started_at"]) for row in rows if row["priority"] != 0
               or not json.loads(row["body"]).get("stopped_at")]
    if not pending:
        return {}
    placeholders = ",".join("(?,?)" for _ in pending)
    bindings = [part for pair in pending for part in pair]
    ends = connection.execute(f"""WITH sessions(session_id,started_at) AS (VALUES {placeholders})
        SELECT sessions.session_id, json_extract(body,'$.time') AS ended_at FROM records
        JOIN sessions ON sessions.session_id=json_extract(body,'$.session_id')
        WHERE kind='room_log' AND json_extract(body,'$.room_id')=?
        AND julianday(json_extract(body,'$.time'))>=julianday(datetime(sessions.started_at))
        AND (json_extract(body,'$.event')='playback_completed'
            OR (json_extract(body,'$.event')='control_accepted' AND json_extract(body,'$.detail')='stop'))
        ORDER BY julianday(ended_at)""", (*bindings, room_id))
    first_end = {}
    for row in ends:
        first_end.setdefault(row["session_id"], row["ended_at"])
    return first_end


def _session(room_id, row, ends):
    body = json.loads(row["body"])
    session_id = row["session_id"]
    stopped_at = body.get("stopped_at") if row["priority"] == 0 else None
    stopped_at = stopped_at or ends.get(session_id)
    instance = srv().rooms.get(room_id)
    active = bool(instance and instance.state.get("session_id") == session_id
                  and instance.state["status"] not in {"stopped", "completed"} and not stopped_at)
    state = "interrupted" if body.get("state") == "interrupted" else (
        "ended" if stopped_at else "active" if active else "end_unknown")
    return {"id": session_id, "room_id": room_id, "started_at": row["started_at"],
            "ended_at": stopped_at, "state": state}


def list_sessions(room_id, cursor=0):
    if not 0 <= cursor <= SQLITE_MAX_INT:
        raise HTTPException(400, "场次游标无效")
    with srv().db() as connection:
        _room_exists(connection, room_id)
        rows = _session_starts(connection, room_id, offset=cursor, limit=SESSION_PAGE + 1)
        ends = _ends_by_session(connection, room_id, rows[:SESSION_PAGE])
        return {"items": [_session(room_id, row, ends) for row in rows[:SESSION_PAGE]],
                "next_cursor": cursor + SESSION_PAGE if len(rows) > SESSION_PAGE else None}


def read_session(room_id, session_id, before=None):
    if before is not None and not 0 < before <= SQLITE_MAX_INT:
        raise HTTPException(400, "事件游标无效")
    with srv().db() as connection:
        _room_exists(connection, room_id)
        rows = _session_starts(connection, room_id, session_id=session_id, limit=1)
        if not rows:
            raise HTTPException(404, "找不到这场直播")
        session = _session(room_id, rows[0], _ends_by_session(connection, room_id, rows))
        result = {"session": session, "sources": [], "events": [], "next_before": None}
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_source_events'").fetchone():
            return result
        # New events are associated only while playback is active. For records
        # produced by older collectors, a known playback window is necessary
        # because the old in-memory session ID survived after stop. Legacy
        # second-resolution timestamps cannot disambiguate the stop second.
        event_time = "json_extract(body,'$.time')"
        precise = f"instr({event_time},'.')>0"
        condition = f"""room_id=? AND json_extract(body,'$.session_id')=?
            AND (json_extract(body,'$.during_playback')=1 OR
                (json_extract(body,'$.during_playback') IS NULL AND ? AND
                 julianday({event_time})>=CASE WHEN {precise} THEN julianday(?) ELSE julianday(datetime(?)) END AND
                 (? IS NULL OR julianday({event_time})<CASE WHEN {precise} THEN julianday(?) ELSE julianday(datetime(?)) END)))"""
        params = (room_id, session_id, bool(session["ended_at"] or session["state"] == "active"),
                  session["started_at"], session["started_at"],
                  session["ended_at"], session["ended_at"], session["ended_at"])
        groups = defaultdict(dict)
        for row in connection.execute(f"""SELECT external_room_id, json_extract(body,'$.purpose') AS purpose,
            event_type, COUNT(*) AS count FROM live_source_events WHERE {condition}
            GROUP BY external_room_id,purpose,event_type""", params):
            groups[row["external_room_id"], row["purpose"]][row["event_type"]] = row["count"]
        snapshots = defaultdict(list)
        for row in connection.execute(f"""WITH ranked AS (
            SELECT body,external_room_id,json_extract(body,'$.purpose') AS purpose,ROW_NUMBER() OVER(
              PARTITION BY external_room_id,json_extract(body,'$.purpose'),event_type ORDER BY seq DESC) AS rank
            FROM live_source_events WHERE {condition} AND (event_type IN ('audience_snapshot','stats_display')
                OR (event_type='like' AND json_extract(body,'$.total') IS NOT NULL)))
            SELECT body,external_room_id,purpose FROM ranked WHERE rank=1""", params):
            snapshots[row["external_room_id"], row["purpose"]].append(row)
        result["sources"] = [{"platform": "douyin", "external_room_id": key[0], "purpose": key[1],
                              "counts": counts, "metrics": _metrics(snapshots[key], None)}
                             for key, counts in groups.items()]
        events = connection.execute(f"""SELECT seq,body FROM live_source_events WHERE {condition}
            AND (? IS NULL OR seq<?) ORDER BY seq DESC LIMIT ?""", (*params, before, before, EVENT_PAGE + 1)).fetchall()
        for row in events[:EVENT_PAGE]:
            event = json.loads(row["body"])
            result["events"].append({key: event.get(key) for key in (
                "id", "type", "time", "external_room_id", "purpose", "nickname", "text", "count")})
        if len(events) > EVENT_PAGE:
            result["next_before"] = events[EVENT_PAGE - 1]["seq"]
        return result
