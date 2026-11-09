"""趣加旅社示范文本 uses the new doc and the same output folder as the old template."""

import unittest
from pathlib import Path

from config_loader import finalize_placeholder_values, resolve_template, load_config

CONFIG = load_config(Path(__file__).resolve().parent / "config.yaml")

COMPLETE_FIELDS = {
    "甲方名称": "杭州某某科技有限公司",
    "甲方税号": "",
    "执行人数": "50",
    "订单号": "DD-001",
    "执行日期": "2026/06/25",
    "签订日期": "2026年11月06日",
    "联系方式": "",
    "经办人": "魏一铭",
    "经办人电话": "18368006726",
    "活动名称": "滨江团建",
    "出发地": "杭州",
    "途径地": "宁波",
    "目的地": "舟山",
    "结束日期": "2026/06/27",
    "旅游时间": "3天",
    "人均费用": "1600",
    "活动费用": "80000",
    "付款方式": "预付款",
    "保险名称": "",
    "通讯地址": "",
}


class QujiaTemplateTests(unittest.TestCase):
    def test_new_template_shares_folder_and_keeps_old_token(self):
        new = resolve_template(signing_unit="趣加旅社示范文本", config=CONFIG)
        alias = resolve_template(signing_unit="趣加旅社", config=CONFIG)
        self.assertEqual(new.template_token, "SKAXdIyWUo0JeVxRa4mc8Apmnze")
        self.assertEqual(alias.template_token, new.template_token)
        self.assertEqual(new.folder_token, "UtBAfoddYlwW3tdA6FTcwiImn1c")
        self.assertEqual(new.empty_optional, "/")
        self.assertTrue(new.append_detail_sheets)
        self.assertEqual(new.detail_sheets["itinerary"]["title"], "行程明细")
        self.assertEqual(new.detail_sheets["fee"]["title"], "费用明细")
        self.assertEqual(new.detail_sheets["style"]["title_kind"], "bold")
        self.assertNotIn("乙方名称", new.required_placeholders)
        self.assertNotIn("途径地", new.required_placeholders)
        with self.assertRaises(Exception):
            resolve_template(signing_unit="不存在的旧趣加旅社模板", config=CONFIG)

    def test_empty_optional_becomes_slash_after_required_check(self):
        tpl = resolve_template(signing_unit="趣加旅社示范文本", config=CONFIG)
        values, missing = finalize_placeholder_values(
            tpl,
            COMPLETE_FIELDS,
            {"途径地": "", "甲方税号": ""},
        )
        self.assertEqual(missing, [])
        self.assertEqual(values["途径地"], "/")
        self.assertEqual(values["甲方税号"], "/")
        self.assertEqual(values["通讯地址"], "/")
        self.assertEqual(values["联系电话"], "/")
        self.assertEqual(values["保险名称"], "/")
        self.assertEqual(values["出发地"], "杭州")

    def test_missing_required_is_not_replaced(self):
        tpl = resolve_template(signing_unit="趣加旅社示范文本", config=CONFIG)
        fields = dict(COMPLETE_FIELDS)
        fields["甲方名称"] = ""
        values, missing = finalize_placeholder_values(tpl, fields, {"途径地": ""})
        self.assertIn("甲方名称", missing)
        self.assertEqual(values["甲方名称"], "")
        self.assertEqual(values["途径地"], "")


if __name__ == "__main__":
    unittest.main()
