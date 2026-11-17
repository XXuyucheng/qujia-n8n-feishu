"""Opening a quote either appends a sheet or copies a new file, never both."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app import _open_quote_spreadsheet
from models import GenerateRequest


class FakeFeishu:
    def __init__(self):
        self.copy_sheet_calls = []
        self.copy_file_calls = []

    def list_spreadsheet_sheets(self, token, spreadsheet_token):
        if spreadsheet_token == "existing":
            return [
                {"sheet_id": "blank", "title": "空白模板"},
                {"sheet_id": "q1", "title": "第一版"},
            ]
        return [
            {"sheet_id": "blank", "title": "空白模板"},
            {"sheet_id": "quote", "title": "报价"},
        ]

    def copy_sheet(self, token, spreadsheet_token, source_sheet_id, title):
        self.copy_sheet_calls.append((spreadsheet_token, source_sheet_id, title))
        return {"sheetId": "copied", "title": title}

    def copy_file(self, token, *, template_token, folder_token, name, file_type="docx"):
        self.copy_file_calls.append((template_token, folder_token, name, file_type))
        return {"token": "newfile", "url": "https://feishu.cn/sheets/newfile"}


def _template():
    return SimpleNamespace(
        template_token="template-token",
        folder_token="CNxzf1R0FlwdY8dkQEhcH9Efnvg",
    )


class OpenQuoteSpreadsheetTests(unittest.TestCase):
    def test_token_copies_blank_sheet_inside_existing_file(self):
        client = FakeFeishu()
        req = GenerateRequest(
            spreadsheet_token="existing",
            fields={"活动名称": "舟山团建"},
        )
        opened = _open_quote_spreadsheet(client, "tenant", _template(), req, "舟山团建-DD1")

        self.assertEqual(client.copy_file_calls, [])
        self.assertEqual(client.copy_sheet_calls, [("existing", "blank", "舟山团建")])
        self.assertEqual(opened["spreadsheet_token"], "existing")
        self.assertEqual(opened["sheet_id"], "copied")
        self.assertEqual(opened["sheets_appended"], 1)

    def test_missing_token_copies_template_file(self):
        client = FakeFeishu()
        req = GenerateRequest(signing_unit="在线报价", fields={"活动名称": "舟山团建"})
        opened = _open_quote_spreadsheet(client, "tenant", _template(), req, "舟山团建-DD1")

        self.assertEqual(client.copy_sheet_calls, [])
        self.assertEqual(
            client.copy_file_calls,
            [("template-token", "CNxzf1R0FlwdY8dkQEhcH9Efnvg", "舟山团建-DD1", "sheet")],
        )
        self.assertEqual(opened["spreadsheet_token"], "newfile")
        self.assertEqual(opened["sheets_appended"], 0)

    def test_blank_token_is_treated_as_missing(self):
        client = FakeFeishu()
        req = GenerateRequest(spreadsheet_token="  ")
        _open_quote_spreadsheet(client, "tenant", _template(), req, "报价")

        self.assertEqual(client.copy_sheet_calls, [])
        self.assertEqual(len(client.copy_file_calls), 1)


if __name__ == "__main__":
    unittest.main()
