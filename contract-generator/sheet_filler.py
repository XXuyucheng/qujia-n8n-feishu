"""Fill Feishu embedded Sheet blocks (docx block_type=30) via Sheets API ranges."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


SHEET_BLOCK_TYPE = 30


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


def build_sheet_value_ranges(
    sheet_id: str,
    sheet_cfg: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build values_batch_update valueRanges for one worksheet.

    Non-contiguous columns (e.g. A-D then F-G, skipping formula col E)
    are emitted as separate contiguous ranges so formulas are preserved.
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
