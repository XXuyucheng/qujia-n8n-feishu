"""Regression tests for invoice party extraction (开户银行 vs 购销方)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import extract_invoice_fields  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTATIONS = json.loads((FIXTURES / "expectations.json").read_text(encoding="utf-8"))


class ExtractInvoiceFieldsTests(unittest.TestCase):
    def _run_case(self, case_id: str) -> dict:
        text = (FIXTURES / f"{case_id}.txt").read_text(encoding="utf-8")
        return extract_invoice_fields(text)

    def test_fixtures_match_expectations(self):
        for case_id, expected in EXPECTATIONS.items():
            with self.subTest(case=case_id):
                fields = self._run_case(case_id)
                if "购方名称" in expected:
                    self.assertEqual(fields["购方名称"], expected["购方名称"])
                if "销方名称" in expected:
                    self.assertEqual(fields["销方名称"], expected["销方名称"])
                if "价税合计" in expected:
                    self.assertEqual(fields["价税合计"], expected["价税合计"])
                for banned in expected.get("forbid_in_parties", []):
                    self.assertNotEqual(fields["购方名称"], banned)
                    self.assertNotEqual(fields["销方名称"], banned)
                    self.assertFalse(
                        banned in (fields["购方名称"] or "")
                        and fields["购方名称"] == banned
                    )

    def test_account_bank_not_in_name_candidates(self):
        fields = self._run_case("taizhou_bank_as_seller")
        candidates = fields.get("公司名称候选") or []
        self.assertNotIn("台州银行股份有限公司", candidates)

    def test_account_banks_debug_field(self):
        fields = self._run_case("taizhou_bank_as_seller")
        banks = fields.get("开户银行候选") or []
        self.assertTrue(any("台州银行" in b for b in banks))


if __name__ == "__main__":
    unittest.main()
