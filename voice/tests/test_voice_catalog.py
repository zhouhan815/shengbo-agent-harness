"""Verified voice selection chain; isolated storage and no provider calls."""
import asyncio
from copy import deepcopy

from fastapi import HTTPException
import pytest

from app.voice_catalog import supported_voice, voice_catalog
from shengbo_contracts.business import VoiceSelection


NEW_VOICES = {
    "Chinese (Mandarin)_Wise_Women": "成熟女声",
    "Chinese (Mandarin)_Sweet_Lady": "甜美女声",
    "Chinese (Mandarin)_Reliable_Executive": "沉稳男声",
}
LEGACY_VOICES = {"Wise_Woman", "Deep_Voice_Man", "Friendly_Person", "sweet_girl"}


@pytest.fixture
def server_module(monkeypatch, tmp_path):
    monkeypatch.setenv("SHENGBO_SKIP_DOTENV", "1")
    monkeypatch.setenv("SHENGBO_DISABLE_AGENT_BRIDGE", "1")
    monkeypatch.setenv("SHENGBO_RUNTIME", str(tmp_path))
    from app import server
    monkeypatch.setattr(server, "RUNTIME", tmp_path)
    return server


def test_catalog_is_finite_keeps_legacy_and_returns_independent_metadata():
    catalog = voice_catalog()
    assert set(catalog["voices"]["minimax"]) == LEGACY_VOICES | set(NEW_VOICES)
    assert {key: catalog["voice_details"]["minimax"][key]["name"] for key in NEW_VOICES} == NEW_VOICES
    catalog["voices"]["minimax"].clear()
    catalog["voice_details"]["minimax"][next(iter(NEW_VOICES))]["name"] = "changed"
    assert len(voice_catalog()["voices"]["minimax"]) == 7
    assert voice_catalog()["voice_details"]["minimax"][next(iter(NEW_VOICES))]["name"] == "成熟女声"


def test_new_legacy_and_unlisted_voice_validation(server_module):
    for voice in LEGACY_VOICES | set(NEW_VOICES):
        selection = VoiceSelection(provider="minimax", voice=voice)
        assert server_module.voice_options({"tts_provider": selection.provider, "tts_voice": selection.voice}) == selection.model_dump()
    for provider, voice in (("minimax", "unverified-voice"), ("system", next(iter(NEW_VOICES))), ("minimax", "Huihui")):
        assert not supported_voice(provider, voice, "speech-2.8-turbo")
        with pytest.raises(HTTPException):
            server_module.voice_options({"tts_provider": provider, "tts_voice": voice})


def test_real_query_and_legacy_library_share_catalog(server_module, monkeypatch, tmp_path):
    from app.prelive import PreliveService
    from app.prelive_bridge import ExistingVoiceAdapter
    service = PreliveService(tmp_path, adapter=ExistingVoiceAdapter(server_module))

    async def query():
        try:
            return await service.query("voice_options", {})
        finally:
            await service.close()

    result = asyncio.run(query())
    monkeypatch.setattr(server_module, "all_of", lambda _: [])
    monkeypatch.setattr(server_module, "settings", lambda *_: {})
    legacy = asyncio.run(server_module.library())["service"]["voice_options"]
    for name in ("voices", "voice_details", "models", "default"):
        assert result[name] == legacy[name] == voice_catalog()[name]
    # VoiceSelection intentionally has no duplicated voice ID enum; the final
    # application allowlist above remains the validation authority.
    assert "enum" not in deepcopy(VoiceSelection.model_json_schema())["properties"]["voice"]
