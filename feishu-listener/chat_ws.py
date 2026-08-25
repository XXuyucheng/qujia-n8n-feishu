"""Dedicated process: Feishu chat-app IM websocket → POST into feishu-listener.

lark-oapi ws.Client cannot share a process with another Client (asyncio loop clash),
so the chat app connection runs here as a child process.
"""

from __future__ import annotations

import base64
import http
import json
import logging
import os
import time
from typing import Any, Dict

import httpx

try:
    import lark_oapi as lark
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"lark_oapi required: {exc}") from exc

from card_callback import bot_card_response_to_sdk

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


# #region agent log
def _agent_log(hypothesis_id: str, location: str, message: str, data: Any = None) -> None:
    payload = {
        "sessionId": "4f5fd2",
        "runId": "post-fix",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data or {},
        "timestamp": int(time.time() * 1000),
    }
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    for path in (
        "/Users/xuyucheng/My_project/n8n/.cursor/debug-4f5fd2.log",
        "/cursor-debug/debug-4f5fd2.log",
        "/data/debug-4f5fd2.log",
        "/app/debug-4f5fd2.log",
    ):
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass
    body = line.encode("utf-8")
    for url in (
        "http://host.docker.internal:7828/ingest/ef15f0ab-2bbe-49ed-97a7-590afdf8b03d",
        "http://127.0.0.1:7828/ingest/ef15f0ab-2bbe-49ed-97a7-590afdf8b03d",
    ):
        try:
            import urllib.request

            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Debug-Session-Id": "4f5fd2",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=0.4)
            break
        except Exception:
            continue
# #endregion


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("feishu-listener.chat-ws")

INGEST_URL = os.getenv(
    "LISTENER_CHAT_INGEST_URL", "http://127.0.0.1:8010/internal/ingest"
)
CARD_ACTION_URL = os.getenv(
    "FEISHU_BOT_CARD_ACTION_URL", "http://feishu-bot:8040/api/card-action"
)


def as_dict(obj: Any) -> Any:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    try:
        return json.loads(lark.JSON.marshal(obj))
    except Exception:
        pass
    raw = getattr(obj, "__dict__", None)
    if isinstance(raw, dict):
        return raw
    return {"raw": str(obj)}


