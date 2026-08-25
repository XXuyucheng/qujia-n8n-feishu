"""WS CARD frame dispatch patch（不依赖连飞书）。"""

import asyncio
import unittest
from types import SimpleNamespace


class CardDispatchPatchTests(unittest.TestCase):
    def test_patch_marks_client_and_is_idempotent(self):
        from chat_ws import patch_ws_card_frame_dispatch

        try:
            from lark_oapi.ws.client import Client
        except Exception:
            self.skipTest("lark-oapi not installed")

        if hasattr(Client, "_feishu_card_dispatch_patched"):
            delattr(Client, "_feishu_card_dispatch_patched")

        self.assertTrue(patch_ws_card_frame_dispatch())
        self.assertTrue(getattr(Client, "_feishu_card_dispatch_patched", False))
        self.assertTrue(patch_ws_card_frame_dispatch())

    def test_patched_handler_dispatches_card_like_event(self):
        """CARD 与 EVENT 都应调用 _do_without_validation（不再 early return）。"""
        from chat_ws import patch_ws_card_frame_dispatch

        try:
            from lark_oapi.ws.client import Client, _get_by_key
            from lark_oapi.ws.enum import MessageType
        except Exception:
            self.skipTest("lark-oapi not installed")

        if hasattr(Client, "_feishu_card_dispatch_patched"):
            delattr(Client, "_feishu_card_dispatch_patched")
        self.assertTrue(patch_ws_card_frame_dispatch())

        calls = {"n": 0, "written": 0}

        class Headers:
            def __init__(self, items):
                self._items = list(items)

            def __iter__(self):
                return iter(self._items)

            def add(self):
                h = SimpleNamespace(key="", value="")
                self._items.append(h)
                return h

        def make_frame(type_value: str):
            f = SimpleNamespace()
            f.headers = Headers(
                [
                    SimpleNamespace(key="type", value=type_value),
                    SimpleNamespace(key="message_id", value="mid"),
                    SimpleNamespace(key="trace_id", value="tid"),
                    SimpleNamespace(key="sum", value="1"),
                    SimpleNamespace(key="seq", value="0"),
                ]
            )
            # minimal JSON; unmarshal may fail — we stub handler
            f.payload = b"{}"
            f.SerializeToString = lambda: b"frame"
            return f

        # Sanity: _get_by_key works with our Headers
        self.assertEqual(
            _get_by_key(make_frame(MessageType.CARD.value).headers, "type"),
            MessageType.CARD.value,
        )

        client = SimpleNamespace()
        client._event_handler = SimpleNamespace(
            _do_without_validation=lambda pl: (
                calls.__setitem__("n", calls["n"] + 1)
                or {"toast": {"type": "info", "content": "ok"}}
            )
        )
        client._fmt_log = lambda fmt, *a: fmt.format(*a)

        async def _write_message(_data):
            calls["written"] += 1

        client._write_message = _write_message
        client._combine = lambda *a, **k: None

        async def run():
            await Client._handle_data_frame(client, make_frame(MessageType.CARD.value))
            await Client._handle_data_frame(client, make_frame(MessageType.EVENT.value))

        asyncio.run(run())
        self.assertEqual(calls["n"], 2)
        self.assertEqual(calls["written"], 2)

    def test_peek_event_type(self):
        from chat_ws import peek_event_type

        raw = b'{"header":{"event_type":"card.action.trigger"},"event":{}}'
        self.assertEqual(peek_event_type(raw), "card.action.trigger")
        self.assertEqual(peek_event_type(b"not-json"), "")

    def test_processor_not_found_returns_200(self):
        import json
        from chat_ws import patch_ws_card_frame_dispatch

        try:
            from lark_oapi.ws.client import Client
            from lark_oapi.ws.enum import MessageType
        except Exception:
            self.skipTest("lark-oapi not installed")

        if hasattr(Client, "_feishu_card_dispatch_patched"):
            delattr(Client, "_feishu_card_dispatch_patched")
        self.assertTrue(patch_ws_card_frame_dispatch())

        class Headers:
            def __init__(self, items):
                self._items = list(items)

            def __iter__(self):
                return iter(self._items)

            def add(self):
                h = SimpleNamespace(key="", value="")
                self._items.append(h)
                return h

        frame = SimpleNamespace()
        frame.headers = Headers(
            [
                SimpleNamespace(key="type", value=MessageType.EVENT.value),
                SimpleNamespace(key="message_id", value="mid"),
                SimpleNamespace(key="trace_id", value="tid"),
                SimpleNamespace(key="sum", value="1"),
                SimpleNamespace(key="seq", value="0"),
            ]
        )
        frame.payload = (
            b'{"header":{"event_type":"im.chat.access_event.bot_p2p_chat_entered_v1"}}'
        )
        frame.SerializeToString = lambda: b"frame"

        written = []
        client = SimpleNamespace()

        def _boom(_pl):
            raise Exception(
                "processor not found, type: im.chat.access_event.bot_p2p_chat_entered_v1"
            )

        client._event_handler = SimpleNamespace(_do_without_validation=_boom)
        client._fmt_log = lambda fmt, *a: fmt.format(*a)

        async def _write_message(data):
            written.append(data)

        client._write_message = _write_message
        client._combine = lambda *a, **k: None

        asyncio.run(Client._handle_data_frame(client, frame))
        self.assertTrue(written)
        body = json.loads(frame.payload.decode("utf-8"))
        self.assertEqual(int(body.get("code")), 200)
        self.assertFalse(body.get("data"))


if __name__ == "__main__":
    unittest.main()
