"""卡片回传 JSON 映射（不依赖 lark SDK）。"""

import unittest

from card_callback import bot_card_response_to_sdk


class CardCallbackMapTests(unittest.TestCase):
    def test_maps_toast_and_raw_card(self):
        body = {
            "toast": {"type": "info", "content": "已更新"},
            "card": {"schema": "2.0", "body": {"elements": []}},
        }
        mapped = bot_card_response_to_sdk(body)
        self.assertEqual(mapped["toast"]["content"], "已更新")
        self.assertEqual(mapped["card"]["type"], "raw")
        self.assertEqual(mapped["card"]["data"]["schema"], "2.0")

    def test_empty_body(self):
        self.assertEqual(bot_card_response_to_sdk({}), {})


if __name__ == "__main__":
    unittest.main()
