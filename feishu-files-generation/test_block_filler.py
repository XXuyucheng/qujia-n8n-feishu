"""Unit tests for block_filler (no Feishu network)."""

from __future__ import annotations

import unittest

from block_filler import (
    build_update_requests,
    find_unresolved,
    probe_placeholders,
    replace_in_block,
)


class ReplaceInBlockTests(unittest.TestCase):
    def test_replace_single_placeholder(self):
        block = {
            "block_id": "blk1",
            "text": {
                "elements": [
                    {
                        "text_run": {
                            "content": "甲方：{{甲方名称}}",
                            "text_element_style": {"bold": True},
                        }
                    }
                ]
            },
        }
        req = replace_in_block(block, {"甲方名称": "测试公司"})
        self.assertIsNotNone(req)
        assert req is not None
        run = req["update_text_elements"]["elements"][0]["text_run"]
        self.assertEqual(run["content"], "甲方：测试公司")
        self.assertTrue(run["text_element_style"]["bold"])

    def test_preserve_mention_doc_and_no_change(self):
        block = {
            "block_id": "blk2",
            "text": {
                "elements": [
                    {"text_run": {"content": "见"}},
                    {"mention_doc": {"token": "doxxx", "obj_type": 22}},
                ]
            },
        }
        req = replace_in_block(block, {})
        self.assertIsNone(req)

    def test_multiple_placeholders_same_run(self):
        block = {
            "block_id": "blk3",
            "text": {
                "elements": [
                    {
                        "text_run": {
                            "content": "{{甲方名称}} / {{乙方名称}}",
                            "text_element_style": {},
                        }
                    }
                ]
            },
        }
        req = replace_in_block(
            block,
            {"甲方名称": "甲", "乙方名称": "乙"},
        )
        self.assertIsNotNone(req)
        assert req is not None
        content = req["update_text_elements"]["elements"][0]["text_run"]["content"]
        self.assertEqual(content, "甲 / 乙")

    def test_unmatched_placeholder_kept(self):
        block = {
            "block_id": "blk4",
            "text": {
                "elements": [
                    {"text_run": {"content": "金额：{{合同价款}} 元", "text_element_style": {}}}
                ]
            },
        }
        req = replace_in_block(block, {})
        self.assertIsNone(req)

    def test_no_text_block(self):
        self.assertIsNone(replace_in_block({"block_id": "page", "block_type": 1}, {"a": "b"}))


class BuildUpdateRequestsTests(unittest.TestCase):
    def test_build_from_blocks(self):
        blocks = [
            {
                "block_id": "a",
                "text": {
                    "elements": [
                        {"text_run": {"content": "{{甲方名称}}", "text_element_style": {}}}
                    ]
                },
            },
            {"block_id": "b", "block_type": 1},
            {
                "block_id": "c",
                "text": {
                    "elements": [
                        {"text_run": {"content": "固定文字", "text_element_style": {}}}
                    ]
                },
            },
        ]
        reqs = build_update_requests(blocks, {"甲方名称": "公司A"})
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["block_id"], "a")


class ProbePlaceholdersTests(unittest.TestCase):
    def test_probe_extracts_keys(self):
        blocks = [
            {
                "block_id": "blkA",
                "text": {
                    "elements": [
                        {
                            "text_run": {
                                "content": "甲方：{{甲方名称}}",
                                "text_element_style": {},
                            }
                        }
                    ]
                },
            },
            {
                "block_id": "blkB",
                "text": {
                    "elements": [
                        {
                            "text_run": {
                                "content": "金额：{{合同价款}} 元",
                                "text_element_style": {},
                            }
                        }
                    ]
                },
            },
        ]
        hits, warnings = probe_placeholders(blocks)
        keys = {h["key"] for h in hits}
        self.assertEqual(keys, {"甲方名称", "合同价款"})
        self.assertEqual(len(warnings), 0)

    def test_probe_warns_split_braces(self):
        blocks = [
            {
                "block_id": "split",
                "text": {
                    "elements": [
                        {"text_run": {"content": "金额：{{", "text_element_style": {}}},
                        {"text_run": {"content": "合同价款", "text_element_style": {}}},
                        {"text_run": {"content": "}}", "text_element_style": {}}},
                    ]
                },
            }
        ]
        hits, warnings = probe_placeholders(blocks)
        self.assertEqual(hits, [])
        self.assertTrue(any("跨 text_run" in w for w in warnings))


class FindUnresolvedTests(unittest.TestCase):
    def test_find_unresolved_after_partial_replace(self):
        # Simulate blocks still containing placeholders (pre-update snapshot)
        blocks = [
            {
                "block_id": "x",
                "text": {
                    "elements": [
                        {
                            "text_run": {
                                "content": "{{甲方名称}} {{未知字段}}",
                                "text_element_style": {},
                            }
                        }
                    ]
                },
            }
        ]
        values = {"甲方名称": "公司"}
        # After replace_in_block logic, unresolved are keys still present in original
        # that were not in values — find_unresolved scans original content for {{}}
        # whose key is missing from values OR still present after substitution check.
        unresolved = find_unresolved(blocks, values)
        self.assertIn("未知字段", unresolved)
        self.assertNotIn("甲方名称", unresolved)


if __name__ == "__main__":
    unittest.main()
