"""Unit tests for config_loader."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from config_loader import (
    build_placeholder_values,
    format_currency,
    format_document_name,
    format_people_count,
    load_config,
    missing_required,
    resolve_template,
)


SAMPLE_CONFIG = """
defaults:
  template_token: "${FEISHU_CONTRACT_TEMPLATE_TOKEN}"
  folder_token: "${FEISHU_CONTRACT_OUTPUT_FOLDER}"
  document_name_pattern: "合同-{订单号}"
  required_placeholders:
    - 甲方名称
    - 乙方名称
    - 合同价款
    - 活动日期
  rate_limit:
    read_qps: 4.0
    write_qps: 3.0
    copy_retry_max: 3
    copy_retry_delay_ms: 800

templates:
  趣加旅社:
    template_token: "doxcnTplQujia"
    folder_token: "fldcnOutQujia"
    document_name_pattern: "合同-{订单号}-趣加旅社"
    party_b_full_name: "杭州趣加旅社有限公司"
    placeholders:
      甲方名称: "{单位}"
      乙方名称: "@party_b_full_name"
      合同价款: "{合同价款}"
      活动日期: "{执行日期}"
      活动人数: "{执行人数}"
      签订日期: "@today"

signing_unit_map:
  趣加旅社: 趣加旅社
"""


class FormatHelpersTests(unittest.TestCase):
    def test_format_currency(self):
        self.assertEqual(format_currency(80000), "80,000.00")
        self.assertEqual(format_currency("80000.5"), "80,000.50")
        self.assertEqual(format_currency(""), "")

    def test_format_people_count(self):
        self.assertEqual(format_people_count("120人"), "120")
        self.assertEqual(format_people_count(30), "30")


class ResolveTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        self.tmp.write(SAMPLE_CONFIG)
        self.tmp.close()
        self.config = load_config(Path(self.tmp.name))

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_resolve_by_signing_unit(self):
        tpl = resolve_template(signing_unit="趣加旅社", config=self.config)
        self.assertEqual(tpl.template_token, "doxcnTplQujia")
        self.assertEqual(tpl.folder_token, "fldcnOutQujia")
        self.assertEqual(tpl.party_b_full_name, "杭州趣加旅社有限公司")
        self.assertFalse(tpl.explicit)

    def test_resolve_explicit_tokens(self):
        tpl = resolve_template(
            template_token="doxcnExplicit",
            folder_token="fldcnExplicit",
            config=self.config,
        )
        self.assertTrue(tpl.explicit)
        self.assertEqual(tpl.template_token, "doxcnExplicit")
        self.assertEqual(tpl.folder_token, "fldcnExplicit")

    def test_unknown_signing_unit(self):
        from config_loader import TemplateResolveError

        with self.assertRaises(TemplateResolveError) as ctx:
            resolve_template(signing_unit="不存在的单位", config=self.config)
        self.assertEqual(ctx.exception.error_code, "UNKNOWN_SIGNING_UNIT")


class BuildPlaceholderValuesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        self.tmp.write(SAMPLE_CONFIG)
        self.tmp.close()
        self.config = load_config(Path(self.tmp.name))
        self.tpl = resolve_template(signing_unit="趣加旅社", config=self.config)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_map_fields_and_specials(self):
        values = build_placeholder_values(
            self.tpl,
            fields={
                "单位": "杭州某某科技有限公司",
                "合同价款": 80000,
                "执行日期": "2026/06/25",
                "执行人数": "120人",
                "订单号": "TEST-001",
            },
        )
        self.assertEqual(values["甲方名称"], "杭州某某科技有限公司")
        self.assertEqual(values["乙方名称"], "杭州趣加旅社有限公司")
        self.assertEqual(values["合同价款"], "80,000.00")
        self.assertEqual(values["活动日期"], "2026/06/25")
        self.assertEqual(values["活动人数"], "120")
        self.assertRegex(values["签订日期"], r"^\d{4}年\d{2}月\d{2}日$")

    def test_explicit_placeholders_override(self):
        values = build_placeholder_values(
            self.tpl,
            fields={"单位": "A公司"},
            placeholders={"甲方名称": "覆盖公司", "乙方名称": "覆盖乙方"},
        )
        self.assertEqual(values["甲方名称"], "覆盖公司")
        self.assertEqual(values["乙方名称"], "覆盖乙方")

    def test_missing_required(self):
        values = build_placeholder_values(self.tpl, fields={})
        missing = missing_required(values, self.tpl.required_placeholders)
        self.assertIn("甲方名称", missing)
        self.assertIn("合同价款", missing)

    def test_document_name(self):
        name = format_document_name(
            self.tpl.document_name_pattern,
            fields={"订单号": "ORD-9"},
        )
        self.assertEqual(name, "合同-ORD-9-趣加旅社")

        name2 = format_document_name(
            self.tpl.document_name_pattern,
            fields={"订单号": "ORD-9"},
            document_name="自定义名称",
        )
        self.assertEqual(name2, "自定义名称")


DEPARTURE_CONFIG = """
defaults:
  template_token: ""
  folder_token: "fldDefault"
  document_name_pattern: "文档-{订单号}"
  required_placeholders:
    - 甲方名称
  rate_limit:
    read_qps: 4.0
    write_qps: 3.0

templates:
  出团计划单:
    template_token: "doxcnDeparture"
    folder_token: "fldDeparture"
    document_name_pattern: "出团计划单-{订单号}"
    required_placeholders:
      - 订单编号
      - 客户名称
    placeholders:
      订单编号: "{订单号}"
      客户名称: "{单位}"
      活动: "{活动名称}"
    sheet:
      start_row: 2
      max_rows: 20
      clear_unused_rows: true
      columns:
        - field: 名称
          col: A
        - field: 数量
          col: C

signing_unit_map:
  出团计划单: 出团计划单
"""


class DepartureTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        self.tmp.write(DEPARTURE_CONFIG)
        self.tmp.close()
        self.config = load_config(Path(self.tmp.name))

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_resolve_sheet_and_required_override(self):
        tpl = resolve_template(signing_unit="出团计划单", config=self.config)
        self.assertEqual(tpl.template_token, "doxcnDeparture")
        self.assertIsNotNone(tpl.sheet)
        assert tpl.sheet is not None
        self.assertEqual(tpl.sheet["max_rows"], 20)
        self.assertEqual(tpl.required_placeholders, ["订单编号", "客户名称"])
        self.assertNotIn("甲方名称", tpl.required_placeholders)


if __name__ == "__main__":
    unittest.main()
