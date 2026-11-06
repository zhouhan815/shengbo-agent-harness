"""Douyin source adapter; external testing remains read-only.

The collector socket is private to this process. Desired enabled state persists,
but a server restart always requires an explicit reconnect from the user.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
from urllib.parse import urlparse
import urllib.request

from fastapi import HTTPException
from websockets.asyncio.client import connect

SESSIONS = {}
CONTROL_LOCKS = {}
COLLECTOR = {"process": None, "key": None, "port": None}
SERVICE_LOCK = asyncio.Lock()
MAX_RETRIES = 5
RETRY_BASE = 1
METRIC_TYPES = {"audience_snapshot", "stats_display"}


def srv():
    from . import server
    return server


def parse_room(value):
    if not isinstance(value, str) or len(value) > 400:
        raise HTTPException(400, "请输入抖音网页直播间链接或数字直播间号")
    value = value.strip()
    if re.fullmatch(r"\d{5,20}", value):
        return value
    try:
        parsed = urlparse(value)
        if (parsed.scheme in {"https", "http"} and parsed.hostname == "live.douyin.com"
                and not parsed.username and not parsed.password and parsed.port is None
                and not parsed.fragment):
            identity = parsed.path.strip("/")
            if re.fullmatch(r"\d{5,20}", identity):
                return identity
    except ValueError:
        pass
    raise HTTPException(400, "只支持 live.douyin.com/数字 的公开直播间链接或数字房间号；不是抖音用户ID")


def executable():
    override = os.environ.get("SHENGBO_DOUYIN_COLLECTOR")
    return Path(override) if override else srv().ROOT / "vendor" / "douyin-live-collector" / "douyinLive-localhost.exe"


def _config(identity):
    srv().settings(identity)  # Missing Shengbo rooms must remain a 404.
    previous = next((x for x in srv().all_of("live_source_config") if x["id"] == identity), {})
    return {"id": identity, "platform": "douyin", "external_room_id": "", "url": "",
            "purpose": "external_test", "enabled": False, **previous}


def _validated(identity, data):
    if not isinstance(data, dict):
        raise HTTPException(400, "采集配置应为对象")
    previous = _config(identity)
    incoming = next((data[k] for k in ("room", "url", "external_room_id") if k in data), None)
    rid = parse_room(incoming) if incoming is not None else previous["external_room_id"]
    if data.get("platform", "douyin") != "douyin":
        raise HTTPException(400, "当前只支持抖音网页直播间")
    purpose = data.get("purpose", previous["purpose"])
    if purpose not in {"external_test", "own_room"}:
        raise HTTPException(400, "请选择公开直播间调试或自己的直播间")
    enabled = data.get("enabled", previous["enabled"])
    if type(enabled) is not bool:
        raise HTTPException(400, "采集开关必须是布尔值")
    if enabled and not rid:
        raise HTTPException(400, "请先填写抖音直播间链接或数字房间号")
    return {"id": identity, "platform": "douyin", "external_room_id": rid,
            "url": "https://live.douyin.com/" + rid if rid else "", "purpose": purpose,
            "enabled": enabled, "updated_at": srv().now()}


def _running(identity):
    state = SESSIONS.get(identity)
    return state if state and state.get("task") and not state["task"].done() else None


def save_config(identity, data):
    config = _validated(identity, data)
    active = _running(identity)
    if active and any(config[key] != active["config"][key] for key in ("external_room_id", "purpose", "enabled")):
        raise HTTPException(409, "请先关闭采集再修改来源；也可通过连接按钮切换直播间")
    if not active and SESSIONS.get(identity) and any(config[key] != SESSIONS[identity]["config"][key] for key in ("external_room_id", "purpose")):
        SESSIONS.pop(identity)
    srv().put("live_source_config", config)
    return get_view(identity)


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS live_source_events(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL,
        platform TEXT NOT NULL, external_room_id TEXT NOT NULL, event_key TEXT NOT NULL,
        event_type TEXT NOT NULL, record_kind TEXT NOT NULL, body TEXT NOT NULL,
        UNIQUE(room_id,platform,external_room_id,event_key))""")
    connection.execute("CREATE INDEX IF NOT EXISTS live_source_room_seq ON live_source_events(room_id,external_room_id,seq)")
    connection.execute("CREATE INDEX IF NOT EXISTS live_source_room_kind_seq ON live_source_events(room_id,external_room_id,event_type,seq)")
    totals_existed = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_source_totals'").fetchone()
    connection.execute("""CREATE TABLE IF NOT EXISTS live_source_totals(
        room_id TEXT NOT NULL, external_room_id TEXT NOT NULL, event_type TEXT NOT NULL,
        count INTEGER NOT NULL, PRIMARY KEY(room_id,external_room_id,event_type))""")
    if not totals_existed:
        # One-time migration only; ordinary polling never scans historical events.
        connection.execute("""INSERT OR IGNORE INTO live_source_totals(room_id,external_room_id,event_type,count)
            SELECT room_id,external_room_id,event_type,COUNT(*) FROM live_source_events
            GROUP BY room_id,external_room_id,event_type""")


