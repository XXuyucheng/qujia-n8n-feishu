"""WeChat Pay personal bill parser (CSV / XLSX / ZIP)."""

from __future__ import annotations

import csv
import io
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml
from openpyxl import load_workbook

logger = logging.getLogger("bill-parser")

HEADER_TIME = "交易时间"
HEADER_TX_ID = "交易单号"
DEFAULT_CATEGORY = "未分类"
BOOKABLE_DIRECTIONS = {"收入", "支出"}

COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "time": ("交易时间",),
    "tx_type": ("交易类型",),
    "peer": ("交易对方",),
    "item": ("商品",),
    "direction": ("收/支",),
    "amount": ("金额(元)", "金额（元）", "金额"),
    "method": ("支付方式",),
    "status": ("当前状态",),
    "tx_id": ("交易单号",),
    "merchant_order_id": ("商户单号",),
    "note": ("备注",),
}

NICKNAME_RE = re.compile(r"微信昵称[：:]\s*\[?([^\]\n]+)\]?")
START_RE = re.compile(r"起始时间[：:]\s*\[?(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?)\]?")
END_RE = re.compile(r"终止时间[：:]\s*\[?(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?)\]?")


class BillParseError(ValueError):
    """User-facing parse failure (maps to HTTP 400)."""


def clean_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\ufeff", "").replace("\t", "").strip()
    return text.strip("`").strip()


def parse_amount(value: Any) -> float:
    text = clean_cell(value)
    for token in ("¥", "￥", "CNY", "元", ",", "，"):
        text = text.replace(token, "")
    text = text.strip()
    if not text or text in {".", "-"}:
        return 0.0
    try:
        return round(float(text), 2)
    except ValueError as exc:
        raise BillParseError(f"无法解析金额: {value!r}") from exc


def load_category_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"default_category": DEFAULT_CATEGORY, "rules": []}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    rules = data.get("rules") or []
    if not isinstance(rules, list):
        rules = []
    return {
        "default_category": str(data.get("default_category") or DEFAULT_CATEGORY),
        "rules": [rule for rule in rules if isinstance(rule, dict)],
    }


def categorize(tx: Dict[str, Any], config: Dict[str, Any]) -> str:
    default = str(config.get("default_category") or DEFAULT_CATEGORY)
    for rule in config.get("rules") or []:
        if _rule_matches(tx, rule):
            return str(rule.get("category") or default)
    return default


def _rule_matches(tx: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    for field in ("peer", "item", "tx_type"):
        needles = str(rule.get(field) or "").strip()
        if not needles:
            continue
        haystack = str(tx.get(field) or "")
        for needle in needles.split(","):
            needle = needle.strip()
            if needle and needle in haystack:
                return True
    return False


def is_header_row(cells: Sequence[Any]) -> bool:
    cleaned = [clean_cell(cell) for cell in cells]
    return HEADER_TIME in cleaned and HEADER_TX_ID in cleaned


def _build_column_index(header: Sequence[Any]) -> Dict[str, int]:
    cleaned = [clean_cell(cell) for cell in header]
    index: Dict[str, int] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in cleaned:
                index[field] = cleaned.index(alias)
                break
    missing = [name for name in ("time", "direction", "amount", "tx_id") if name not in index]
    if missing:
        raise BillParseError(f"账单表头缺少必要列: {', '.join(missing)}")
    return index


def _cell(row: Sequence[Any], index: Dict[str, int], field: str) -> str:
    pos = index.get(field)
    if pos is None or pos >= len(row):
        return ""
    return clean_cell(row[pos])


def parse_preamble(lines: Iterable[str]) -> Dict[str, str]:
    blob = "\n".join(lines)
    nickname = ""
    start = ""
    end = ""
    nick_match = NICKNAME_RE.search(blob)
    if nick_match:
        nickname = nick_match.group(1).strip()
    start_match = START_RE.search(blob)
    if start_match:
        start = start_match.group(1).strip()
    end_match = END_RE.search(blob)
    if end_match:
        end = end_match.group(1).strip()
    return {"nickname": nickname, "start": start, "end": end}


def parse_wechat_rows(
    rows: Sequence[Sequence[Any]],
    category_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    header_idx = next((i for i, row in enumerate(rows) if is_header_row(row)), -1)
    if header_idx < 0:
        raise BillParseError("未找到账单表头（需要同时包含「交易时间」和「交易单号」）")

    preamble_lines = [",".join(clean_cell(c) for c in row) for row in rows[:header_idx]]
    meta = parse_preamble(preamble_lines)
    col = _build_column_index(rows[header_idx])
    config = category_config or {"default_category": DEFAULT_CATEGORY, "rules": []}

    transactions: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    income = 0.0
    expense = 0.0

    for row in rows[header_idx + 1 :]:
        if not any(clean_cell(cell) for cell in row):
            continue
        direction = _cell(row, col, "direction")
        record = {
            "time": _cell(row, col, "time"),
            "tx_type": _cell(row, col, "tx_type"),
            "peer": _cell(row, col, "peer"),
            "item": _cell(row, col, "item"),
            "direction": direction,
            "amount": parse_amount(_cell(row, col, "amount")),
            "method": _cell(row, col, "method"),
            "status": _cell(row, col, "status"),
            "tx_id": _cell(row, col, "tx_id"),
            "merchant_order_id": _cell(row, col, "merchant_order_id"),
            "note": _cell(row, col, "note"),
        }
        if direction not in BOOKABLE_DIRECTIONS:
            skipped.append({**record, "reason": "neutral_direction"})
            continue
        record["category"] = categorize(record, config)
        transactions.append(record)
        if direction == "收入":
            income += record["amount"]
        else:
            expense += record["amount"]

    return {
        "nickname": meta["nickname"],
        "period": {"start": meta["start"], "end": meta["end"]},
        "transactions": transactions,
        "skipped": skipped,
        "stats": {
            "income": round(income, 2),
            "expense": round(expense, 2),
            "count": len(transactions),
            "skipped": len(skipped),
        },
    }


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_wechat_csv(data: bytes, category_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    text = decode_text(data)
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader]
    return parse_wechat_rows(rows, category_config)


def parse_wechat_xlsx(data: bytes, category_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise BillParseError(f"无法打开 Excel 文件: {exc}") from exc
    try:
        sheet = wb.worksheets[0]
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        wb.close()
    return parse_wechat_rows(rows, category_config)


def parse_wechat_file(
    data: bytes,
    filename: str,
    category_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    lower = (filename or "").lower()
    if lower.endswith(".xlsx") or lower.endswith(".xls"):
        return parse_wechat_xlsx(data, category_config)
    return parse_wechat_csv(data, category_config)
