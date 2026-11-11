"""Replace {{key}} inside one paragraph, including runs Word has split."""

from __future__ import annotations

import io
import re
import zipfile

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
W_P = f"{{{W}}}p"
W_T = f"{{{W}}}t"
W_BR = f"{{{W}}}br"

PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")


class InvalidDocx(Exception):
    pass


def fill_docx(data: bytes, values: dict[str, str]) -> tuple[bytes, list[str]]:
    """Fill document, header, and footer parts. Other zip entries are copied."""
    try:
        source = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InvalidDocx("not a docx zip") from exc

    unresolved: list[str] = []
    seen: set[str] = set()
    out = io.BytesIO()
    with source, zipfile.ZipFile(out, "w") as dest:
        for info in source.infolist():
            raw = source.read(info.filename)
            if _is_fill_part(info.filename):
                try:
                    raw, keys = fill_xml(raw, values)
                except etree.XMLSyntaxError as exc:
                    raise InvalidDocx(info.filename) from exc
                for key in keys:
                    if key not in seen:
                        seen.add(key)
                        unresolved.append(key)
            dest.writestr(info.filename, raw)
    return out.getvalue(), unresolved


def fill_xml(xml: bytes, values: dict[str, str]) -> tuple[bytes, list[str]]:
    root = etree.fromstring(xml)
    text_nodes = list(root.iter(W_T))
    original_ids = {id(node) for node in text_nodes}
    edits: dict[int, str] = {}
    extras: dict[int, str] = {}
    for paragraph in list(root.iter(W_P)):
        _replace_in_paragraph(paragraph, values, edits, extras, original_ids)
    rendered = _patch_xml(xml, text_nodes, edits, extras)
    return rendered, _unresolved_keys(root)


def _is_fill_part(name: str) -> bool:
    prefix = "word/"
    if not name.startswith(prefix):
        return False
    leaf = name[len(prefix):]
    if "/" in leaf:
        return False
    if leaf == "document.xml":
        return True
    return (leaf.startswith("header") or leaf.startswith("footer")) and leaf.endswith(".xml")


def _replace_in_paragraph(
    paragraph: etree._Element,
    values: dict[str, str],
    edits: dict[int, str],
    extras: dict[int, str],
    original_ids: set[int],
) -> None:
    nodes = _text_nodes(paragraph)
    if not nodes:
        return
    spans: list[tuple[etree._Element, int, int]] = []
    pos = 0
    for node in nodes:
        text = node.text or ""
        spans.append((node, pos, pos + len(text)))
        pos += len(text)
    full = "".join((node.text or "") for node in nodes)
    for match in reversed(list(PLACEHOLDER_RE.finditer(full))):
        key = match.group(1).strip()
        if not key or key not in values:
            continue
        _apply_span(spans, match.start(), match.end(), str(values[key]), edits, extras, original_ids)


def _apply_span(
    spans: list[tuple[etree._Element, int, int]],
    match_start: int,
    match_end: int,
    replacement: str,
    edits: dict[int, str],
    extras: dict[int, str],
    original_ids: set[int],
) -> None:
    overlapping = [
        (node, start, end)
        for node, start, end in spans
        if start < match_end and end > match_start
    ]
    if not overlapping:
        return
    first, first_start, _first_end = overlapping[0]
    last, last_start, _last_end = overlapping[-1]
    prefix = (first.text or "")[: match_start - first_start]
    suffix = (last.text or "")[match_end - last_start :]
    if first is last:
        _write_replacement(first, prefix, replacement, suffix, edits, extras, original_ids)
        return
    _write_replacement(first, prefix, replacement, "", edits, extras, original_ids)
    for node, _start, _end in overlapping[1:-1]:
        _set_text(node, "", edits, original_ids)
    _set_text(last, suffix, edits, original_ids)


