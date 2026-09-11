"""feishu-cardbot 单测：配置、卡片生成、注入事件、校验回包、查重写表（mock）。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("CARDBOT_ENABLE_WS", "false")
os.environ.setdefault("CARDBOT_ENABLE_INJECT", "true")
os.environ["CARDBOT_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "sessions.sqlite")

from fastapi.testclient import TestClient

from app import app
from card_engine import (
    ACTION_CANCEL,
    ACTION_CONFIRM_WRITE,
    ACTION_OVERWRITE,
    ACTION_SUBMIT_FORM,
    EXPIRED_WRITE_TOAST,
    STATE_AWAIT_OVERWRITE,
    STATE_COLLECTING,
    STATE_CONFIRMING,
    CardEngine,
)
from cards import build_confirm, build_supplier_form, build_welcome, collect_select_options
from config_loader import load_platform, load_supplier_runtime
from handlers import handle_card_action, parse_im_content, session_key
from session_store import SessionStore
from validator import can_overwrite, fuzzy_match_option, payment_satisfied

CONFIG_DIR = Path(__file__).resolve().parent / "config"
APP_CFG, SKILLS = load_platform(CONFIG_DIR)
RUNTIME = load_supplier_runtime(CONFIG_DIR)
CLIENT = TestClient(app)


def _action_event(
    open_id: str,
    action: str,
    form_value: dict | None = None,
    token: str = "",
    *,
    extra: dict | None = None,
    context: dict | None = None,
) -> dict:
    value = {"action": action}
    if extra:
        value.update(extra)
    return {
        "operator": {"open_id": open_id},
        "context": context or {"open_id": open_id, "chat_type": "p2p"},
        "token": token,
        "action": {
            "tag": "button",
            "value": value,
            "form_value": form_value or {},
        },
    }


COMPLETE_FORM = {
    "supplier_name": "德清茶歇A",
    "account_name": "张三",
    "account_no": "6222 001",
    "bank_name": "工行",
    "region": "德清",
    "settlement_type": ["月结"],
    "supplier_type": ["餐厅"],
}


class HealthAndConfigTests(unittest.TestCase):
    def test_health(self):
        resp = CLIENT.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "feishu-cardbot")
        self.assertFalse(body["ws_enabled"])
        self.assertIn("supplier", body["skills"])

    def test_platform_loads_supplier(self):
        self.assertIn("supplier", SKILLS)
        self.assertEqual(RUNTIME["skill_id"], "supplier")
        self.assertTrue(RUNTIME["table_id"])
        self.assertTrue(RUNTIME["require_any_group"])
        self.assertEqual(RUNTIME["fields"]["account_no"].get("pattern"), "digits")

    def test_parse_im_image_and_post(self):
        text, keys = parse_im_content({"content": '{"text":"你好"}'})
        self.assertEqual(text, "你好")
        self.assertEqual(keys, [])
        _, keys = parse_im_content({"content": '{"image_key":"img_1"}'})
        self.assertEqual(keys, ["img_1"])
        text, keys = parse_im_content(
            {
                "content": '{"content":[[{"tag":"text","text":"见图"},{"tag":"img","image_key":"img_2"}]]}'
            }
        )
        self.assertIn("见图", text)
        self.assertEqual(keys, ["img_2"])


class CardBuildTests(unittest.TestCase):
    def test_welcome_has_open_form(self):
        card = build_welcome(open_id="ou_test")
        self.assertEqual(card["schema"], "2.0")
        dumped = str(card)
        self.assertIn("open_form", dumped)
        self.assertIn("录入供应商", dumped)

    def test_form_includes_yaml_options(self):
        card = build_supplier_form(RUNTIME)
        options = collect_select_options(card)
        banks = RUNTIME["fields"]["bank_name"]["options"]
        regions = RUNTIME["fields"]["region"]["options"]
        types = RUNTIME["fields"]["supplier_type"]["options"]
        settlements = RUNTIME["fields"]["settlement_type"]["options"]
        self.assertEqual(options["bank_name"], [str(x) for x in banks])
        self.assertEqual(options["region"], [str(x) for x in regions])
        self.assertEqual(options["supplier_type"], [str(x) for x in types])
        self.assertEqual(options["settlement_type"], [str(x) for x in settlements])
        dumped = str(card)
        self.assertIn("submit_form", dumped)
        self.assertIn("cancel", dumped)
        self.assertNotIn("qrcode", options)

    def test_confirm_button_embeds_snapshot(self):
        card = build_confirm(RUNTIME, COMPLETE_FORM)
        found = None

        def walk(node):
            nonlocal found
            if isinstance(node, dict):
                if node.get("tag") == "button":
                    behaviors = node.get("behaviors") or [{}]
                    val = (behaviors[0] or {}).get("value")
                    if isinstance(val, dict) and val.get("action") == "confirm_write":
                        found = val.get("data")
                for item in node.values():
                    walk(item)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(card)
        self.assertIsInstance(found, dict)
        self.assertEqual(found["supplier_name"], "德清茶歇A")
        self.assertEqual(found["account_no"], "6222 001")


class InjectSendCardTests(unittest.TestCase):
    def test_p2p_entered_returns_welcome_without_sending(self):
        resp = CLIENT.post("/api/inject/p2p-entered", json={"open_id": "ou_welcome"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["sent"])
        self.assertIn("欢迎", str(body["card"]))

    def test_message_returns_form_without_sending(self):
        resp = CLIENT.post(
            "/api/inject/message",
            json={
                "open_id": "ou_msg1",
                "chat_type": "p2p",
                "message_id": "om_msg1",
                "text": "你好",
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["state"], STATE_COLLECTING)
        self.assertFalse(body["sent"])
        self.assertIn("supplier_form", str(body["card"]))

    def test_menu_opens_form(self):
        resp = CLIENT.post(
            "/api/inject/menu",
            json={"open_id": "ou_menu", "event_key": "add_supplier"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["state"], STATE_COLLECTING)


class CardActionValidateTests(unittest.TestCase):
    def test_missing_supplier(self):
        resp = CLIENT.post(
            "/api/inject/card-action",
            json=_action_event(
                "ou_miss",
                ACTION_SUBMIT_FORM,
                {"account_name": "张三", "account_no": "123", "bank_name": "工行"},
                token="t_miss",
            ),
        )
        body = resp.json()
        self.assertEqual(body["state"], STATE_COLLECTING)
        toast = (body["callback"].get("toast") or {}).get("content") or ""
        self.assertTrue("供应商" in toast or "补充" in str(body["callback"]))

    def test_account_not_digits(self):
        form = dict(COMPLETE_FORM)
        form["account_no"] = "6222abc"
        resp = CLIENT.post(
            "/api/inject/card-action",
            json=_action_event("ou_digits", ACTION_SUBMIT_FORM, form, token="t_digits"),
        )
        body = resp.json()
        self.assertEqual(body["state"], STATE_COLLECTING)
        self.assertIn("纯数字", str(body["callback"]))

    def test_unknown_bank(self):
        form = dict(COMPLETE_FORM)
        form["bank_name"] = "火星银行不存在"
        resp = CLIENT.post(
            "/api/inject/card-action",
            json=_action_event("ou_bank", ACTION_SUBMIT_FORM, form, token="t_bank"),
        )
        body = resp.json()
        self.assertEqual(body["state"], STATE_COLLECTING)
        self.assertIn("不存在", str(body["callback"]))

    def test_refund_name_rule(self):
        form = {
            "supplier_name": "随便退款",
            "supplier_type": ["客户退款"],
            "account_name": "张三",
            "account_no": "123456",
            "bank_name": "工行",
        }
        resp = CLIENT.post(
            "/api/inject/card-action",
            json=_action_event("ou_refund", ACTION_SUBMIT_FORM, form, token="t_refund"),
        )
        body = resp.json()
        self.assertEqual(body["state"], STATE_COLLECTING)
        self.assertIn("客户退款", str(body["callback"]))

    def test_payment_group_or_qrcode(self):
        only_name = {"supplier_name": "仅名称"}
        resp = CLIENT.post(
            "/api/inject/card-action",
            json=_action_event("ou_pay1", ACTION_SUBMIT_FORM, only_name, token="t_pay1"),
        )
        self.assertEqual(resp.json()["state"], STATE_COLLECTING)

        store = SessionStore(Path(os.environ["CARDBOT_DB_PATH"]))
        skey = session_key("ou_pay2")
        store.save(
            skey,
            STATE_COLLECTING,
            {
                "skill_id": "supplier",
                "data": {"qrcode": [{"file_token": "tok"}]},
                "active_rules": [],
            },
        )
        resp2 = CLIENT.post(
            "/api/inject/card-action",
            json=_action_event(
                "ou_pay2",
                ACTION_SUBMIT_FORM,
                {"supplier_name": "有收款码"},
                token="t_pay2",
            ),
        )
        self.assertEqual(resp2.json()["state"], STATE_CONFIRMING)

    def test_submit_ok_then_cancel(self):
        resp = CLIENT.post(
            "/api/inject/card-action",
            json=_action_event(
                "ou_ok", ACTION_SUBMIT_FORM, COMPLETE_FORM, token="t_ok"
            ),
        )
        body = resp.json()
        self.assertEqual(body["state"], STATE_CONFIRMING)
        self.assertEqual(body["callback"]["card"]["type"], "raw")
        self.assertIn("确认写入", str(body["callback"]["card"]))

        cancel = CLIENT.post(
            "/api/inject/card-action",
            json=_action_event("ou_ok", ACTION_CANCEL, token="t_cancel"),
        )
        self.assertIsNone(cancel.json()["state"])
        self.assertIn("已取消", str(cancel.json()["callback"]))

    def test_confirm_dry_run_does_not_need_feishu(self):
        CLIENT.post(
            "/api/inject/card-action",
            json=_action_event(
                "ou_dry", ACTION_SUBMIT_FORM, COMPLETE_FORM, token="t_dry1"
            ),
        )
        resp = CLIENT.post(
            "/api/inject/card-action",
            json={
                **_action_event("ou_dry", ACTION_CONFIRM_WRITE, token="t_dry2"),
                "dry_run": True,
            },
        )
        body = resp.json()
        self.assertTrue(body["dry_run"])
        self.assertIsNone(body["state"])
        self.assertIn("dry_run", str(body["callback"]))


class DedupeWriteTests(unittest.TestCase):
    def test_bank_fuzzy_and_payment(self):
        opts = RUNTIME["fields"]["bank_name"]["options"]
        self.assertEqual(fuzzy_match_option("工行", opts), "中国工商银行")
        data = {
            "supplier_name": "A",
            "account_name": "张三",
            "account_no": "123",
            "bank_name": "中国工商银行",
        }
        self.assertTrue(payment_satisfied(RUNTIME, data))
        self.assertTrue(
            payment_satisfied(RUNTIME, {"supplier_name": "A", "qrcode": [{"file_token": "t"}]})
        )

    def test_denied_without_whitelist(self):
        feishu = MagicMock()
        feishu.search_records.return_value = [
            {"record_id": "rec1", "fields": {"供应商": "德清茶歇A"}}
        ]
        engine = CardEngine(RUNTIME, feishu, open_id="ou_stranger", dry_run=False)
        store = SessionStore(Path(tempfile.mkdtemp()) / "s.sqlite")
        result = handle_card_action(
            runtime=RUNTIME,
            event=_action_event("ou_stranger", ACTION_SUBMIT_FORM, COMPLETE_FORM, token="w1"),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        self.assertEqual(result["state"], STATE_CONFIRMING)
        result2 = handle_card_action(
            runtime=RUNTIME,
            event=_action_event("ou_stranger", ACTION_CONFIRM_WRITE, token="w2"),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        self.assertIsNone(result2["state"])
        self.assertIn("无法覆盖", str(result2["callback"]))
        feishu.create_record.assert_not_called()

    def test_whitelist_asks_overwrite_then_updates(self):
        admin = (RUNTIME.get("permissions") or {}).get("overwrite_open_ids") or []
        self.assertTrue(admin)
        open_id = str(admin[0])
        self.assertTrue(can_overwrite(RUNTIME, open_id))
        feishu = MagicMock()
        feishu.search_records.return_value = [
            {"record_id": "rec9", "fields": {"供应商": "德清茶歇A"}}
        ]
        feishu.update_record.return_value = {"record_id": "rec9"}
        store = SessionStore(Path(tempfile.mkdtemp()) / "s2.sqlite")
        handle_card_action(
            runtime=RUNTIME,
            event=_action_event(open_id, ACTION_SUBMIT_FORM, COMPLETE_FORM, token="o1"),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        confirm = handle_card_action(
            runtime=RUNTIME,
            event=_action_event(open_id, ACTION_CONFIRM_WRITE, token="o2"),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        self.assertEqual(confirm["state"], STATE_AWAIT_OVERWRITE)
        over = handle_card_action(
            runtime=RUNTIME,
            event=_action_event(open_id, ACTION_OVERWRITE, token="o3"),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        self.assertIsNone(over["state"])
        feishu.update_record.assert_called_once()
        self.assertEqual(feishu.update_record.call_args.kwargs["record_id"], "rec9")

    def test_create_when_no_dup(self):
        feishu = MagicMock()
        feishu.search_records.return_value = []
        feishu.create_record.return_value = {"record_id": "rec_new"}
        store = SessionStore(Path(tempfile.mkdtemp()) / "s3.sqlite")
        handle_card_action(
            runtime=RUNTIME,
            event=_action_event("ou_new", ACTION_SUBMIT_FORM, COMPLETE_FORM, token="c1"),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        done = handle_card_action(
            runtime=RUNTIME,
            event=_action_event("ou_new", ACTION_CONFIRM_WRITE, token="c2"),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        self.assertIsNone(done["state"])
        feishu.create_record.assert_called_once()
        fields = feishu.create_record.call_args.kwargs["fields"]
        self.assertTrue(fields)
        self.assertIn("供应商", fields)
        self.assertEqual(fields["供应商"], "德清茶歇A")
        self.assertIn("rec_new", str(done["callback"]))

    def test_confirm_without_session_does_not_write(self):
        feishu = MagicMock()
        store = SessionStore(Path(tempfile.mkdtemp()) / "empty.sqlite")
        result = handle_card_action(
            runtime=RUNTIME,
            event=_action_event("ou_empty", ACTION_CONFIRM_WRITE, token="e1"),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        feishu.create_record.assert_not_called()
        feishu.search_records.assert_not_called()
        self.assertEqual(result["state"], STATE_COLLECTING)
        self.assertIn(EXPIRED_WRITE_TOAST, str(result["callback"]))

    def test_second_confirm_does_not_write_empty(self):
        feishu = MagicMock()
        feishu.search_records.return_value = []
        feishu.create_record.return_value = {"record_id": "rec_once"}
        store = SessionStore(Path(tempfile.mkdtemp()) / "dup_confirm.sqlite")
        handle_card_action(
            runtime=RUNTIME,
            event=_action_event("ou_twice", ACTION_SUBMIT_FORM, COMPLETE_FORM, token="tw1"),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        first = handle_card_action(
            runtime=RUNTIME,
            event=_action_event("ou_twice", ACTION_CONFIRM_WRITE, token="tw2"),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        self.assertIsNone(first["state"])
        feishu.create_record.assert_called_once()
        feishu.create_record.reset_mock()
        feishu.search_records.reset_mock()
        second = handle_card_action(
            runtime=RUNTIME,
            event=_action_event("ou_twice", ACTION_CONFIRM_WRITE, token="tw3"),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        feishu.create_record.assert_not_called()
        feishu.search_records.assert_not_called()
        self.assertEqual(second["state"], STATE_COLLECTING)
        self.assertIn(EXPIRED_WRITE_TOAST, str(second["callback"]))

    def test_snapshot_writes_full_record_when_session_missing(self):
        feishu = MagicMock()
        feishu.search_records.return_value = []
        feishu.create_record.return_value = {"record_id": "rec_snap"}
        store = SessionStore(Path(tempfile.mkdtemp()) / "snap.sqlite")
        snapshot = {
            "supplier_name": "快照供应商",
            "account_name": "李四",
            "account_no": "6222001",
            "bank_name": "中国工商银行",
        }
        done = handle_card_action(
            runtime=RUNTIME,
            event=_action_event(
                "ou_snap",
                ACTION_CONFIRM_WRITE,
                token="snap1",
                extra={"data": snapshot},
            ),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        self.assertIsNone(done["state"])
        feishu.create_record.assert_called_once()
        fields = feishu.create_record.call_args.kwargs["fields"]
        self.assertTrue(fields)
        self.assertEqual(fields["供应商"], "快照供应商")
        self.assertEqual(fields["账号"], "6222001")
        self.assertIn("rec_snap", str(done["callback"]))

    def test_confirming_empty_data_does_not_create(self):
        feishu = MagicMock()
        engine = CardEngine(RUNTIME, feishu, open_id="ou_blank", dry_run=False)
        session = {"state": STATE_CONFIRMING, "payload": {"data": {}, "active_rules": []}}
        callback, state, _payload = engine.handle_action(
            action=ACTION_CONFIRM_WRITE,
            session=session,
        )
        feishu.create_record.assert_not_called()
        self.assertEqual(state, STATE_COLLECTING)
        self.assertIn(EXPIRED_WRITE_TOAST, str(callback))

    def test_session_key_p2p_ignores_chat_id(self):
        self.assertEqual(session_key("ou_1", "oc_x", "p2p"), "ou_1")
        self.assertEqual(session_key("ou_1", "", "p2p"), "ou_1")
        self.assertEqual(session_key("ou_1", "oc_g", "group"), "ou_1:oc_g")

    def test_confirm_open_chat_id_reuses_p2p_session(self):
        feishu = MagicMock()
        feishu.search_records.return_value = []
        feishu.create_record.return_value = {"record_id": "rec_key"}
        store = SessionStore(Path(tempfile.mkdtemp()) / "skey.sqlite")
        handle_card_action(
            runtime=RUNTIME,
            event=_action_event("ou_jitter", ACTION_SUBMIT_FORM, COMPLETE_FORM, token="jk1"),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        done = handle_card_action(
            runtime=RUNTIME,
            event=_action_event(
                "ou_jitter",
                ACTION_CONFIRM_WRITE,
                token="jk2",
                context={
                    "open_id": "ou_jitter",
                    "chat_type": "group",
                    "open_chat_id": "oc_jitter",
                },
            ),
            store=store,
            ttl_seconds=600,
            feishu=feishu,
            dry_run=False,
        )
        self.assertIsNone(done["state"])
        feishu.create_record.assert_called_once()
        fields = feishu.create_record.call_args.kwargs["fields"]
        self.assertEqual(fields["供应商"], "德清茶歇A")

    def test_after_image_reaches_confirm_when_name_present(self):
        engine = CardEngine(RUNTIME, None, open_id="ou_img", dry_run=True)
        session = {
            "state": STATE_COLLECTING,
            "payload": {"data": {"supplier_name": "有图供应商"}, "active_rules": []},
        }
        card, state, payload = engine.after_image(session, "ftoken")
        self.assertEqual(state, STATE_CONFIRMING)
        self.assertTrue(payload["data"]["qrcode"])
        self.assertIn("确认写入", str(card))


class WsClientTests(unittest.TestCase):
    def test_build_handler_registers(self):
        try:
            import lark_oapi  # noqa: F401
        except ImportError:
            self.skipTest("lark-oapi not installed")
        from ws_client import build_event_handler

        calls = []

        def on_event(kind, event):
            calls.append(kind)
            return {"callback": {"toast": {"type": "info", "content": "ok"}}}

        handler = build_event_handler(on_event)
        self.assertIsNotNone(handler)


if __name__ == "__main__":
    unittest.main()
