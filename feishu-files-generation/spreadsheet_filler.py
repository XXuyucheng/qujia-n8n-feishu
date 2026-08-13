"""Fill standalone Feishu Spreadsheet templates (copy + placeholders + insert rows)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sheet_filler import SheetConfigError, build_sheet_value_ranges

PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
DEFAULT_LINE_TOTAL = '=IF(OR(D{row}="",E{row}=""),"",D{row}*E{row})'
DEFAULT_PROFIT_SUM = "=SUM(I{start}:I{end})"
DEFAULT_TOTAL_I = "=D{row}-H{row}"


def _formula_cell(text: str) -> Dict[str, str]:
    return {"type": "formula", "text": text}


def replace_placeholders_in_text(text: str, values: Dict[str, str]) -> Tuple[str, List[str]]:
    """Replace {{key}} in a cell string. Returns (new_text, unresolved_keys)."""
    unresolved: List[str] = []

    def repl(match: re.Match) -> str:
        key = match.group(1).strip()
        if key in values and values[key] is not None:
            return str(values[key])
        unresolved.append(key)
        return match.group(0)

    return PLACEHOLDER_RE.sub(repl, text), unresolved


def build_placeholder_value_ranges(
    sheet_id: str,
    grid: Sequence[Sequence[Any]],
    values: Dict[str, str],
    *,
    start_row: int = 1,
    start_col: int = 1,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Scan a values grid for {{placeholders}} and build sparse valueRanges.

    `grid` is row-major as returned by Sheets values API (1-based start_row/col).
    Only cells whose text changes are emitted (one range per cell for simplicity).
    """
    if not sheet_id:
        raise SheetConfigError("sheet_id is required", error_code="MISSING_SHEET_ID")

    ranges: List[Dict[str, Any]] = []
    unresolved: List[str] = []

    for r_idx, row in enumerate(grid):
        if not isinstance(row, (list, tuple)):
            continue
        for c_idx, cell in enumerate(row):
            if cell is None or isinstance(cell, (int, float, bool)):
                continue
            text = str(cell)
            if "{{" not in text:
                continue
            new_text, missing = replace_placeholders_in_text(text, values)
            unresolved.extend(missing)
            if new_text == text:
                continue
            a1 = _a1(sheet_id, start_row + r_idx, start_col + c_idx)
            ranges.append({"range": a1, "values": [[new_text]]})

    # de-dupe unresolved while preserving order
    seen = set()
    uniq_unresolved = []
    for key in unresolved:
        if key not in seen:
            seen.add(key)
            uniq_unresolved.append(key)
    return ranges, uniq_unresolved


def _a1(sheet_id: str, row: int, col: int) -> str:
    cell = f"{_col_letter(col)}{row}"
    # Feishu values_batch_update rejects bare single-cell refs like Sheet!C3
    return f"{sheet_id}!{cell}:{cell}"


def _col_letter(index: int) -> str:
    """1-based column index -> Excel letter."""
    if index < 1:
        raise SheetConfigError(f"invalid column index: {index}", error_code="INVALID_SHEET_COLUMNS")
    n = index
    letters = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters))


def insert_row_spec(sheet_cfg: Dict[str, Any], row_count: int) -> Optional[Tuple[int, int]]:
    """Return (startIndex, endIndex) for insert_dimension_range, or None if no insert.

    Feishu convention: insert (endIndex - startIndex) rows before startIndex (1-based).
    We insert between insert_after_row and insert_before_row (defaults 9 and 10).
    """
    if row_count <= 0:
        return None

    max_rows = int(sheet_cfg.get("max_rows") or 80)
    if row_count > max_rows:
        raise SheetConfigError(
            f"sheet rows {row_count} exceed max_rows={max_rows}",
            error_code="SHEET_ROWS_OVERFLOW",
        )

    insert_before = int(
        sheet_cfg.get("insert_before_row")
        or (int(sheet_cfg.get("insert_after_row") or 9) + 1)
    )
    # Insert N rows starting at insert_before (pushes old row down)
    return insert_before, insert_before + row_count


