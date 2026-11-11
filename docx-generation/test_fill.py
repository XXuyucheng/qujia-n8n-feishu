"""Fill {{key}} across split runs, tables, headers, and footers."""

from __future__ import annotations

import io
import unittest
import zipfile

from lxml import etree

from fill import fill_docx
from values import MissingRequired, prepare_values

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W_P = f"{{{W}}}p"
W_R = f"{{{W}}}r"
W_T = f"{{{W}}}t"
W_B = f"{{{W}}}b"
W_BR = f"{{{W}}}br"
W_RPR = f"{{{W}}}rPr"

DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <!--format-keep-->
    <w:p>
      <w:pPr><w:ind w:firstLine="480"/><w:jc w:val="both"/></w:pPr>
      <w:r>
        <w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="仿宋" w:cs="Times New Roman"/><w:sz w:val="21"/><w:b/></w:rPr>
        <w:t>加粗</w:t>
      </w:r>
      <w:r>
        <w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="仿宋" w:cs="Times New Roman"/><w:sz w:val="21"/></w:rPr>
        <w:t>{{甲方名称}}</w:t>
      </w:r>
    </w:p>
    <w:p>
      <w:r><w:t>{{途</w:t></w:r>
      <w:proofErr w:type="spellStart"/>
      <w:r><w:t>径地}}</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:p><w:r><w:t>{{甲方税号}}</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
    <w:p><w:r><w:t>{{付款方式}}</w:t></w:r></w:p>
    <w:p><w:r><w:t>{{出发地}}-{{目的地}}</w:t></w:r></w:p>
    <w:p><w:r><w:t>{{保险名称}}</w:t></w:r></w:p>
    <w:p><w:r><w:t>上一段{{途</w:t></w:r></w:p>
    <w:p><w:r><w:t>径地}}下一段</w:t></w:r></w:p>
  </w:body>
</w:document>
"""

HEADER_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:p><w:r><w:t>{{签订日期}}</w:t></w:r></w:p>
</w:hdr>
"""

FOOTER_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:p><w:r><w:t>{{订单号}}</w:t></w:r></w:p>
</w:ftr>
"""

STYLES_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:styleId="marker"/>
</w:styles>
"""

CONTENT_TYPES = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
  <Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
</Types>
"""

PACKAGE_RELS = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

DOCUMENT_RELS = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdHeader" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
  <Relationship Id="rIdFooter" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
</Relationships>
"""


