from io import BytesIO
from pathlib import Path
import tempfile
import unittest
import zipfile

from shengbo_contracts.common import ArtifactRef, ContractError
from shengbo_prelive.materials import MaterialStore
from shengbo_prelive.store import RuntimeStore


class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RuntimeStore(self.root / "runtime.sqlite")
        self.materials = MaterialStore(self.root, self.store)
    def tearDown(self):
        self.temporary.cleanup()
    def test_text_manifest_immutable_ref_locator_and_conversation_binding(self):
        data = "商品名称：测试杯\n价格：20元".encode()
        result = self.materials.accept("c1", "商品.txt", "text/plain", data)
        ref = result["material_ref"]
        ArtifactRef.model_validate(ref)
        self.assertEqual(ref["kind"], "source_manifest")
        recovered = self.materials.read(ref, "c1", section="text:1")
        self.assertEqual(recovered["text"], data.decode())
        self.assertEqual(self.materials.blob_bytes(ref, "c1"), data)
        with self.assertRaises(ContractError):
            self.materials.read(ref, "c2")
        with self.assertRaises(ContractError):
            self.materials.read({**ref, "revision": 2}, "c1")
    def test_images_pending_visual_extract_no_fake_ocr(self):
        from PIL import Image
        buffer = BytesIO()
        Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, format="PNG")
        result = self.materials.accept("c1", "商品.png", "image/png", buffer.getvalue())
        self.assertEqual(result["status"], "needs_visual_extraction")
        saved = self.materials.read(result["material_ref"], "c1")
        self.assertEqual(saved["text"], "")
        self.assertEqual(saved["manifest"]["locators"][0]["id"], "image:1")
        self.assertEqual(saved["manifest"]["confidence"], "unknown")
        blocks = self.materials.image_messages(result["material_ref"], "c1")
        self.assertTrue(blocks[1]["image_url"]["url"].startswith("data:image/png;base64,"))
    def test_filename_paths_bad_type_and_size_rejected(self):
        for name in ["../file.txt", "C:\\secret.txt", "file.exe", "file\x00.txt"]:
            with self.assertRaises(ContractError):
                self.materials.accept("c1", name, "text/plain", b"test")
        with self.assertRaises(ContractError):
            self.materials.accept("c1", "file.png", "image/png", b"not image")
        with self.assertRaises(ContractError):
            self.materials.accept("c1", "file.txt", "text/plain", b"x" * (10 * 1024 * 1024 + 1))
        self.assertEqual(self.store.list("material"), [])
    def test_docx_paragraph_and_table_extraction(self):
        from docx import Document
        document = Document()
        document.add_paragraph("商品：测试杯")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "价格"
        table.cell(0, 1).text = "20"
        data = BytesIO()
        document.save(data)
        result = self.materials.accept("c1", "商品.docx", "application/octet-stream", data.getvalue())
        recovered = self.materials.read(result["material_ref"], "c1")
        self.assertIn("商品：测试杯", recovered["text"])
        self.assertIn("价格 | 20", recovered["text"])
    def test_zip_bomb_and_incorrect_json_rejected_safely(self):
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
            handle.writestr("word/document.xml", "A" * 2_000_000)
        with self.assertRaises(ContractError):
            self.materials.accept("c1", "bomb.docx", "application/octet-stream", archive.getvalue())
        with self.assertRaises(ContractError):
            self.materials.accept("c1", "bad.json", "application/json", b"{bad}")
    def test_pdf_page_limit_and_no_ocr_claim(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=20, height=20)
        buffer = BytesIO()
        writer.write(buffer)
        result = self.materials.accept("c1", "empty.pdf", "application/pdf", buffer.getvalue())
        manifest = self.materials.read(result["material_ref"], "c1")["manifest"]
        self.assertEqual(len(manifest["pages"]), 1)
        self.assertIn("no_text_extracted_scanned_pdf_needs_visual_or_text_source", manifest["unknowns"])
        for _ in range(64):
            writer.add_blank_page(width=20, height=20)
        buffer = BytesIO()
        writer.write(buffer)
        with self.assertRaises(ContractError):
            self.materials.accept("c1", "long.pdf", "application/pdf", buffer.getvalue())
    def test_tampered_blob_or_path_blocked(self):
        result = self.materials.accept("c1", "test.txt", "text/plain", b"source")
        ref = result["material_ref"]
        row = self.store.get("material", ref["id"])
        Path(row["path"]).write_bytes(b"changed")
        with self.assertRaises(ContractError):
            self.materials.blob_bytes(ref, "c1")
        self.store.update("material", ref["id"], path=str(self.root / "runtime.sqlite"))
        with self.assertRaises(ContractError):
            self.materials.blob_bytes(ref, "c1")


if __name__ == "__main__":
    unittest.main()
