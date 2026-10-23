"""RealtimeTTS + Windows SAPI voice, WAV output only; the browser owns playback."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import wave
import io
import os
import urllib.request
import urllib.error
import math
import re

POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech")


_EMOTION_MODELS = {"speech-2.8-hd", "speech-2.8-turbo", "speech-2.6-hd", "speech-2.6-turbo",
                   "speech-02-hd", "speech-02-turbo", "speech-01-hd", "speech-01-turbo"}
_EMOTIONS = {"happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm"}

# Explicit input/auth/quota rejections from the official MiniMax error-code
# table. Timeouts, internal/output errors and unrecognised codes stay unknown:
# receiving an error response does not prove that no paid work was performed.
_MINIMAX_KNOWN_REJECTIONS = frozenset({1002, 1004, 1008, 1026, 1039, 1040, 1041,
    1042, 1043, 1044, 1045, 1046, 2013, 20132, 2014, 2022, 2037, 2038, 2039,
    2042, 2043, 2045, 2046, 2047, 2048, 2049, 2051, 2052, 2053, 2054, 2056,
    2057, 2061, 2062, 2067, 2068, 2069})
_KNOWN_REJECTION_HTTP = frozenset({400, 401, 403, 404, 405, 413, 415, 422, 429})
_DIAGNOSTIC_STAGES = frozenset({"configuration", "transport", "http_response", "json_decode",
    "provider_response", "audio_decode", "audio_validate", "audio_write", "cache_validate", "observation", "adapter"})
_DIAGNOSTIC_OUTCOMES = frozenset({"known_rejected", "outcome_unknown", "not_sent", "response_received"})


def tts_diagnostics(stage, outcome, *, response_received=False, exception=None,
                    http_status=None, provider_code=None, metadata=None):
    """A stable scalar whitelist; never inspect exception text, URLs or headers."""
    result = {"provider": "minimax", "stage": stage if isinstance(stage, str) and stage in _DIAGNOSTIC_STAGES else "adapter",
              "outcome": outcome if isinstance(outcome, str) and outcome in _DIAGNOSTIC_OUTCOMES else "outcome_unknown",
              "response_received": response_received is True, "usage_state": "unknown"}
    if exception is not None:
        name = type(exception).__name__
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name):
            result["exception_type"] = name
    if isinstance(http_status, int) and not isinstance(http_status, bool) and 100 <= http_status <= 599:
        result["http_status"] = http_status
    if isinstance(provider_code, int) and not isinstance(provider_code, bool) and 0 <= provider_code <= 999999:
        result["provider_code"] = provider_code
    metadata = metadata if isinstance(metadata, dict) else {}
    usage = metadata.get("usage_characters")
    if isinstance(usage, int) and not isinstance(usage, bool) and usage >= 0:
        result.update(usage_characters=usage, usage_state="known")
    trace = metadata.get("trace_id")
    if isinstance(trace, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", trace):
        result["trace_id"] = trace
    return result


class SpeechRenderError(RuntimeError):
    """Safe typed failure, separate from provider-controlled error messages."""
    def __init__(self, message, diagnostics):
        super().__init__(message)
        self.diagnostics = dict(diagnostics)


def _minimax_error_outcome(code=None, http_status=None):
    if code in _MINIMAX_KNOWN_REJECTIONS:
        return "known_rejected"
    if code not in {None, 0}:
        return "outcome_unknown"
    return "known_rejected" if http_status in _KNOWN_REJECTION_HTTP else "outcome_unknown"


def _observe_tts_response(observer, diagnostics):
    if observer is not None:
        try:
            observer(dict(diagnostics))
        except Exception as error:
            safe = tts_diagnostics("observation", "outcome_unknown", response_received=True,
                exception=error, http_status=diagnostics.get("http_status"),
                provider_code=diagnostics.get("provider_code"), metadata=diagnostics)
            raise SpeechRenderError("MiniMax已返回，但公开用量记录未完成；请先核实，不可自动重复付费", safe) from None


def resolved_style(style, provider="minimax", model="speech-2.8-turbo"):
    """Whitelist real API parameters; descriptions must never enter spoken text."""
    if style is None:
        return None
    result = {key: style[key] for key in ("profile_id", "style_revision", "label", "direction", "capability_note") if key in style}
    for field, default, low, high in (("speed", 1.0, 0.5, 2.0), ("vol", 1.0, 0.0001, 10.0), ("pitch", 0, -12, 12)):
        value = style.get(field, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"语音风格参数{field}超出允许范围")
        if field == "pitch" and int(value) != value:
            raise ValueError("语音音高pitch必须是整数")
        result[field] = int(value) if field == "pitch" else float(value)
    emotion = style.get("emotion")
    allowed = _EMOTIONS | ({"fluent", "whisper"} if model in {"speech-2.6-hd", "speech-2.6-turbo"} else set())
    if emotion is not None and emotion not in allowed:
        raise ValueError("当前模型不支持该情绪参数")
    result.update(provider=provider, model=model, emotion=emotion if provider == "minimax" and model in _EMOTION_MODELS else None,
                  emotion_supported=provider == "minimax" and model in _EMOTION_MODELS)
    if provider == "system":
        result.update(speed=1.0, pitch=0, vol=1.0, capability_note="本机普通话保持默认读法，不支持分段情绪控制")
    if provider == "minimax" and style.get("pronunciation_dict"):
        dictionary = style["pronunciation_dict"]
        if not isinstance(dictionary, dict) or not isinstance(dictionary.get("tone"), list) or not all(isinstance(x, str) for x in dictionary["tone"]):
            raise ValueError("发音字典必须是tone字符串列表")
        result["pronunciation_dict"] = {"tone": list(dictionary["tone"])}
    if style.get("subtitle_enable") is True:
        if provider != "minimax" or style.get("subtitle_type", "sentence") not in {"sentence", "word"}:
            raise ValueError("连续音频时间轴参数无效")
        result.update(subtitle_enable=True, subtitle_type=style.get("subtitle_type", "sentence"))
    return result


def audio_key(text: str, voice="Huihui", provider="system", model="speech-2.8-turbo", style=None):
    prefix = "system-v1|" + voice if provider == "system" else "minimax-wav-v1|" + model + "|" + voice
    if style is not None:
        prefix += "|style-v1|" + json.dumps(resolved_style(style, provider, model), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prefix + "|" + text).encode()).hexdigest()


def minimax_request_body(text: str, voice: str, model: str, style=None) -> dict:
    """Build the real HTTP T2A contract without credentials or editorial prompts."""
    profile = resolved_style(style, "minimax", model)
    body = {"model": model, "text": text, "stream": False, "language_boost": "Chinese",
            "voice_setting": {"voice_id": voice, "speed": 1, "vol": 1, "pitch": 0},
            "audio_setting": {"sample_rate": 32000, "format": "wav", "channel": 1}, "output_format": "hex"}
    if profile is not None:
        body["voice_setting"].update({k: profile[k] for k in ("speed", "vol", "pitch")})
        if profile.get("emotion"):
            body["voice_setting"]["emotion"] = profile["emotion"]
        if profile.get("pronunciation_dict"):
            body["pronunciation_dict"] = profile["pronunciation_dict"]
        if profile.get("subtitle_enable"):
            body.update(subtitle_enable=True, subtitle_type=profile["subtitle_type"])
    return body


def _minimax_response_metadata(response, *, secret=None) -> dict:
    """Expose only provider-returned billing/tracing fields for this request."""
    metadata = {}
    if not isinstance(response, dict):
        return metadata
    extra = response.get("extra_info")
    usage = extra.get("usage_characters") if isinstance(extra, dict) else None
    if isinstance(usage, int) and not isinstance(usage, bool) and usage >= 0:
        metadata["usage_characters"] = usage
    trace = response.get("trace_id")
    if (isinstance(trace, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", trace)
            and not (secret and secret in trace)):
        metadata["trace_id"] = trace
    return metadata


def cached_audio(text, directory, voice="Huihui", provider="system", model="speech-2.8-turbo", style=None):
    """Read and validate an existing file, without creating an engine or making a request."""
    identity = audio_key(text, voice, provider, model, style)
    try:
        with wave.open(str(Path(directory) / (identity + ".wav")), "rb") as source:
            if source.getnframes() < 100 or source.getframerate() <= 0:
                return None
            duration = source.getnframes() / source.getframerate()
        result = {"audio_id": identity, "url": f"/audio/{identity}.wav", "duration": duration,
                  "provider": provider, "voice": voice, "model": model}
        if provider == "minimax":
            result["cache_hit"] = True
        if style is not None:
            result["style"] = resolved_style(style, provider, model)
        return result
    except (OSError, EOFError, wave.Error):
        return None


def _render_local(text: str, directory: Path, voice: str = "Huihui", style=None, model="speech-2.8-turbo") -> dict:
    # COM and engine are created/destroyed on this single dedicated worker.
    import comtypes
    from RealtimeTTS import SystemEngine, TextToAudioStream

    comtypes.CoInitialize()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        audio_id = audio_key(text, voice, "system", model, style)
        target = directory / (audio_id + ".wav")
        if not target.exists():
            class WindowsWaveEngine(SystemEngine):
                def synthesize(self, text, sentence_count=0):
                    # SAPI already writes WAV. Avoid upstream ffprobe/AIFF inspection.
                    self.engine.save_to_file(text, self.file_path)
                    self.engine.runAndWait()
                    with wave.open(self.file_path, "rb") as source:
                        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 22050):
                            raise RuntimeError("本机语音输出格式不符合单声道16位22050Hz")
                        self.queue.put(source.readframes(source.getnframes()))
                    return True

            engine = WindowsWaveEngine(voice=voice)
            if not any(voice in item.name for item in engine.get_voices()):
                raise RuntimeError("本机未安装所选普通话音色")
            with tempfile.TemporaryDirectory(prefix="voice-man-") as temp:
                engine.file_path = str(Path(temp) / "sapi.wav")
                pending = directory / (audio_id + ".partial.wav")
                stream = TextToAudioStream(engine, tokenizer="rule-based", language="zh", muted=True)
                stream.feed(text)
                stream.play(output_wavfile=str(pending), muted=True, tokenizer="rule-based",
                            tokenize_sentences=lambda value: [value],
                            minimum_sentence_length=1, minimum_first_fragment_length=1,
                            sentence_fragment_delimiters="", fast_sentence_fragment=False)
                with wave.open(str(pending), "rb") as output:
                    if output.getnframes() < 100:
                        raise RuntimeError("语音合成未产生有效音频")
                pending.replace(target)
        with wave.open(str(target), "rb") as result:
            duration = result.getnframes() / result.getframerate()
        result = {"audio_id": audio_id, "url": f"/audio/{audio_id}.wav", "duration": duration,
                  "engine": "RealtimeTTS.SystemEngine / Windows SAPI", "voice": voice}
        if style is not None:
            result["style"] = resolved_style(style, "system", model)
        return result
    finally:
        comtypes.CoUninitialize()


def _render_minimax(text: str, directory: Path, voice: str, model: str, style=None, *, response_observer=None) -> dict:
    key = os.environ.get("MINIMAX_API_KEY", "")
    if not key:
        raise SpeechRenderError("尚未配置MiniMax API Key", tts_diagnostics("configuration", "not_sent"))
    directory.mkdir(parents=True, exist_ok=True)
    profile = resolved_style(style, "minimax", model)
    audio_id = audio_key(text, voice, "minimax", model, style)
    target = directory / (audio_id + ".wav")
    cache_hit = target.exists()
    metadata = {}
    diagnostics = None
    if not cache_hit:
        body = minimax_request_body(text, voice, model, style)
        request = urllib.request.Request(os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1/t2a_v2"),
            data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        http_status = None
        try:
            # Full-script auditions can return minutes of WAV in one response.
            # Keep the existing short-reaction bound; never retry a paid timeout.
            with urllib.request.urlopen(request, timeout=180 if len(text) > 1000 else 45) as response:
                http_status = getattr(response, "status", None) or 200
                try:
                    result = json.load(response)
                except Exception as error:
                    safe = tts_diagnostics("json_decode", "outcome_unknown", response_received=True,
                        exception=error, http_status=http_status)
                    _observe_tts_response(response_observer, safe)
                    raise SpeechRenderError("MiniMax响应无法解析，请先核实，不能自动重复付费", safe) from None
        except urllib.error.HTTPError as error:
            # Do not persist the body, reason, request, URL or any headers.
            try:
                rejected = json.loads(error.read(65536))
            except Exception:
                rejected = {}
            finally:
                error.close()
            base = rejected.get("base_resp", {}) if isinstance(rejected, dict) else {}
            code = base.get("status_code") if isinstance(base, dict) else None
            code = code if isinstance(code, int) and not isinstance(code, bool) else None
            metadata = _minimax_response_metadata(rejected, secret=key)
            safe = tts_diagnostics("http_response", _minimax_error_outcome(code, error.code),
                response_received=True, exception=error, http_status=error.code, provider_code=code, metadata=metadata)
            _observe_tts_response(response_observer, safe)
            raise SpeechRenderError("MiniMax返回HTTP错误，请检查安全诊断信息", safe) from None
        except SpeechRenderError:
            raise
        except Exception as error:
            safe = tts_diagnostics("transport", "outcome_unknown", exception=error, http_status=http_status)
            raise SpeechRenderError("MiniMax连接或读取结果未知，请先核实，不能自动重复付费", safe) from None
        base = result.get("base_resp", {}) if isinstance(result, dict) else None
        code = base.get("status_code") if isinstance(base, dict) else None
        valid_code = code is None or (isinstance(code, int) and not isinstance(code, bool))
        metadata = _minimax_response_metadata(result, secret=key)
        diagnostics = tts_diagnostics("provider_response", "response_received", response_received=True,
            http_status=http_status, provider_code=code, metadata=metadata)
        if not isinstance(result, dict) or not isinstance(base, dict) or not valid_code:
            diagnostics["outcome"] = "outcome_unknown"
            _observe_tts_response(response_observer, diagnostics)
            raise SpeechRenderError("MiniMax响应结构无法核实，不能自动重复付费", diagnostics) from None
        if code:
            diagnostics["outcome"] = _minimax_error_outcome(code, http_status)
            _observe_tts_response(response_observer, diagnostics)
            raise SpeechRenderError("MiniMax未返回可用语音，请检查安全诊断信息", diagnostics) from None
        # Account for an actual provider response before decoding/validating its
        # audio. Known usage must survive a later corrupt or missing WAV.
        _observe_tts_response(response_observer, diagnostics)
        try:
            raw = bytes.fromhex(result["data"]["audio"])
        except Exception as error:
            safe = tts_diagnostics("audio_decode", "outcome_unknown", response_received=True,
                exception=error, http_status=http_status, provider_code=code, metadata=metadata)
            raise SpeechRenderError("MiniMax未返回可解析的语音，不能自动重复付费", safe) from None
        try:
            with wave.open(io.BytesIO(raw), "rb") as audio:
                if audio.getnframes() < 100:
                    raise ValueError()
        except Exception as error:
            safe = tts_diagnostics("audio_validate", "outcome_unknown", response_received=True,
                exception=error, http_status=http_status, provider_code=code, metadata=metadata)
            raise SpeechRenderError("MiniMax返回的音频格式无效，未加入播出；请先核实", safe) from None
        try:
            pending = directory / (audio_id + ".partial.wav")
            pending.write_bytes(raw)
            pending.replace(target)
        except Exception as error:
            safe = tts_diagnostics("audio_write", "outcome_unknown", response_received=True,
                exception=error, http_status=http_status, provider_code=code, metadata=metadata)
            raise SpeechRenderError("MiniMax语音已返回但本地保存未完成，不能自动重复付费", safe) from None
        if profile and profile.get("subtitle_enable"):
            # Save audio first. Subtitle failures must never resend a paid TTS call.
            from .audio_timeline import archive_provider_subtitles
            archive_provider_subtitles(result.get("data", {}), directory, audio_id)
    with wave.open(str(target), "rb") as source:
        duration = source.getnframes() / source.getframerate()
    result = {"audio_id": audio_id, "url": f"/audio/{audio_id}.wav", "duration": duration,
              "engine": "MiniMax HTTP T2A", "provider": "minimax", "voice": voice, "model": model,
              "cache_hit": cache_hit, **metadata}
    if profile is not None:
        result["style"] = profile
    if diagnostics is not None:
        result["response_diagnostics"] = diagnostics
    if profile and profile.get("subtitle_enable"):
        from .audio_timeline import subtitle_metadata
        result.update(subtitle_metadata(directory, audio_id))
    return result


def render(text: str, directory: Path, voice: str = "Huihui", provider: str = "system", model: str = "speech-2.8-turbo", style=None, *, response_observer=None) -> dict:
    if provider == "minimax":
        return _render_minimax(text, directory, voice, model, style, response_observer=response_observer)
    if provider != "system":
        raise RuntimeError("不支持的声音服务")
    directory.mkdir(parents=True, exist_ok=True)
    audio_id = audio_key(text, voice, provider, model, style)
    target = directory / (audio_id + ".wav")
    if target.exists():
        with wave.open(str(target), "rb") as source:
            result = {"audio_id": audio_id, "url": f"/audio/{audio_id}.wav",
                    "duration": source.getnframes() / source.getframerate(),
                    "engine": "RealtimeTTS.SystemEngine / Windows SAPI", "voice": voice}
            if style is not None:
                result["style"] = resolved_style(style, "system", model)
            return result
    # SAPI event loops can hang on ASGI worker threads. A bounded, hidden
    # subprocess gives each synthesis a main-thread COM event loop.
    payload = json.dumps({"text": text, "directory": str(directory.resolve()), "voice": voice, "style": style, "model": model})
    proc = subprocess.run([sys.executable, "-m", "app.speech"], input=payload,
                          encoding="utf-8", capture_output=True, timeout=45,
                          cwd=Path(__file__).resolve().parents[1],
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0 or not target.exists():
        raise RuntimeError("本机语音合成失败，请检查音色及引擎状态")
    return json.loads(proc.stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    item = json.loads(sys.stdin.read())
    print(json.dumps(_render_local(item["text"], Path(item["directory"]), item["voice"], item.get("style"), item.get("model", "speech-2.8-turbo"))))