def build_sample_docx() -> bytes:
    parts = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": PACKAGE_RELS,
        "word/document.xml": DOCUMENT_XML.encode("utf-8"),
        "word/header1.xml": HEADER_XML.encode("utf-8"),
        "word/footer1.xml": FOOTER_XML.encode("utf-8"),
        "word/styles.xml": STYLES_XML,
        "word/_rels/document.xml.rels": DOCUMENT_RELS,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in parts.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def zip_names(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.namelist()


def zip_part(data: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(name)


def parse_xml(data: bytes) -> etree._Element:
    return etree.fromstring(data)


def nearest_p(node: etree._Element) -> etree._Element | None:
    cur = node.getparent()
    while cur is not None:
        if cur.tag == W_P:
            return cur
        cur = cur.getparent()
    return None


def paragraph_texts(root: etree._Element) -> list[str]:
    texts: list[str] = []
    for paragraph in root.iter(W_P):
        chunks: list[str] = []
        for node in paragraph.iter(W_T):
            if nearest_p(node) is paragraph:
                chunks.append(node.text or "")
        texts.append("".join(chunks))
    return texts


def bold_prefix_kept(root: etree._Element) -> bool:
    for run in root.iter(W_R):
        props = run.find(W_RPR)
        if props is None or props.find(W_B) is None:
            continue
        text = "".join((node.text or "") for node in run.findall(W_T))
        if text == "加粗":
            return True
    return False


FILLED = {
    "甲方名称": "duoling教育科技有限公司",
    "途径地": "桐庐",
    "甲方税号": "",
    "签订日期": "2026年11月09日",
    "订单号": "26087858",
    "付款方式": "第一句\n第二句",
    "出发地": "杭州",
    "目的地": "千岛湖",
}


class PrepareValuesTests(unittest.TestCase):
    def test_blank_optional_becomes_slash(self) -> None:
        got = prepare_values(
            {"甲方税号": "  ", "途径地": "桐庐"},
            required=[],
            empty_optional="/",
        )
        self.assertEqual(got["甲方税号"], "/")
        self.assertEqual(got["途径地"], "桐庐")

    def test_missing_required_does_not_apply_slash(self) -> None:
        with self.assertRaises(MissingRequired) as caught:
            prepare_values(
                {"甲方名称": "", "甲方税号": ""},
                required=["甲方名称"],
                empty_optional="/",
            )
        self.assertEqual(caught.exception.missing, ["甲方名称"])


class FillDocxTests(unittest.TestCase):
    def test_split_runs_table_header_footer_and_blank_tax(self) -> None:
        values = prepare_values(FILLED, required=[], empty_optional="/")
        source = build_sample_docx()
        filled, unresolved = fill_docx(source, values)

        self.assertEqual(zip_names(filled), zip_names(source))
        self.assertEqual(zip_part(filled, "word/styles.xml"), STYLES_XML)

        document = parse_xml(zip_part(filled, "word/document.xml"))
        texts = paragraph_texts(document)
        self.assertIn("加粗duoling教育科技有限公司", texts)
        self.assertIn("桐庐", texts)
        self.assertIn("/", texts)
        self.assertIn("第一句第二句", texts)
        self.assertIn("杭州-千岛湖", texts)
        self.assertIn("{{保险名称}}", texts)
        self.assertIn("上一段{{途", texts)
        self.assertIn("径地}}下一段", texts)
        self.assertNotIn("上一段桐庐下一段", texts)
        self.assertTrue(bold_prefix_kept(document))
        self.assertTrue(document.find(f".//{W_BR}") is not None)
        self.assertEqual(unresolved, ["保险名称"])
        document_xml = zip_part(filled, "word/document.xml").decode("utf-8")
        self.assertIn('w:eastAsia="仿宋"', document_xml)
        self.assertIn('w:firstLine="480"', document_xml)
        self.assertIn("<!--format-keep-->", document_xml)
        self.assertIn("<w:b/>", document_xml)

        header = paragraph_texts(parse_xml(zip_part(filled, "word/header1.xml")))
        footer = paragraph_texts(parse_xml(zip_part(filled, "word/footer1.xml")))
        self.assertEqual(header, ["2026年11月09日"])
        self.assertEqual(footer, ["26087858"])

    def test_repairs_textutil_indent_without_touching_fonts(self) -> None:
        document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:ind w:first-line="420"/></w:pPr>
      <w:r>
        <w:rPr><w:rFonts w:ascii="Times" w:hAnsi="Times" w:cs="Times"/><w:sz w:val="21"/><w:sz-cs w:val="21"/></w:rPr>
        <w:t xml:space="preserve">{{甲方名称}}</w:t>
      </w:r>
    </w:p>
  </w:body>
</w:document>
"""
        source = _docx({"word/document.xml": document.encode("utf-8")})
        filled, unresolved = fill_docx(source, {"甲方名称": "A&B"})
        xml = zip_part(filled, "word/document.xml").decode("utf-8")
        self.assertEqual(unresolved, [])
        self.assertIn("A&amp;B", xml)
        self.assertIn('w:firstLine="420"', xml)
        self.assertNotIn("first-line", xml)
        self.assertIn("<w:szCs ", xml)
        self.assertNotIn("sz-cs", xml)
        self.assertIn('w:ascii="Times"', xml)


def _docx(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in parts.items():
            zf.writestr(name, payload)
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
