"""Insert a table in place of a paragraph whose text is exactly {{anchor}}."""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass

from fill import InvalidDocx

FALLBACK_TEXT_WIDTH = 8306

_TOKEN = re.compile(r"<(/?)([A-Za-z_][\w.-]*):(p|tbl)\b([^>]*)>")
_SECT = re.compile(r"<([A-Za-z_][\w.-]*):sectPr\b.*?</\1:sectPr>", re.DOTALL)
_OPEN_T = re.compile(r"<([A-Za-z_][\w.-]*):t\b([^>]*)>")


class InvalidTable(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class TableAnchorNotFound(Exception):
    def __init__(self, anchor: str):
        self.anchor = anchor
        super().__init__(anchor)


class TableAnchorDuplicate(Exception):
    def __init__(self, anchor: str):
        self.anchor = anchor
        super().__init__(anchor)


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    width: int | None
    align: str


@dataclass(frozen=True)
class Table:
    anchor: str
    columns: tuple[Column, ...]
    rows: tuple[dict, ...]
    header: bool
    borders: bool
    empty_cell: str


@dataclass(frozen=True)
class _Paragraph:
    start: int
    end: int
    text: str
    prefix: str
    font: str
    xml: str


def normalize_tables(raw: object, empty_optional: str = "") -> list[Table]:
    """Validate the request tables before a document is opened."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InvalidTable("tables must be a list")
    tables: list[Table] = []
    seen: set[str] = set()
    for item in raw:
        table = _normalize_table(item, empty_optional)
        if table.anchor in seen:
            raise TableAnchorDuplicate(table.anchor)
        seen.add(table.anchor)
        tables.append(table)
    return tables


def apply_tables(data: bytes, tables: list[Table]) -> bytes:
    """Replace anchor paragraphs in word/document.xml. Other parts are copied."""
    if not tables:
        return data
    try:
        source = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InvalidDocx("not a docx zip") from exc
    with source:
        try:
            document = source.read("word/document.xml")
        except KeyError as exc:
            raise InvalidDocx("word/document.xml") from exc
        updated = splice_document(document, tables)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as dest:
            for info in source.infolist():
                raw = updated if info.filename == "word/document.xml" else source.read(info.filename)
                dest.writestr(info.filename, raw)
    return out.getvalue()


def splice_document(xml: bytes, tables: list[Table]) -> bytes:
    try:
        text = xml.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidDocx("word/document.xml") from exc
    paragraphs = list(_body_paragraphs(text))
    replacements: list[tuple[int, int, str]] = []
    for table in tables:
        token = "{{" + table.anchor + "}}"
        matches = [paragraph for paragraph in paragraphs if token in paragraph.text]
        if not matches:
            raise TableAnchorNotFound(table.anchor)
        if len(matches) > 1 or any(paragraph.text.count(token) > 1 for paragraph in matches):
            raise TableAnchorDuplicate(table.anchor)
        paragraph = matches[0]
        widths = _column_widths(table.columns, _text_width(text, paragraph))
        built = _render_table(paragraph, table, widths)
        edited = _cut_token(paragraph.xml, token, paragraph.prefix)
        if _paragraph_text(edited).strip() or re.search(rf"<{paragraph.prefix}:br\b", edited):
            built += edited
        replacements.append((paragraph.start, paragraph.end, built))
    for start, end, built in sorted(replacements, reverse=True):
        text = text[:start] + built + text[end:]
    return text.encode("utf-8")


def _normalize_table(item: object, empty_optional: str) -> Table:
    if not isinstance(item, dict):
        raise InvalidTable("table must be an object")
    anchor = item.get("anchor")
    if not isinstance(anchor, str) or not anchor.strip() or "{" in anchor or "}" in anchor:
        raise InvalidTable("anchor is invalid")
    columns_raw = item.get("columns")
    if not isinstance(columns_raw, list) or not columns_raw:
        raise InvalidTable("columns is required")
    columns = tuple(_normalize_column(column) for column in columns_raw)
    rows_raw = item.get("rows", [])
    if rows_raw is None:
        rows_raw = []
    if not isinstance(rows_raw, list):
        raise InvalidTable("rows must be a list")
    rows: list[dict] = []
    for row in rows_raw:
        if not isinstance(row, dict) or any(not isinstance(key, str) for key in row):
            raise InvalidTable("table row must be an object")
        for value in row.values():
            _cell_text(value, "")
        rows.append(row)
    header = item.get("header", True)
    borders = item.get("borders", True)
    if not isinstance(header, bool) or not isinstance(borders, bool):
        raise InvalidTable("header and borders must be booleans")
    if "empty_cell" in item and item.get("empty_cell") is not None:
        empty_cell = item.get("empty_cell")
        if not isinstance(empty_cell, str):
            raise InvalidTable("empty_cell must be a string")
    else:
        empty_cell = empty_optional
    return Table(anchor.strip(), columns, tuple(rows), header, borders, empty_cell)


def _normalize_column(item: object) -> Column:
    if not isinstance(item, dict):
        raise InvalidTable("column must be an object")
    key = item.get("key")
    if not isinstance(key, str) or not key.strip():
        raise InvalidTable("column key is required")
    header = item.get("header", key.strip())
    if not isinstance(header, str):
        raise InvalidTable("column header must be a string")
    width = item.get("width")
    if width is not None and (isinstance(width, bool) or not isinstance(width, int) or width <= 0):
        raise InvalidTable("column width must be a positive integer")
    align = item.get("align", "left")
    if align not in {"left", "center", "right"}:
        raise InvalidTable("column align must be left, center, or right")
    return Column(key.strip(), header, width, align)


def _cell_text(value: object, empty: str) -> str:
    if value is None:
        return empty
    if isinstance(value, bool):
        raise InvalidTable("table cell must be a string, number, or null")
    if isinstance(value, str):
        return value if value.strip() else empty
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise InvalidTable("table cell must be a string, number, or null")
        return _format_float(value)
    raise InvalidTable("table cell must be a string, number, or null")


def _format_float(value: float) -> str:
    text = format(value, ".15f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _column_widths(columns: tuple[Column, ...], text_width: int) -> list[int]:
    missing = [index for index, column in enumerate(columns) if column.width is None]
    if not missing:
        return [column.width or 0 for column in columns]
    used = sum(column.width or 0 for column in columns)
    remain = text_width - used
    base, extra = divmod(remain, len(missing))
    if base <= 0:
        raise InvalidTable("column widths exceed the text width")
    widths: list[int] = []
    missing_at = 0
    for column in columns:
        if column.width is not None:
            widths.append(column.width)
            continue
        widths.append(base + (1 if missing_at < extra else 0))
        missing_at += 1
    return widths


def _body_paragraphs(xml: str) -> list[_Paragraph]:
    found: list[_Paragraph] = []
    tbl_depth = 0
    start: int | None = None
    prefix = "w"
    for match in _TOKEN.finditer(xml):
        closing, tag_prefix, kind, attrs = match.group(1), match.group(2), match.group(3), match.group(4)
        self_closing = attrs.rstrip().endswith("/")
        if kind == "tbl":
            if closing:
                tbl_depth = max(0, tbl_depth - 1)
            elif not self_closing:
                tbl_depth += 1
            continue
        if tbl_depth:
            continue
        if closing:
            if start is None:
                continue
            span = xml[start:match.end()]
            found.append(
                _Paragraph(
                    start,
                    match.end(),
                    _paragraph_text(span),
                    prefix,
                    _font_markup(span, prefix),
                    span,
                )
            )
            start = None
            continue
        if self_closing or start is not None:
            continue
        start = match.start()
        prefix = tag_prefix
    return found


@dataclass(frozen=True)
class _TextNode:
    text: str
    joined_start: int
    joined_end: int
    content_start: int
    content_end: int
    element_start: int


def _cut_token(xml: str, token: str, prefix: str) -> str:
    nodes = _indexed_text_nodes(xml)
    joined = "".join(node.text for node in nodes)
    start = joined.find(token)
    if start < 0:
        return xml
    end = start + len(token)
    edits: list[tuple[int, int, str]] = []
    consumed: set[int] = set()
    for node in nodes:
        if node.joined_end <= start or node.joined_start >= end or id(node) in consumed:
            continue
        run = _run_bounds(xml, node.element_start, prefix)
        if run is not None:
            run_xml = xml[run[0]:run[1]]
            run_nodes = [
                item
                for item in nodes
                if run[0] <= item.element_start and item.content_end <= run[1]
            ]
            fully = all(start <= item.joined_start and item.joined_end <= end for item in run_nodes)
            structural = re.search(rf"<{prefix}:(br|tab|drawing|object|pict)\b", run_xml)
            if fully and run_nodes and not structural:
                edits.append((run[0], run[1], ""))
                consumed.update(id(item) for item in run_nodes)
                continue
        local_start = max(0, start - node.joined_start)
        local_end = min(len(node.text), end - node.joined_start)
        kept = node.text[:local_start] + node.text[local_end:]
        edits.append((node.content_start, node.content_end, _xml_text(kept)))
        consumed.add(id(node))
    for edit_start, edit_end, replacement in sorted(edits, reverse=True):
        xml = xml[:edit_start] + replacement + xml[edit_end:]
    return xml


def _indexed_text_nodes(span: str) -> list[_TextNode]:
    nodes: list[_TextNode] = []
    joined = 0
    cursor = 0
    while True:
        match = _OPEN_T.search(span, cursor)
        if not match:
            break
        if match.group(2).rstrip().endswith("/"):
            cursor = match.end()
            continue
        close = f"</{match.group(1)}:t>"
        close_at = span.find(close, match.end())
        if close_at < 0:
            break
        text = _unescape(span[match.end():close_at])
        nodes.append(
            _TextNode(text, joined, joined + len(text), match.end(), close_at, match.start())
        )
        joined += len(text)
        cursor = close_at + len(close)
    return nodes


def _run_bounds(xml: str, pos: int, prefix: str) -> tuple[int, int] | None:
    start = None
    for match in re.finditer(rf"<{prefix}:r\b[^>]*>", xml):
        if match.start() >= pos:
            break
        start = match.start()
    if start is None or xml.rfind(f"</{prefix}:r>", start, pos) != -1:
        return None
    close = xml.find(f"</{prefix}:r>", pos)
    if close < 0:
        return None
    return start, close + len(f"</{prefix}:r>")


def _paragraph_text(span: str) -> str:
    parts: list[str] = []
    cursor = 0
    while True:
        match = _OPEN_T.search(span, cursor)
        if not match:
            break
        if match.group(2).rstrip().endswith("/"):
            cursor = match.end()
            continue
        close = f"</{match.group(1)}:t>"
        close_at = span.find(close, match.end())
        if close_at < 0:
            break
        parts.append(_unescape(span[match.end():close_at]))
        cursor = close_at + len(close)
    return "".join(parts).strip()


def _font_markup(span: str, prefix: str) -> str:
    paragraph_fonts = _markup_in(span, prefix, "pPr")
    runs = re.finditer(rf"<{prefix}:r\b[^>]*>.*?</{prefix}:r>", span, re.DOTALL)
    for run in runs:
        if f"<{prefix}:t" not in run.group(0):
            continue
        props = re.search(rf"<{prefix}:rPr\b[^>]*>(.*?)</{prefix}:rPr>", run.group(0), re.DOTALL)
        if not props:
            return ""
        parts: list[str] = []
        for tag in ("rFonts", "sz", "szCs"):
            element = _element(props.group(1), prefix, tag)
            if element and tag == "rFonts":
                element = _fill_east_asia(element, paragraph_fonts)
            if element:
                parts.append(element)
        return "".join(parts)
    return ""


def _markup_in(span: str, prefix: str, parent: str) -> str:
    block = re.search(rf"<{prefix}:{parent}\b[^>]*>.*?</{prefix}:{parent}>", span, re.DOTALL)
    if not block:
        return ""
    return _element(block.group(0), prefix, "rFonts") or ""


def _element(span: str, prefix: str, tag: str) -> str | None:
    element = re.search(rf"<{prefix}:{tag}\b[^>]*/>", span)
    if element:
        return element.group(0)
    element = re.search(rf"<{prefix}:{tag}\b[^>]*>.*?</{prefix}:{tag}>", span, re.DOTALL)
    if element:
        return element.group(0)
    return None


def _fill_east_asia(fonts: str, fallback: str) -> str:
    if "eastAsia=" in fonts or "eastAsia=" not in fallback:
        return fonts
    match = re.search(r'[\w.-]*:eastAsia="[^"]*"', fallback)
    if not match or not fonts.endswith("/>"):
        return fonts
    return fonts[:-2] + " " + match.group(0) + "/>"


def _text_width(xml: str, paragraph: _Paragraph) -> int:
    inner = _SECT.search(paragraph.xml)
    if inner and (parsed := _parse_text_width(inner.group(0))):
        return parsed
    chosen = None
    for match in _SECT.finditer(xml):
        if match.start() >= paragraph.end:
            chosen = match
            break
    if chosen is None:
        matches = list(_SECT.finditer(xml))
        chosen = matches[-1] if matches else None
    if chosen is None:
        return FALLBACK_TEXT_WIDTH
    return _parse_text_width(chosen.group(0)) or FALLBACK_TEXT_WIDTH


def _parse_text_width(block: str) -> int | None:
    prefix = re.match(r"<([A-Za-z_][\w.-]*):sectPr\b", block)
    if not prefix:
        return None
    tag = prefix.group(1)
    page = re.search(rf"<{tag}:pgSz\b([^>]*)/?>", block)
    margin = re.search(rf"<{tag}:pgMar\b([^>]*)/?>", block)
    if not page or not margin:
        return None
    width = _attr_int(page.group(1), "w")
    left = _attr_int(margin.group(1), "left")
    right = _attr_int(margin.group(1), "right")
    if width is None or left is None or right is None:
        return None
    usable = width - left - right
    if usable <= 0:
        return None
    return usable


def _attr_int(attrs: str, name: str) -> int | None:
    match = re.search(rf'\b[\w.-]*:{name}="(-?\d+)"', attrs)
    if not match:
        return None
    return int(match.group(1))


def _render_table(paragraph: _Paragraph, table: Table, widths: list[int]) -> str:
    prefix = paragraph.prefix
    total = sum(widths)
    borders = _borders(prefix) if table.borders else ""
    grid = "".join(f'<{prefix}:gridCol {prefix}:w="{width}"/>' for width in widths)
    rows: list[str] = []
    if table.header:
        rows.append(
            _render_row(
                prefix,
                [column.header for column in table.columns],
                widths,
                [column.align for column in table.columns],
                paragraph.font + f"<{prefix}:b/>",
                header=True,
            )
        )
    for row in table.rows:
        values = [_cell_text(row.get(column.key), table.empty_cell) for column in table.columns]
        rows.append(
            _render_row(
                prefix,
                values,
                widths,
                [column.align for column in table.columns],
                paragraph.font,
                header=False,
            )
        )
    return (
        f"<{prefix}:tbl>"
        f"<{prefix}:tblPr>"
        f'<{prefix}:tblW {prefix}:w="{total}" {prefix}:type="dxa"/>'
        f'<{prefix}:tblLayout {prefix}:type="fixed"/>'
        f"{borders}"
        f"</{prefix}:tblPr>"
        f"<{prefix}:tblGrid>{grid}</{prefix}:tblGrid>"
        f"{''.join(rows)}"
        f"</{prefix}:tbl>"
    )


def _borders(prefix: str) -> str:
    edge = f'{prefix}:val="single" {prefix}:sz="4" {prefix}:space="0" {prefix}:color="auto"'
    sides = "".join(f"<{prefix}:{side} {edge}/>" for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
    return f"<{prefix}:tblBorders>{sides}</{prefix}:tblBorders>"


def _render_row(
    prefix: str,
    values: list[str],
    widths: list[int],
    aligns: list[str],
    font: str,
    header: bool,
) -> str:
    marker = f"<{prefix}:trPr><{prefix}:tblHeader/></{prefix}:trPr>" if header else ""
    cells = []
    for value, width, align in zip(values, widths, aligns):
        cells.append(
            f"<{prefix}:tc>"
            f'<{prefix}:tcPr><{prefix}:tcW {prefix}:w="{width}" {prefix}:type="dxa"/></{prefix}:tcPr>'
            f"{_render_paragraph(prefix, align, font, value)}"
            f"</{prefix}:tc>"
        )
    return f"<{prefix}:tr>{marker}{''.join(cells)}</{prefix}:tr>"


def _render_paragraph(prefix: str, align: str, font: str, value: str) -> str:
    props = f"<{prefix}:rPr>{font}</{prefix}:rPr>" if font else ""
    runs: list[str] = []
    lines = value.split("\n")
    for index, line in enumerate(lines):
        if index:
            runs.append(f"<{prefix}:r>{props}<{prefix}:br/></{prefix}:r>")
        space = ' xml:space="preserve"' if line[:1].isspace() or line[-1:].isspace() else ""
        runs.append(f"<{prefix}:r>{props}<{prefix}:t{space}>{_xml_text(line)}</{prefix}:t></{prefix}:r>")
    return (
        f"<{prefix}:p>"
        f'<{prefix}:pPr><{prefix}:jc {prefix}:val="{align}"/></{prefix}:pPr>'
        f"{''.join(runs)}"
        f"</{prefix}:p>"
    )


def _xml_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unescape(value: str) -> str:
    value = re.sub(r"&#x([0-9a-fA-F]+);", lambda match: chr(int(match.group(1), 16)), value)
    value = re.sub(r"&#(\d+);", lambda match: chr(int(match.group(1))), value)
    return (
        value.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
    )
