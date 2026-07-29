"""Unit tests for sheet_filler (no Feishu network)."""

from __future__ import annotations

import unittest

from sheet_filler import (
    SheetConfigError,
    build_formula_ranges,
    build_sheet_value_ranges,
    find_sheet_refs,
    parse_sheet_token,
    resolve_sheet_rows,
    unused_row_delete_range,
)


class ParseSheetTokenTests(unittest.TestCase):
    def test_split_spreadsheet_and_sheet_id(self):
        spreadsheet, sheet_id = parse_sheet_token(
            "HP8psReUphghsYtr3VVcnqabcef_6ZSnoL"
        )
        self.assertEqual(spreadsheet, "HP8psReUphghsYtr3VVcnqabcef")
        self.assertEqual(sheet_id, "6ZSnoL")

    def test_invalid_token(self):
        with self.assertRaises(SheetConfigError):
            parse_sheet_token("no-underscore")


class FindSheetRefsTests(unittest.TestCase):
    def test_find_block_type_30(self):
        blocks = [
            {"block_id": "a", "block_type": 2, "text": {}},
            {
                "block_id": "b",
                "block_type": 30,
                "sheet": {"token": "ShtTokenAAA_SheetBBB"},
            },
        ]
        refs = find_sheet_refs(blocks)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["spreadsheet_token"], "ShtTokenAAA")
        self.assertEqual(refs[0]["sheet_id"], "SheetBBB")
        self.assertEqual(refs[0]["block_id"], "b")


class BuildValueRangesTests(unittest.TestCase):
    def test_write_qty_and_skip_formula_cols(self):
        sheet_cfg = {
            "start_row": 2,
            "max_rows": 5,
            "clear_unused_rows": True,
            "columns": [
                {"field": "名称", "col": "A"},
                {"field": "描述", "col": "B"},
                {"field": "数量", "col": "C"},
                {"field": "单价", "col": "D"},
            ],
        }
        rows = [
            {"名称": "大巴车（55座）", "描述": "接送", "数量": 1, "单价": 4800},
            {"名称": "保险", "描述": "意外险", "数量": 52, "单价": 10},
        ]
        ranges = build_sheet_value_ranges("shtId1", sheet_cfg, rows)
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0]["range"], "shtId1!A2:D6")
        values = ranges[0]["values"]
        self.assertEqual(values[0], ["大巴车（55座）", "接送", 1, 4800])
        self.assertEqual(values[1], ["保险", "意外险", 52, 10])
        # unused rows cleared
        self.assertEqual(values[2], ["", "", "", ""])
        self.assertEqual(len(values), 5)

    def test_trim_writes_only_actual_rows(self):
        sheet_cfg = {
            "start_row": 2,
            "max_rows": 20,
            "trim_unused_rows": True,
            "clear_unused_rows": True,  # ignored when trim is on
            "columns": [
                {"field": "名称", "col": "A"},
                {"field": "描述", "col": "B"},
                {"field": "数量", "col": "C"},
                {"field": "单价", "col": "D"},
            ],
        }
        rows = [
            {"名称": "大巴", "描述": "接送", "数量": 1, "单价": 100},
            {"名称": "保险", "描述": "意外", "数量": 10, "单价": 5},
        ]
        ranges = build_sheet_value_ranges("sht", sheet_cfg, rows)
        self.assertEqual(ranges[0]["range"], "sht!A2:D3")
        self.assertEqual(len(ranges[0]["values"]), 2)

    def test_content_alias_maps_to_desc(self):
        sheet_cfg = {
            "start_row": 2,
            "max_rows": 5,
            "trim_unused_rows": True,
            "columns": [
                {"field": "内容", "col": "B"},
            ],
        }
        rows = [{"描述": "接送说明"}]
        ranges = build_sheet_value_ranges("sht", sheet_cfg, rows)
        self.assertEqual(ranges[0]["values"][0], ["接送说明"])

    def test_contiguous_groups_skip_e(self):
        sheet_cfg = {
            "start_row": 2,
            "max_rows": 2,
            "clear_unused_rows": False,
            "columns": [
                {"field": "名称", "col": "A"},
                {"field": "描述", "col": "B"},
                {"field": "数量", "col": "C"},
                {"field": "单价", "col": "D"},
                {"field": "结算方式", "col": "F"},
                {"field": "联系人", "col": "G"},
            ],
        }
        rows = [{"名称": "车", "描述": "接送", "数量": 1, "单价": 100, "结算方式": "", "联系人": ""}]
        ranges = build_sheet_value_ranges("sht", sheet_cfg, rows)
        self.assertEqual([r["range"] for r in ranges], ["sht!A2:D2", "sht!F2:G2"])
        self.assertEqual(ranges[0]["values"][0], ["车", "接送", 1, 100])
        self.assertEqual(ranges[1]["values"][0], ["", ""])

    def test_overflow_raises(self):
        sheet_cfg = {
            "start_row": 2,
            "max_rows": 1,
            "clear_unused_rows": False,
            "columns": [{"field": "数量", "col": "C"}],
        }
        with self.assertRaises(SheetConfigError):
            build_sheet_value_ranges(
                "sht",
                sheet_cfg,
                [{"数量": 1}, {"数量": 2}],
            )


