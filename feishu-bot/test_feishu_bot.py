import copy
import unittest
from pathlib import Path

from config_loader import load_config, load_platform, skill_to_runtime
from dialog import (
    DialogEngine,
    is_trigger,
    STATE_AWAIT_OVERWRITE,
    STATE_COLLECTING,
    STATE_CONFIRMING,
)
from extractor import extract_by_rules
from router import match_trigger, resolve_skill_id
from rules import apply_rules
from validator import (
    apply_field_patterns,
    build_bitable_fields,
    can_overwrite,
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
        out, problems = resolve_select_fields(CONFIG["fields"], data, CONFIG)
        self.assertEqual(out["bank_name"], "中国工商银行")
        self.assertEqual(out["settlement_type"], ["月结"])
        self.assertEqual(out["region"], "德清")
        self.assertEqual(problems, [])

    def test_option_missing_hard(self):
        data = {"bank_name": "火星银行不存在"}
        out, problems = resolve_select_fields(CONFIG["fields"], data, CONFIG)
        self.assertNotIn("bank_name", out)
        self.assertTrue(any("不存在" in p and "管理员" in p for p in problems))

    def test_account_digits(self):
        out, problems = apply_field_patterns(
            CONFIG["fields"], {"account_no": "6222abc"}, CONFIG
        )
        self.assertNotIn("account_no", out)
        self.assertTrue(any("纯数字" in p for p in problems))
        out2, problems2 = apply_field_patterns(
            CONFIG["fields"], {"account_no": "6222 001"}, CONFIG
        )
        self.assertEqual(out2["account_no"], "6222001")
        self.assertEqual(problems2, [])

    def test_can_overwrite(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["permissions"] = {"overwrite_open_ids": ["ou_admin"]}
        self.assertTrue(can_overwrite(cfg, "ou_admin"))
        self.assertFalse(can_overwrite(cfg, "ou_other"))
        self.assertFalse(can_overwrite(CONFIG, "ou_anyone"))

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
    def __init__(self, hits=None):
        self.hits = hits or []
        self.search_calls = []
        self.created = None
        self.updated = None

    def upload_bitable_media(self, **kwargs):
        return "file_token_x"

    def search_records(self, **kwargs):
        self.search_calls.append(kwargs)
        field = kwargs.get("field_name")
        value = kwargs.get("value")
        out = []
        for hit in self.hits:
            # hit: {"match_field": "供应商"|"户名"|"账号", "record": {...}}
            if hit.get("match_field") == field and hit.get("match_value") == value:
                out.append(hit["record"])
        return out

    def create_record(self, **kwargs):
        self.created = kwargs
        return {"record_id": "rec_new"}

    def update_record(self, **kwargs):
        self.updated = kwargs
        return {"record_id": "rec_upd"}


class RulesTests(unittest.TestCase):
    def test_customer_refund_sets_type_and_hint(self):
        data, hints, active = apply_rules(
            "客户退款 需要处理",
            {"supplier_name": "普通店"},
            CONFIG,
        )
        self.assertEqual(data["supplier_type"], "客户退款")
        self.assertIn("customer_refund", active)
        self.assertTrue(any("客户退款+订单号" in h for h in hints))

    def test_customer_refund_name_ok(self):
        data, hints, active = apply_rules(
            "客户退款",
            {"supplier_name": "客户退款26071001"},
            CONFIG,
        )
        self.assertEqual(data["supplier_type"], "客户退款")
        self.assertEqual(hints, [])
        self.assertIn("customer_refund", active)


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

    def test_refund_blocks_until_name_pattern(self):
        engine = DialogEngine(CONFIG, FakeFeishu())
        text = (
            "添加供应商\n客户退款\n供应商：随便店\n户名：李四\n账号：6222001\n"
            "银行：工行\n地域：德清\n结算类型：月结"
        )
        reply, state, payload = engine.handle(
            session=None, text=text, message_type="text"
        )
        self.assertEqual(state, STATE_COLLECTING)
        self.assertEqual(payload["data"]["supplier_type"], "客户退款")
        self.assertIn("客户退款+订单号", reply)

    def test_account_invalid_in_dialog(self):
        engine = DialogEngine(CONFIG, FakeFeishu())
        text = (
            "添加供应商\n供应商：账号测\n户名：李四\n账号：6222ABCD\n"
            "银行：工行"
        )
        reply, state, payload = engine.handle(
            session=None, text=text, message_type="text"
        )
        self.assertEqual(state, STATE_COLLECTING)
        self.assertNotIn("account_no", payload["data"])
        self.assertIn("纯数字", reply)

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

    def test_dedupe_denied_without_permission(self):
        feishu = FakeFeishu(
            hits=[
                {
                    "match_field": "供应商",
                    "match_value": "撞名店",
                    "record": {
                        "record_id": "rec_old",
                        "fields": {"供应商": "撞名店"},
                    },
                }
            ]
        )
        engine = DialogEngine(CONFIG, feishu, open_id="ou_normal")
        session = {
            "state": STATE_CONFIRMING,
            "payload": {
                "skill_id": "supplier",
                "data": {
                    "supplier_name": "撞名店",
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
        self.assertIsNone(payload)
        self.assertIn("覆盖权限", reply)
        self.assertIn("撞名店", reply)
        self.assertIsNone(feishu.created)

    def test_dedupe_ask_with_permission(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["permissions"] = {"overwrite_open_ids": ["ou_admin"]}
        feishu = FakeFeishu(
            hits=[
                {
                    "match_field": "账号",
                    "match_value": "999888",
                    "record": {
                        "record_id": "rec_old",
                        "fields": {"供应商": "已有供应商"},
                    },
                }
            ]
        )
        engine = DialogEngine(cfg, feishu, open_id="ou_admin")
        session = {
            "state": STATE_CONFIRMING,
            "payload": {
                "skill_id": "supplier",
                "data": {
                    "supplier_name": "新店",
                    "account_name": "王五",
                    "account_no": "999888",
                    "bank_name": "中国工商银行",
                },
                "skipped_suggested": True,
            },
        }
        reply, state, payload = engine.handle(
            session=session, text="确认", message_type="text"
        )
        self.assertEqual(state, STATE_AWAIT_OVERWRITE)
        self.assertIn("覆盖", reply)
        self.assertIn("已有供应商", reply)

        reply2, state2, _ = engine.handle(
            session={"state": state, "payload": payload},
            text="覆盖",
            message_type="text",
        )
        self.assertIsNone(state2)
        self.assertIn("已更新", reply2)
        self.assertIsNotNone(feishu.updated)

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
