"""Exercise the isolated QA service: local TTS only, no live catalog changes."""
import json
from pathlib import Path
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8766"


def api(path, data=None, method=None):
    request = urllib.request.Request(BASE + path, data=None if data is None else json.dumps(data).encode(),
        method=method, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(request,timeout=60) as response:
        return json.load(response)


def main():
    library = api("/api/library")
    assert all(p["id"] == "product_slippers_001" for p in library["products"]), "QA出现非默认数据，停止避免改动未知数据"
    product = api("/api/products", {"name":"QA分段测试拖鞋","price":19,"selling_points":"足弓支撑，软弹厚底。"})
    # Import an exact, short test script directly into the QA database only.
    import sys, os
    sys.path.insert(0,str(ROOT))
    os.environ["SHENGBO_RUNTIME"] = str(ROOT / "runtime/qa-prosody-v1.4")
    from app import server
    script = server.structure_script(product,{"blocks":[
        {"stage":"welcome","focus":"开场","sentences":[{"display_text":"欢迎来到直播间。","kind":"engagement","fact_ids":[]}]},
        {"stage":"benefit","focus":"足弓支撑","sentences":[{"display_text":"这双拖鞋有足弓支撑和软弹厚底。","fact_ids":["SELLING"]}]},
        {"stage":"cta","focus":"选择","sentences":[{"display_text":"选好合适的尺码，再按需要下单。","kind":"engagement","fact_ids":[]}]}]},"qa","1")
    server.put("script",script)
    room = api("/api/rooms",{"name":"QA分段完整流程","product_ids":[product["id"]],"plan_mode":"all","script_order":[script["id"]],"tts_provider":"system","tts_voice":"Huihui"})
    detail = api("/api/scripts/"+script["id"])
    assert len(detail["paragraphs"]) == 3
    detail["sentences"][1].update(text="这双拖鞋主打足弓支撑和软弹厚底。",spoken_text="这双拖鞋主打足弓支撑和软弹厚底。")
    edited = api("/api/scripts/"+script["id"],detail,"PUT")
    assert [b["stage"] for b in edited["blocks"]] == ["welcome","benefit","cta"]
    api("/api/rooms/"+room["id"],{"script_order":[edited["id"]]},"PUT")
    job = api("/api/rooms/"+room["id"]+"/jobs",{"kind":"audio"})
    deadline = time.monotonic()+90
    while time.monotonic()<deadline:
        jobs=api("/api/jobs?room_id="+room["id"])
        current=next(v for v in jobs["jobs"] if v["id"]==job["id"]) if isinstance(jobs,dict) else next(v for v in jobs if v["id"]==job["id"])
        if current["status"] not in {"queued","running"}:
            break
        time.sleep(.3)
    assert current["status"]=="completed", current
    api("/api/rooms/"+room["id"]+"/control",{"action":"start"})
    state=api("/api/rooms/"+room["id"]+"/state")
    assert state["current"] and edited["status"]=="draft"
    api("/api/rooms/"+room["id"]+"/control",{"action":"stop"})
    evaluation=api("/api/copywriting/evaluation")
    assert len(evaluation["audio_samples"])==6
    for sample in evaluation["audio_samples"]:
        with urllib.request.urlopen(BASE+sample["url"]) as response:
            assert response.read(4)==b"RIFF"
    report={"passed":True,"room":room["id"],"sections":3,"audio_done":current["done"],"text_confirmation_required":False,
        "audio_samples":6,"checks":["分段详情接口","结构化编辑保留段落","真实本机语音后台生成","未确认文本语音完成即可开播","语音A/B文件可读取"],
        "not_tested":["浏览器实际点击","实际扬声器听感","OBS接入或真实观众转化"]}
    (ROOT/"docs/话术评测_v1.4/api-verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False))


if __name__=="__main__":
    main()
