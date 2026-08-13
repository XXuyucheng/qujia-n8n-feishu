"""Unit tests for contract embedded sheet helpers."""

from __future__ import annotations

import unittest
from typing import Any, Dict, List

from contract_sheet_filler import (
    align_code,
    append_contract_detail_sheets,
    build_settlement_formula_ranges,
    build_settlement_matrix,
    build_sheet_matrix,
    cell_str,
    col_letter,
    normalize_columns,
    parse_style_cfg,
    resolve_fee_rows,
    resolve_itinerary_rows,
    resolve_settlement_rows,
    title_align_code,
    _heading2_child,
    H_ALIGN,
    V_ALIGN,
    DEFAULT_FEE_COLUMNS,
    DEFAULT_ITINERARY_COLUMNS,
    DEFAULT_SETTLEMENT_COLUMNS,
)


SAMPLE_FIELDS: Dict[str, Any] = {
    "费用明细": [
        {
            "分类": "交通",
            "名称": "大巴车（38座）",
            "描述": "全程3天包车",
            "数量": 1,
            "单价": 4000,
            "总价": 4000,
        },
        {
            "分类": "税费及服务",
            "名称": "税费及服务",
            "描述": "发票税费1%",
            "数量": None,
            "单价": None,
            "总价": 0.04,
        },
    ],
    "活动行程": [
        {"日期": "day1", "时间": "07:30-12:00", "内容": "上海集合出发"},
        {"日期": "day1", "时间": "12:00-13:30", "内容": "午餐"},
    ],
}


class HelperTests(unittest.TestCase):
    def test_cell_str_and_col_letter(self):
        self.assertEqual(cell_str(None), "")
        self.assertEqual(cell_str(4000.0), "4000")
        self.assertEqual(cell_str(0.04), "0.04")
        self.assertEqual(col_letter(0), "A")
        self.assertEqual(col_letter(5), "F")
        self.assertEqual(col_letter(26), "AA")

    def test_align_and_style(self):
        self.assertEqual(align_code("center", H_ALIGN), 1)
        self.assertEqual(align_code("left", H_ALIGN), 0)
        self.assertEqual(align_code("right", H_ALIGN), 2)
        self.assertEqual(align_code("top", V_ALIGN), 0)
        self.assertEqual(title_align_code("left"), 1)
        self.assertEqual(title_align_code("center"), 2)
        self.assertEqual(title_align_code("right"), 3)
        style = parse_style_cfg(
            {
                "h_align": "center",
                "v_align": "center",
                "font_size": "11pt/1.5",
                "header_bold": True,
                "title_align": "center",
            }
        )
        self.assertEqual(style["h_align"], 1)
        self.assertEqual(style["v_align"], 1)
        self.assertEqual(style["font_size"], "11pt/1.5")
        self.assertTrue(style["header_bold"])
        self.assertEqual(style["title_align"], 2)
        heading = _heading2_child("预定结算", align=2)
        self.assertEqual(heading["heading2"]["style"]["align"], 2)

    def test_normalize_columns_defaults_and_widths(self):
        cols = normalize_columns(None, DEFAULT_FEE_COLUMNS)
        self.assertEqual(len(cols), 6)
        self.assertEqual(cols[0]["width"], 75)
        self.assertEqual(cols[2]["width"], 330)

        trip = normalize_columns(
            [
                {"field": "日期", "width": 50},
                {"field": "时间", "header": "时间", "width": 100},
                {"field": "内容", "width": 300},
            ],
            DEFAULT_ITINERARY_COLUMNS,
        )
        self.assertEqual([c["width"] for c in trip], [50, 100, 300])


class ResolveAndMatrixTests(unittest.TestCase):
    def test_resolve_and_matrix(self):
        fee = resolve_fee_rows(SAMPLE_FIELDS)
        trip = resolve_itinerary_rows(SAMPLE_FIELDS)
        self.assertEqual(len(fee), 2)
        self.assertIsNone(fee[1]["数量"])
        self.assertEqual(trip[0]["日期"], "day1")

        cols = normalize_columns(None, DEFAULT_FEE_COLUMNS)
        matrix = build_sheet_matrix(cols, fee)
        self.assertEqual(matrix[0], ["分类", "名称", "描述", "数量", "单价", "总价"])
        self.assertEqual(len(matrix), 3)
        self.assertEqual(matrix[2][3], "")  # null quantity
        self.assertEqual(matrix[2][5], "0.04")

    def test_empty_data_still_has_header(self):
        cols = normalize_columns(None, DEFAULT_ITINERARY_COLUMNS)
        matrix = build_sheet_matrix(cols, [])
        self.assertEqual(len(matrix), 1)
        self.assertEqual(matrix[0], ["日期", "时间", "内容"])

    def test_fallback_keys(self):
        fields = {
            "报价明细条目": [{"分类": "住宿", "名称": "酒店", "数量": 2}],
            "活动行程条目": [{"day": "day2", "时间": "09:00", "内容": "出发"}],
        }
        self.assertEqual(resolve_fee_rows(fields)[0]["名称"], "酒店")
        self.assertEqual(resolve_itinerary_rows(fields)[0]["日期"], "day2")


