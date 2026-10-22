"""Real service/grants/receipts with temporary DB and one silent fake render."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
import wave

from shengbo_contracts.common import CommandReceipt, MutationContext, Principal, WorkScope
from app.prelive.service import PreliveService, Product
from app.prelive.storage import BusinessError, digest


class ScriptAudioPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.calls, self.fail = [], False

        def render(text, directory, voice, provider, model, style):
            self.calls.append((text, voice, provider, model, style))
            if self.fail:
                raise RuntimeError("fake uncertain outcome; DO_NOT_LOG_PROVIDER_SECRET")
            directory.mkdir(exist_ok=True, parents=True)
            with wave.open(str(directory / "scoped.wav"), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(32000)
                wav.writeframes(b"\0\0" * 3200)
            return {"audio_id": "scoped", "url": "/audio/scoped.wav", "duration": .1,
                "provider": provider, "voice": voice, "model": model, "style": style,
                "cache_hit": False, "usage_characters": len(text), "trace_id": "fake-trace"}

        self.server = SimpleNamespace(RUNTIME=self.runtime, POOL=None, render=render,
            voice_options=lambda *_: None, sentence_style=lambda *_: {})
        self.service = PreliveService(self.runtime, SimpleNamespace(server=self.server))
        self.human = Principal(kind="human", actor_id="human-fixture")
        self.agent = Principal(kind="agent", actor_id="agent:preview-run", run_id="preview-run")
        self.controller = Principal(kind="controller", actor_id="controller-fixture")
        self.seq = 0
        product = (await self.command("save_product", {"schema_hash": self.service.schema()["schema_hash"],
            "patches": [], "fields": Product(name="隔离试听商品", price=99).model_dump()}))["data"]["product_ref"]
        room = (await self.command("create_room", {"name": "隔离试听房间"}))["data"]["room_ref"]
        self.prep = (await self.command("create_preparation", {"room_id": room["id"],
            "room_revision": room["revision"]}))["data"]["preparation_ref"]
        self.scope = WorkScope(kind="preparation", preparation_id=self.prep["id"])
        with self.service.store.transaction(write=True) as c:
            self.module_refs = {}
            for key, texts in {"opening_long": ["第一句开场。", "第二句开场。"],
                    "product_intro": ["第一句商品介绍。", "第二句商品介绍。"], "selling_points": ["禁止附带的卖点。"]}.items():
                self.module_refs[key] = self.service.store.artifact(c, "module", {"key": key,
                    "sentences": [{"id": key + str(i), "text": text} for i, text in enumerate(texts)]})
            self.script_ref = self.service.store.artifact(c, "script_revision", {
                "preparation_id": self.prep["id"], "product_ref": product,
                "product_id": product["id"], "variant_id": "first", "script_revision": 17,
                "version_number": 1, "module_refs": self.module_refs})
            prep = self.service._prep(c, self.prep["id"])
            body = {**prep["body"], "script_refs": [self.script_ref], "selected_product_refs": [product]}
            self.prep = self.service._save_prep(c, prep, body)["preparation_ref"]
        self.grant = (await self.command("issue_grant", {"run_id": self.agent.run_id,
            "scope": self.scope.model_dump(), "capabilities": ["preview_script_audio"],
            "target_constraints": [{"id": product["id"]}, {"module_key": "opening_long"},
                                   {"module_key": "product_intro"}]}, principal=self.controller))["data"]
        self.style = {"profile_id": "ecommerce_continuous_preview", "emotion": "happy", "speed": 1.02, "pitch": 0, "vol": 1}
        self.text = "第一句开场。第二句开场。\n第一句商品介绍。第二句商品介绍。"
        self.approval_args = {"preparation_id": self.prep["id"], "run_id": self.agent.run_id,
            "script_ref": self.script_ref, "module_keys": ["opening_long", "product_intro"],
            "module_refs": [self.module_refs[key] for key in ("opening_long", "product_intro")],
            "ordered_text_hash": digest(self.text), "tts_selection": {"provider": "minimax",
                "voice": "Wise_Woman", "model": "speech-2.8-turbo"}, "style": self.style,
            "authorization_quote": "只试听第一版长开场和商品介绍，连贯合成一段。"}

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def command(self, key, args, *, principal=None, operation=None):
        self.seq += 1
        principal = principal or self.human
        mutation = MutationContext(operation_id=operation or "fixture-op-" + str(self.seq),
            grant_id=self.grant["grant_id"] if principal.kind == "agent" else None,
            expected_write_generation=self.grant["write_generation"] if principal.kind == "agent" else None)
        return await self.service.execute_command(key, args, mutation, principal,
            scope=getattr(self, "scope", None) if key != "issue_grant" else None)

    async def authorize(self):
        receipt = await self.command("approve_script_audio_preview", self.approval_args,
            operation="explicit-user-consent")
        self.approved = receipt["data"]
        return {"preparation_id": self.prep["id"], "preview_approval_ref": self.approved["preview_approval_ref"],
            "binding_hash": self.approved["binding_hash"]}

    async def test_exact_one_continuous_call_scope_and_durable_receipts(self):
        before = await self.service.query("read_preparation", {"preparation_id": self.prep["id"]})
        args = await self.authorize()
        self.assertFalse(self.calls)
        discovered = await self.service.query("read_script_audio_preview", {"preparation_id": self.prep["id"],
            "operation_id": "explicit-user-consent"}, principal=self.agent, scope=self.scope)
        self.assertEqual(discovered["ref"], args["preview_approval_ref"])
        first = await self.command("preview_script_audio", args, principal=self.agent, operation="audition-1")
        CommandReceipt.model_validate(first)
        self.assertEqual(self.calls, [(self.text, "Wise_Woman", "minimax", "speech-2.8-turbo", self.style)])
        self.assertEqual(first["data"]["module_keys"], ["opening_long", "product_intro"])
        self.assertEqual(first["data"]["usage_characters"], len(self.text))
        self.assertEqual(first["data"]["trace_id"], "fake-trace")
        self.assertFalse(first["data"]["whole_package_approval"])
        replay = await self.command("preview_script_audio", args, principal=self.agent, operation="audition-1")
        self.assertEqual(replay, first)
        cached = await self.command("preview_script_audio", args, principal=self.agent, operation="audition-2")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(cached["data"]["cache_hit"])
        self.assertNotIn("usage_characters", cached["data"])
        self.assertEqual(cached["data"]["source_usage_characters"], len(self.text))
        observed = await self.service.query("read_operation_receipt", {"operation_id": "audition-2"}, principal=self.agent, scope=self.scope)
        self.assertEqual(observed["receipt"], cached)
        self.assertEqual(await self.service.query("read_preparation", {"preparation_id": self.prep["id"]}), before)
        listing = await self.service.query("list_script_audio_previews", {"preparation_id": self.prep["id"], "ref": self.script_ref})
        self.assertEqual(listing["total"], 2)
        self.assertTrue(all(item["playable"] for item in listing["items"]))
        (self.runtime / "audio/scoped.wav").unlink()
        unavailable = await self.service.query("read_operation_receipt", {"operation_id": "audition-1"}, principal=self.agent, scope=self.scope)
        self.assertEqual(unavailable["state"], "audio_unavailable")
        self.assertEqual(len(self.calls), 1)

    async def test_agent_cannot_self_approve_or_expand_or_change_binding(self):
        with self.assertRaises(BusinessError) as error:
            await self.command("approve_script_audio_preview", self.approval_args, principal=self.agent)
        self.assertEqual(error.exception.code, "HUMAN_REQUIRED")
        args = await self.authorize()
        with self.assertRaises(BusinessError) as error:
            await self.command("preview_script_audio", {**args, "binding_hash": "sha256:" + "0" * 64}, principal=self.agent)
        self.assertEqual(error.exception.code, "PREVIEW_APPROVAL_BINDING_MISMATCH")
        expanded = {**self.approval_args, "module_keys": ["selling_points"],
            "module_refs": [self.module_refs["selling_points"]], "ordered_text_hash": digest("禁止附带的卖点。")}
        approval = (await self.command("approve_script_audio_preview", expanded))["data"]
        with self.assertRaises(BusinessError) as error:
            await self.command("preview_script_audio", {**args, "preview_approval_ref": approval["preview_approval_ref"],
                "binding_hash": approval["binding_hash"]}, principal=self.agent)
        self.assertEqual(error.exception.code, "SCOPE_CONFLICT")
        self.assertFalse(self.calls)

    async def test_unknown_result_never_reissues_after_service_restart(self):
        args = await self.authorize()
        self.fail = True
        with self.assertRaises(BusinessError) as error:
            await self.command("preview_script_audio", args, principal=self.agent, operation="uncertain-1")
        self.assertEqual(error.exception.code, "PREVIEW_OUTCOME_UNKNOWN")
        self.service = PreliveService(self.runtime, SimpleNamespace(server=self.server))
        self.fail = False
        with self.assertRaises(BusinessError) as error:
            await self.command("preview_script_audio", args, principal=self.agent, operation="uncertain-2")
        self.assertEqual(error.exception.code, "PREVIEW_OUTCOME_UNKNOWN")
        self.assertEqual(error.exception.details["original_operation_id"], "uncertain-1")
        self.assertEqual(len(self.calls), 1)
        with self.service.store.transaction() as c:
            self.assertNotIn("DO_NOT_LOG_PROVIDER_SECRET", "".join(row[0] for row in c.execute("SELECT payload FROM prelive_events")))

    async def test_stale_script_and_wrong_run_rejected_before_provider(self):
        args = await self.authorize()
        other = Principal(kind="agent", actor_id=self.agent.actor_id, run_id="other-run")
        with self.assertRaises(BusinessError) as error:
            await self.service.query("read_script_audio_preview", {"ref": args["preview_approval_ref"]}, principal=other, scope=self.scope)
        self.assertEqual(error.exception.code, "PREVIEW_APPROVAL_RUN_MISMATCH")
        with self.service.store.transaction(write=True) as c:
            prep = self.service._prep(c, self.prep["id"])
            self.service._save_prep(c, prep, {**prep["body"], "script_refs": []})
        with self.assertRaises(BusinessError) as error:
            await self.command("preview_script_audio", args, principal=self.agent)
        self.assertEqual(error.exception.code, "PREVIEW_SCRIPT_STALE")
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