def detail_start_row(sheet_cfg: Dict[str, Any]) -> int:
    """First row index where detail data is written after insert."""
    if sheet_cfg.get("start_row"):
        return int(sheet_cfg["start_row"])
    insert_after = int(sheet_cfg.get("insert_after_row") or 9)
    return insert_after + 1


def detail_start_row_after_anchor_delete(sheet_cfg: Dict[str, Any]) -> int:
    """First detail row index after the two blank anchor rows are removed.

    With insert_after_row=9, details shift up into row 9 once the upper blank is deleted.
    """
    return int(sheet_cfg.get("insert_after_row") or 9)


def anchor_rows_to_delete(
    sheet_cfg: Dict[str, Any],
    row_count: int,
) -> Optional[List[Tuple[int, int]]]:
    """Return 1-based inclusive row ranges for blank anchors to delete after insert.

    After inserting ``row_count`` rows before ``insert_before_row``:
      - upper blank stays at ``insert_after_row``
      - lower blank moves to ``insert_before_row + row_count``

    Ranges are ordered high → low so callers can delete without index drift.
    Returns None when insert anchors are not configured or row_count <= 0.
    """
    if row_count <= 0:
        return None
    if "insert_after_row" not in sheet_cfg and "insert_before_row" not in sheet_cfg:
        return None

    after = int(sheet_cfg.get("insert_after_row") or 9)
    before = int(sheet_cfg.get("insert_before_row") or (after + 1))
    lower = before + row_count
    # Distinct rows: delete lower first, then upper
    if lower == after:
        return [(after, after)]
    return [(lower, lower), (after, after)]


