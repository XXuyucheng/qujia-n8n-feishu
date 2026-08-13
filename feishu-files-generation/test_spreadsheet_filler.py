"""Unit tests for spreadsheet_filler anchor delete and itinerary helpers."""

from __future__ import annotations

import unittest

from spreadsheet_filler import (
    anchor_rows_to_delete,
    build_detail_formula_ranges,
    build_itinerary_value_ranges,
    build_online_quote_detail_ranges,
    detail_start_row,
    detail_start_row_after_anchor_delete,
    itinerary_insert_before_row,
    normalize_itinerary_rows,
)


class AnchorDeleteTests(unittest.TestCase):
    def test_n3_deletes_lower_then_upper(self):
        cfg = {"insert_after_row": 9, "insert_before_row": 10}
        self.assertEqual(anchor_rows_to_delete(cfg, 3), [(13, 13), (9, 9)])

    def test_no_insert_config_returns_none(self):
        self.assertIsNone(anchor_rows_to_delete({"start_row": 2, "max_rows": 20}, 3))

    def test_zero_rows_returns_none(self):
        cfg = {"insert_after_row": 9, "insert_before_row": 10}
        self.assertIsNone(anchor_rows_to_delete(cfg, 0))

    def test_detail_start_before_and_after_delete(self):
        cfg = {"insert_after_row": 9, "insert_before_row": 10}
        self.assertEqual(detail_start_row(cfg), 10)
        self.assertEqual(detail_start_row_after_anchor_delete(cfg), 9)


class DetailFormulaRangesTests(unittest.TestCase):
    def setUp(self):
        self.sheet = {
            "insert_after_row": 9,
            "insert_before_row": 10,
            "formula": {
                "line_total_col": "F",
                "line_total": '=IF(OR(D{row}="",E{row}=""),"",D{row}*E{row})',
                "profit_line_col": "I",
                "profit_line": '=IF(OR(G{row}="",H{row}=""),"",G{row}*H{row})',
            },
        }

    def test_writes_f_and_i_for_each_detail_row(self):
        ranges = build_detail_formula_ranges("sht1", self.sheet, 2)
        range_keys = [r["range"] for r in ranges]
        self.assertEqual(
            range_keys,
            [
                "sht1!F10:F10",
                "sht1!F11:F11",
                "sht1!I10:I10",
                "sht1!I11:I11",
            ],
        )
        f_text = ranges[0]["values"][0][0]["text"]
        i_text = ranges[2]["values"][0][0]["text"]
        self.assertIn("D10*E10", f_text)
        self.assertIn("G10*H10", i_text)
        self.assertEqual(ranges[0]["values"][0][0]["type"], "formula")

    def test_exclude_rows_skips_both_columns(self):
        ranges = build_detail_formula_ranges(
            "sht1", self.sheet, 3, exclude_rows=[11]
        )
        range_keys = [r["range"] for r in ranges]
        self.assertEqual(
            range_keys,
            [
                "sht1!F10:F10",
                "sht1!F12:F12",
                "sht1!I10:I10",
                "sht1!I12:I12",
            ],
        )

    def test_without_profit_line_only_f(self):
        sheet = {
            "insert_after_row": 9,
            "insert_before_row": 10,
            "formula": {
                "line_total_col": "F",
                "line_total": "=D{row}*E{row}",
            },
        }
        ranges = build_detail_formula_ranges("sht1", sheet, 1)
        self.assertEqual([r["range"] for r in ranges], ["sht1!F10:F10"])


class DetailGCopiesDTests(unittest.TestCase):
    def test_g_column_writes_same_quantity_as_d(self):
        sheet = {
            "insert_after_row": 9,
            "insert_before_row": 10,
            "max_rows": 80,
            "columns": [
                {"field": "类目", "col": "A"},
                {"field": "物品名称", "col": "B"},
                {"field": "描述", "col": "C"},
                {"field": "数量", "col": "D"},
                {"field": "单价", "col": "E"},
                {"field": "数量", "col": "G"},
            ],
        }
        rows = [
            {
                "类目": "交通",
                "物品名称": "大巴",
                "描述": "",
                "数量": 30,
                "单价": None,
            },
            {
                "类目": "住宿",
                "物品名称": "标间",
                "描述": "",
                "数量": 15,
                "单价": 200,
            },
        ]
        ranges = build_online_quote_detail_ranges("sht1", sheet, rows)
        by_range = {r["range"]: r["values"] for r in ranges}
        self.assertIn("sht1!A10:E11", by_range)
        self.assertIn("sht1!G10:G11", by_range)
        # D is index 3 in A–E block
        d_vals = [row[3] for row in by_range["sht1!A10:E11"]]
        g_vals = [row[0] for row in by_range["sht1!G10:G11"]]
        self.assertEqual(d_vals, [30, 15])
        self.assertEqual(g_vals, d_vals)


class ItineraryInsertTests(unittest.TestCase):
    def setUp(self):
        self.sheet = {
            "insert_after_row": 9,
            "insert_before_row": 10,
            "itinerary": {
                "insert_before_row": 15,
                "max_rows": 80,
                "columns": [
                    {"field": "日期", "col": "A"},
                    {"field": "时间", "col": "B"},
                    {"field": "内容", "col": "C"},
                ],
            },
        }

    def test_shift_with_quote_n3(self):
        self.assertEqual(itinerary_insert_before_row(self.sheet, 3), 16)

    def test_no_quote_rows(self):
        self.assertEqual(itinerary_insert_before_row(self.sheet, 0), 15)

    def test_normalize_and_build_ranges(self):
        rows = normalize_itinerary_rows(
            [
                {"日期": "d1", "时间": "09:00", "内容": "集合"},
                {"date": "d2", "time": "12:00", "content": "午餐"},
            ]
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["日期"], "d1")
        ranges = build_itinerary_value_ranges("sht1", self.sheet, rows, start_row=16)
        self.assertTrue(ranges)
        joined = " ".join(r["range"] for r in ranges)
        self.assertIn("A16:C17", joined)


if __name__ == "__main__":
    unittest.main()
