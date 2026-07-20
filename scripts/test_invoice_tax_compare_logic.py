#!/usr/bin/env python3
"""Unit tests for invoice tax status comparison logic (mirrors n8n Code node)."""

from __future__ import annotations

import re
import unittest
from typing import Any, Dict, List, Mapping, MutableMapping


SPLIT_RE = re.compile(r"[，,;；\s]+")


def split_invoice_nos(raw: Any) -> List[str]:
    if raw is None:
        return []
    return [s for s in SPLIT_RE.split(str(raw)) if s.strip()]


def compare(
    tax_map: Mapping[str, Mapping[str, str]],
    bitable_map: Mapping[str, str],
) -> Dict[str, str]:
    """Return record_id -> problem text for problem records only."""
    problems_by_record: MutableMapping[str, List[str]] = {}
    for no, record_id in bitable_map.items():
        tax = tax_map.get(no)
        if tax is None:
            problems_by_record.setdefault(record_id, []).append(f"未找到:{no}")
            continue
        status = tax.get("status") or ""
        risk = tax.get("risk") or ""
        if status != "正常" or risk != "正常":
            problems_by_record.setdefault(record_id, []).append(
                f"状态异常({status or '-'}/{risk or '-'}):{no}"
            )
    return {rid: "；".join(parts) for rid, parts in problems_by_record.items()}


def expand_bitable(
    records: List[Mapping[str, Any]],
) -> Dict[str, str]:
    bitable_map: Dict[str, str] = {}
    for rec in records:
        record_id = str(rec.get("record_id") or "")
        if not record_id:
            continue
        raw = (rec.get("fields") or {}).get("发票号码")
        for no in split_invoice_nos(raw):
            bitable_map[no] = record_id
    return bitable_map


class CompareLogicTests(unittest.TestCase):
    def test_split_chinese_and_english_commas(self):
        self.assertEqual(
            split_invoice_nos("111，222,333；444;555"),
            ["111", "222", "333", "444", "555"],
        )

    def test_only_problem_records_written(self):
        tax_map = {
            "A1": {"status": "正常", "risk": "正常"},
            "A2": {"status": "作废", "risk": "正常"},
            "A3": {"status": "正常", "risk": "高风险"},
        }
        bitable_map = {
            "A1": "rec_ok",
            "A2": "rec_bad_status",
            "A3": "rec_bad_risk",
            "A4": "rec_missing",
        }
        result = compare(tax_map, bitable_map)
        self.assertNotIn("rec_ok", result)
        self.assertEqual(result["rec_bad_status"], "状态异常(作废/正常):A2")
        self.assertEqual(result["rec_bad_risk"], "状态异常(正常/高风险):A3")
        self.assertEqual(result["rec_missing"], "未找到:A4")

    def test_multi_invoice_one_record_aggregates(self):
        tax_map = {
            "X1": {"status": "正常", "risk": "正常"},
            "X2": {"status": "红冲", "risk": "正常"},
        }
        records = [
            {
                "record_id": "rec_multi",
                "fields": {"发票号码": "X1，X2，X3"},
            }
        ]
        bitable_map = expand_bitable(records)
        result = compare(tax_map, bitable_map)
        self.assertEqual(
            result["rec_multi"],
            "状态异常(红冲/正常):X2；未找到:X3",
        )

    def test_all_ok_returns_empty(self):
        tax_map = {"N1": {"status": "正常", "risk": "正常"}}
        bitable_map = {"N1": "rec1"}
        self.assertEqual(compare(tax_map, bitable_map), {})


if __name__ == "__main__":
    unittest.main()
