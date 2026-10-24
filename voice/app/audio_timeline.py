"""Provider subtitle archive and exact text-to-word boundary alignment.

Never infer timestamps from text length. Audio can be auditioned even when
alignment fails; such candidates cannot enter the continuous broadcast path.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
from pathlib import Path
import socket
import unicodedata
import urllib.request
import urllib.error
from urllib.parse import urlsplit, urljoin


def normalized(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text) if c.isalnum())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _public_json(url):
    """Fetch public provider metadata only, never forward API credentials."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(4):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname or parsed.port not in {None, 443}:
            raise ValueError("invalid subtitle URL")
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("non-public subtitle host")
        try:
            with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=25) as response:
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise ValueError("subtitle too large")
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location")
            status = error.code
            error.close()
            if status in {301, 302, 303, 307, 308} and location:
                url = urljoin(url, location)
                continue
            raise ValueError("subtitle download failed") from None
    raise ValueError("too many subtitle redirects")


def archive_provider_subtitles(data, directory, identity):
    path = Path(directory) / (identity + ".subtitles.json")
    try:
        source = data.get("subtitle") or data.get("subtitles") or data.get("subtitle_file")
        if isinstance(source, str):
            source = _public_json(source)
        if not isinstance(source, (list, dict)) or not source:
            raise ValueError("missing subtitles")
        body = {"schema_version": "CONTINUOUS-SUBTITLES-1", "status": "received", "provider_subtitles": source}
    except Exception:
        body = {"schema_version": "CONTINUOUS-SUBTITLES-1", "status": "unavailable",
                "message": "音频已保存，字幕未取得；未重复调用语音服务。"}
    pending = path.with_suffix(".partial.json")
    pending.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    pending.replace(path)


def subtitle_metadata(directory, identity):
    path = Path(directory) / (identity + ".subtitles.json")
    try:
        raw = path.read_bytes()
        body = json.loads(raw)
        return {"subtitle_status": body["status"], "subtitle_file_hash": "sha256:" + hashlib.sha256(raw).hexdigest()}
    except (OSError, ValueError, KeyError):
        return {"subtitle_status": "unavailable"}


def align_subtitles(source, sentences, duration):
    """Return only word/segment boundaries supported by an exact text match.

    MiniMax time_begin/time_end are milliseconds. Accept start/end in seconds
    for normalized word records; never guess a global scale from audio length.
    """
    if isinstance(source, dict):
        source = source.get("subtitles", source.get("segments", source.get("data")))
    if not isinstance(source, list) or not source:
        raise ValueError("missing subtitle segments")
    words = []
    previous_source_span = None
    for segment in source:
        units = segment.get("timestamped_words") or segment.get("words") or [segment]
        for unit in units:
            text = unit.get("word", unit.get("text", ""))
            if not isinstance(text, str):
                raise ValueError("invalid subtitle text")
            if "time_begin" in unit:
                begin, end = unit["time_begin"] / 1000, unit["time_end"] / 1000
            else:
                begin, end = unit.get("start"), unit.get("end")
            if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (begin, end))
                    or not 0 <= begin <= end <= duration + .08):
                raise ValueError("subtitle timing out of range")
            norm = normalized(text)
            if norm:
                span = (unit.get("word_begin"), unit.get("word_end"), text)
                # Pronunciation dictionaries can expose several phonemes for
                # the SAME source word/span. Merge timing, never duplicate text.
                if (words and isinstance(span[0], int) and isinstance(span[1], int)
                        and span == previous_source_span):
                    words[-1]["end"] = min(end, duration)
                else:
                    words.append({"text": norm, "start": begin, "end": min(end, duration)})
                previous_source_span = span
            elif words:
                # Keep punctuation's natural release in the preceding sentence.
                words[-1]["end"] = min(end, duration)
                previous_source_span = None
    for left, right in zip(words, words[1:]):
        if right["start"] < left["start"] or right["end"] < left["end"]:
            raise ValueError("subtitle order invalid")
    # Continuous preview synthesis sends the immutable module ``text`` field.
    # ``spoken_text`` is a separate pronunciation projection and can expand a
    # numeric spelling (for example 23.58 -> 二十三点五八). Provider subtitles
    # describe the text actually sent, so bind the timeline to that same field.
    # Legacy callers without ``text`` keep the original spoken_text behaviour.
    target = "".join(normalized(s.get("text", s["spoken_text"])) for s in sentences)
    actual = "".join(w["text"] for w in words)
    if actual != target:
        raise ValueError("subtitle text does not match approved script")
    ends, cursor = {}, 0
    for word in words:
        cursor += len(word["text"])
        ends[cursor] = word["end"]
    result, cursor, previous = [], 0, 0.0
    for sentence in sentences:
        cursor += len(normalized(sentence.get("text", sentence["spoken_text"])))
        if cursor not in ends:
            raise ValueError("sentence boundary falls inside a subtitle word")
        end = ends[cursor]
        if end <= previous:
            raise ValueError("empty sentence timing")
        result.append({"sentence_id": sentence["id"], "start": previous, "end": end})
        previous = end
    # End the final playback range at the file end, including natural release.
    result[-1]["end"] = duration
    return result
