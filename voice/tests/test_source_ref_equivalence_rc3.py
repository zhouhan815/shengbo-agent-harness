"""RC3: verified source rebinding accepts either legal reference wire shape."""
import base64
from copy import deepcopy
import hashlib
import tempfile
import unittest

from pydantic import ValidationError
from app.prelive.service import PreliveService
from app.prelive.storage import BusinessError, digest
from shengbo_contracts.business import AnalysisReport
from shengbo_contracts.common import MutationContext, Principal


class SourceReferenceEquivalenceRC3Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-ref-rc3-")
        self.service = PreliveService(self.temp.name)
        self.sequence = 0
        self.sources = []
        for index, text in enumerate(("商品：测试唇膏；日常129元。", "本场活动99元；先领券满180减20。")):
            raw = text.encode("utf-8")
            blob = await self.cmd("import_blob", {"content_base64": base64.b64encode(raw).decode("ascii"),
                "expected_sha256": hashlib.sha256(raw).hexdigest(), "mime": "text/plain"})
            manifest = {"original_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "original_size": len(raw),
                "trust": "untrusted_uploaded_source", "text": text}
            ref = {"kind": "source_manifest", "id": "private-material-" + str(index), "revision": 1,
                "content_hash": digest(manifest), "schema_version": "PRELIVE-2.3"}
            self.sources.append({"source_ref": ref, "manifest": manifest, "original_blob_ref": blob["blob_ref"]})
        room = await self.cmd("create_room", {"name": "来源等价测试"})
        prep = await self.cmd("create_preparation", {"room_id": room["room_ref"]["id"], "room_revision": room["room_ref"]["revision"]})
        self.prep_id = prep["preparation_ref"]["id"]

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def cmd(self, key, args):
        self.sequence += 1
        return (await self.service.execute_command(key, args, MutationContext(operation_id="rc3-" + str(self.sequence)),
            Principal(kind="human")))["data"]

    def report(self, *, full=False, fields=None):
        refs = [deepcopy(source["source_ref"]) for source in self.sources]
        deps = refs if full else [{key: value for key, value in ref.items() if key != "schema_version"} for ref in refs]
        return AnalysisReport(summary="来源均为商家材料；顾客场景作为待验证假设。", fields=fields or {},
            source_refs=refs, dependencies=deps).model_dump(exclude_none=True)

    async def accept(self, report, role="audience_scenes", *, readset=None):
        prep = await self.service.query("read_preparation", {"preparation_id": self.prep_id})
        return await self.cmd("accept_analysis_report", {"role": role, "report": report, "report_hash": digest(report),
            "preparation_id": self.prep_id, "expected_revision": prep["ref"]["revision"],
            "readset": readset or [], "source_manifests": self.sources})

    async def test_source_only_audience_record_ref_dependencies_and_readset_are_rebound(self):
        report = self.report()
        self.assertTrue(all("schema_version" not in ref for ref in report["dependencies"]))
        result = await self.accept(report, readset=report["dependencies"])
        accepted = await self.service.query("read_analysis_report", {"ref": result["analysis_ref"]})
        body = accepted["body"]
        formal = body["report"]["source_refs"]
        self.assertEqual(body["report"]["dependencies"], formal)
        self.assertEqual(body["readset"], formal)
        self.assertTrue(all(ref["id"].startswith("source_manifest_") for ref in formal))
        self.assertEqual(body["lineage"]["original_prepared_report"], report)
        self.assertEqual(body["lineage"]["prepared_report_hash"], digest(report))

    async def test_full_artifact_ref_dependencies_remain_supported(self):
        report = self.report(full=True)
        result = await self.accept(report)
        accepted = await self.service.query("read_analysis_report", {"ref": result["analysis_ref"]})
        self.assertEqual(accepted["body"]["report"]["dependencies"], accepted["body"]["report"]["source_refs"])

    async def test_wrong_reference_identity_or_explicit_schema_never_rebinds(self):
        for field, wrong in (("kind", "blob"), ("id", "unverified-material"), ("revision", 2),
                             ("content_hash", "sha256:" + "0" * 64), ("schema_version", "PRELIVE-invalid")):
            report = self.report(full=True)
            report["dependencies"][0][field] = wrong
            before = await self.service.query("read_preparation", {"preparation_id": self.prep_id})
            with self.subTest(field=field), self.assertRaises(BusinessError):
                await self.accept(report)
            after = await self.service.query("read_preparation", {"preparation_id": self.prep_id})
            self.assertEqual(after, before)

    async def test_reference_with_extra_fields_is_rejected_by_declared_dependency_contract(self):
        report = self.report(full=True)
        report["dependencies"][0]["business_note"] = "不能伪装引用以删除业务字段"
        with self.assertRaises(ValidationError):
            await self.accept(report)
        prep = await self.service.query("read_preparation", {"preparation_id": self.prep_id})
        self.assertEqual(prep["body"]["analysis_refs"], [])

    async def test_free_form_dictionary_with_reference_keys_and_extra_fields_is_preserved(self):
        ordinary = {**deepcopy(self.sources[0]["source_ref"]), "business_note": "这是普通业务字典，必须保留。"}
        report = self.report(fields={"ordinary_record": ordinary})
        result = await self.accept(report)
        accepted = await self.service.query("read_analysis_report", {"ref": result["analysis_ref"]})
        self.assertEqual(accepted["body"]["report"]["fields"]["ordinary_record"], ordinary)
        self.assertNotEqual(accepted["body"]["report"]["source_refs"][0], self.sources[0]["source_ref"])

    async def test_committed_facts_survive_failed_audience_and_remaining_reports_can_continue(self):
        facts = await self.accept(self.report(full=True), "product_facts")
        broken = self.report(full=True)
        broken["dependencies"][0]["schema_version"] = "PRELIVE-invalid"
        with self.assertRaises(BusinessError):
            await self.accept(broken)
        prep = await self.service.query("read_preparation", {"preparation_id": self.prep_id})
        self.assertEqual(prep["body"]["analysis_refs"], [facts["analysis_ref"]])
        audience = await self.accept(self.report())
        session = await self.accept(self.report(), "session_config")
        prep = await self.service.query("read_preparation", {"preparation_id": self.prep_id})
        self.assertEqual(prep["body"]["analysis_refs"], [facts["analysis_ref"], audience["analysis_ref"], session["analysis_ref"]])
        facts_after = await self.service.query("read_analysis_report", {"ref": facts["analysis_ref"]})
        self.assertEqual(facts_after["ref"], facts["analysis_ref"])


if __name__ == "__main__":
    unittest.main()
