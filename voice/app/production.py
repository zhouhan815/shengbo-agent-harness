"""Room-owned prerecorded audio and durable, observable preparation jobs."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import shutil

from fastapi import HTTPException

from .speech import cached_audio
from .prosody import sentence_style

TASKS = {}


def srv():
    from . import server
    return server


def room_directory(identity):
    srv().settings(identity)  # Never allow arbitrary paths through a room id.
    return srv().RUNTIME / "room_audio" / identity


def prerecorded(identity, sentence, tts):
    audio = cached_audio(sentence.get("spoken_text") or sentence["text"], room_directory(identity),
                         tts["voice"], tts["provider"], tts["model"], sentence_style(sentence, tts["provider"]))
    if audio:
        audio["url"] = f"/room-audio/{identity}/{audio['audio_id']}.wav"
    return audio


def plan_ids(config):
    return list(config.get("script_order", []) if config.get("plan_mode") == "all" else config.get("selected_script_ids", []))


def audio_status(identity):
    s = srv()
    config = s.settings(identity)
    tts = s.voice_options(config)
    versions = []
    for sid in plan_ids(config):
        script = s.get("script", sid)
        ready = sum(prerecorded(identity, entry, tts) is not None for entry in script["sentences"])
        total = len(script["sentences"])
        try:
            stale = s.commerce.script_is_stale(script, s.get("product", script["product_id"]), config)
        except HTTPException:
            stale = True
        versions.append({"script_id": sid, "product_id": script["product_id"], "ready": ready, "total": total,
                         "configuration_stale": stale,
                         "status": "ready" if ready == total else "partial" if ready else "missing"})
    return {"room_id": identity, "tts": tts, "scripts": versions,
            "ready": sum(v["ready"] for v in versions), "total": sum(v["total"] for v in versions),
            "all_ready": bool(versions) and all(v["status"] == "ready" for v in versions)}


def require_audio(identity, identities, tts):
    missing = []
    for sid in identities:
        script = srv().get("script", sid)
        count = sum(prerecorded(identity, entry, tts) is None for entry in script["sentences"])
        if count:
            missing.append(f"{script['product_snapshot']['name']}：缺少{count}句")
    if missing:
        raise HTTPException(409, "本场语音尚未生成完整，请在配置页点击生成语音。" + "；".join(missing[:4]))


def room_scripts(identity):
    """Room is the management unit; legacy room references remain available in that room."""
    config = srv().settings(identity)
    referenced = set(config.get("script_order", [])) | set(config.get("selected_script_ids", []))
    return [v for v in srv().all_of("script") if v.get("room_id") == identity or v["id"] in referenced]


def number_script(script, identity=None):
    candidates = room_scripts(identity) if identity else srv().all_of("script")
    existing = [v for v in candidates if v["product_id"] == script["product_id"]]
    script["version_number"] = max([v.get("version_number", i + 1) for i, v in enumerate(existing)] or [0]) + 1
    if identity:
        script["room_id"] = identity
    return script


def variation_context(identity, product_id):
    return [{"angle": v.get("angle", ""), "opening": [e["text"] for e in v["sentences"][:4]],
             "benefit_order": [b.get("focus") or b["stage"] for b in v["blocks"]],
             "outline": [e["text"] for e in v["sentences"] if e["stage"] in {"benefit", "scene", "selling_point", "scenario"}][:10],
             "sentences_to_avoid": [e["text"] for e in v["sentences"] if e["stage"] not in {"identity", "specification", "price", "promotion"}][:45]}
            for v in room_scripts(identity) if v["product_id"] == product_id][-3:]


def list_jobs(identity=None):
    jobs = srv().all_of("job")
    for job in jobs:
        if job["status"] in {"queued", "running"} and job["id"] not in TASKS:
            job.update(status="interrupted", error="服务已重启，任务已中断；已完成的文本和语音保留，可重新生成剩余部分。")
            for item in job["items"]:
                if item["status"] in {"queued", "running"}:
                    item["status"] = "interrupted"
            srv().put("job", job)
    return [j for j in reversed(jobs) if identity is None or j["room_id"] == identity][:60]


def persist(job):
    job["done"] = sum(item["done"] for item in job["items"])
    job["total"] = sum(item["total"] for item in job["items"])
    job["updated_at"] = srv().now()
    srv().put("job", job)


def validate_scripts(config, identities):
    if not identities or len(identities) > 1000:
        raise HTTPException(400, "本场还没有话术，请先生成或添加话术")
    for sid in identities:
        script = srv().get("script", sid)
        if script["product_id"] not in config["product_ids"]:
            raise HTTPException(400, "话术不属于本场商品")
        product = srv().get("product", script["product_id"])
        if srv().commerce.script_is_stale(script, product, config):
            raise HTTPException(409, "商品或本场配置已更新，请先更新本场话术再生成语音")


def attach_scripts(identity, scripts):
    s = srv()
    config = s.settings(identity)
    if any(v["product_id"] not in config["product_ids"] for v in scripts):
        raise RuntimeError("生成期间商品已移出直播间；文本已保存在本直播间任务记录，未加入本场播出列表。")
    ids = plan_ids(config)
    ids.extend(v["id"] for v in scripts if v["id"] not in ids)
    s.save_room_config(identity, {"plan_mode": "all", "script_order": ids,
                                "selected_script_ids": ids})


async def run_job(job):
    s = srv()
    job["status"] = "running"
    persist(job)
    try:
        for item in job["items"]:
            item["status"] = "running"
            persist(job)
            if job["kind"] == "scripts":
                product = s.commerce.effective_product(s.get("product", item["product_id"]), s.settings(job["room_id"]))
                prior = variation_context(job["room_id"], product["id"])
                result = await asyncio.to_thread(s.generate_remote, product, 1, prior)
                script = number_script(result[0], job["room_id"])
                s.put("script", script)
                item.update(script_id=script["id"], version_number=script["version_number"])
                job["result_script_ids"].append(script["id"])
                attach_scripts(job["room_id"], [script])
                item["done"] = 1
            else:
                script = s.get("script", item["script_id"])
                # Freeze the text and voice for this job. A later edit is a new script id.
                tts = job["tts"]
                directory = room_directory(job["room_id"])
                directory.mkdir(parents=True, exist_ok=True)
                manifest = {"id": job["room_id"] + ":" + script["id"] + ":" + "|".join(tts.values()),
                            "room_id": job["room_id"], "script_id": script["id"], "tts": tts, "sentences": []}
                for entry in script["sentences"]:
                    audio = prerecorded(job["room_id"], entry, tts)
                    if audio:
                        item["reused"] += 1
                    else:
                        text = entry.get("spoken_text") or entry["text"]
                        # Adopt a valid legacy preview cache into this room's own storage.
                        style = sentence_style(entry, tts["provider"])
                        legacy = cached_audio(text, s.RUNTIME / "audio", tts["voice"], tts["provider"], tts["model"], style)
                        if legacy:
                            shutil.copy2(s.RUNTIME / "audio" / (legacy["audio_id"] + ".wav"), directory / (legacy["audio_id"] + ".wav"))
                            item["reused"] += 1
                        else:
                            # Remove invalid files so render cannot mistake them for a cache hit.
                            from .speech import audio_key
                            target = directory / (audio_key(text, tts["voice"], tts["provider"], tts["model"], style) + ".wav")
                            if target.exists():
                                target.unlink()
                            await asyncio.get_running_loop().run_in_executor(s.POOL, s.render, text, directory,
                                    tts["voice"], tts["provider"], tts["model"], style)
                            item["generated"] += 1
                        audio = prerecorded(job["room_id"], entry, tts)
                        if not audio:
                            raise RuntimeError("合成结果不是有效本地音频，未标记为已完成")
                    manifest["sentences"].append({"sentence_id": entry["id"], **audio})
                    item["done"] += 1
                    s.put("room_audio", manifest)
                    persist(job)
            item["status"] = "completed"
            persist(job)
        job["status"] = "completed"
    except asyncio.CancelledError:
        job.update(status="interrupted", error="任务已中断，已完成内容保留")
        for item in job["items"]:
            if item["status"] in {"queued", "running"}:
                item["status"] = "interrupted"
        raise
    except Exception as error:
        message = error.detail if isinstance(error, HTTPException) else str(error) if isinstance(error, RuntimeError) else "生成失败，已完成内容保留，请重试剩余部分"
        job.update(status="error", error=message)
        for item in job["items"]:
            if item["status"] == "running":
                item.update(status="error", error=message)
            elif item["status"] == "queued":
                item["status"] = "not_run"
    finally:
        persist(job)


async def create_job(identity, data):
    s = srv()
    config = s.settings(identity)
    kind = data.get("kind")
    if kind not in {"scripts", "audio"}:
        raise HTTPException(400, "生成任务类型不正确")
    if any(j["kind"] == kind and j["status"] in {"queued", "running"} for j in list_jobs(identity)):
        raise HTTPException(409, "本直播间已有同类生成任务，请查看后台任务")
    items = []
    if kind == "scripts":
        products = data.get("product_ids", config["product_ids"])
        try:
            count = int(data.get("version_count", config["version_count"]))
        except (ValueError, TypeError):
            raise HTTPException(400, "请输入1至6的版本数量") from None
        if not isinstance(products, list) or not products or len(products) > 100 or any(not isinstance(p, str) for p in products) or not 1 <= count <= 6:
            raise HTTPException(400, "请选择商品，并设置每件生成1至6版话术")
        for pid in dict.fromkeys(products):
            if pid not in config["product_ids"]:
                raise HTTPException(400, "请先从商品库选择本场商品")
            product = s.get("product", pid)
            s.commerce.effective_product(product, config)
            existing = [v for v in room_scripts(identity) if v["product_id"] == pid]
            base = max([v.get("version_number", i + 1) for i, v in enumerate(existing)] or [0])
            for n in range(count):
                items.append({"product_id": pid, "product_name": product["name"], "version_number": base + n + 1,
                              "status": "queued", "done": 0, "total": 1})
    else:
        # The audio button always covers the whole saved plan, never a hidden subset.
        ids = plan_ids(config)
        validate_scripts(config, ids)
        if sum(len(s.get("script", sid)["sentences"]) for sid in ids) > 12000:
            raise HTTPException(400, "单次语音任务最多12000句，请减少本场话术")
        for sid in ids:
            script = s.get("script", sid)
            items.append({"script_id": sid, "product_id": script["product_id"], "product_name": script["product_snapshot"]["name"],
                          "version_number": script.get("version_number"), "status": "queued", "done": 0,
                          "total": len(script["sentences"]), "reused": 0, "generated": 0})
    job = {"id": s.uid("job"), "room_id": identity, "kind": kind, "status": "queued", "items": items,
           "tts": s.voice_options(config), "result_script_ids": [], "done": 0, "total": sum(v["total"] for v in items),
           "error": None, "created_at": s.now()}
    persist(job)
    task = asyncio.create_task(run_job(job))
    TASKS[job["id"]] = task
    task.add_done_callback(lambda finished: TASKS.pop(job["id"], None))
    return deepcopy(job)
