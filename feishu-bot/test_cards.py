"""JSON 2.0 卡片结构快照：欢迎卡 / 表单 / 结果卡。"""

import unittest

from cards import (
    ACTION_OPEN_QUERY,
    ACTION_OPEN_SUPPLIER,
    ACTION_SUBMIT_QUERY,
    ACTION_SUBMIT_SUPPLIER,
    build_processing_card,
    build_query_form_card,
    build_result_card,
    build_supplier_form_card,
    build_welcome_card,
    card_for_send,
    find_submit_button,
    form_names,
    iter_elements,
    make_toast,
)
from config_loader import load_platform, skill_to_runtime
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent / "config"
APP, SKILLS = load_platform(CONFIG_DIR)
CONFIG = skill_to_runtime(SKILLS["supplier"], APP)


class WelcomeCardTests(unittest.TestCase):
    def test_schema_and_actions(self):
        card = build_welcome_card(session_key="ou_1")
        self.assertEqual(card["schema"], "2.0")
        self.assertTrue(card["config"]["update_multi"])
        actions = [
            el["behaviors"][0]["value"]["action"]
            for el in iter_elements(card)
            if el.get("tag") == "button"
        ]
        self.assertEqual(actions, [ACTION_OPEN_SUPPLIER, ACTION_OPEN_QUERY])
        self.assertTrue(any(el.get("tag") == "markdown" for el in iter_elements(card)))


class SupplierFormCardTests(unittest.TestCase):
    def test_form_has_submit_and_unique_names(self):
        data = {
            "supplier_name": "德清茶歇",
            "region": "德清",
            "supplier_type": ["餐厅"],
            "qrcode": [{"file_token": "x"}],
        }
        card = build_supplier_form_card(CONFIG, data, session_key="ou_1")
        self.assertEqual(card["schema"], "2.0")
        names = form_names(card)
        self.assertIn("supplier_name", names)
        self.assertIn("bank_name", names)
        self.assertIn("region", names)
        self.assertIn("supplier_type", names)
        self.assertNotIn("qrcode", names)
        self.assertEqual(len(names), len(set(names)))
        submit = find_submit_button(card)
        self.assertIsNotNone(submit)
        self.assertEqual(
            submit["behaviors"][0]["value"]["action"], ACTION_SUBMIT_SUPPLIER
        )
        self.assertEqual(submit["form_action_type"], "submit")
        self.assertIn("已附收款码", str(card))
        # 预填
        inputs = [el for el in iter_elements(card) if el.get("name") == "supplier_name"]
        self.assertEqual(inputs[0]["default_value"], "德清茶歇")

    def test_bank_options_from_yaml(self):
        card = build_supplier_form_card(CONFIG, {}, session_key="s")
        bank = next(el for el in iter_elements(card) if el.get("name") == "bank_name")
        self.assertEqual(bank["tag"], "select_static")
        values = [opt["value"] for opt in bank["options"]]
        self.assertIn("中国工商银行", values)
        self.assertGreater(len(values), 20)

    def test_multi_select_tag(self):
        card = build_supplier_form_card(
            CONFIG, {"settlement_type": ["月结"]}, session_key="s"
        )
        st = next(el for el in iter_elements(card) if el.get("name") == "settlement_type")
        self.assertEqual(st["tag"], "multi_select_static")
        selected = [opt["value"] for opt in st["options"] if opt.get("selected")]
        self.assertEqual(selected, ["月结"])

    def test_card_for_send_strips_private(self):
        card = {"schema": "2.0", "_tmp": 1, "body": {"elements": []}}
        out = card_for_send(card)
        self.assertNotIn("_tmp", out)
        self.assertEqual(out["schema"], "2.0")


class QueryAndResultCardTests(unittest.TestCase):
    def test_query_form_submit(self):
        card = build_query_form_card(session_key="ou_1", default_text="查供应商 A")
        self.assertEqual(form_names(card), ["query_text"])
        submit = find_submit_button(card)
        self.assertEqual(submit["behaviors"][0]["value"]["action"], ACTION_SUBMIT_QUERY)

    def test_processing_and_result(self):
        proc = build_processing_card(message="正在写入…")
        self.assertEqual(proc["schema"], "2.0")
        result = build_result_card(
            title="录入成功", body="已创建", session_key="ou_1", ok=True
        )
        actions = [
            el["behaviors"][0]["value"]["action"]
            for el in iter_elements(result)
            if el.get("tag") == "button"
        ]
        self.assertIn(ACTION_OPEN_SUPPLIER, actions)
        self.assertIn(ACTION_OPEN_QUERY, actions)

    def test_toast(self):
        t = make_toast("error", "校验失败")
        self.assertEqual(t["type"], "error")
        self.assertEqual(t["content"], "校验失败")


if __name__ == "__main__":
    unittest.main()
