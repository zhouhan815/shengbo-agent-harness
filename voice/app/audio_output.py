"""Read-only Windows render-device diagnostics. Never changes audio routing.

Only browser audio sessions are returned; no microphone, audio recording,
window titles, command lines or credentials are read.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sys
import time


BROWSERS = {
    "chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox",
    "codex.exe": "Codex 内置浏览器", "chatgpt.exe": "ChatGPT 内置浏览器",
    "msedgewebview2.exe": "内嵌浏览器",
}


def output_status():
    result = {"schema_version": "AUDIO-OUTPUT-1", "available": False,
              "observed_at": datetime.now(timezone.utc).isoformat(),
              "default_device_id": None, "devices": [], "browser_sessions": [],
              "message": "本机输出设备检测暂不可用"}
    if sys.platform != "win32":
        result["message"] = "此系统不支持 Windows 输出设备检测；请查看系统声音设置"
        return result
    try:
        import comtypes
        from pycaw.api.audiopolicy import IAudioSessionControl2
        from pycaw.api.endpointvolume import IAudioMeterInformation
        from pycaw.constants import DEVICE_STATE, EDataFlow
        from pycaw.utils import AudioSession, AudioUtilities
    except ImportError:
        result["message"] = "输出设备检测组件未安装；网页播放仍可使用"
        return result

    comtypes.CoInitialize()
    try:
        try:
            default = AudioUtilities.GetSpeakers()
            result["default_device_id"] = default.id if default else None
        except Exception:
            pass
        devices = AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
        meters = []
        for device in devices:
            row = {"id": device.id, "name": device.FriendlyName or "未命名输出设备",
                   "default": device.id == result["default_device_id"],
                   "muted": None, "volume_percent": None, "peak": None}
            result["devices"].append(row)
            try:
                volume = device.EndpointVolume
                row.update(muted=bool(volume.GetMute()),
                           volume_percent=round(volume.GetMasterVolumeLevelScalar() * 100))
            except Exception:
                pass
            try:
                meter = device._dev.Activate(IAudioMeterInformation._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioMeterInformation)
                meters.append((row, meter))
            except Exception:
                pass
            try:
                sessions = device.AudioSessionManager.GetSessionEnumerator()
                for i in range(sessions.GetCount()):
                    try:
                        session = AudioSession(sessions.GetSession(i).QueryInterface(IAudioSessionControl2))
                        process = session.Process
                        name = process.name().lower() if process else ""
                        if name not in BROWSERS or session.State == 2:
                            continue
                        volume = session.SimpleAudioVolume
                        item = {"application": BROWSERS[name], "device_id": device.id,
                                "device_name": row["name"], "active": session.State == 1,
                                "muted": bool(volume.GetMute()),
                                "volume_percent": round(volume.GetMasterVolume() * 100), "peak": None}
                        result["browser_sessions"].append(item)
                        try:
                            meters.append((item, session._ctl.QueryInterface(IAudioMeterInformation)))
                        except Exception:
                            pass
                    except Exception:
                        continue
            except Exception:
                continue
        # A few samples catch speech gaps without recording or saving sound.
        for i in range(4):
            for row, meter in meters:
                try:
                    row["peak"] = round(max(row["peak"] or 0, float(meter.GetPeakValue())), 4)
                except Exception:
                    pass
            if i < 3:
                time.sleep(.05)
        result.update(available=True, message="已读取 Windows 输出设备；浏览器会话可能包含其他网页，不能单凭电平认定直播伴侣已采集")
        # COM pointers must be released in this thread, before CoUninitialize.
        meters.clear()
        return result
    except Exception:
        result["message"] = "读取输出设备失败；请在 Windows 音量混合器检查浏览器输出"
        return result
    finally:
        default = devices = device = volume = sessions = session = process = meter = meters = None
        comtypes.CoUninitialize()
