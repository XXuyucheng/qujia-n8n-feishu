import unittest
from pathlib import Path

from config_loader import load_config, load_platform, skill_to_runtime
from dialog import DialogEngine, is_trigger, STATE_COLLECTING, STATE_CONFIRMING
from extractor import extract_by_rules
from router import match_trigger, resolve_skill_id
from validator import (
    build_bitable_fields,
    fuzzy_match_option,
    missing_required,
    payment_satisfied,
    resolve_select_fields,
)

CONFIG_DIR = Path(__file__).resolve().parent / "config"
APP, SKILLS = load_platform(CONFIG_DIR)
CONFIG = skill_to_runtime(SKILLS["supplier"], APP)


class ConfigSkillTests(unittest.TestCase):
    def test_platform_loads_supplier(self):
        self.assertIn("supplier", SKILLS)
        self.assertEqual(CONFIG["skill_id"], "supplier")
        self.assertTrue(CONFIG["table_id"])
        self.assertTrue(CONFIG["require_any_group"])

    def test_load_config_compat(self):
        cfg = load_config(CONFIG_DIR)
        self.assertEqual(cfg["skill_id"], "supplier")


class RouterTests(unittest.TestCase):
    def test_match_trigger(self):
        self.assertEqual(match_trigger("添加供应商：测试", SKILLS), "supplier")
        self.assertIsNone(match_trigger("你好", SKILLS))

    def test_sticky_session(self):
        session = {"state": STATE_COLLECTING, "payload": {"skill_id": "supplier", "data": {}}}
        self.assertEqual(
            resolve_skill_id(text="补联系方式", session=session, skills=SKILLS),
            "supplier",
        )


class ExtractorTests(unittest.TestCase):
    def test_labeled_lines(self):
        text = "添加供应商\n供应商：德清茶歇A\n户名：张三\n账号：6222 001\n银行：工行\n结算类型：月结\n地域：德清"
        data = extract_by_rules(text, CONFIG)
        self.assertEqual(data["supplier_name"], "德清茶歇A")
        self.assertEqual(data["account_name"], "张三")
        self.assertEqual(data["account_no"], "6222001")
        self.assertEqual(data["bank_name"], "工行")
        self.assertEqual(data["settlement_type"], "月结")
        self.assertEqual(data["region"], "德清")

    def test_trigger_name(self):
        data = extract_by_rules("添加供应商 杭州某某民宿", CONFIG)
        self.assertEqual(data.get("supplier_name"), "杭州某某民宿")


class ValidatorTests(unittest.TestCase):
    def test_bank_fuzzy(self):
        opts = CONFIG["fields"]["bank_name"]["options"]
        self.assertEqual(fuzzy_match_option("工行", opts), "中国工商银行")
        self.assertEqual(fuzzy_match_option("中国工商银行", opts), "中国工商银行")

    def test_payment_bank_group(self):
        data = {
            "supplier_name": "A",
            "account_name": "张三",
            "account_no": "123",
            "bank_name": "中国工商银行",
        }
        self.assertTrue(payment_satisfied(CONFIG, data))
        self.assertEqual(missing_required(CONFIG["fields"], data), [])

    def test_payment_qrcode(self):
        data = {"supplier_name": "A", "qrcode": [{"file_token": "tok"}]}
        self.assertTrue(payment_satisfied(CONFIG, data))

    def test_resolve_select(self):
        data = {"bank_name": "工行", "settlement_type": "月结", "region": "德清"}
        out, problems = resolve_select_fields(CONFIG["fields"], data)
        self.assertEqual(out["bank_name"], "中国工商银行")
        self.assertEqual(out["settlement_type"], ["月结"])
        self.assertEqual(out["region"], "德清")
        self.assertEqual(problems, [])

    def test_build_fields(self):
        data = {
            "supplier_name": "A",
            "bank_name": "中国工商银行",
            "settlement_type": ["月结"],
            "qrcode": [{"file_token": "ftoken"}],
        }
        fields = build_bitable_fields(CONFIG["fields"], data)
        self.assertEqual(fields["供应商"], "A")
        self.assertEqual(fields["银行名称"], "中国工商银行")
        self.assertEqual(fields["结算类型"], ["月结"])
        self.assertEqual(fields["支付宝/微信"], [{"file_token": "ftoken"}])


class FakeFeishu:
    def upload_bitable_media(self, **kwargs):
        return "file_token_x"

    def search_records(self, **kwargs):
        return []

    def create_record(self, **kwargs):
        return {"record_id": "rec_new"}

    def update_record(self, **kwargs):
        return {"record_id": "rec_upd"}


class DialogTests(unittest.TestCase):
    def test_trigger(self):
        self.assertTrue(is_trigger("添加供应商 测试", CONFIG))

    def test_collect_to_confirm(self):
        engine = DialogEngine(CONFIG, FakeFeishu())
        text = (
            "添加供应商\n供应商：单元测试店\n户名：李四\n账号：6222001\n"
            "银行：工行\n地域：德清\n类型：餐厅\n结算类型：月结\n联系方式：13800000000\n开户支行：某某支行"
        )
        reply, state, payload = engine.handle(
            session=None, text=text, message_type="text"
        )
        self.assertEqual(state, STATE_CONFIRMING)
        self.assertIn("确认", reply)
        self.assertEqual(payload["data"]["supplier_name"], "单元测试店")
        self.assertEqual(payload.get("skill_id"), "supplier")

    def test_missing_payment(self):
        engine = DialogEngine(CONFIG, FakeFeishu())
        reply, state, payload = engine.handle(
            session=None,
            text="添加供应商：只有名字的店",
            message_type="text",
        )
        self.assertEqual(state, STATE_COLLECTING)
        self.assertIn("还需要", reply)

    def test_confirm_writes(self):
        engine = DialogEngine(CONFIG, FakeFeishu())
        session = {
            "state": STATE_CONFIRMING,
            "payload": {
                "skill_id": "supplier",
                "data": {
                    "supplier_name": "写入店",
                    "account_name": "王五",
                    "account_no": "123",
                    "bank_name": "中国工商银行",
                },
                "skipped_suggested": True,
            },
        }
        reply, state, payload = engine.handle(
            session=session, text="确认", message_type="text"
        )
        self.assertIsNone(state)
        self.assertIn("已创建", reply)

    def test_image_upload_in_collecting(self):
        engine = DialogEngine(CONFIG, FakeFeishu())
        session = {
            "state": STATE_COLLECTING,
            "payload": {
                "skill_id": "supplier",
                "data": {
                    "supplier_name": "图测店",
                    "account_name": "赵六",
                    "account_no": "999",
                    "bank_name": "中国工商银行",
                },
            },
        }
        reply, state, payload = engine.handle(
            session=session,
            text="结算类型：月结",
            message_type="post",
            image_bytes=b"\x89PNG\r\n\x1a\nfake",
            image_name="qrcode.png",
        )
        self.assertIn("qrcode", payload["data"])
        self.assertEqual(payload["data"]["qrcode"][0]["file_token"], "file_token_x")
        self.assertIn(state, (STATE_COLLECTING, STATE_CONFIRMING))
        self.assertTrue(reply)


if __name__ == "__main__":
    unittest.main()
