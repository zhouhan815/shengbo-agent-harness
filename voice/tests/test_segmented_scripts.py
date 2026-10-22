import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import server


class SegmentedScriptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = patch.object(server, "RUNTIME", Path(self.temp.name))
        self.runtime.start()
        self.product = server.put("product", server.ProductInput(name="测试拖鞋",price=19,selling_points="软弹").record("p1"))
        self.script = server.structure_script(self.product,{"blocks":[
            {"stage":"welcome","focus":"欢迎","sentences":[{"display_text":"欢迎来到直播间。","kind":"engagement","fact_ids":[]}]},
            {"stage":"benefit","focus":"软弹","sentences":[{"display_text":"这款拖鞋有软弹特点。","fact_ids":["SELLING"]}]}]},"test","1")
        self.script.update(room_id="default",version_number=2)
        server.put("script",self.script)

    def tearDown(self):
        self.runtime.stop()
        self.temp.cleanup()

    async def test_edit_preserves_semantic_sections_and_creates_snapshot(self):
        payload = deepcopy(self.script)
        payload["sentences"][1].update(text="这双拖鞋主打软弹。",spoken_text="这双拖鞋主打软弹。")
        edited = await server.edit_script(self.script["id"],payload)
        self.assertEqual([b["stage"] for b in edited["blocks"]],["welcome","benefit"])
        self.assertEqual(edited["blocks"][1]["focus"],"软弹")
        self.assertNotEqual(edited["id"],self.script["id"])
        self.assertEqual(edited["version_number"],2)
        self.assertEqual(edited["room_id"],"default")
        self.assertEqual(edited["sentences"][1]["stage"],"benefit")
        self.assertEqual(server.get("script",self.script["id"])["sentences"][1]["text"],"这款拖鞋有软弹特点。")

    async def test_reject_orphan_sentence_instead_of_silently_dropping(self):
        payload = deepcopy(self.script)
        payload["sentences"][1]["block_id"]="unknown"
        with self.assertRaises(server.HTTPException):
            await server.edit_script(self.script["id"],payload)

    async def test_view_separates_reading_directions_from_spoken_text(self):
        detail = await server.get_script(self.script["id"])
        self.assertEqual(len(detail["paragraphs"]),2)
        self.assertEqual(detail["blocks"][0]["speech_style"]["emotion"],"happy")
        self.assertNotIn("方向说明",detail["sentences"][0]["spoken_text"])
        local = await server.tts_profiles("system")
        self.assertTrue(all(p["emotion"] is None for p in local["profiles"]))

    async def test_preview_uses_server_profile_and_ignores_untrusted_style(self):
        with patch.object(server,"render",return_value={"url":"test"}) as render:
            await server.preview({"text":"这次活动欢迎了解。","stage":"promotion","speech_style":{"vol":10,"pitch":12}})
        style = render.call_args.args[-1]
        self.assertEqual(style["vol"],1)
        self.assertEqual(style["pitch"],0)

    async def test_targeted_rewrite_receives_actual_draft_sentences(self):
        product = {**self.product,"selling_points":"EVA材质，软弹。"}
        fixed = deepcopy(self.script)
        fixed["sentences"][1]["text"]="这双拖鞋采用EVA材质，主打软弹。"
        with patch.object(server,"_generate_remote_once",side_effect=[[self.script],[fixed]]) as generate:
            server.generate_remote(product,1)
        feedback = generate.call_args.args[3]
        self.assertEqual(feedback["drafts_to_revise"][0]["blocks"][1]["sentences"][0]["display_text"],"这款拖鞋有软弹特点。")


if __name__ == "__main__":
    unittest.main()
