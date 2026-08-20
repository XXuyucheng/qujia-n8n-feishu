"""Tests for WeChat personal bill parsing."""

from __future__ import annotations

import io
import os
import sys
import unittest
import zipfile
from pathlib import Path

import pyzipper
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BILL_PARSER_CONFIG_PATH", str(ROOT / "config.yaml"))

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from parser import (  # noqa: E402
    BillParseError,
    categorize,
    load_category_config,
    parse_amount,
    parse_wechat_csv,
    parse_wechat_file,
    parse_wechat_rows,
)

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_category_config(ROOT / "config.yaml")
client = TestClient(app)


def _csv_bytes() -> bytes:
    return (FIXTURES / "wechat_sample.csv").read_bytes()


class ParseAmountTests(unittest.TestCase):
    def test_strips_currency_and_backticks(self):
        self.assertEqual(parse_amount("¥25.50"), 25.5)
        self.assertEqual(parse_amount("`1,200.00"), 1200.0)
        self.assertEqual(parse_amount("￥3"), 3.0)

    def test_invalid_amount_raises(self):
        with self.assertRaises(BillParseError):
            parse_amount("not-a-number")


class WechatCsvTests(unittest.TestCase):
    def test_skips_preamble_and_neutral_rows(self):
        parsed = parse_wechat_csv(_csv_bytes(), CONFIG)
        self.assertEqual(parsed["nickname"], "测试用户")
        self.assertEqual(parsed["period"]["start"], "2026-08-01 00:00:00")
        self.assertEqual(parsed["period"]["end"], "2026-08-31 23:59:59")
        self.assertEqual(parsed["stats"]["count"], 2)
        self.assertEqual(parsed["stats"]["skipped"], 1)
        self.assertEqual(parsed["stats"]["income"], 100.0)
        self.assertEqual(parsed["stats"]["expense"], 25.5)

        tx_ids = [row["tx_id"] for row in parsed["transactions"]]
        self.assertEqual(tx_ids, ["420000111", "420000222"])
        self.assertEqual(parsed["transactions"][0]["amount"], 25.5)
        self.assertEqual(parsed["transactions"][0]["category"], "餐饮")
        self.assertEqual(parsed["skipped"][0]["direction"], "/")
        self.assertEqual(parsed["skipped"][0]["reason"], "neutral_direction")

    def test_header_not_tied_to_line_number(self):
        rows = [
            ["说明"],
            ["交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)", "支付方式", "当前状态", "交易单号", "商户单号", "备注"],
            ["2026-08-01 10:00:00", "商户消费", "京东", "耳机", "支出", "99", "零钱", "支付成功", "TX1", "", ""],
        ]
        parsed = parse_wechat_rows(rows, CONFIG)
        self.assertEqual(parsed["stats"]["count"], 1)
        self.assertEqual(parsed["transactions"][0]["category"], "购物")

    def test_missing_header_raises(self):
        with self.assertRaises(BillParseError):
            parse_wechat_rows([["foo", "bar"]], CONFIG)


class CategorizeTests(unittest.TestCase):
    def test_default_when_no_rule(self):
        self.assertEqual(
            categorize({"peer": "无名店", "item": "杂项", "tx_type": "商户消费"}, CONFIG),
            "未分类",
        )


class XlsxAndZipTests(unittest.TestCase):
    def _xlsx_bytes(self) -> bytes:
        wb = Workbook()
        sheet = wb.active
        sheet.append(["微信支付账单明细"])
        sheet.append(["微信昵称：[表格用户]"])
        sheet.append(
            ["交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)", "支付方式", "当前状态", "交易单号", "商户单号", "备注"]
        )
        sheet.append(
            ["2026-08-05 09:00:00", "商户消费", "滴滴出行", "打车", "支出", "18.00", "零钱", "支付成功", "420000444", "", ""]
        )
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_xlsx_parse(self):
        parsed = parse_wechat_file(self._xlsx_bytes(), "wechat.xlsx", CONFIG)
        self.assertEqual(parsed["nickname"], "表格用户")
        self.assertEqual(parsed["transactions"][0]["category"], "交通")
        self.assertEqual(parsed["transactions"][0]["tx_id"], "420000444")

    def test_xlsx_upload_not_treated_as_zip(self):
        resp = client.post(
            "/parse",
            files={
                "file": (
                    "wechat.xlsx",
                    self._xlsx_bytes(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["transactions"][0]["tx_id"], "420000444")

    def test_aes_zip_with_password(self):
        buf = io.BytesIO()
        with pyzipper.AESZipFile(buf, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(b"123456")
            zf.writestr("微信支付账单.csv", _csv_bytes())
        resp = client.post(
            "/parse",
            files={"file": ("bill.zip", buf.getvalue(), "application/zip")},
            data={"password": "123456", "platform": "wechat"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["stats"]["count"], 2)

    def test_wrong_zip_password(self):
        buf = io.BytesIO()
        with pyzipper.AESZipFile(buf, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(b"123456")
            zf.writestr("微信支付账单.csv", _csv_bytes())
        resp = client.post(
            "/parse",
            files={"file": ("bill.zip", buf.getvalue(), "application/zip")},
            data={"password": "000000"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("密码", resp.json()["detail"])

    def test_plain_zip_without_password(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("微信支付账单.csv", _csv_bytes())
        resp = client.post(
            "/parse",
            files={"file": ("bill.zip", buf.getvalue(), "application/zip")},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["stats"]["count"], 2)


class HealthAndApiTests(unittest.TestCase):
    def test_health(self):
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "service": "bill-parser"})

    def test_csv_upload(self):
        resp = client.post(
            "/parse",
            files={"file": ("wechat.csv", _csv_bytes(), "text/csv")},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["platform"], "wechat")
        self.assertEqual(body["filename"], "wechat.csv")
        self.assertEqual(len(body["transactions"]), 2)

    def test_unsupported_platform(self):
        resp = client.post(
            "/parse",
            files={"file": ("wechat.csv", _csv_bytes(), "text/csv")},
            data={"platform": "alipay"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_empty_file(self):
        resp = client.post(
            "/parse",
            files={"file": ("empty.csv", b"", "text/csv")},
        )
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
