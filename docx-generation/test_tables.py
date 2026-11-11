"""Insert tables at {{anchor}} paragraphs without rewriting the rest of the document."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import unquote

from fastapi.testclient import TestClient
from lxml import etree

from app import app
from fill import fill_docx
from tables import apply_tables, normalize_tables
from test_fill import paragraph_texts, parse_xml, zip_part

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W_TBL = f"{{{W}}}tbl"
W_TR = f"{{{W}}}tr"
W_T = f"{{{W}}}t"

MINI_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <!--format-keep-->
    <w:p>
      <w:pPr><w:ind w:firstLine="480"/></w:pPr>
      <w:r>
        <w:rPr><w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体"/><w:sz w:val="21"/><w:b/></w:rPr>
        <w:t>前文加粗</w:t>
      </w:r>
    </w:p>
    <w:p>
      <w:r>
        <w:rPr><w:rFonts w:eastAsia="宋体"/><w:sz w:val="21"/></w:rPr>
        <w:t>{{报</w:t>
      </w:r>
      <w:r>
        <w:rPr><w:rFonts w:eastAsia="宋体"/><w:sz w:val="21"/></w:rPr>
        <w:t>价}}</w:t>
      </w:r>
    </w:p>
    <w:p><w:r><w:t>后文{{甲方名称}}</w:t></w:r></w:p>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1800" w:bottom="1440" w:left="1800"/>
    </w:sectPr>
  </w:body>
</w:document>
"""

HEADER_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:p><w:r><w:t>{{签订日期}}</w:t></w:r></w:p>
</w:hdr>
"""

STYLES_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style w:styleId="marker"/></w:styles>
"""

QUOTE_COLUMNS = [
    {"key": "分类", "header": "分类", "width": 1100},
    {"key": "名称", "header": "名称", "width": 1600},
    {"key": "描述", "header": "描述", "width": 3206},
    {"key": "数量", "header": "数量", "width": 700, "align": "right"},
    {"key": "单价", "header": "单价", "width": 850, "align": "right"},
    {"key": "总价", "header": "总价", "width": 850, "align": "right"},
]

ITINERARY_COLUMNS = [
    {"key": "日期", "header": "日期", "width": 1400},
    {"key": "时间", "header": "时间", "width": 1200},
    {"key": "内容", "header": "内容", "width": 5706},
]

FEE_ROWS = [
    {"分类": "交通", "名称": "大巴车（38座）", "描述": "杭州-千岛湖往返及行程内用车，含司机、燃油费、过路费，2天", "数量": 1, "单价": 3000, "总价": 3000},
    {"分类": "用餐安排", "名称": "DAY1 午餐·千岛湖特色桌餐", "描述": "10人/桌，共3桌，不含酒水", "数量": 3, "单价": None, "总价": None},
    {"分类": "用餐安排", "名称": "DAY1 晚餐·晚宴", "描述": "千岛湖特色菜品，10人/桌，共3桌，不含酒水", "数量": 3, "单价": None, "总价": None},
    {"分类": "用餐安排", "名称": "DAY2 午餐·自主烧烤", "描述": "按30人配置烧烤食材、炉具及用具", "数量": 30, "单价": None, "总价": None},
    {"分类": "住宿", "名称": "千岛湖酒店住宿（标间）", "描述": "两人一间，共15间，含次日早餐，单房差另算", "数量": 15, "单价": None, "总价": None},
    {"分类": "活动选配", "名称": "主题团建活动·飞盘", "描述": "含专业教练组织、飞盘道具及活动场地，时长约2小时", "数量": 1, "单价": None, "总价": None},
    {"分类": "活动选配", "名称": "户外活动·千岛湖骑行", "描述": "含山地自行车及头盔租赁，骑行路线保障，时长约2-3小时", "数量": 30, "单价": None, "总价": None},
    {"分类": "人员安排", "名称": "全程导游", "描述": "持证中文导游，负责行程协调、衔接及讲解，全程2天", "数量": 1, "单价": None, "总价": None},
    {"分类": "保险", "名称": "团建意外保险", "描述": "每人一份，保额30万（待确认），含意外医疗", "数量": 30, "单价": None, "总价": None},
    {"分类": "其他", "名称": "饮用水", "描述": "按每人每天2瓶配置，30人×2天=120瓶，约5箱（24瓶/箱）", "数量": 5, "单价": None, "总价": None},
    {"分类": "税费及服务", "名称": "税费及服务", "描述": "1.在线/上门咨询服务，团建活动策划，设计方案、调整+优化按照客户要求修改协调。", "数量": None, "单价": None, "总价": 0.03},
]