def build_online_quote_detail_ranges(
    sheet_id: str,
    sheet_cfg: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build valueRanges for online-quote detail columns after insert."""
    if not rows:
        return []

    start = detail_start_row(sheet_cfg)
    cfg = dict(sheet_cfg)
    cfg["start_row"] = start
    cfg["clear_unused_rows"] = False
    cfg["trim_unused_rows"] = True
    # Alias 名称 ↔ 物品名称 for shared extractors
    normalized: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if "物品名称" not in item and item.get("名称"):
            item["物品名称"] = item["名称"]
        if "类目" not in item and item.get("分类"):
            item["类目"] = item["分类"]
        normalized.append(item)
    return build_sheet_value_ranges(sheet_id, cfg, normalized)


def build_detail_formula_ranges(
    sheet_id: str,
    sheet_cfg: Dict[str, Any],
    row_count: int,
    *,
    exclude_rows: Optional[Sequence[int]] = None,
) -> List[Dict[str, Any]]:
    """Write per-row formulas (e.g. F=D*E, I=G*H). Feishu inheritStyle does not copy formulas.

    exclude_rows: 1-based rows to skip (e.g. 税费及服务 percentage row).
    """
    if not sheet_id or row_count <= 0:
        return []

    formula_cfg = sheet_cfg.get("formula")
    if not isinstance(formula_cfg, dict) or not formula_cfg:
        return []

    formulas: List[tuple[str, str]] = []
    line_col = str(formula_cfg.get("line_total_col") or "").strip().upper()
    line_tpl = str(formula_cfg.get("line_total") or "").strip()
    if line_col and line_tpl:
        formulas.append((line_col, line_tpl))
    profit_col = str(formula_cfg.get("profit_line_col") or "").strip().upper()
    profit_tpl = str(formula_cfg.get("profit_line") or "").strip()
    if profit_col and profit_tpl:
        formulas.append((profit_col, profit_tpl))
    if not formulas:
        return []

    start = detail_start_row(sheet_cfg)
    end = start + row_count - 1
    skip = {int(r) for r in (exclude_rows or []) if r is not None}

    ranges: List[Dict[str, Any]] = []
    for col, tpl in formulas:
        for r in range(start, end + 1):
            if r in skip:
                continue
            text = tpl.format(row=r, start=start, end=end)
            ranges.append(
                {
                    "range": f"{sheet_id}!{col}{r}:{col}{r}",
                    "values": [[_formula_cell(text)]],
                }
            )
    return ranges


def find_total_label_row(
    grid: Sequence[Sequence[Any]],
    label: str,
    *,
    start_row: int = 1,
    col_index: int = 0,
) -> Optional[int]:
    """Return 1-based sheet row whose given column (default A) contains label text."""
    needle = str(label or "").strip()
    if not needle:
        return None
    for r_idx, row in enumerate(grid):
        if not isinstance(row, (list, tuple)):
            continue
        if col_index >= len(row):
            continue
        cell = row[col_index]
        if cell is None:
            continue
        if needle in str(cell):
            return start_row + r_idx
    return None


def _sum_range_excluding(
    col: str,
    start: int,
    end: int,
    exclude_row: Optional[int],
) -> str:
    """Build SUM(...) covering start..end but skipping exclude_row when inside."""
    if exclude_row is None or exclude_row < start or exclude_row > end:
        return f"SUM({col}{start}:{col}{end})"
    parts: List[str] = []
    if exclude_row > start:
        parts.append(f"{col}{start}:{col}{exclude_row - 1}")
    if exclude_row < end:
        parts.append(f"{col}{exclude_row + 1}:{col}{end}")
    if not parts:
        return "0"
    if len(parts) == 1:
        return f"SUM({parts[0]})"
    return f"SUM({parts[0]},{parts[1]})"


def build_activity_total_formula_ranges(
    sheet_id: str,
    sheet_cfg: Dict[str, Any],
    row_count: int,
    *,
    total_row: Optional[int] = None,
    tax_row: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Write formulas on the 活动总价（含税） row.

    - D: SUM(detail F) * (1 + F of 税费及服务 row)
    - H: SUM(detail I)
    - I: D - H on the total row
    """
    if not sheet_id or row_count <= 0 or total_row is None:
        return []

    formula_cfg = sheet_cfg.get("formula")
    if not isinstance(formula_cfg, dict) or not formula_cfg:
        return []

    start = detail_start_row(sheet_cfg)
    end = start + row_count - 1

    total_d_col = str(formula_cfg.get("total_amount_col") or "D").strip().upper()
    profit_sum_col = str(formula_cfg.get("profit_sum_col") or "H").strip().upper()
    total_i_col = str(formula_cfg.get("total_diff_col") or "I").strip().upper()

    f_sum = _sum_range_excluding("F", start, end, tax_row)
    if tax_row is not None:
        d_text = f"=({f_sum})*(1+F{tax_row})"
    else:
        d_text = f"={f_sum}"

    h_tpl = str(formula_cfg.get("profit_sum") or DEFAULT_PROFIT_SUM).strip()
    h_text = h_tpl.format(row=total_row, start=start, end=end, tax_row=tax_row or 0)
    if not h_text.startswith("="):
        h_text = "=" + h_text

    i_tpl = str(formula_cfg.get("total_diff") or DEFAULT_TOTAL_I).strip()
    i_text = i_tpl.format(row=total_row, start=start, end=end)
    if not i_text.startswith("="):
        i_text = "=" + i_text

    return [
        {
            "range": f"{sheet_id}!{total_d_col}{total_row}:{total_d_col}{total_row}",
            "values": [[_formula_cell(d_text)]],
        },
        {
            "range": f"{sheet_id}!{profit_sum_col}{total_row}:{profit_sum_col}{total_row}",
            "values": [[_formula_cell(h_text)]],
        },
        {
            "range": f"{sheet_id}!{total_i_col}{total_row}:{total_i_col}{total_row}",
            "values": [[_formula_cell(i_text)]],
        },
    ]


# Back-compat alias used by older tests / imports
def build_total_sum_range(
    sheet_id: str,
    sheet_cfg: Dict[str, Any],
    row_count: int,
    *,
    total_row: Optional[int] = None,
    tax_row: Optional[int] = None,
) -> List[Dict[str, Any]]:
    return build_activity_total_formula_ranges(
        sheet_id,
        sheet_cfg,
        row_count,
        total_row=total_row,
        tax_row=tax_row,
    )


def normalize_quote_sheet_rows(
    sheet_rows: Optional[Sequence[Dict[str, Any]]] = None,
    fields: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Map sheet_rows / 报价明细条目 into online-quote columns."""
    if sheet_rows:
        out: List[Dict[str, Any]] = []
        for row in sheet_rows:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "类目": row.get("类目") or row.get("分类") or "",
                    "物品名称": row.get("物品名称") or row.get("名称") or "",
                    "描述": row.get("描述") or row.get("内容") or "",
                    "数量": row.get("数量"),
                    "单价": row.get("单价"),
                }
            )
        return out

    fields = fields or {}
    items = fields.get("报价明细条目")
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "类目": item.get("类目") or item.get("分类") or "",
                "物品名称": item.get("物品名称") or item.get("名称") or "",
                "描述": item.get("描述") or item.get("内容") or "",
                "数量": item.get("数量"),
                "单价": item.get("单价"),
            }
        )
    return out


def normalize_itinerary_rows(
    itinerary_rows: Optional[Sequence[Dict[str, Any]]] = None,
    fields: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Map itinerary_rows / 行程明细 into 日期/时间/内容 columns."""
    raw: Sequence[Any]
    if itinerary_rows:
        raw = itinerary_rows
    else:
        fields = fields or {}
        items = fields.get("行程明细")
        raw = items if isinstance(items, list) else []

    out: List[Dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        date = str(row.get("日期") or row.get("date") or "").strip()
        time = str(row.get("时间") or row.get("time") or "").strip()
        content = str(row.get("内容") or row.get("content") or row.get("描述") or "").strip()
        if not date and not time and not content:
            continue
        out.append({"日期": date, "时间": time, "内容": content})
    return out


def itinerary_insert_before_row(sheet_cfg: Dict[str, Any], quote_row_count: int) -> int:
    """Pristine insert_before_row shifted by quote insert/delete net rows.

    Quote path inserts N rows then deletes 2 anchors → net +(N-2) for rows below.
    When quote_row_count == 0, no shift.
    """
    itin = sheet_cfg.get("itinerary") if isinstance(sheet_cfg.get("itinerary"), dict) else {}
    base = int((itin or {}).get("insert_before_row") or 15)
    if quote_row_count <= 0:
        return base
    # Anchors deleted only when quote insert path ran (N > 0)
    net_shift = quote_row_count - 2
    return base + max(net_shift, 0)


def build_itinerary_value_ranges(
    sheet_id: str,
    sheet_cfg: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
    *,
    start_row: int,
) -> List[Dict[str, Any]]:
    """Build valueRanges for itinerary columns starting at start_row."""
    if not rows or start_row < 1:
        return []
    itin = sheet_cfg.get("itinerary") if isinstance(sheet_cfg.get("itinerary"), dict) else None
    if not itin:
        return []
    columns = itin.get("columns")
    if not isinstance(columns, list) or not columns:
        return []

    max_rows = int(itin.get("max_rows") or 80)
    if len(rows) > max_rows:
        raise SheetConfigError(
            f"itinerary rows {len(rows)} exceed max_rows={max_rows}",
            error_code="ITINERARY_ROWS_OVERFLOW",
        )

    cfg = {
        "start_row": start_row,
        "columns": columns,
        "clear_unused_rows": False,
        "trim_unused_rows": True,
        "max_rows": max_rows,
    }
    return build_sheet_value_ranges(sheet_id, cfg, list(rows))
