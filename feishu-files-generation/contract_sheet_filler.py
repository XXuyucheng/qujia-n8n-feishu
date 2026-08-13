"""Append contract fee/itinerary as embedded Feishu Sheet blocks under H2 titles."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from sheet_filler import parse_sheet_token

BLOCK_HEADING2 = 4
BLOCK_SHEET = 30
# Feishu create-children Sheet block: row_size max is 9
SHEET_CREATE_MAX_ROWS = 9
SHEET_CREATE_MAX_COLS = 9

H_ALIGN = {"left": 0, "center": 1, "right": 2}
V_ALIGN = {"top": 0, "center": 1, "bottom": 2}
# Feishu docx text/heading style.align: 1 left | 2 center | 3 right
TITLE_ALIGN = {"left": 1, "center": 2, "right": 3}

DEFAULT_FEE_COLUMNS: List[Dict[str, Any]] = [
    {"field": "分类", "header": "分类", "width": 75},
    {"field": "名称", "header": "名称", "width": 150},
    {"field": "描述", "header": "描述", "width": 330},
    {"field": "数量", "header": "数量", "width": 50},
    {"field": "单价", "header": "单价", "width": 60},
    {"field": "总价", "header": "总价", "width": 70},
]

DEFAULT_ITINERARY_COLUMNS: List[Dict[str, Any]] = [
    {"field": "日期", "header": "日期", "width": 50},
    {"field": "时间", "header": "时间", "width": 100},
    {"field": "内容", "header": "内容", "width": 300},
]

DEFAULT_SETTLEMENT_COLUMNS: List[Dict[str, Any]] = [
    {"field": "物品名称", "header": "物品名称", "width": 150},
    {"field": "内容", "header": "内容", "width": 200},
    {"field": "数量", "header": "数量", "width": 40},
    {"field": "单价", "header": "单价", "width": 50},
    {"field": "小计", "header": "小计", "width": 60},
    {"field": "预定状态", "header": "预定状态", "width": 70},
    {"field": "结算方式", "header": "结算方式", "width": 70},
    {"field": "联系人", "header": "联系人", "width": 70},
    {"field": "预定信息", "header": "预定信息", "width": 250},
]


def cell_str(value: Any) -> str:
    """Render a cell value for sheet write."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not (value == value):  # NaN
            return ""
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        text = f"{value:.10f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value).strip()