class FakeClient:
    def __init__(self) -> None:
        self.children_calls: List[Dict[str, Any]] = []
        self.value_calls: List[Dict[str, Any]] = []
        self.width_calls: List[Dict[str, Any]] = []
        self.style_calls: List[Dict[str, Any]] = []
        self.insert_calls: List[Dict[str, Any]] = []
        self.add_calls: List[Dict[str, Any]] = []
        self._sheet_n = 0

    def create_block_children(self, token, document_id, parent_block_id, children, **kwargs):
        self.children_calls.append(
            {
                "token": token,
                "document_id": document_id,
                "parent_block_id": parent_block_id,
                "children": children,
                **kwargs,
            }
        )
        self._sheet_n += 1
        sheet_id = f"SID{self._sheet_n}"
        return {
            "children": [
                {"block_type": 4, "block_id": f"h{self._sheet_n}"},
                {
                    "block_type": 30,
                    "block_id": f"s{self._sheet_n}",
                    "sheet": {"token": f"SPREAD{self._sheet_n}_{sheet_id}"},
                },
            ]
        }

    def values_batch_update(self, token, spreadsheet_token, value_ranges):
        self.value_calls.append(
            {
                "spreadsheet_token": spreadsheet_token,
                "value_ranges": value_ranges,
            }
        )
        return {}

    def update_dimension_range(self, token, spreadsheet_token, sheet_id, start, end, **kwargs):
        self.width_calls.append(
            {
                "sheet_id": sheet_id,
                "start": start,
                "end": end,
                **kwargs,
            }
        )
        return {}

    def styles_batch_update(self, token, spreadsheet_token, data_items):
        self.style_calls.append(
            {"spreadsheet_token": spreadsheet_token, "data": data_items}
        )
        return {}

    def insert_dimension_range(self, token, spreadsheet_token, sheet_id, start, end, **kwargs):
        self.insert_calls.append(
            {
                "spreadsheet_token": spreadsheet_token,
                "sheet_id": sheet_id,
                "start": start,
                "end": end,
                **kwargs,
            }
        )
        return {}

    def add_dimension_range(self, token, spreadsheet_token, sheet_id, length, **kwargs):
        self.add_calls.append(
            {
                "spreadsheet_token": spreadsheet_token,
                "sheet_id": sheet_id,
                "length": length,
                **kwargs,
            }
        )
        return {}


class AppendSheetsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "style": {
                "h_align": "center",
                "v_align": "center",
                "font_size": "10pt/1.5",
                "header_bold": True,
            },
            "fee": {
                "title": "费用明细",
                "columns": [
                    {"field": "分类", "width": 75},
                    {"field": "名称", "width": 150},
                    {"field": "描述", "width": 330},
                    {"field": "数量", "width": 50},
                    {"field": "单价", "width": 60},
                    {"field": "总价", "width": 70},
                ],
            },
            "itinerary": {
                "title": "行程明细",
                "columns": [
                    {"field": "日期", "width": 50},
                    {"field": "时间", "width": 100},
                    {"field": "内容", "width": 300},
                ],
            },
        }

    def test_appends_two_sheets_with_widths_and_center(self):
        client = FakeClient()
        n = append_contract_detail_sheets(
            client,
            "tok",
            "doc123",
            SAMPLE_FIELDS,
            self.cfg,
        )
        self.assertEqual(n, 2)
        self.assertEqual(len(client.children_calls), 2)
        # SAMPLE has 2+1 fee rows and 2+1 trip rows — both <=9, no grow
        self.assertEqual(client.add_calls, [])
        self.assertEqual(client.insert_calls, [])

        titles = []
        for call in client.children_calls:
            self.assertEqual(call["parent_block_id"], "doc123")
            kids = call["children"]
            self.assertEqual(kids[0]["block_type"], 4)
            self.assertEqual(kids[1]["block_type"], 30)
            titles.append(kids[0]["heading2"]["elements"][0]["text_run"]["content"])
            sheet = kids[1]["sheet"]
            self.assertLessEqual(sheet["row_size"], 9)
            self.assertGreaterEqual(sheet["column_size"], 1)
        self.assertEqual(titles, ["费用明细", "行程明细"])

        # fee widths 75..70 (6 cols) + trip 50/100/300 (3 cols)
        fee_widths = [c["fixed_size"] for c in client.width_calls[:6]]
        trip_widths = [c["fixed_size"] for c in client.width_calls[6:]]
        self.assertEqual(fee_widths, [75, 150, 330, 50, 60, 70])
        self.assertEqual(trip_widths, [50, 100, 300])

        self.assertEqual(len(client.style_calls), 2)
        for call in client.style_calls:
            style0 = call["data"][0]["style"]
            self.assertEqual(style0["hAlign"], 1)
            self.assertEqual(style0["vAlign"], 1)
            self.assertEqual(style0["font"]["fontSize"], "10pt/1.5")
            # header bold second item
            self.assertTrue(call["data"][1]["style"]["font"]["bold"])

    def test_create_caps_at_9_then_inserts_extra_rows(self):
        client = FakeClient()
        many_fee = [
            {
                "分类": "类",
                "名称": f"项{i}",
                "描述": "d",
                "数量": 1,
                "单价": 10,
                "总价": 10,
            }
            for i in range(16)
        ]
        fields = {"费用明细": many_fee, "活动行程": []}
        append_contract_detail_sheets(client, "tok", "doc", fields, self.cfg)

        fee_create = client.children_calls[0]["children"][1]["sheet"]
        self.assertEqual(fee_create["row_size"], 9)  # capped
        # need 17 rows (header+16); created 9 → add 8 at end
        self.assertEqual(client.insert_calls, [])
        self.assertEqual(len(client.add_calls), 1)
        add = client.add_calls[0]
        self.assertEqual(add["length"], 8)
        self.assertEqual(add.get("major_dimension"), "ROWS")
        # values still cover full matrix
        fee_values = client.value_calls[0]["value_ranges"][0]["values"]
        self.assertEqual(len(fee_values), 17)