def _number(value):
    return int(value) if type(value) in (int, str) and re.fullmatch(r"\d{1,30}", str(value)) else None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def normalize(message, config, capture_id):
    """Only map proven protobuf methods. Social enum values stay unclassified."""
    if not isinstance(message, dict):
        return None
    method = message.get("method")
    types = {"WebcastChatMessage": "comment", "WebcastLikeMessage": "like",
             "WebcastMemberMessage": "enter", "WebcastGiftMessage": "gift",
             "WebcastSocialMessage": "social", "WebcastRoomUserSeqMessage": "audience_snapshot",
             "WebcastRoomStatsMessage": "stats_display"}
    if method not in types:
        return None
    common, user = _mapping(message.get("common")), _mapping(message.get("user"))
    upstream = str(common.get("msgId") or message.get("msgId") or "")
    nickname = str(user.get("nickname") or "匿名观众")[:100]
    user_key = str(user.get("idStr") or user.get("id") or "")
    event = {"id": srv().uid("douyin_event"), "room_id": config["id"], "platform": "douyin",
             "external_room_id": config["external_room_id"], "purpose": config["purpose"],
             "source": "douyin", "type": types[method], "username": nickname, "nickname": nickname,
             "viewer_hash": hashlib.sha256(user_key.encode()).hexdigest()[:20] if user_key else None,
             "upstream_event_id": upstream, "raw_method": method, "time": srv().now(),
             "upstream_time": str(common.get("createTime") or ""), "capture_id": capture_id,
             "text": "", "count": None}
    if event["type"] == "comment":
        event["text"] = str(message.get("content") or "")[:2000]
    elif event["type"] == "like":
        event.update(count=_number(message.get("count")), total=_number(message.get("total")))
    elif event["type"] == "enter":
        event.update(member_action_raw=str(message.get("action") or ""),
                     action_description=str(message.get("actionDescription") or "")[:200],
                     enter_type_raw=str(message.get("enterType") or ""))
    elif event["type"] == "gift":
        gift = _mapping(message.get("gift"))
        event.update(gift_name=str(gift.get("name") or "礼物")[:100],
                     count=_number(message.get("repeatCount")) or _number(message.get("count")),
                     gift_id=str(message.get("giftId") or gift.get("id") or ""),
                     repeat_end=message.get("repeatEnd"), group_id=str(message.get("groupId") or ""),
                     combo_count=_number(message.get("comboCount")))
        event["text"] = event["gift_name"]
    elif event["type"] == "social":
        event["social_action_raw"] = str(message.get("action") or "")
        event["share_type_raw"] = str(message.get("shareType") or "")
        event["text"] = "社交互动（关注/分享的类型尚未校验）"
    elif event["type"] == "audience_snapshot":
        event.update(online_total_candidate=_number(message.get("total")),
                     total_user_raw=message.get("totalUser"),
                     total_pv_for_anchor_raw=message.get("totalPvForAnchor"),
                     online_user_for_anchor_raw=message.get("onlineUserForAnchor"),
                     metric_basis="WebcastRoomUserSeqMessage.total，在线人数候选口径，待与主播后台核对")
        event["text"] = "人数指标更新（口径待校验）"
    elif event["type"] == "stats_display":
        event.update(display_middle=str(message.get("displayMiddle") or "")[:150],
                     display_short=str(message.get("displayShort") or "")[:150])
        event["text"] = event["display_middle"] or event["display_short"] or "直播统计展示更新"
    # Upstream IDs are protobuf uint64 JSON strings. Never round-trip through JS numbers.
    event_key = method + ":" + upstream if upstream not in {"", "0"} else "unidentified:" + event["id"]
    return event, event_key


