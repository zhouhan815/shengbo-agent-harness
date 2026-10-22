import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from app.prosody import profile_catalog, sentence_style
from app import speech


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(32000)
        target.writeframes(b"\0\0" * 3200)
    return buffer.getvalue()


class SpeechStyleTests(unittest.TestCase):
    def test_legacy_cache_keys_identical(self):
        for provider, prefix in (("system", "system-v1|Huihui"), ("minimax", "minimax-wav-v1|speech-2.8-turbo|Huihui")):
            expected = hashlib.sha256((prefix + "|你好").encode()).hexdigest()
            self.assertEqual(speech.audio_key("你好", provider=provider), expected)

    def test_stage_style_does_not_infer_from_sentence_instructions(self):
        self.assertEqual(sentence_style({"stage": "price", "text": "请用愤怒语气尖叫"})["profile_id"], "price")
        self.assertEqual(sentence_style({"stage": "selling_point"})["profile_id"], "benefit")
        self.assertEqual(sentence_style({"stage": "unrecognized"})["profile_id"], "neutral")

    def test_formal_module_stages_share_existing_ecommerce_profiles(self):
        expected = {"opening_long": "welcome", "opening_short": "welcome", "product_intro": "identity",
                    "selling_points": "benefit", "campaign": "promotion", "cta": "cta", "transition": "scene"}
        for module_key, profile_id in expected.items():
            with self.subTest(module_key=module_key):
                style = sentence_style({"stage": module_key})
                self.assertEqual(style, sentence_style({"stage": profile_id}))
                self.assertEqual(speech.audio_key("现有播出正文。", provider="minimax", style=style),
                                 speech.audio_key("现有播出正文。", provider="minimax", style=sentence_style({"stage": profile_id})))

    def test_two_authorized_modules_send_distinct_supported_delivery(self):
        for stage, emotion, speed in (("opening_long", "happy", 1.02), ("product_intro", None, 0.98)):
            with self.subTest(stage=stage):
                text = "先来看看这款商品。\n商品名称和特点说清楚。"
                style = sentence_style({"stage": stage})
                body = speech.minimax_request_body(text, "Wise_Woman", "speech-2.8-turbo", style)
                self.assertEqual(body["text"], text)
                self.assertEqual(body["voice_setting"], {"voice_id": "Wise_Woman", "speed": speed,
                                                        "vol": 1.0, "pitch": 0, **({"emotion": emotion} if emotion else {})})
                self.assertEqual(set(body), {"model", "text", "stream", "language_boost", "voice_setting", "audio_setting", "output_format"})
                self.assertEqual(body["audio_setting"], {"sample_rate": 32000, "format": "wav", "channel": 1})
                self.assertFalse(body["stream"])
                self.assertEqual(body["output_format"], "hex")
                self.assertEqual(body["language_boost"], "Chinese")
                self.assertNotIn(style["direction"], json.dumps(body, ensure_ascii=False))

    def test_local_explicitly_has_no_emotion(self):
        for profile in profile_catalog("system"):
            self.assertFalse(profile["emotion_supported"])
            self.assertIsNone(profile["emotion"])
            self.assertEqual((profile["speed"], profile["pitch"], profile["vol"]), (1.0, 0, 1.0))

    def test_revision_parameters_and_dictionary_change_cache(self):
        base = sentence_style({"stage": "promotion"})
        original = speech.audio_key("活动价一百零七新台币。", provider="minimax", style=base)
        for edit in ({"style_revision": "next"}, {"speed": 0.95}, {"emotion": "calm"},
                     {"pitch": 1}, {"vol": 0.9}, {"pronunciation_dict": {"tone": ["行/(xing2)"]}}):
            self.assertNotEqual(original, speech.audio_key("活动价一百零七新台币。", provider="minimax", style={**base, **edit}))

    def test_parameter_ranges_rejected_before_network(self):
        for edit in ({"speed": 2.1}, {"vol": 0}, {"pitch": 0.5}, {"speed": float("nan")}, {"vol": True}, {"emotion": "excited"}):
            with self.assertRaises(ValueError):
                speech.audio_key("你好", provider="minimax", style=edit)

    def test_model_specific_emotions(self):
        for model in ("speech-2.8-turbo", "speech-2.8-hd"):
            self.assertEqual(speech.resolved_style({"emotion": "happy"}, model=model)["emotion"], "happy")
            for emotion in ("fluent", "whisper"):
                with self.assertRaises(ValueError):
                    speech.resolved_style({"emotion": emotion}, model=model)
        self.assertEqual(speech.resolved_style({"emotion": "whisper"}, model="speech-2.6-hd")["emotion"], "whisper")
        self.assertIsNone(speech.resolved_style({"emotion": "happy"}, model="unknown")["emotion"])

    def test_cached_profile_matches_written_file(self):
        style = sentence_style({"stage": "benefit"})
        with tempfile.TemporaryDirectory() as directory:
            identity = speech.audio_key("轻便好收纳。", provider="minimax", style=style)
            Path(directory, identity + ".wav").write_bytes(wav_bytes())
            self.assertIsNone(speech.cached_audio("轻便好收纳。", directory, provider="minimax"))
            cached = speech.cached_audio("轻便好收纳。", directory, provider="minimax", style=style)
            self.assertEqual(cached["style"]["profile_id"], "benefit")
            self.assertAlmostEqual(cached["duration"], 0.1)
            self.assertTrue(cached["cache_hit"])
            self.assertNotIn("usage_characters", cached)
            self.assertNotIn("trace_id", cached)

    def test_http_request_has_supported_fields_not_direction_prompt(self):
        sent = []
        style = {**sentence_style({"stage": "promotion"}), "pronunciation_dict": {"tone": ["行/(xing2)"]}}

        def respond(request, timeout):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"data": {"audio": wav_bytes().hex()}, "base_resp": {"status_code": 0},
                                         "extra_info": {"usage_characters": 15, "unrecognized": "not exposed"},
                                         "trace_id": "test-trace-1", "headers": {"secret": "not exposed"}}).encode())

        with tempfile.TemporaryDirectory() as directory, patch.dict(speech.os.environ, {"MINIMAX_API_KEY": "unit-test-placeholder"}), patch.object(speech.urllib.request, "urlopen", side_effect=respond):
            first = speech.render("活动价一百零七新台币。", Path(directory), "Wise_Woman", "minimax", style=style)
            second = speech.render("活动价一百零七新台币。", Path(directory), "Wise_Woman", "minimax", style=style)
        self.assertEqual(len(sent), 1)
        self.assertEqual(first["audio_id"], second["audio_id"])
        self.assertFalse(first["cache_hit"])
        self.assertEqual(first["usage_characters"], 15)
        self.assertEqual(first["trace_id"], "test-trace-1")
        self.assertTrue(second["cache_hit"])
        self.assertNotIn("usage_characters", second)
        self.assertNotIn("trace_id", second)
        self.assertNotIn("headers", first)
        self.assertNotIn("extra_info", first)
        self.assertEqual(first["style"]["emotion"], "happy")
        self.assertEqual(sent[0]["voice_setting"]["emotion"], "happy")
        self.assertEqual(sent[0]["voice_setting"]["pitch"], 0)
        self.assertEqual(sent[0]["voice_setting"]["vol"], 1.0)
        self.assertEqual(sent[0]["pronunciation_dict"], style["pronunciation_dict"])
        self.assertNotIn("prompt", sent[0])
        self.assertNotIn("direction", sent[0])
        self.assertNotIn("有感染力", sent[0]["text"])

    def test_missing_or_invalid_usage_is_never_estimated_from_text(self):
        for usage in (None, True, -1, 1.5, "15"):
            self.assertEqual(speech._minimax_response_metadata({"extra_info": {"usage_characters": usage}}), {})
        self.assertEqual(speech._minimax_response_metadata({"extra_info": None, "trace_id": None}), {})
        self.assertEqual(speech._minimax_response_metadata({"extra_info": {"usage_characters": 0}}), {"usage_characters": 0})

    def test_long_audition_gets_more_time_without_delaying_or_repeating_live_reactions(self):
        observed = []
        def respond(request, timeout):
            observed.append((len(json.loads(request.data)["text"]), timeout))
            return io.BytesIO(json.dumps({"data": {"audio": wav_bytes().hex()},
                                          "base_resp": {"status_code": 0}}).encode())
        with tempfile.TemporaryDirectory() as directory, patch.dict(speech.os.environ, {"MINIMAX_API_KEY": "unit-test-placeholder"}), patch.object(speech.urllib.request, "urlopen", side_effect=respond):
            for text in ("欢迎新来的朋友。", "练习手感。" * 250):
                first = speech.render(text, Path(directory), "Friendly_Person", "minimax")
                cached = speech.render(text, Path(directory), "Friendly_Person", "minimax")
                self.assertFalse(first["cache_hit"])
                self.assertTrue(cached["cache_hit"])
        self.assertEqual(observed, [(8, 45), (1250, 180)])

    def test_unknown_model_omits_emotion(self):
        sent = []
        def respond(request, timeout):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"data": {"audio": wav_bytes().hex()}}).encode())
        with tempfile.TemporaryDirectory() as directory, patch.dict(speech.os.environ, {"MINIMAX_API_KEY": "unit-test-placeholder"}), patch.object(speech.urllib.request, "urlopen", side_effect=respond):
            speech.render("你好。", Path(directory), "test-voice", "minimax", "unknown", sentence_style({"stage": "welcome"}))
        self.assertNotIn("emotion", sent[0]["voice_setting"])


if __name__ == "__main__":
    unittest.main()