def peek_event_type(payload: bytes | None) -> str:
    """从 WS 帧 JSON 取出 header.event_type，便于区分卡片点击与未注册事件。"""
    if not payload:
        return ""
    try:
        obj = json.loads(payload.decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""
    header = obj.get("header") or {}
    if isinstance(header, dict):
        return str(header.get("event_type") or "")
    return str(obj.get("type") or "")


def is_processor_not_found(exc: BaseException) -> bool:
    return "processor not found" in str(exc)


def patch_ws_card_frame_dispatch() -> bool:
    """Fix lark-oapi WS bug: MessageType.CARD must be dispatched like EVENT.

    Upstream 1.6.x early-returns on CARD frames (issue #126), so Feishu never
    receives Response(code=200) for card.action.trigger → client error 200671.
    """
    try:
        from lark_oapi.core.const import UTF_8
        from lark_oapi.core.json import JSON
        from lark_oapi.ws.client import (
            HEADER_BIZ_RT,
            HEADER_MESSAGE_ID,
            HEADER_SEQ,
            HEADER_SUM,
            HEADER_TRACE_ID,
            HEADER_TYPE,
            Client,
            _get_by_key,
        )
        from lark_oapi.ws.enum import MessageType
        from lark_oapi.ws.model import Response
    except Exception as exc:
        logger.warning("cannot patch CARD frame dispatch: %s", exc)
        return False

    if getattr(Client, "_feishu_card_dispatch_patched", False):
        return True

    async def _handle_data_frame(self, frame):  # type: ignore[no-untyped-def]
        hs = frame.headers
        msg_id = _get_by_key(hs, HEADER_MESSAGE_ID)
        trace_id = _get_by_key(hs, HEADER_TRACE_ID)
        sum_ = _get_by_key(hs, HEADER_SUM)
        seq = _get_by_key(hs, HEADER_SEQ)
        type_ = _get_by_key(hs, HEADER_TYPE)

        pl = frame.payload
        if int(sum_) > 1:
            pl = self._combine(msg_id, int(sum_), int(seq), pl)
            if pl is None:
                return

        message_type = MessageType(type_)
        event_type = peek_event_type(pl)
        # #region agent log
        _agent_log(
            "B",
            "chat_ws.py:_handle_data_frame:entry",
            "ws data frame",
            {
                "message_type": getattr(message_type, "value", str(message_type)),
                "event_type": event_type,
                "msg_id": msg_id,
                "trace_id": trace_id,
            },
        )
        # #endregion

        resp = Response(code=http.HTTPStatus.OK)
        wrote_response = False
        dispatched = False
        result = None
        try:
            start = int(round(time.time() * 1000))
            if message_type in (MessageType.EVENT, MessageType.CARD):
                dispatched = True
                result = self._event_handler._do_without_validation(pl)
            else:
                # #region agent log
                _agent_log(
                    "B",
                    "chat_ws.py:_handle_data_frame:ignored",
                    "ignored non event/card frame",
                    {
                        "message_type": getattr(message_type, "value", str(message_type)),
                        "event_type": event_type,
                    },
                )
                # #endregion
                return
            end = int(round(time.time() * 1000))
            header = hs.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
                resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception as e:
            logger.error(
                self._fmt_log(
                    "handle message failed, message_type: {}, event_type: {}, "
                    "message_id: {}, trace_id: {}, err: {}",
                    message_type.value,
                    event_type,
                    msg_id,
                    trace_id,
                    e,
                )
            )
            # 未订阅事件（如打开单聊）不应回 500，否则飞书可能映射成卡片 200671
            if is_processor_not_found(e):
                resp = Response(code=http.HTTPStatus.OK)
            else:
                resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = JSON.marshal(resp).encode(UTF_8)
        await self._write_message(frame.SerializeToString())
        wrote_response = True
        data_prefix = ""
        if resp.data:
            try:
                raw = resp.data
                if isinstance(raw, bytes):
                    data_prefix = base64.b64decode(raw).decode("utf-8")[:180]
                else:
                    data_prefix = str(raw)[:180]
            except Exception:
                data_prefix = ""
        # #region agent log
        _agent_log(
            "B",
            "chat_ws.py:_handle_data_frame:exit",
            "ws response written",
            {
                "message_type": getattr(message_type, "value", str(message_type)),
                "event_type": event_type,
                "dispatched": dispatched,
                "wrote_response": wrote_response,
                "response_code": int(resp.code) if resp.code is not None else None,
                "has_data": bool(resp.data),
                "payload_len": len(frame.payload or b""),
                "data_prefix": data_prefix,
                "msg_id": msg_id,
            },
        )
        # #endregion

    Client._handle_data_frame = _handle_data_frame  # type: ignore[method-assign]
    Client._feishu_card_dispatch_patched = True  # type: ignore[attr-defined]
    logger.info("patched lark-oapi WS CARD frame dispatch (issue #126)")
    return True


def forward(raw: Any) -> None:
    payload = as_dict(raw)
    for attempt in range(1, 4):
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(INGEST_URL, json=payload)
                resp.raise_for_status()
            return
        except Exception as exc:
            logger.warning("ingest attempt %s failed: %s", attempt, exc)
            time.sleep(attempt)
    logger.error("gave up forwarding chat event")


def forward_card_action(raw: Any) -> Any:
    """同步转发卡片回调并构造 SDK 响应（须在约 3s 内返回）。"""
    payload = as_dict(raw)
    started = time.time()
    # #region agent log
    _agent_log(
        "C",
        "chat_ws.py:forward_card_action:entry",
        "card callback received",
        {
            "url": CARD_ACTION_URL,
            "payload_keys": list(payload.keys())
            if isinstance(payload, dict)
            else type(payload).__name__,
            "event_keys": list((payload.get("event") or {}).keys())
            if isinstance(payload, dict)
            else [],
            "action_tag": ((payload.get("event") or {}).get("action") or {}).get("tag")
            if isinstance(payload, dict)
            else None,
            "has_value": bool(
                ((payload.get("event") or {}).get("action") or {}).get("value")
            )
            if isinstance(payload, dict)
            else False,
        },
    )
    # #endregion
    # 飞书回调总预算约 3s；留一点余量给 WS 回写
    try:
        with httpx.Client(timeout=2.7) as client:
            resp = client.post(CARD_ACTION_URL, json=payload)
            resp.raise_for_status()
            body = resp.json()
            if not isinstance(body, dict):
                body = {}
        # #region agent log
        _agent_log(
            "C",
            "chat_ws.py:forward_card_action:http_ok",
            "bot card-action http ok",
            {
                "status": resp.status_code,
                "elapsed_ms": int((time.time() - started) * 1000),
                "body_keys": list(body.keys()),
                "has_card": isinstance(body.get("card"), dict),
            },
        )
        # #endregion
    except Exception as exc:
        logger.warning("card-action forward failed: %s", exc)
        # #region agent log
        _agent_log(
            "E",
            "chat_ws.py:forward_card_action:http_fail",
            "bot card-action http failed",
            {
                "error": str(exc),
                "elapsed_ms": int((time.time() - started) * 1000),
                "url": CARD_ACTION_URL,
            },
        )
        # #endregion
        body = {
            "toast": {
                "type": "error",
                "content": "卡片处理超时或失败，请改用文字回复。",
            }
        }
    mapped = bot_card_response_to_sdk(body)
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        return P2CardActionTriggerResponse(mapped)
    except Exception:
        try:
            from lark.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )

            return P2CardActionTriggerResponse(mapped)
        except Exception:
            return mapped


