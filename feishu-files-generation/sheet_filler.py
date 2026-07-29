"""Fill Feishu embedded Sheet blocks (docx block_type=30) via Sheets API ranges."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


SHEET_BLOCK_TYPE = 30

DEFAULT_SUBTOTAL_FORMULA = '=IF(C{row}="","",C{row}*D{row})'
DEFAULT_TOTAL_FORMULA = "=SUM(E{start}:E{end})"


class SheetConfigError(Exception):
    """Invalid sheet token or sheet write configuration."""

    def __init__(self, message: str, *, error_code: str = "SHEET_CONFIG_ERROR"):
        super().__init__(message)
        self.message = message
        self.error_code = error_code


def parse_sheet_token(token: str) -> Tuple[str, str]:
    """Split sheet.token into (spreadsheet_token, sheet_id).

    Feishu format: SpreadsheetToken_SheetID
    """
    raw = str(token or "").strip()
    if "_" not in raw:
        raise SheetConfigError(
            f"invalid sheet.token (expected SpreadsheetToken_SheetID): {token!r}",
            error_code="INVALID_SHEET_TOKEN",
        )
    spreadsheet, sheet_id = raw.rsplit("_", 1)
    if not spreadsheet or not sheet_id:
        raise SheetConfigError(
            f"invalid sheet.token parts: {token!r}",
            error_code="INVALID_SHEET_TOKEN",
        )
    return spreadsheet, sheet_id


def find_sheet_refs(blocks: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Return sheet refs found in docx blocks."""
    refs: List[Dict[str, str]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if int(block.get("block_type") or 0) != SHEET_BLOCK_TYPE:
            continue
        sheet = block.get("sheet") or {}
        if not isinstance(sheet, dict):
            continue
        token = str(sheet.get("token") or "").strip()
        if not token:
            continue
        spreadsheet_token, sheet_id = parse_sheet_token(token)
        refs.append(
            {
                "block_id": str(block.get("block_id") or ""),
                "raw_token": token,
                "spreadsheet_token": spreadsheet_token,
                "sheet_id": sheet_id,
            }
        )
    return refs


def _cell_value(row: Dict[str, Any], field: str) -> Any:
    if field in row:
        value = row[field]
    else:
        value = None
        # Compat: 内容 ↔ 描述 (config vs n8n sheet_rows)
        aliases = {"内容": ("描述",), "描述": ("内容",)}
        for alt in aliases.get(field, ()):
            if alt in row:
                value = row[alt]
                break
        if value is None:
            for key, candidate in row.items():
                if str(key).strip() == field:
                    value = candidate
                    break
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if text == "":
        return ""
    # Prefer numeric for 数量/单价 when possible
    if field in {"数量", "单价", "总价"}:
        try:
            num = float(text.replace(",", "").replace("，", ""))
            if num.is_integer():
                return int(num)
            return num
        except ValueError:
            return text
    return text


def _formula_cell(text: str) -> Dict[str, str]:
    return {"type": "formula", "text": text}


def build_sheet_value_ranges(
    sheet_id: str,
    sheet_cfg: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build values_batch_update valueRanges for one worksheet.

    Non-contiguous columns (e.g. A-D then F-G, skipping formula col E)
    are emitted as separate contiguous ranges so formulas are preserved.

    When trim_unused_rows is enabled (default True with formula/trim config),
    only write len(rows) data rows — empty padding is deleted afterward.
    """
    if not sheet_id:
        raise SheetConfigError("sheet_id is required", error_code="MISSING_SHEET_ID")

    columns = sheet_cfg.get("columns") or []
    if not columns:
        raise SheetConfigError(
            "sheet.columns is required",
            error_code="MISSING_SHEET_COLUMNS",
        )

    start_row = int(sheet_cfg.get("start_row") or 2)
    max_rows = int(sheet_cfg.get("max_rows") or 20)
    trim_unused = bool(sheet_cfg.get("trim_unused_rows", False))
    # When trimming, never pad to max_rows; otherwise honor clear_unused_rows.
    if trim_unused:
        clear_unused = False
    else:
        clear_unused = bool(sheet_cfg.get("clear_unused_rows", True))

    if len(rows) > max_rows:
        raise SheetConfigError(
            f"sheet rows {len(rows)} exceed max_rows={max_rows}",
            error_code="SHEET_ROWS_OVERFLOW",
        )

    col_letters = [str(c.get("col") or "").strip().upper() for c in columns]
    fields = [str(c.get("field") or "").strip() for c in columns]
    if any(not c or not f for c, f in zip(col_letters, fields)):
        raise SheetConfigError(
            "each sheet column needs field + col",
            error_code="INVALID_SHEET_COLUMNS",
        )

    row_count = max_rows if clear_unused else len(rows)
    if row_count <= 0:
        return []

    end_row = start_row + row_count - 1

    # Materialize full grid for configured columns
    grid: List[List[Any]] = []
    for i in range(row_count):
        if i < len(rows):
            grid.append([_cell_value(rows[i], field) for field in fields])
        else:
            grid.append([""] * len(fields))

    # Split into contiguous column runs by Excel letter index
    def col_index(letter: str) -> int:
        n = 0
        for ch in letter:
            n = n * 26 + (ord(ch) - ord("A") + 1)
        return n

    ranges: List[Dict[str, Any]] = []
    run_start = 0
    while run_start < len(col_letters):
        run_end = run_start
        while (
            run_end + 1 < len(col_letters)
            and col_index(col_letters[run_end + 1]) == col_index(col_letters[run_end]) + 1
        ):
            run_end += 1

        first = col_letters[run_start]
        last = col_letters[run_end]
        range_a1 = f"{sheet_id}!{first}{start_row}:{last}{end_row}"
        values = [row[run_start : run_end + 1] for row in grid]
        ranges.append({"range": range_a1, "values": values})
        run_start = run_end + 1

    return ranges


def build_formula_ranges(
    sheet_id: str,
    sheet_cfg: Dict[str, Any],
    row_count: int,
) -> List[Dict[str, Any]]:
    """Build formula valueRanges for subtotal column + total row.

    Returns empty list when formula config is absent or row_count is 0.
    """
    if not sheet_id or row_count <= 0:
        return []

    formula_cfg = sheet_cfg.get("formula")
    if not isinstance(formula_cfg, dict) or not formula_cfg:
        return []

    start_row = int(sheet_cfg.get("start_row") or 2)
    end_data_row = start_row + row_count - 1
    total_row = end_data_row + 1

    subtotal_col = str(formula_cfg.get("subtotal_col") or "E").strip().upper()
    subtotal_tpl = str(formula_cfg.get("subtotal") or DEFAULT_SUBTOTAL_FORMULA)
    total_col = str(formula_cfg.get("total_col") or "E").strip().upper()
    total_tpl = str(formula_cfg.get("total") or DEFAULT_TOTAL_FORMULA)
    total_label_col = str(formula_cfg.get("total_label_col") or "A").strip().upper()
    total_label = str(formula_cfg.get("total_label") or "总价")

    subtotal_values: List[List[Any]] = []
    for r in range(start_row, end_data_row + 1):
        text = subtotal_tpl.format(row=r, start=start_row, end=end_data_row)
        subtotal_values.append([_formula_cell(text)])

    ranges: List[Dict[str, Any]] = [
        {
            "range": f"{sheet_id}!{subtotal_col}{start_row}:{subtotal_col}{end_data_row}",
            "values": subtotal_values,
        }
    ]

    total_formula = total_tpl.format(row=total_row, start=start_row, end=end_data_row)
    # Write label and total formula; clear leftover template cells on the total row
    if total_label_col == total_col:
        ranges.append(
            {
                "range": f"{sheet_id}!{total_col}{total_row}:{total_col}{total_row}",
                "values": [[_formula_cell(total_formula)]],
            }
        )
    else:
        ranges.append(
            {
                "range": f"{sheet_id}!{total_label_col}{total_row}:{total_label_col}{total_row}",
                "values": [[total_label]],
            }
        )
        ranges.append(
            {
                "range": f"{sheet_id}!{total_col}{total_row}:{total_col}{total_row}",
                "values": [[_formula_cell(total_formula)]],
            }
        )

    # Clear B–D / F–G on total row so template sample cells do not linger
    clear_cols = [
        c
        for c in ("B", "C", "D", "F", "G")
        if c not in {total_label_col, total_col, subtotal_col}
    ]
    for col in clear_cols:
        ranges.append(
            {
                "range": f"{sheet_id}!{col}{total_row}:{col}{total_row}",
                "values": [[""]],
            }
        )

    return ranges


def unused_row_delete_range(
    sheet_cfg: Dict[str, Any],
    row_count: int,
) -> Optional[Tuple[int, int]]:
    """Return 1-based inclusive (startIndex, endIndex) of rows to delete.

    Deletes from the row after the total row through the template's reserved
    end (data rows + original total row). Returns None when nothing to delete.
    """
    if not bool(sheet_cfg.get("trim_unused_rows", False)):
        return None
    if row_count <= 0:
        return None

    start_row = int(sheet_cfg.get("start_row") or 2)
    template_data_rows = int(
        sheet_cfg.get("template_data_rows")
        or sheet_cfg.get("max_rows")
        or 20
    )
    # After write: data occupies start_row .. start_row+N-1, total at start_row+N
    total_row = start_row + row_count
    # Template reserved: data rows start_row .. start_row+template_data_rows-1,
    # plus original total typically at start_row+template_data_rows
    template_end = start_row + template_data_rows  # inclusive old total row
    delete_start = total_row + 1
    if delete_start > template_end:
        return None
    return (delete_start, template_end)


def resolve_sheet_rows(
    *,
    sheet_rows: Optional[Sequence[Dict[str, Any]]] = None,
    fields: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Prefer explicit sheet_rows; else map from fields['报价明细条目']."""
    if sheet_rows:
        return [dict(r) for r in sheet_rows if isinstance(r, dict)]

    fields = fields or {}
    items = fields.get("报价明细条目")
    if not isinstance(items, list):
        return []

    rows: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "名称": item.get("名称") or item.get("物品名称") or "",
                "描述": item.get("描述") or item.get("内容") or "",
                "数量": item.get("数量"),
                "单价": item.get("单价"),
                "结算方式": item.get("结算方式") or "",
                "联系人": item.get("联系人") or "",
            }
        )
    return rows