def _dict_list(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def resolve_fee_rows(fields: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Prefer fields['费用明细']; fall back to 报价明细条目."""
    fields = fields or {}
    items = _dict_list(fields.get("费用明细"))
    if not items:
        items = _dict_list(fields.get("报价明细条目"))
    rows: List[Dict[str, Any]] = []
    for item in items:
        rows.append(
            {
                "分类": item.get("分类") or item.get("类目") or "",
                "名称": item.get("名称") or item.get("物品名称") or "",
                "描述": item.get("描述") or "",
                "数量": item.get("数量"),
                "单价": item.get("单价"),
                "总价": item.get("总价"),
            }
        )
    return rows


def resolve_itinerary_rows(
    fields: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Prefer fields['活动行程']; fall back to 活动行程条目."""
    fields = fields or {}
    items = _dict_list(fields.get("活动行程"))
    if not items:
        items = _dict_list(fields.get("活动行程条目"))
    rows: List[Dict[str, Any]] = []
    for item in items:
        rows.append(
            {
                "日期": item.get("日期") or item.get("day") or "",
                "时间": item.get("时间") or "",
                "内容": item.get("内容") or "",
            }
        )
    return rows


def _as_number(value: Any) -> Any:
    """Return int/float for sheet numeric cells, else original / empty."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        if value == int(value) and abs(value) < 1e15:
            return int(value)
        return value
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return ""
    try:
        num = float(text)
    except ValueError:
        return str(value).strip()
    if num == int(num) and abs(num) < 1e15:
        return int(num)
    return num


def resolve_settlement_rows(
    sheet_rows: Optional[Sequence[Dict[str, Any]]] = None,
    fields: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Prefer explicit sheet_rows; else fields['报价明细条目']. 小计 left empty."""
    items: List[Dict[str, Any]] = []
    if sheet_rows:
        items = [dict(r) for r in sheet_rows if isinstance(r, dict)]
    if not items:
        fields = fields or {}
        items = _dict_list(fields.get("报价明细条目"))
        if not items:
            items = _dict_list(fields.get("费用明细"))

    rows: List[Dict[str, Any]] = []
    for item in items:
        rows.append(
            {
                "物品名称": item.get("物品名称")
                or item.get("名称")
                or item.get("name")
                or "",
                "内容": item.get("内容") or item.get("描述") or "",
                "数量": item.get("数量"),
                "单价": item.get("单价"),
                "小计": "",  # formula written separately
                "预定状态": item.get("预定状态") or "",
                "结算方式": item.get("结算方式") or "",
                "联系人": item.get("联系人") or "",
                "预定信息": item.get("预定信息") or "",
            }
        )
    return rows


def build_settlement_matrix(
    columns: Sequence[Dict[str, Any]],
    data_rows: Sequence[Dict[str, Any]],
) -> List[List[Any]]:
    """Header + data rows; 数量/单价 as numbers; 小计 always empty."""
    headers = [str(c.get("header") or c.get("field") or "") for c in columns]
    matrix: List[List[Any]] = [headers]
    fields = [str(c.get("field") or "") for c in columns]
    for row in data_rows:
        cells: List[Any] = []
        for field in fields:
            if field == "小计":
                cells.append("")
            elif field in {"数量", "单价"}:
                cells.append(_as_number(row.get(field)))
            else:
                cells.append(cell_str(row.get(field)))
        matrix.append(cells)
    return matrix


def _formula_cell(text: str) -> Dict[str, str]:
    return {"type": "formula", "text": text}


def _field_col_letter(
    columns: Sequence[Dict[str, Any]], field: str
) -> Optional[str]:
    for i, col in enumerate(columns):
        if str(col.get("field") or "") == field:
            return col_letter(i)
    return None


def build_settlement_formula_ranges(
    sheet_id: str,
    columns: Sequence[Dict[str, Any]],
    data_row_count: int,
    *,
    total_label: str = "总价",
    total_label_field: str = "物品名称",
    qty_field: str = "数量",
    price_field: str = "单价",
    subtotal_field: str = "小计",
) -> List[Dict[str, Any]]:
    """Subtotal formulas for data rows + total SUM row."""
    if data_row_count <= 0:
        return []

    qty_col = _field_col_letter(columns, qty_field)
    price_col = _field_col_letter(columns, price_field)
    subtotal_col = _field_col_letter(columns, subtotal_field)
    label_col = _field_col_letter(columns, total_label_field) or "A"
    if not qty_col or not price_col or not subtotal_col:
        raise ValueError(
            "settlement columns must include 数量 / 单价 / 小计 for formulas"
        )

    start_row = 2
    end_data_row = start_row + data_row_count - 1
    total_row = end_data_row + 1

    subtotal_values: List[List[Any]] = []
    for r in range(start_row, end_data_row + 1):
        text = (
            f'=IF(OR({qty_col}{r}="",{price_col}{r}=""),"",'
            f"{qty_col}{r}*{price_col}{r})"
        )
        subtotal_values.append([_formula_cell(text)])

    ranges: List[Dict[str, Any]] = [
        {
            "range": (
                f"{sheet_id}!{subtotal_col}{start_row}:"
                f"{subtotal_col}{end_data_row}"
            ),
            "values": subtotal_values,
        }
    ]

    # Total label
    ranges.append(
        {
            "range": f"{sheet_id}!{label_col}{total_row}:{label_col}{total_row}",
            "values": [[total_label]],
        }
    )
    # Total SUM on subtotal column
    ranges.append(
        {
            "range": (
                f"{sheet_id}!{subtotal_col}{total_row}:"
                f"{subtotal_col}{total_row}"
            ),
            "values": [
                [
                    _formula_cell(
                        f"=SUM({subtotal_col}{start_row}:{subtotal_col}{end_data_row})"
                    )
                ]
            ],
        }
    )

    # Clear other cells on total row
    for i, col in enumerate(columns):
        field = str(col.get("field") or "")
        letter = col_letter(i)
        if field in {total_label_field, subtotal_field}:
            continue
        if letter == label_col or letter == subtotal_col:
            continue
        ranges.append(
            {
                "range": f"{sheet_id}!{letter}{total_row}:{letter}{total_row}",
                "values": [[""]],
            }
        )

    return ranges


def align_code(value: Any, mapping: Dict[str, int], default: int = 1) -> int:
    if isinstance(value, int) and value in (0, 1, 2):
        return value
    key = str(value or "").strip().lower()
    return mapping.get(key, default)


def normalize_columns(
    raw: Any,
    defaults: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        return [dict(c) for c in defaults]
    cols: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or item.get("header") or "").strip()
        if not field:
            continue
        header = str(item.get("header") or field).strip()
        width = item.get("width")
        try:
            width_i = int(width) if width is not None else 100
        except (TypeError, ValueError):
            width_i = 100
        cols.append({"field": field, "header": header, "width": max(width_i, 1)})
    return cols or [dict(c) for c in defaults]


def title_align_code(value: Any, default: str = "left") -> int:
    """Map YAML title_align to Feishu heading style.align (1/2/3)."""
    if isinstance(value, int) and value in (1, 2, 3):
        return value
    key = str(value if value is not None else default).strip().lower()
    return TITLE_ALIGN.get(key, TITLE_ALIGN.get(str(default).lower(), 1))


def parse_style_cfg(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "h_align": align_code(raw.get("h_align", "center"), H_ALIGN, 1),
        "v_align": align_code(raw.get("v_align", "center"), V_ALIGN, 1),
        "font_size": str(raw.get("font_size") or "10pt/1.5").strip() or "10pt/1.5",
        "header_bold": bool(raw.get("header_bold", True)),
        # H2 title alignment for appended sheets (left | center | right)
        "title_align": title_align_code(raw.get("title_align", "left"), "left"),
    }


def col_letter(index_zero_based: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    n = index_zero_based + 1
    letters: List[str] = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def build_sheet_matrix(
    columns: Sequence[Dict[str, Any]],
    data_rows: Sequence[Dict[str, Any]],
) -> List[List[Any]]:
    headers = [str(c.get("header") or c.get("field") or "") for c in columns]
    matrix: List[List[Any]] = [headers]
    fields = [str(c.get("field") or "") for c in columns]
    for row in data_rows:
        matrix.append([cell_str(row.get(f)) for f in fields])
    return matrix


def _heading2_child(title: str, *, align: int = 1) -> Dict[str, Any]:
    """Create heading2 block; align is Feishu style.align (1 left / 2 center / 3 right)."""
    align_i = align if align in (1, 2, 3) else 1
    return {
        "block_type": BLOCK_HEADING2,
        "heading2": {
            "elements": [{"text_run": {"content": str(title or "")}}],
            "style": {"align": align_i},
        },
    }


def _sheet_child(row_size: int, column_size: int) -> Dict[str, Any]:
    return {
        "block_type": BLOCK_SHEET,
        "sheet": {
            "row_size": min(max(int(row_size), 1), SHEET_CREATE_MAX_ROWS),
            "column_size": min(max(int(column_size), 1), SHEET_CREATE_MAX_COLS),
        },
    }


def ensure_sheet_row_count(
    client: Any,
    token: str,
    spreadsheet_token: str,
    sheet_id: str,
    *,
    created_rows: int,
    needed_rows: int,
) -> None:
    """Grow sheet rows at the end when create was capped at max 9.

    Uses add-rows API (POST dimension_range + length), not insert_dimension_range,
    because insert endIndex cannot exceed the current sheetMaxRowCount.
    """
    created = max(int(created_rows), 0)
    needed = max(int(needed_rows), 0)
    extra = needed - created
    if extra <= 0:
        return
    client.add_dimension_range(
        token,
        spreadsheet_token,
        sheet_id,
        extra,
        major_dimension="ROWS",
    )


def _extract_sheet_token(create_data: Dict[str, Any]) -> str:
    for block in create_data.get("children") or []:
        if not isinstance(block, dict):
            continue
        if int(block.get("block_type") or 0) != BLOCK_SHEET:
            continue
        sheet = block.get("sheet") or {}
        token = sheet.get("token")
        if token:
            return str(token)
    raise ValueError("create_block_children response missing sheet.token")


def _apply_column_widths(
    client: Any,
    token: str,
    spreadsheet_token: str,
    sheet_id: str,
    columns: Sequence[Dict[str, Any]],
) -> None:
    for i, col in enumerate(columns):
        width = int(col.get("width") or 100)
        # Feishu indexes are 1-based inclusive
        client.update_dimension_range(
            token,
            spreadsheet_token,
            sheet_id,
            i + 1,
            i + 1,
            major_dimension="COLUMNS",
            fixed_size=width,
        )


def _apply_styles(
    client: Any,
    token: str,
    spreadsheet_token: str,
    sheet_id: str,
    *,
    row_count: int,
    col_count: int,
    style: Dict[str, Any],
) -> None:
    if row_count < 1 or col_count < 1:
        return
    end_col = col_letter(col_count - 1)
    end_row = row_count
    full_range = f"{sheet_id}!A1:{end_col}{end_row}"
    base_style: Dict[str, Any] = {
        "font": {"fontSize": style["font_size"], "bold": False},
        "hAlign": style["h_align"],
        "vAlign": style["v_align"],
    }
    items: List[Dict[str, Any]] = [
        {"ranges": [full_range], "style": base_style},
    ]
    if style.get("header_bold") and row_count >= 1:
        header_range = f"{sheet_id}!A1:{end_col}1"
        items.append(
            {
                "ranges": [header_range],
                "style": {
                    "font": {"fontSize": style["font_size"], "bold": True},
                    "hAlign": style["h_align"],
                    "vAlign": style["v_align"],
                },
            }
        )
    client.styles_batch_update(token, spreadsheet_token, items)


def fill_one_detail_sheet(
    client: Any,
    token: str,
    document_id: str,
    *,
    title: str,
    columns: Sequence[Dict[str, Any]],
    data_rows: Sequence[Dict[str, Any]],
    style: Dict[str, Any],
) -> None:
    """Create H2 + Sheet at doc end, write values, set widths and styles."""
    col_count = len(columns)
    matrix = build_sheet_matrix(columns, data_rows)
    row_count = len(matrix)
    create_rows = min(row_count, SHEET_CREATE_MAX_ROWS)
    create_cols = min(col_count, SHEET_CREATE_MAX_COLS)

    title_align = int(style.get("title_align") or 1)
    create_data = client.create_block_children(
        token,
        document_id,
        document_id,
        [
            _heading2_child(title, align=title_align),
            _sheet_child(create_rows, create_cols),
        ],
    )
    raw_token = _extract_sheet_token(create_data)
    spreadsheet_token, sheet_id = parse_sheet_token(raw_token)

    ensure_sheet_row_count(
        client,
        token,
        spreadsheet_token,
        sheet_id,
        created_rows=create_rows,
        needed_rows=row_count,
    )

    end_col = col_letter(col_count - 1)
    value_range = {
        "range": f"{sheet_id}!A1:{end_col}{row_count}",
        "values": matrix,
    }
    client.values_batch_update(token, spreadsheet_token, [value_range])
    _apply_column_widths(client, token, spreadsheet_token, sheet_id, columns)
    _apply_styles(
        client,
        token,
        spreadsheet_token,
        sheet_id,
        row_count=row_count,
        col_count=col_count,
        style=style,
    )


def append_settlement_sheet(
    client: Any,
    token: str,
    document_id: str,
    *,
    sheet_rows: Optional[Sequence[Dict[str, Any]]] = None,
    fields: Optional[Dict[str, Any]] = None,
    settlement_cfg: Optional[Dict[str, Any]] = None,
    style: Optional[Dict[str, Any]] = None,
) -> int:
    """Create H2 + settlement Sheet at doc end with subtotal/total formulas.

    Returns number of data rows written (excludes header and total).
    """
    cfg = settlement_cfg if isinstance(settlement_cfg, dict) else {}
    style_cfg = style if isinstance(style, dict) else parse_style_cfg({})
    title = str(cfg.get("title") or "预定结算").strip() or "预定结算"
    # settlement.title_align overrides style.title_align
    if "title_align" in cfg:
        title_align = title_align_code(cfg.get("title_align"), "left")
    else:
        title_align = int(style_cfg.get("title_align") or 1)
    columns = normalize_columns(cfg.get("columns"), DEFAULT_SETTLEMENT_COLUMNS)
    data_rows = resolve_settlement_rows(sheet_rows=sheet_rows, fields=fields)

    matrix = build_settlement_matrix(columns, data_rows)
    # header + data + total row
    total_row_count = len(matrix) + (1 if data_rows else 0)
    if not data_rows:
        # still create header-only sheet + empty total-friendly layout
        total_row_count = 1

    col_count = len(columns)
    create_rows = min(max(total_row_count, 1), SHEET_CREATE_MAX_ROWS)
    create_cols = min(col_count, SHEET_CREATE_MAX_COLS)

    create_data = client.create_block_children(
        token,
        document_id,
        document_id,
        [
            _heading2_child(title, align=title_align),
            _sheet_child(create_rows, create_cols),
        ],
    )
    raw_token = _extract_sheet_token(create_data)
    spreadsheet_token, sheet_id = parse_sheet_token(raw_token)

    ensure_sheet_row_count(
        client,
        token,
        spreadsheet_token,
        sheet_id,
        created_rows=create_rows,
        needed_rows=max(total_row_count, 1),
    )

    end_col = col_letter(col_count - 1)
    data_row_count = len(data_rows)
    value_end = 1 + data_row_count  # header + data
    value_range = {
        "range": f"{sheet_id}!A1:{end_col}{value_end}",
        "values": matrix,
    }
    client.values_batch_update(token, spreadsheet_token, [value_range])

    if data_row_count > 0:
        formula_ranges = build_settlement_formula_ranges(
            sheet_id,
            columns,
            data_row_count,
            total_label=str(cfg.get("total_label") or "总价"),
            total_label_field=str(cfg.get("total_label_field") or "物品名称"),
            qty_field=str(cfg.get("qty_field") or "数量"),
            price_field=str(cfg.get("price_field") or "单价"),
            subtotal_field=str(cfg.get("subtotal_field") or "小计"),
        )
        if formula_ranges:
            client.values_batch_update(
                token, spreadsheet_token, formula_ranges
            )

    style_rows = value_end + (1 if data_row_count > 0 else 0)
    _apply_column_widths(client, token, spreadsheet_token, sheet_id, columns)
    _apply_styles(
        client,
        token,
        spreadsheet_token,
        sheet_id,
        row_count=style_rows,
        col_count=col_count,
        style=style_cfg,
    )
    return data_row_count


def append_contract_detail_sheets(
    client: Any,
    token: str,
    document_id: str,
    fields: Optional[Dict[str, Any]] = None,
    detail_sheets_cfg: Optional[Dict[str, Any]] = None,
    sheet_rows: Optional[Sequence[Dict[str, Any]]] = None,
) -> int:
    """Append embedded detail sheets. Returns sheets created (0–2).

    If ``detail_sheets.settlement`` is set (出团计划单), only the settlement
    sheet is appended. Otherwise appends contract 费用明细 + 行程明细.
    """
    cfg = detail_sheets_cfg if isinstance(detail_sheets_cfg, dict) else {}
    style = parse_style_cfg(cfg.get("style") if isinstance(cfg.get("style"), dict) else {})

    settlement_cfg = cfg.get("settlement") if isinstance(cfg.get("settlement"), dict) else None
    if settlement_cfg is not None:
        append_settlement_sheet(
            client,
            token,
            document_id,
            sheet_rows=sheet_rows,
            fields=fields,
            settlement_cfg=settlement_cfg,
            style=style,
        )
        return 1

    fee_cfg = cfg.get("fee") if isinstance(cfg.get("fee"), dict) else {}
    trip_cfg = cfg.get("itinerary") if isinstance(cfg.get("itinerary"), dict) else {}

    fee_title = str(fee_cfg.get("title") or "费用明细").strip() or "费用明细"
    trip_title = str(trip_cfg.get("title") or "行程明细").strip() or "行程明细"
    fee_cols = normalize_columns(fee_cfg.get("columns"), DEFAULT_FEE_COLUMNS)
    trip_cols = normalize_columns(trip_cfg.get("columns"), DEFAULT_ITINERARY_COLUMNS)

    fee_rows = resolve_fee_rows(fields)
    trip_rows = resolve_itinerary_rows(fields)

    fill_one_detail_sheet(
        client,
        token,
        document_id,
        title=fee_title,
        columns=fee_cols,
        data_rows=fee_rows,
        style=style,
    )
    fill_one_detail_sheet(
        client,
        token,
        document_id,
        title=trip_title,
        columns=trip_cols,
        data_rows=trip_rows,
        style=style,
    )
    return 2