class BuildFormulaRangesTests(unittest.TestCase):
    def test_subtotal_and_total(self):
        sheet_cfg = {
            "start_row": 2,
            "formula": {
                "subtotal_col": "E",
                "subtotal": '=IF(C{row}="","",C{row}*D{row})',
                "total_label_col": "A",
                "total_label": "总价",
                "total_col": "E",
                "total": "=SUM(E{start}:E{end})",
            },
        }
        ranges = build_formula_ranges("sht", sheet_cfg, row_count=3)
        self.assertGreaterEqual(len(ranges), 3)
        self.assertEqual(ranges[0]["range"], "sht!E2:E4")
        self.assertEqual(
            ranges[0]["values"][0],
            [{"type": "formula", "text": '=IF(C2="","",C2*D2)'}],
        )
        self.assertEqual(
            ranges[0]["values"][2],
            [{"type": "formula", "text": '=IF(C4="","",C4*D4)'}],
        )
        self.assertEqual(ranges[1]["range"], "sht!A5:A5")
        self.assertEqual(ranges[1]["values"][0], ["总价"])
        self.assertEqual(ranges[2]["range"], "sht!E5:E5")
        self.assertEqual(
            ranges[2]["values"][0],
            [{"type": "formula", "text": "=SUM(E2:E4)"}],
        )
        # leftover template cells on total row cleared
        clear_ranges = {r["range"] for r in ranges[3:]}
        self.assertTrue({"sht!B5:B5", "sht!C5:C5", "sht!D5:D5", "sht!F5:F5", "sht!G5:G5"} <= clear_ranges)

    def test_no_formula_config_returns_empty(self):
        self.assertEqual(build_formula_ranges("sht", {"start_row": 2}, 3), [])
        self.assertEqual(
            build_formula_ranges("sht", {"start_row": 2, "formula": {}}, 0),
            [],
        )


class UnusedRowDeleteRangeTests(unittest.TestCase):
    def test_delete_after_total(self):
        sheet_cfg = {
            "start_row": 2,
            "trim_unused_rows": True,
            "template_data_rows": 20,
        }
        # 3 data rows → total at row 5; delete 6..22
        self.assertEqual(unused_row_delete_range(sheet_cfg, 3), (6, 22))

    def test_full_template_no_delete(self):
        sheet_cfg = {
            "start_row": 2,
            "trim_unused_rows": True,
            "template_data_rows": 3,
        }
        # 3 data → total at 5; template_end = 2+3=5 → nothing after total
        self.assertIsNone(unused_row_delete_range(sheet_cfg, 3))

    def test_disabled(self):
        self.assertIsNone(
            unused_row_delete_range(
                {"start_row": 2, "trim_unused_rows": False, "template_data_rows": 20},
                3,
            )
        )


class ResolveSheetRowsTests(unittest.TestCase):
    def test_prefer_explicit_sheet_rows(self):
        rows = resolve_sheet_rows(
            sheet_rows=[{"名称": "A", "数量": 1}],
            fields={"报价明细条目": [{"名称": "B", "数量": 2}]},
        )
        self.assertEqual(rows[0]["名称"], "A")

    def test_fallback_to_fee_items(self):
        rows = resolve_sheet_rows(
            sheet_rows=None,
            fields={
                "报价明细条目": [
                    {"名称": "DAY1团建游戏", "描述": "教练", "数量": 1, "单价": 5000}
                ]
            },
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["名称"], "DAY1团建游戏")


if __name__ == "__main__":
    unittest.main()
