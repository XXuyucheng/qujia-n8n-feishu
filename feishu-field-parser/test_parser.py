import json
import os
import unittest
from pathlib import Path

os.environ.setdefault(
    "FIELD_PARSER_CONFIG_PATH",
    str(Path(__file__).resolve().parent / "config.yaml"),
)

from app import (
    SAMPLE_INPUT,
    extract_text_segments,
    format_object_as_text,
    format_user_names,
    normalize_date_value,
    parse_records,
    resolve_table_key,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "files" / "quote-raw-event-test.json"


def load_quote_records():
    with FIXTURE_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    action = raw["event"]["action_list"][0]
    return action["after_value"]


class ExtractTextSegmentsTests(unittest.TestCase):
    def test_plain_array_text(self):
        value = '[{"type":"text","text":"客户想做一日团建，约30人"}]'
        self.assertEqual(extract_text_segments(value), "客户想做一日团建，约30人")

    def test_bus_type_text(self):
        value = {"bus_type": [1], "data": [{"type": "text", "text": "张三"}]}
        self.assertEqual(extract_text_segments(value), "张三")

    def test_plain_string(self):
        self.assertEqual(extract_text_segments("hello"), "hello")


class FormatUserNamesTests(unittest.TestCase):
    def test_single_user_from_field_value(self):
        value = {
            "users": [
                {
                    "userId": "7563182993139466243",
                    "avatarUrl": "https://example.com/avatar.jpg",
                    "name": "徐雅琪",
                    "enName": "徐雅琪",
                }
            ]
        }
        self.assertEqual(format_user_names(value), "徐雅琪")

    def test_multiple_users(self):
        value = {
            "users": [
                {"name": "徐雅琪"},
                {"name": "魏航杰"},
            ]
        }
        self.assertEqual(format_user_names(value), "徐雅琪, 魏航杰")

    def test_identity_users_list(self):
        users = [{"name": "阿铭"}]
        self.assertEqual(format_user_names(users), "阿铭")


class NormalizeDateValueTests(unittest.TestCase):
    def test_millisecond_timestamp(self):
        meta = {"type": 1001, "property": {"dateFormat": "yyyy/MM/dd HH:mm"}}
        self.assertEqual(normalize_date_value("1782268447000", meta), "2026/06/24 10:34")

    def test_date_only_format(self):
        meta = {"type": 5, "property": {"dateFormat": "yyyy/MM/dd"}}
        self.assertEqual(normalize_date_value("1718006400000", meta), "2024/06/10")

    def test_existing_date_string(self):
        meta = {"type": 5, "property": {"dateFormat": "yyyy/MM/dd"}}
        self.assertEqual(normalize_date_value("2024/06/15", meta), "2024/06/15")

    def test_second_timestamp(self):
        meta = {"type": 1002, "property": {"dateFormat": "yyyy/MM/dd"}}
        self.assertEqual(normalize_date_value(1718006400, meta), "2024/06/10")


class ParseRecordsTests(unittest.TestCase):
    def test_quote_fixture_fields(self):
        records = load_quote_records()
        result = parse_records("quote_records", records)
        obj = result["object"]

        self.assertEqual(obj["报价生成状态"], "待生成")
        self.assertEqual(obj["客户需求"], "客户想做一日团建，约30人")
        self.assertEqual(obj["联系人"], "张三")
        self.assertEqual(obj["联系方式"], "13800000000")
        self.assertEqual(obj["单位"], "测试公司")
        self.assertEqual(obj["策划师"], "阿铭")

    def test_sample_input_income_records(self):
        result = parse_records("income_records", SAMPLE_INPUT)
        obj = result["object"]

        self.assertEqual(obj["类型"], "业务收入-定金")
        self.assertEqual(obj["创建人"], "魏航杰")
        self.assertEqual(obj["收入编号"], "SR26061538")
        self.assertIn("2026", obj["创建时间"])

    def test_format_text_output(self):
        records = load_quote_records()
        result = parse_records("quote_records", records)
        text = format_object_as_text(result)

        self.assertIn("策划师: 阿铭", text)
        self.assertIn("客户需求: 客户想做一日团建，约30人", text)
        self.assertNotIn('"type": "text"', text)
        self.assertNotIn("avatarUrl", text)

    def test_unknown_fields_appended_in_text(self):
        records = [{"field_id": "fldUnknown", "field_value": "raw"}]
        result = parse_records("quote_records", records)
        text = format_object_as_text(result)
        self.assertIn("[未配置字段] fldUnknown: raw", text)


class ResolveTableKeyTests(unittest.TestCase):
    def test_resolve_by_table_id(self):
        self.assertEqual(resolve_table_key(table_id="tbl91gZyDPCLhlva"), "quote_records")
        self.assertEqual(resolve_table_key(table_id="tbl8jKSb6kHvKlCL"), "expense_records")

    def test_table_key_takes_priority(self):
        self.assertEqual(
            resolve_table_key(table_key="income_records", table_id="tbl91gZyDPCLhlva"),
            "income_records",
        )


class ApiParseFormatTests(unittest.TestCase):
    def test_api_parse_text_format(self):
        from app import api_parse

        records = load_quote_records()
        result = api_parse(
            {
                "table_id": "tbl91gZyDPCLhlva",
                "records": records,
                "format": "text",
            }
        )
        self.assertIn("text", result)
        self.assertIn("策划师: 阿铭", result["text"])


if __name__ == "__main__":
    unittest.main()