def _persist(event, event_key):
    kind = "live_metric" if event["type"] in METRIC_TYPES else "viewer_event"
    with srv().db() as connection:
        _schema(connection)
        cursor = connection.execute("""INSERT OR IGNORE INTO live_source_events
            (room_id,platform,external_room_id,event_key,event_type,record_kind,body) VALUES(?,?,?,?,?,?,?)""",
            (event["room_id"], "douyin", event["external_room_id"], event_key, event["type"], kind, "{}"))
        if cursor.rowcount == 0:
            return None
        event["cursor"] = cursor.lastrowid
        body = json.dumps(event, ensure_ascii=False)
        connection.execute("UPDATE live_source_events SET body=? WHERE seq=?", (body, cursor.lastrowid))
        connection.execute("INSERT INTO records(kind,id,body) VALUES(?,?,?)", (kind, event["id"], body))
        connection.execute("""INSERT INTO live_source_totals(room_id,external_room_id,event_type,count)
            VALUES(?,?,?,1) ON CONFLICT(room_id,external_room_id,event_type) DO UPDATE SET count=count+1""",
            (event["room_id"], event["external_room_id"], event["type"]))
    return event


def _metric(value, raw, basis, updated_at, stale):
    return {"value": value, "raw": raw, "basis": basis, "verified": False,
            "updated_at": updated_at, "stale": stale}


def _metrics(rows, active):
    result = {"online": None, "total_visits": None, "likes_total": None, "display": None}
    for row in rows:
        item = json.loads(row[0])
        stale = not active or item.get("capture_id") != active.get("capture_id") or not active.get("connected")
        # A silent source must not display a previous snapshot as live indefinitely.
        try:
            from datetime import datetime
            stale = stale or (datetime.fromisoformat(srv().now()) - datetime.fromisoformat(item["time"])).total_seconds() > 30
        except (ValueError, TypeError):
            stale = True
        if item["type"] == "audience_snapshot" and result["online"] is None:
            result["online"] = _metric(item.get("online_total_candidate"), item.get("online_total_candidate"),
                item.get("metric_basis"), item["time"], bool(stale))
            result["total_visits"] = _metric(_number(item.get("total_pv_for_anchor_raw")),
                item.get("total_pv_for_anchor_raw"), "totalPvForAnchor，累计访问候选值，非当前在线，口径待校验",
                item["time"], bool(stale))
        elif item["type"] == "like" and result["likes_total"] is None and item.get("total") is not None:
            result["likes_total"] = _metric(item["total"], item["total"], "WebcastLikeMessage.total，上游累计点赞",
                                           item["time"], bool(stale))
        elif item["type"] == "stats_display" and result["display"] is None:
            result["display"] = _metric(None, item.get("display_middle") or item.get("display_short"),
                                        "上游展示文案；未解释为在线或累计", item["time"], bool(stale))
    return result


def get_view(identity, after=0):
    config = _config(identity)
    if type(after) is not int or after < 0 or after > 9223372036854775807:
        raise HTTPException(400, "事件游标必须是非负整数")
    with srv().db() as connection:
        _schema(connection)
        params = (identity, config["external_room_id"])
        if after:
            rows = connection.execute("SELECT body FROM live_source_events WHERE room_id=? AND external_room_id=? AND seq>? ORDER BY seq LIMIT 200", (*params, after)).fetchall()
        else:
            rows = connection.execute("SELECT body FROM live_source_events WHERE room_id=? AND external_room_id=? ORDER BY seq DESC LIMIT 200", params).fetchall()[::-1]
        history = dict(connection.execute("SELECT event_type,count FROM live_source_totals WHERE room_id=? AND external_room_id=?", params).fetchall())
        last = connection.execute("SELECT body FROM live_source_events WHERE room_id=? AND external_room_id=? ORDER BY seq DESC LIMIT 1", params).fetchone()
        # Only a handful of latest metric rows per type are needed, regardless of chat volume.
        metric_rows = []
        for kind in ("audience_snapshot", "stats_display", "like"):
            metric_rows += connection.execute("SELECT body FROM live_source_events WHERE room_id=? AND external_room_id=? AND event_type=? ORDER BY seq DESC LIMIT 1", (*params, kind)).fetchall()
    active = SESSIONS.get(identity)
    state = active["state"] if active else ("reconnect_required" if config["enabled"] else "disabled")
    status = {"state": state, "connected": bool(active and active["connected"] and _running(identity)),
              "enabled": config["enabled"], "message": active["message"] if active else (
                  "已保存采集开关；应用重启后请手动重新连接" if config["enabled"] else "采集已关闭"),
              "last_event_at": json.loads(last[0])["time"] if last else None,
              "last_event_stale": bool(last and (not active or not active.get("connected")
                  or json.loads(last[0]).get("capture_id") != active.get("capture_id"))),
              "counts": dict(active["counts"]) if active else {}, "history_counts": history,
              "metrics": _metrics(metric_rows, active), "collector_ready": executable().is_file(),
              "external_room_id": config["external_room_id"],
              "capture_id": active.get("capture_id") if active else None,
              "capture_started_at": active.get("capture_started_at") if active else None,
              "local_socket_connected": bool(active and active.get("local_socket_connected")),
              "retry_count": active.get("retry_count", 0) if active else 0,
              "unknown_messages": active.get("unknown_messages", 0) if active else 0,
              "automatic_reply": config.get("purpose") == "own_room",
              "welcome_policy": "own_room_only"}
    events = [json.loads(row[0]) for row in rows]
    return {"config": config, "status": status, "events": events,
            "next_cursor": events[-1]["cursor"] if events else after}


