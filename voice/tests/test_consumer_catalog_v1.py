"""Admission/legacy prompt boundaries. Isolated stores, no provider calls."""
from copy import deepcopy
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from shengbo_contracts.common import MutationContext, Principal

from app import commerce_config as commerce, reply_lab, server
from app.consumer_catalog import (assert_consumer_product, consumer_content_issues,
                                  consumer_context_view, policy_metadata)
from app.prelive import PreliveService, BusinessError
from app.prelive.module_generation import build_request
from app.prelive.service import Product
from app.prelive.whole_script_generation import build_request as build_whole_request


def clean_product():
    return Product(name="单人网球回弹训练器", price=23.58, sales_platform="douyin",
        material="拍杆：镁合金；网线：聚酯纤维", ingredients="镁合金、聚酯纤维",
        target_audience="初学者、男女通用", shipping_origin="",
        specifications=[{"name": "颜色", "value": "红色、渐变蓝色、深蓝色"},
                        {"name": "套装构成", "value": "球拍、回弹网球1个、底座"},
                        {"name": "产地", "value": "浙江"}],
        knowledge_documents=[{"id": "service", "title": "定制与服务", "content":
            "不支持加工定制。不支持加印LOGO。2025年春季上市。"}]).model_dump()


class PolicyTests(unittest.TestCase):
    def test_explicit_faq_documents_must_use_faq_with_safe_no_commit_contract(self):
        for title, content in (("定制服务FAQ", "不支持定制。"),
                               ("常见问题", "不支持定制。"),
                               ("購買問答", "不支持定制。"),
                               ("定制说明", "问：支持加工定制吗？\n答：不支持。"),
                               ("Service", "Q: Is customization supported?\nA: No.")):
            with self.subTest(title=title):
                value = clean_product()
                value["knowledge_documents"] = [{"id": "service", "title": title, "content": content}]
                before = deepcopy(value)
                with self.assertRaises(BusinessError) as caught:
                    assert_consumer_product(value)
                details = caught.exception.details
                self.assertEqual(caught.exception.code, "PRODUCT_CONSUMER_CONTENT_INVALID")
                self.assertEqual(details["commit_state"], "not_committed")
                self.assertEqual(details["issues"], [{"path": "/knowledge_documents/0",
                    "reason": "knowledge_placement", "action": "move_to_faq"}])
                self.assertNotIn(content, json.dumps(details, ensure_ascii=False))
                self.assertEqual(value, before)

    def test_narrative_knowledge_with_question_words_or_marks_is_valid(self):
        value = clean_product()
        value["knowledge_documents"] += [{"id": "care", "title": "使用中的问题背景", "content":
            "遇到收纳问题怎么办？可按说明书整理。这里是使用背景说明。"},
            {"id": "hardness", "title": "材质与硬度", "content": "拍杆硬度适中，球拍硬度为硬。"}]
        assert_consumer_product(value)
        self.assertEqual(consumer_context_view(value), value)

    def test_historical_faq_is_not_silently_migrated_or_deleted(self):
        value = clean_product()
        value["knowledge_documents"] += [{"id": "old", "title": "定制服务FAQ", "content":
            "问：支持加工定制吗？\n答：不支持。"}]
        value["faq"] = "问：有哪些颜色？\n答：红色、蓝色。"
        before = deepcopy(value)
        projected = consumer_context_view(value)
        self.assertEqual(projected, before)
        self.assertEqual(value, before)
        self.assertEqual(projected["faq"], before["faq"])

    def test_consumer_content_and_platform_do_not_invent_shipping(self):
        value = clean_product()
        assert_consumer_product(value)
        self.assertEqual(value["shipping_origin"], "")
        self.assertEqual(value["shipping_time"], "")
        self.assertEqual(consumer_context_view(value), value)
        # These are customer-facing service facts, not procurement prices.
        assert_consumer_product({**value, "faq": "不支持加工定制，消费者可按订单页规则办理售后。"})
        self.assertEqual(Product(name="平台未选", price=1).sales_platform, "unspecified")
        assert_consumer_product({**value, "price": 1688, "knowledge": "售价1688元，重量1688克。", "shipping_origin": "浙江"})

    def test_sensitive_text_rejected_with_only_safe_coordinates(self):
        value = clean_product()
        value["knowledge"] = "供货店铺：绝密供货公司；进价9.48元。链接https://detail.1688.com/offer/1034247361942.html"
        before = deepcopy(value)
        with self.assertRaises(BusinessError) as caught:
            assert_consumer_product(value)
        error = caught.exception
        self.assertEqual((error.code, error.status), ("PRODUCT_CONSUMER_CONTENT_INVALID", 422))
        self.assertEqual(error.details["commit_state"], "not_committed")
        self.assertEqual(error.details["issues"][0]["path"], "/knowledge")
        for needle in ("绝密", "9.48", "1688", "1034247361942"):
            self.assertNotIn(needle, json.dumps(error.payload(), ensure_ascii=False))
        self.assertEqual(value, before)

    def test_contextual_supplier_literal_cannot_move_to_another_field(self):
        value = clean_product()
        value.update(knowledge="供应商：绝密采购公司", faq="联系人请找绝密采购公司")
        issues = consumer_content_issues(value)
        self.assertEqual({issue["path"] for issue in issues}, {"/knowledge", "/faq"})
        projected = consumer_context_view(value)
        self.assertNotIn("绝密采购公司", json.dumps(projected, ensure_ascii=False))
        # Do not guess that every company is a supplier.
        assert_consumer_product({**clean_product(), "knowledge": "零售品牌由星海体育有限公司经营。"})

    def test_traditional_and_full_width_supply_markers(self):
        for text in ("進貨價9.48元", "供應商：甲乙公司", "來源平台１６８８", "採購價12元",
                     "1688平台", "阿里巴巴采购", "https://detail.1688.com/offer/123456789"):
            with self.subTest(text=text), self.assertRaises(BusinessError):
                assert_consumer_product({**clean_product(), "knowledge": text})

    def test_specification_placement_and_unknown_values_are_actionable(self):
        value = clean_product()
        value["specifications"] += [{"name": "上市年份季节", "value": "2025年春季"},
                                    {"name": "框架材质", "value": "其它"},
                                    {"name": "拍杆材质（页面）", "value": "镁合金"}]
        issues = consumer_content_issues(value)
        self.assertEqual([issue["action"] for issue in issues],
                         ["move_to_knowledge", "omit_unknown", "remove_from_product"])

    def test_legacy_projection_removes_contamination_without_mutating_hash_inputs(self):
        old = clean_product()
        old.update(knowledge="使用后收纳。\n进价9.48元。\n请妥善保存。", hidden_optional_fields=[])
        old["knowledge_documents"] += [
            {"id": "source", "title": "供货店铺", "content": "供应商：绝密商家"},
            {"id": "reviews", "title": "页面销量和评价记录", "content": "全网销量10万件。"}]
        before = deepcopy(old)
        projected = commerce.active_product_fields(old)
        self.assertEqual(projected["knowledge"], "使用后收纳。\n请妥善保存。")
        self.assertEqual(len(projected["knowledge_documents"]), 1)
        self.assertEqual(old, before)
        context = {"records": [{"body": old}], "source_evidence": "链接https://1688.com/offer/1034247361942",
                   "generation_instruction": "讲清训练套装。"}
        whole, _ = build_whole_request(old, context,
            [{"key": "selling_points", "action": "generate", "instruction": "讲清训练套装"}], model="deepseek-flash")
        for needle in ("9.48", "绝密", "10万", "1688", "1034247361942"):
            self.assertNotIn(needle, whole["messages"][1]["content"])
        body = build_request(old, context, "selling_points", [{"text": "进价9.48元。\n正常使用后收纳。"}], model="deepseek-flash")
        prompt = body["messages"][1]["content"]
        for needle in ("9.48", "绝密", "10万", "1688", "1034247361942"):
            self.assertNotIn(needle, prompt)
        self.assertIn("23.58", prompt)
        self.assertEqual(old, before)

    def test_validation_never_silently_cleans_supply_text(self):
        value = {**clean_product(), "knowledge": "进价9.48元"}
        self.assertEqual(Product.model_validate(value).knowledge, "进价9.48元")

    def test_legacy_generator_cannot_copy_supply_notes_from_prior_versions(self):
        raw = {"versions": [{"blocks": [{"sentences": [{"display_text": "单人网球回弹训练器。", "fact_ids": ["NAME"]}]}]}]}
        response = json.dumps({"choices": [{"message": {"content": json.dumps(raw)}}]}).encode()
        value = clean_product()
        value["id"] = "legacy-generator"
        previous = [{"angle": "进价9.48元", "opening": ["来自1688平台"], "outline": ["使用后收纳"]}]
        with patch.dict(server.os.environ, {"DEEPSEEK_API_KEY": "unit-test-only"}), \
             patch.object(server.urllib.request, "urlopen", return_value=io.BytesIO(response)) as request:
            server._generate_remote_once(value, 1, previous)
        prompt = json.loads(request.call_args.args[0].data)["messages"][1]["content"]
        self.assertNotIn("9.48", prompt)
        self.assertNotIn("1688", prompt)
        self.assertIn("使用后收纳", prompt)
        self.assertEqual(previous[0]["angle"], "进价9.48元")

    def test_rag_index_rebuild_excludes_legacy_supplier_notes(self):
        old = {**clean_product(), "id": "legacy-test", "knowledge": "用后收纳。\n供货价9.48元。"}
        old["knowledge_documents"] += [{"id": "supplier", "title": "供应商信息", "content": "绝密商家地址"}]
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "RUNTIME", Path(directory)):
            reply_lab.sync_index([old])
            with server.db() as connection:
                text = " ".join(row[0] for row in connection.execute("SELECT body FROM rag_chunks"))
            self.assertNotIn("9.48", text)
            self.assertNotIn("绝密", text)
            self.assertIn("用后收纳", text)

    def test_room_knowledge_titles_and_body_cannot_reintroduce_source_information(self):
        value = {**clean_product(), "id": "room-product"}
        config = {"id": "room-test", "commerce": commerce.parse_room({"knowledge_documents": [
            {"id": "source-room", "title": "供货店铺", "content": "内部公司资料"},
            {"id": "allowed-room", "title": "活动提醒", "content": "本场按订单页规则办理售后。"}]})}
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "RUNTIME", Path(directory)):
            documents, chunks = reply_lab.read_index([value], config)
        text = json.dumps([documents, chunks], ensure_ascii=False)
        self.assertNotIn("供货店铺", text)
        self.assertNotIn("内部公司资料", text)
        self.assertIn("活动提醒", text)

    def test_manual_put_uses_same_guard_before_writing(self):
        value = {**clean_product(), "id": "manual", "knowledge": "供货渠道1688"}
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "RUNTIME", Path(directory)):
            with self.assertRaises(HTTPException) as caught:
                server.put("product", value)
            self.assertEqual(caught.exception.status_code, 422)
            self.assertEqual(caught.exception.detail["code"], "PRODUCT_CONSUMER_CONTENT_INVALID")
            self.assertEqual(server.all_of("product"), [])


class ServiceAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_faq_misplacement_rejects_before_save_and_corrected_payload_is_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            service = PreliveService(directory)
            try:
                fields = clean_product()
                fields["knowledge_documents"] = [{"id": "faq", "title": "定制服务FAQ",
                    "content": "问：支持定制吗？\n答：不支持定制。"}]
                args = {"fields": fields, "patches": [], "schema_hash": service.schema()["schema_hash"]}
                with self.assertRaises(BusinessError) as caught:
                    await service.execute_command("save_product", args,
                        MutationContext(operation_id="faq-rejected"), Principal(kind="human"))
                self.assertEqual(caught.exception.details["issues"][0]["action"], "move_to_faq")
                with service.store.transaction() as connection:
                    self.assertEqual(service.store.all(connection, "product"), [])
                fields["knowledge_documents"] = []
                fields["faq"] = "问：支持定制吗？\n答：不支持定制。"
                receipt = await service.execute_command("save_product", args,
                    MutationContext(operation_id="faq-corrected"), Principal(kind="human"))
                actual = receipt["data"]["product"]
                self.assertEqual(actual["faq"], fields["faq"])
                self.assertEqual(actual["knowledge_documents"], [])
            finally:
                await service.close()

    async def test_approved_payload_is_rejected_prewrite_and_clean_retry_is_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            service = PreliveService(directory)
            try:
                fields = {**clean_product(), "knowledge": "供货店铺：绝密公司；进价9.48元"}
                args = {"fields": fields, "patches": [], "schema_hash": service.schema()["schema_hash"]}
                sql = []
                connect = service.store.connect
                def traced():
                    connection = connect()
                    connection.set_trace_callback(sql.append)
                    return connection
                with patch.object(service.store, "connect", side_effect=traced):
                    with self.assertRaises(BusinessError) as caught:
                        await service.execute_command("save_product", args,
                            MutationContext(operation_id="rejected"), Principal(kind="human"))
                self.assertEqual(caught.exception.code, "PRODUCT_CONSUMER_CONTENT_INVALID")
                self.assertFalse([statement for statement in sql
                    if statement.split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}])
                args["fields"] = clean_product()
                receipt = await service.execute_command("save_product", args,
                    MutationContext(operation_id="clean"), Principal(kind="human"))
                actual = receipt["data"]["product"]
                for key, value in args["fields"].items():
                    if key != "sku_id":
                        self.assertEqual(actual[key], value)
                self.assertEqual(service.schema()["consumer_catalog_policy"], policy_metadata())
            finally:
                await service.close()