def _write_replacement(
    node: etree._Element,
    prefix: str,
    replacement: str,
    suffix: str,
    edits: dict[int, str],
    extras: dict[int, str],
    original_ids: set[int],
) -> None:
    lines = replacement.split("\n")
    if len(lines) == 1:
        _set_text(node, prefix + lines[0] + suffix, edits, original_ids)
        return
    _set_text(node, prefix + lines[0], edits, original_ids)
    anchor: etree._Element = node
    rest = lines[1:]
    extra_parts: list[str] = []
    ns = node.prefix or "w"
    for index, line in enumerate(rest):
        line_break = etree.Element(W_BR)
        anchor.addnext(line_break)
        text_node = etree.Element(W_T)
        piece = line + suffix if index == len(rest) - 1 else line
        _set_text(text_node, piece, edits, original_ids)
        line_break.addnext(text_node)
        anchor = text_node
        space = ' xml:space="preserve"' if _needs_preserve(piece) else ""
        extra_parts.append(
            f"<{ns}:br/><{ns}:t{space}>{_xml_text(piece)}</{ns}:t>"
        )
    if id(node) in original_ids:
        extras[id(node)] = "".join(extra_parts)


def _set_text(
    node: etree._Element,
    value: str,
    edits: dict[int, str],
    original_ids: set[int],
) -> None:
    node.text = value
    if _needs_preserve(value):
        node.set(XML_SPACE, "preserve")
    if id(node) in original_ids:
        edits[id(node)] = value


def _patch_xml(
    xml: bytes,
    nodes: list[etree._Element],
    edits: dict[int, str],
    extras: dict[int, str],
) -> bytes:
    text = _repair_ooxml_names(xml.decode("utf-8"))
    pieces: list[str] = []
    cursor = 0
    index = 0
    while True:
        match = _OPEN_T.search(text, cursor)
        if not match:
            pieces.append(text[cursor:])
            break
        if index >= len(nodes):
            raise InvalidDocx("text node count mismatch")
        prefix = match.group(1)
        attrs = match.group(2)
        self_closing = attrs.rstrip().endswith("/")
        if self_closing:
            element_end = match.end()
            close = ""
        else:
            close = f"</{prefix}:t>"
            close_at = text.find(close, match.end())
            if close_at < 0:
                raise InvalidDocx("unclosed text node")
            element_end = close_at + len(close)
        node = nodes[index]
        index += 1
        pieces.append(text[cursor:match.start()])
        if id(node) not in edits:
            pieces.append(text[match.start():element_end])
        else:
            value = edits[id(node)]
            open_tag = match.group(0)
            if self_closing:
                open_tag = open_tag[:-2].rstrip() + ">"
                close = f"</{prefix}:t>"
            open_tag = _ensure_preserve(open_tag, value)
            pieces.append(open_tag + _xml_text(value) + close)
            pieces.append(extras.get(id(node), ""))
        cursor = element_end
    if index != len(nodes):
        raise InvalidDocx("text node count mismatch")
    return "".join(pieces).encode("utf-8")


def _repair_ooxml_names(xml: str) -> str:
    """textutil writes CSS-style names Word does not apply."""
    xml = re.sub(r"\b([\w.-]+):first-line=", r"\1:firstLine=", xml)
    xml = re.sub(r"(</?)([\w.-]+):sz-cs\b", r"\1\2:szCs", xml)
    return xml


_OPEN_T = re.compile(r"<([A-Za-z_][\w.-]*):t\b([^>]*)>")


def _xml_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _needs_preserve(value: str) -> bool:
    return bool(value) and (value[:1].isspace() or value[-1:].isspace())


def _ensure_preserve(open_tag: str, value: str) -> str:
    if not _needs_preserve(value) or "xml:space" in open_tag:
        return open_tag
    if open_tag.endswith("/>"):
        return open_tag[:-2] + ' xml:space="preserve"/>'
    return open_tag[:-1] + ' xml:space="preserve">'


def _text_nodes(paragraph: etree._Element) -> list[etree._Element]:
    return [node for node in paragraph.iter(W_T) if _nearest_paragraph(node) is paragraph]


def _nearest_paragraph(node: etree._Element) -> etree._Element | None:
    current = node.getparent()
    while current is not None:
        if current.tag == W_P:
            return current
        current = current.getparent()
    return None


def _unresolved_keys(root: etree._Element) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for paragraph in root.iter(W_P):
        chunks: list[str] = []
        for node in paragraph.iter(W_T):
            if _nearest_paragraph(node) is paragraph:
                chunks.append(node.text or "")
        for match in PLACEHOLDER_RE.finditer("".join(chunks)):
            key = match.group(1).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            found.append(key)
    return found