def collector_environment(key):
    # Ignore arbitrary collector overrides inherited from the desktop environment.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("APP_")}
    env["APP_API_KEY"] = key
    for protocol, proxy in urllib.request.getproxies().items():
        if protocol in {"http", "https"}:
            env[protocol.upper() + "_PROXY"] = proxy
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
    return env


def _health(port, key):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # Authenticated readiness avoids mistaking another service on a raced port for ours.
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/health", headers={"Authorization": "Bearer " + key})
    with opener.open(request, timeout=.5) as response:
        return response.status == 200


async def _stop_collector_unlocked():
    process = COLLECTOR["process"]
    COLLECTOR.update(process=None, key=None, port=None)
    if process and process.returncode is None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass


async def _ensure_collector():
    async with SERVICE_LOCK:
        if COLLECTOR["process"] and COLLECTOR["process"].returncode is None:
            return COLLECTOR["port"], COLLECTOR["key"]
        path = executable()
        if not path.is_file():
            raise RuntimeError("采集插件未安装；缺少本机采集程序")
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        directory = srv().RUNTIME / "live_source"
        directory.mkdir(parents=True, exist_ok=True)
        config_path = directory / "collector.yaml"
        config_path.write_text(f'port: "{port}"\nprotocol:\n  mode: web\nlog:\n  level: error\nsign:\n  provider: local\ncookie:\n  use_stored: false\nmonitor:\n  poll_interval: "30s"\n  notify_interval: "30s"\n', encoding="utf-8")
        key = secrets.token_urlsafe(32)
        process = await asyncio.create_subprocess_exec(str(path), "--config", str(config_path),
            cwd=directory, env=collector_environment(key), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        COLLECTOR.update(process=process, key=key, port=port)
        try:
            for _ in range(60):
                if process.returncode is not None:
                    raise RuntimeError("采集插件启动失败，请检查本机端口或程序文件")
                try:
                    if await asyncio.to_thread(_health, port, key):
                        return port, key
                except Exception:
                    pass
                await asyncio.sleep(.1)
            raise RuntimeError("采集插件启动超时，请重新连接")
        except BaseException:
            await _stop_collector_unlocked()
            raise


async def _stop_if_idle():
    async with SERVICE_LOCK:
        current = asyncio.current_task()
        if not any(s.get("task") and s["task"] is not current and not s["task"].done() for s in SESSIONS.values()):
            await _stop_collector_unlocked()


async def _publish(identity, state, message):
    normalized = normalize(message, state["config"], state["capture_id"])
    if not normalized:
        state["unknown_messages"] += 1
        return
    event, event_key = normalized
    target = srv().room_instance(identity)
    event["session_id"] = target.state["session_id"]
    persisted = _persist(event, event_key)
    state.update(connected=True, state="connected", message=("正在接收自己直播间的抖音消息" if state["config"]["purpose"] == "own_room" else "正在只读接收抖音直播消息"))
    if persisted is None:
        return
    state["counts"][event["type"]] += 1
    state["last_event_at"] = event["time"]
    if event["type"] not in METRIC_TYPES:
        async with target.lock:
            target.state["audience_events"] = (target.state["audience_events"] + [event])[-120:]
            if event["type"] == "enter":
                target.welcome_entry(event)
            elif event["type"] == "comment":
                target.comment_entry(event)
            elif event["type"] == "like":
                target.like_entry(event)
            await target.broadcast()


async def _system(state, message):
    status = message.get("status")
    if status == "online" and message.get("live") is True:
        state.update(state="connected", connected=True, message="抖音直播间已开播，等待并接收消息")
    elif status in {"offline", "account_offline"}:
        state.update(state="offline", connected=False, message="抖音直播间未开播或已下播，保持连接等待开播")
    elif status == "ended":
        state.update(state="checking", connected=False, message="上游采集连接结束，等待确认直播状态或恢复连接")
    elif status == "not_found":
        state.update(state="not_found", connected=False, message="直播间不存在或房间号无效，请确认公开网页链接")
        return False
    elif status == "error":
        state.update(state="error", connected=False, message="上游直播间检查失败，请稍后重新连接")
        return False
    else:
        state.update(state="checking", connected=False, message="已连接本机插件，暂时无法确认上游直播状态；等待检查")
    return True


async def _run(identity, state):
    try:
        while True:
            try:
                port, key = await _ensure_collector()
                options = {"additional_headers": {"Authorization": "Bearer " + key},
                           "open_timeout": 10, "close_timeout": 2, "max_size": 4_000_000,
                           "ping_interval": 20, "ping_timeout": 20}
                if "proxy" in inspect.signature(connect).parameters:
                    options["proxy"] = None  # The local collector must never use a network proxy.
                async with connect(f"ws://127.0.0.1:{port}/ws/{state['config']['external_room_id']}", **options) as ws:
                    state.update(local_socket_connected=True, state="checking", connected=False,
                                 message="本机采集插件已连接，正在确认抖音直播状态")
                    while True:
                        raw = await ws.recv()
                        if raw in ("ping", "pong", b"ping", b"pong"):
                            continue
                        try:
                            message = json.loads(raw)
                        except (ValueError, TypeError):
                            state["unknown_messages"] += 1
                            continue
                        if not isinstance(message, dict):
                            state["unknown_messages"] += 1
                            continue
                        if message.get("type") == "system":
                            if not await _system(state, message):
                                return
                        else:
                            await _publish(identity, state, message)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                state.update(connected=False, local_socket_connected=False)
                # Do not return exception repr: proxy headers and websocket auth may contain secrets.
                if isinstance(error, RuntimeError):
                    state.update(state="error", message=str(error)[:150])
                    return
                if state["retry_count"] >= MAX_RETRIES:
                    state.update(state="error", message="采集连接多次中断，已停止重试；请手动重新连接")
                    return
                state["retry_count"] += 1
                state.update(state="reconnecting", message=f"采集连接中断，正在重连（{state['retry_count']}/{MAX_RETRIES}）")
                await asyncio.sleep(min(30, RETRY_BASE * 2 ** (state["retry_count"] - 1)))
    except asyncio.CancelledError:
        state.update(state="disconnected", connected=False, message="采集已关闭，历史记录保留")
        raise
    finally:
        state.update(connected=False, local_socket_connected=False)
        await _stop_if_idle()


async def _disconnect(identity, persist=True):
    state = SESSIONS.get(identity)
    if state:
        task = state.get("task")
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        state.update(state="disabled", connected=False, local_socket_connected=False,
                     message="采集已关闭，历史记录保留")
    if persist:
        config = _config(identity)
        config.update(enabled=False, updated_at=srv().now())
        srv().put("live_source_config", config)
    await _stop_if_idle()


async def connect_source(identity, data=None):
    if data is not None and not isinstance(data, dict):
        raise HTTPException(400, "采集配置应为对象")
    config = _validated(identity, {**(data or {}), "enabled": True})
    lock = CONTROL_LOCKS.setdefault(identity, asyncio.Lock())
    async with lock:
        await _disconnect(identity, persist=False)
        srv().put("live_source_config", config)
        state = {"config": config, "state": "connecting", "connected": False,
                 "local_socket_connected": False, "message": "正在启动只读采集插件",
                 "counts": Counter(), "last_event_at": None, "retry_count": 0, "unknown_messages": 0,
                 "capture_id": srv().uid("capture"), "capture_started_at": srv().now(), "task": None}
        SESSIONS[identity] = state
        state["task"] = asyncio.create_task(_run(identity, state), name="douyin-source-" + identity)
        await asyncio.sleep(0)
    return get_view(identity)


async def disconnect_source(identity):
    srv().settings(identity)
    lock = CONTROL_LOCKS.setdefault(identity, asyncio.Lock())
    async with lock:
        await _disconnect(identity)
    return get_view(identity)


async def shutdown():
    # Preserve enabled as user intent but never reconnect automatically on startup.
    for identity in list(SESSIONS):
        await _disconnect(identity, persist=False)
    SESSIONS.clear()
    CONTROL_LOCKS.clear()
    async with SERVICE_LOCK:
        await _stop_collector_unlocked()