class SettlementTests(unittest.TestCase):
    def test_resolve_aliases_and_matrix(self):
        rows = resolve_settlement_rows(
            sheet_rows=[
                {
                    "名称": "大巴车（38座）",
                    "描述": "全程包车",
                    "数量": 1,
                    "单价": 4000,
                    "预定信息": "预定文案",
                }
            ]
        )
        self.assertEqual(rows[0]["物品名称"], "大巴车（38座）")
        self.assertEqual(rows[0]["内容"], "全程包车")
        self.assertEqual(rows[0]["小计"], "")

        cols = normalize_columns(None, DEFAULT_SETTLEMENT_COLUMNS)
        matrix = build_settlement_matrix(cols, rows)
        self.assertEqual(matrix[0][0], "物品名称")
        self.assertEqual(matrix[1][0], "大巴车（38座）")
        self.assertEqual(matrix[1][2], 1)
        self.assertEqual(matrix[1][3], 4000)
        self.assertEqual(matrix[1][4], "")  # 小计 empty

    def test_formula_ranges(self):
        cols = normalize_columns(None, DEFAULT_SETTLEMENT_COLUMNS)
        ranges = build_settlement_formula_ranges("sid", cols, 3)
        self.assertEqual(ranges[0]["range"], "sid!E2:E4")
        self.assertEqual(
            ranges[0]["values"][0],
            [{"type": "formula", "text": '=IF(OR(C2="",D2=""),"",C2*D2)'}],
        )
        self.assertEqual(ranges[1]["range"], "sid!A5:A5")
        self.assertEqual(ranges[1]["values"][0], ["总价"])
        self.assertEqual(ranges[2]["range"], "sid!E5:E5")
        self.assertEqual(
            ranges[2]["values"][0],
            [{"type": "formula", "text": "=SUM(E2:E4)"}],
        )

    def test_append_settlement_only(self):
        client = FakeClient()
        cfg = {
            "style": {
                "h_align": "center",
                "v_align": "center",
                "header_bold": True,
                "title_align": "center",
            },
            "settlement": {
                "title": "预定结算",
                "columns": [
                    {"field": "物品名称", "width": 150},
                    {"field": "内容", "width": 200},
                    {"field": "数量", "width": 40},
                    {"field": "单价", "width": 50},
                    {"field": "小计", "width": 60},
                    {"field": "预定状态", "width": 70},
                    {"field": "结算方式", "width": 70},
                    {"field": "联系人", "width": 70},
                    {"field": "预定信息", "width": 250},
                ],
            },
        }
        fields = {
            "报价明细条目": [
                {"名称": "A", "描述": "da", "数量": 2, "单价": 10},
                {"名称": "B", "描述": "db", "数量": 3, "单价": 20},
            ]
        }
        n = append_contract_detail_sheets(
            client, "tok", "doc", fields, cfg, sheet_rows=None
        )
        self.assertEqual(n, 1)
        self.assertEqual(len(client.children_calls), 1)
        heading = client.children_calls[0]["children"][0]["heading2"]
        title = heading["elements"][0]["text_run"]["content"]
        self.assertEqual(title, "预定结算")
        self.assertEqual(heading["style"]["align"], 2)  # center
        # values + formulas
        self.assertGreaterEqual(len(client.value_calls), 2)
        widths = [c["fixed_size"] for c in client.width_calls]
        self.assertEqual(widths, [150, 200, 40, 50, 60, 70, 70, 70, 250])

    def test_settlement_title_align_override(self):
        client = FakeClient()
        cfg = {
            "style": {"title_align": "center"},
            "settlement": {
                "title": "预定结算",
                "title_align": "left",
                # omit columns → use DEFAULT_SETTLEMENT_COLUMNS
            },
        }
        append_contract_detail_sheets(
            client,
            "tok",
            "doc",
            {"报价明细条目": [{"名称": "A", "数量": 1, "单价": 1}]},
            cfg,
        )
        align = client.children_calls[0]["children"][0]["heading2"]["style"]["align"]
        self.assertEqual(align, 1)  # left overrides style center