ITINERARY_ROWS = [
    {"日期": "day1", "时间": "08:00", "内容": "杭州集合出发，前往千岛湖"},
    {"日期": "day1", "时间": "11:00", "内容": "抵达千岛湖，办理入住/寄存行李"},
    {"日期": "day1", "时间": "11:30", "内容": "千岛湖特色桌餐午餐"},
    {"日期": "day1", "时间": "13:30", "内容": "飞盘主题团建（教练组织，约2小时）"},
    {"日期": "day1", "时间": "16:00", "内容": "千岛湖骑行（约2-3小时）"},
    {"日期": "day1", "时间": "18:30", "内容": "晚宴"},
    {"日期": "day1", "时间": "20:30", "内容": "返回酒店休息"},
    {"日期": "day2", "时间": "08:00", "内容": "酒店早餐"},
    {"日期": "day2", "时间": "09:30", "内容": "自由活动或湖边漫步"},
    {"日期": "day2", "时间": "11:30", "内容": "自主烧烤午餐"},
    {"日期": "day2", "时间": "13:30", "内容": "集合返回杭州"},
    {"日期": "day2", "时间": "16:30", "内容": "抵达杭州，结束团建"},
]


def _docx(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in parts.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def mini_docx() -> bytes:
    return _docx(
        {
            "word/document.xml": MINI_XML.encode("utf-8"),
            "word/header1.xml": HEADER_XML.encode("utf-8"),
            "word/styles.xml": STYLES_XML,
        }
    )


def _tables(root: etree._Element) -> list[etree._Element]:
    return list(root.iter(W_TBL))


def _row_texts(table: etree._Element) -> list[list[str]]:
    rows = []
    for tr in table.findall(W_TR):
        cells = []
        for tc in tr.findall(f"{{{W}}}tc"):
            cells.append("".join((node.text or "") for node in tc.iter(W_T)))
        rows.append(cells)
    return rows


class TableApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_templates = os.environ.get("DOCX_TEMPLATES_DIR")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["DOCX_TEMPLATES_DIR"] = str(self.root)
        (self.root / "迷你.docx").write_bytes(mini_docx())
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self._old_templates is None:
            os.environ.pop("DOCX_TEMPLATES_DIR", None)
        else:
            os.environ["DOCX_TEMPLATES_DIR"] = self._old_templates
        self.tmp.cleanup()

    def test_inserts_table_and_keeps_neighboring_paragraphs(self) -> None:
        source = (self.root / "迷你.docx").read_bytes()
        resp = self.client.post(
            "/api/generate",
            json={
                "template": "迷你.docx",
                "placeholders": {"甲方名称": "示例公司", "签订日期": "2026年11月10日"},
                "empty_optional": "/",
                "tables": [
                    {
                        "anchor": "报价",
                        "columns": [
                            {"key": "名称", "header": "项目"},
                            {"key": "金额", "align": "right", "width": 1000},
                            {"key": "备注"},
                        ],
                        "rows": [
                            {"名称": "大巴", "金额": 0, "备注": "A&B\n下一行"},
                            {"名称": "午餐", "金额": None},
                        ],
                    }
                ],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.content.startswith(b"{"))
        xml = zip_part(resp.content, "word/document.xml").decode("utf-8")
        self.assertIn("<!--format-keep-->", xml)
        self.assertIn('w:firstLine="480"', xml)
        self.assertIn("前文加粗", xml)
        self.assertIn('w:eastAsia="宋体"', xml)
        self.assertNotIn("{{报", xml)
        self.assertNotIn("价}}", xml)
        self.assertEqual(zip_part(resp.content, "word/styles.xml"), STYLES_XML)
        header = paragraph_texts(parse_xml(zip_part(resp.content, "word/header1.xml")))
        self.assertEqual(header, ["2026年11月10日"])

        document = parse_xml(zip_part(resp.content, "word/document.xml"))
        texts = paragraph_texts(document)
        self.assertIn("后文示例公司", texts)
        tables = _tables(document)
        self.assertEqual(len(tables), 1)
        rows = _row_texts(tables[0])
        self.assertEqual(rows[0], ["项目", "金额", "备注"])
        self.assertEqual(rows[1], ["大巴", "0", "A&B下一行"])
        self.assertEqual(rows[2], ["午餐", "/", "/"])
        self.assertIn('w:w="3653"', xml)
        self.assertIn('w:w="3653"', xml)
        self.assertIn('w:w="1000"', xml)
        self.assertIn('<w:tblLayout w:type="fixed"/>', xml)
        self.assertIn("<w:tblHeader/>", xml)
        self.assertIn("A&amp;B", xml)
        self.assertIn("<w:br/>", xml)
        table_xml = etree.tostring(tables[0], encoding=str)
        self.assertIn('w:eastAsia="宋体"', table_xml)
        self.assertIn('w:sz w:val="21"', table_xml)
        self.assertIn("<w:b/>", table_xml.split("</w:tr>", 1)[0])
        self.assertNotIn("<w:b/>", table_xml.split("</w:tr>", 1)[1])
        self.assertNotIn("firstLine", table_xml)
        neighbor = source.split(b"<!--format-keep-->", 1)[1].split(b"<w:p>", 2)[1]
        self.assertIn(neighbor.decode("utf-8"), xml)

    def test_zero_rows_leave_header_and_drop_anchor(self) -> None:
        resp = self.client.post(
            "/api/generate",
            json={
                "template": "迷你.docx",
                "placeholders": {},
                "tables": [{"anchor": "报价", "columns": [{"key": "名称"}], "rows": []}],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        texts = paragraph_texts(parse_xml(zip_part(resp.content, "word/document.xml")))
        self.assertNotIn("{{报价}}", texts)
        unresolved = json.loads(unquote(resp.headers["x-docx-unresolved"]))
        self.assertNotIn("报价", unresolved)
        self.assertIn("甲方名称", unresolved)
        rows = _row_texts(_tables(parse_xml(zip_part(resp.content, "word/document.xml")))[0])
        self.assertEqual(rows, [["名称"]])

    def test_missing_anchor_and_bad_cell_do_not_return_a_docx(self) -> None:
        missing = self.client.post(
            "/api/generate",
            json={
                "template": "迷你.docx",
                "placeholders": {},
                "tables": [{"anchor": "没有这行", "columns": [{"key": "名称"}], "rows": []}],
            },
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json(), {"error": "table_anchor_not_found", "anchor": "没有这行"})
        self.assertFalse(missing.content.startswith(b"PK"))

        bad_cell = self.client.post(
            "/api/generate",
            json={
                "template": "迷你.docx",
                "placeholders": {},
                "tables": [
                    {
                        "anchor": "报价",
                        "columns": [{"key": "名称"}],
                        "rows": [{"名称": ["不是单元格"]}],
                    }
                ],
            },
        )
        self.assertEqual(bad_cell.status_code, 400)
        self.assertEqual(bad_cell.json()["error"], "invalid_tables")

        bad_width = self.client.post(
            "/api/generate",
            json={
                "template": "迷你.docx",
                "placeholders": {},
                "tables": [{"anchor": "报价", "columns": [{"key": "名称", "width": 1.5}], "rows": []}],
            },
        )
        self.assertEqual(bad_width.status_code, 400)
        self.assertEqual(bad_width.json()["error"], "invalid_tables")

        duplicate = self.client.post(
            "/api/generate",
            json={
                "template": "迷你.docx",
                "placeholders": {},
                "tables": [
                    {"anchor": "报价", "columns": [{"key": "名称"}], "rows": []},
                    {"anchor": "报价", "columns": [{"key": "名称"}], "rows": []},
                ],
            },
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(duplicate.json()["error"], "table_anchor_duplicate")

    def test_duplicate_anchor_in_the_document(self) -> None:
        document = MINI_XML.replace(
            "<w:p><w:r><w:t>后文{{甲方名称}}</w:t></w:r></w:p>",
            "<w:p><w:r><w:t>{{报价}}</w:t></w:r></w:p>",
            1,
        )
        (self.root / "重复.docx").write_bytes(_docx({"word/document.xml": document.encode("utf-8")}))
        resp = self.client.post(
            "/api/generate",
            json={
                "template": "重复.docx",
                "placeholders": {},
                "tables": [{"anchor": "报价", "columns": [{"key": "名称"}], "rows": []}],
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "table_anchor_duplicate", "anchor": "报价"})


class ContractTemplateTests(unittest.TestCase):
    def test_quote_and_itinerary_tables_on_the_contract_template(self) -> None:
        path = Path(__file__).resolve().parent / "templates" / "旅行社合同模板.docx"
        source = path.read_bytes()
        before = _zip_parts(source)
        before_xml = before["word/document.xml"].decode("utf-8")
        tables = normalize_tables(
            [
                {"anchor": "报价", "columns": QUOTE_COLUMNS, "rows": FEE_ROWS},
                {"anchor": "行程", "columns": ITINERARY_COLUMNS, "rows": ITINERARY_ROWS},
            ],
            "/",
        )
        filled, unresolved = fill_docx(apply_tables(source, tables), {})
        after = _zip_parts(filled)
        self.assertEqual(set(before), set(after))
        for name, payload in before.items():
            if name != "word/document.xml":
                self.assertEqual(after[name], payload, name)
        xml = after["word/document.xml"].decode("utf-8")
        self.assertEqual(before_xml.count("firstLine"), xml.count("firstLine"))
        self.assertEqual(before_xml.count('w:type="page"'), xml.count('w:type="page"'))
        self.assertIn("浙江省团队境内旅游合同", xml)
        self.assertIn("宋体", xml)
        self.assertNotIn("{{报价}}", xml)
        self.assertNotIn("{{行程}}", xml)
        self.assertIn("附件2", "".join(paragraph_texts(parse_xml(after["word/document.xml"]))))
        self.assertNotIn("报价", unresolved)
        self.assertNotIn("行程", unresolved)

        document = parse_xml(after["word/document.xml"])
        inserted = _tables(document)
        self.assertEqual(len(inserted), 2)
        quote, itinerary = _row_texts(inserted[0]), _row_texts(inserted[1])
        self.assertEqual(len(quote), 1 + len(FEE_ROWS))
        self.assertEqual(len(itinerary), 1 + len(ITINERARY_ROWS))
        self.assertEqual(quote[0], ["分类", "名称", "描述", "数量", "单价", "总价"])
        self.assertEqual(quote[1][1], "大巴车（38座）")
        self.assertEqual(quote[2][4], "/")
        self.assertEqual(quote[2][5], "/")
        self.assertEqual(quote[-1][3], "/")
        self.assertEqual(quote[-1][5], "0.03")
        self.assertEqual(itinerary[0], ["日期", "时间", "内容"])
        self.assertEqual(itinerary[1][2], "杭州集合出发，前往千岛湖")
        self.assertEqual(itinerary[-1][2], "抵达杭州，结束团建")
        self.assertIn('w:w="8306"', xml)
        self.assertIn("<w:tblLayout w:type=\"fixed\"/>", xml)
        self.assertIn('w:eastAsia="宋体"', xml[xml.find("<w:tbl>"):xml.find("</w:tbl>")])


def _zip_parts(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


if __name__ == "__main__":
    unittest.main()
