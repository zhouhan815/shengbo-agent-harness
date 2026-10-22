"""Bounded real audio probes; waveform checks never substitute for human listening."""
import json
from pathlib import Path
import sys
import wave
import struct
import math

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import server, speech
from app.prosody import sentence_style


def main():
    directory = ROOT / "docs/语音试听_v1.4"
    directory.mkdir(exist_ok=True)
    samples = [
        ("pain", "你有没有过这种小烦恼：下楼散步、倒垃圾，穿运动鞋太麻烦，穿普通拖鞋又怕走久了脚累。"),
        ("benefit", "这双拖鞋采用一体成型的EVA材质，搭配足弓支撑和软弹厚底。在家走动时，可以按自己的需要选择。"),
        ("promotion", "活动价是一百零七新台币。喜欢这款的朋友，先选好颜色和尺码，合适就下单。"),
    ]
    rows = []
    for stage, text in samples:
        for version, style in [("A-原参数", None), ("B-分段参数", sentence_style({"stage": stage}))]:
            row = {"stage": stage, "version": version, "text": text, "voice": "Wise_Woman", "model": "speech-2.8-turbo",
                   "style": style, "listening": {"唱腔": "未试听", "背景杂音": "未试听", "感染力": "未试听"}}
            try:
                result = speech.render(text, directory, row["voice"], "minimax", row["model"], style)
                path = directory / (result["audio_id"] + ".wav")
                with wave.open(str(path), "rb") as audio:
                    values = struct.unpack("<"+"h"*audio.getnframes()*audio.getnchannels(), audio.readframes(audio.getnframes())) if audio.getsampwidth()==2 else ()
                    row["waveform"] = {"duration": round(result["duration"],3), "sample_rate": audio.getframerate(),
                        "channels": audio.getnchannels(), "peak": round(max(map(abs, values), default=0)/32768,5),
                        "rms": round(math.sqrt(sum(v*v for v in values)/max(1,len(values)))/32768,5),
                        "clip_ratio": round(sum(abs(v)>=32760 for v in values)/max(1,len(values)),6)}
                row.update(file=path.name, status="generated")
            except RuntimeError as error:
                row.update(status="error", error=str(error))
            rows.append(row)
            (directory / "results.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps({"stage":stage,"version":version,"status":row["status"],"error":row.get("error"),"waveform":row.get("waveform")},ensure_ascii=False), flush=True)
            if row["status"] == "error":
                return  # do not keep charging/retrying an unavailable service


if __name__ == "__main__":
    main()
