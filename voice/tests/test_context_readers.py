"""Formal draft/context readers: exact identities, scoped data and discoverability."""
import unittest

import test_prelive_service as helpers
from app.prelive import BusinessError
from shengbo_contracts.common import WorkScope


class ContextReaderTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = helpers.BusinessTests.asyncSetUp
    asyncTearDown = helpers.BusinessTests.asyncTearDown
    cmd = helpers.BusinessTests.cmd
    prep = helpers.BusinessTests.prep
    state = helpers.BusinessTests.state

    async def draft(self):
        _, prep = await self.prep()
        saved = await self.cmd("save_session_draft", {"preparation_id": prep["id"],
            "expected_revision": prep["revision"], "draft": {"theme": "情人节", "commerce": {}}})
        return prep, saved["data"]["draft_ref"]

    async def test_formal_draft_has_discoverable_exact_read_entry_and_sections(self):
        prep, ref = await self.draft()
        before = await self.state(prep)
        self.assertEqual(ref["kind"], "draft")
        self.assertEqual(before["context_readers"]["draft_ref"],
                         {"query": "read_context", "ref_kind": "draft", "sections": ["draft"]})
        scope = WorkScope(kind="preparation", account_id="local", preparation_id=prep["id"])
        result = await self.service.query("read_context", {"ref": ref, "sections": ["draft"]}, scope=scope)
        self.assertEqual(result["ref"], ref)
        self.assertEqual(result["body"]["draft"]["theme"], "情人节")
        self.assertEqual(set(result["body"]), {"draft"})
        self.assertIn("preparation_id", result["available_sections"])
        self.assertEqual(await self.state(prep), before)

    async def test_wrong_sections_report_actual_sections_instead_of_empty_success(self):
        _, ref = await self.draft()
        with self.assertRaises(BusinessError) as error:
            await self.service.query("read_context", {"ref": ref, "sections": ["session_draft"]})
        self.assertEqual(error.exception.code, "CONTEXT_SECTION_INVALID")
        self.assertEqual(set(error.exception.details["available_sections"]), {"preparation_id", "draft"})

    async def test_foreign_prep_and_tampered_ref_stay_rejected(self):
        prep, ref = await self.draft()
        other, _ = await self.draft()
        scope = WorkScope(kind="preparation", account_id="local", preparation_id=other["id"])
        with self.assertRaises(BusinessError) as error:
            await self.service.query("read_context", {"ref": ref}, scope=scope)
        self.assertEqual(error.exception.code, "ARTIFACT_SCOPE_CONFLICT")
        for bad, code in (({**ref, "kind": "session_draft"}, "NOT_FOUND"),
                          ({**ref, "content_hash": "sha256:" + "0" * 64}, "REF_HASH_MISMATCH")):
            with self.subTest(code=code), self.assertRaises(BusinessError) as error:
                await self.service.query("read_context", {"ref": bad})
            self.assertEqual(error.exception.code, code)


if __name__ == "__main__":
    unittest.main()
