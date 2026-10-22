"""Supported voices for validation and UI; additions require verified provider IDs."""
from copy import deepcopy


# The three Mandarin IDs were returned by the configured account's read-only
# get_voice inventory on 2026-10-21. Legacy IDs remain for saved selections.
VOICE_CATALOG = {
    "system": [
        {"id": "Huihui", "name": "慧慧 · 本机普通话", "description": "Windows 本机普通话音色。"},
    ],
    "minimax": [
        {"id": "Wise_Woman", "name": "智慧女声 · 原有音色", "description": "保留已有声音设置。"},
        {"id": "Deep_Voice_Man", "name": "沉稳男声 · 原有音色", "description": "保留已有声音设置。"},
        {"id": "Friendly_Person", "name": "亲切声线 · 原有音色", "description": "保留已有声音设置。"},
        {"id": "sweet_girl", "name": "甜美女声 · 原有音色", "description": "保留已有声音设置。"},
        {"id": "Chinese (Mandarin)_Wise_Women", "name": "成熟女声", "description": "标准普通话，抒情而睿智的中年女声。",
         "provider_name": "Wise Women"},
        {"id": "Chinese (Mandarin)_Sweet_Lady", "name": "甜美女声", "description": "标准普通话，温柔甜美的成年女声。",
         "provider_name": "Sweet Lady"},
        {"id": "Chinese (Mandarin)_Reliable_Executive", "name": "沉稳男声", "description": "标准普通话，稳重可靠的中年男声。",
         "provider_name": "Reliable Executive"},
    ],
}
TTS_MODELS = ("speech-2.8-turbo", "speech-2.8-hd")


def voice_catalog():
    """Return fresh wire values; ID lists stay compatible with existing clients."""
    return {"providers": list(VOICE_CATALOG),
            "voices": {provider: [item["id"] for item in items] for provider, items in VOICE_CATALOG.items()},
            "voice_details": {provider: {item["id"]: deepcopy(item) for item in items} for provider, items in VOICE_CATALOG.items()},
            "models": {"system": [TTS_MODELS[0]], "minimax": list(TTS_MODELS)},
            "default": {"provider": "system", "voice": "Huihui", "model": TTS_MODELS[0]},
            "preview_requires_approval": True}


def supported_voice(provider, voice, model):
    return (provider in VOICE_CATALOG and model in TTS_MODELS
            and any(item["id"] == voice for item in VOICE_CATALOG[provider]))