def main() -> None:
    app_id = os.getenv("FEISHU_CHAT_APP_ID", "")
    app_secret = os.getenv("FEISHU_CHAT_APP_SECRET", "")
    if not app_id or not app_secret:
        logger.error("FEISHU_CHAT_APP_ID/SECRET missing; chat ws exiting")
        raise SystemExit(1)

    # Wait for parent HTTP to be ready
    for _ in range(30):
        try:
            with httpx.Client(timeout=2.0) as client:
                r = client.get("http://127.0.0.1:8010/health")
                if r.status_code == 200:
                    break
        except Exception:
            time.sleep(1)
    else:
        logger.warning("parent health not ready; continuing anyway")

    def handle_im_message(data: Any) -> None:
        forward(data)

    def handle_card_action(data: Any) -> Any:
        return forward_card_action(data)

    patched = patch_ws_card_frame_dispatch()

    # 长连接官方示例：builder 两个参数必须空串（鉴权在 WS 建连时完成）
    builder = lark.EventDispatcherHandler.builder("", "")
    builder = builder.register_p2_im_message_receive_v1(handle_im_message)
    register_card = getattr(builder, "register_p2_card_action_trigger", None)
    # #region agent log
    _agent_log(
        "A",
        "chat_ws.py:main:register",
        "card processor registration",
        {
            "has_register_p2_card_action_trigger": callable(register_card),
            "builder_arg_order": "empty,empty",
            "card_dispatch_patched": patched,
        },
    )
    # #endregion
    if callable(register_card):
        builder = register_card(handle_card_action)
        logger.info("registered p2 card.action.trigger handler")
    else:
        logger.warning(
            "lark-oapi missing register_p2_card_action_trigger; card clicks will not work"
        )
    handler = builder.build()

    logger.info(
        "starting chat websocket for app_id=%s ingest=%s card_patch=%s",
        app_id,
        INGEST_URL,
        patched,
    )
    ws_client = lark.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )
    ws_client.start()


if __name__ == "__main__":
    main()
