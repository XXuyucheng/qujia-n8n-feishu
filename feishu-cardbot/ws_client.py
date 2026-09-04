"""官方长连接：仅 CARDBOT_ENABLE_WS=true 时启动。默认关闭，避免抢生产 chat_ws。"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger("feishu-cardbot.ws")


def _as_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    try:
        import lark_oapi as lark

        return json.loads(lark.JSON.marshal(obj))
    except Exception:
        raw = getattr(obj, "__dict__", None)
        return dict(raw) if isinstance(raw, dict) else {"raw": str(obj)}


def _event_body(data: Any) -> Dict[str, Any]:
    payload = _as_dict(data)
    if isinstance(payload.get("event"), dict):
        return payload["event"]
    inner = payload.get("event")
    if inner is not None and not isinstance(inner, dict):
        nested = _as_dict(inner)
        if nested:
            return nested
    return payload


def build_event_handler(on_event: Callable[[str, Dict[str, Any]], Any]) -> Any:
    """按官方 Python 示例注册四个入口。"""
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
    )

    def on_p2p(data: Any) -> None:
        event = _event_body(data)
        on_event("p2p_entered", event)

    def on_menu(data: Any) -> None:
        event = _event_body(data)
        on_event("bot_menu", event)

    def on_message(data: Any) -> None:
        event = _event_body(data)
        on_event("im_message", event)

    def on_card(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        event = _event_body(data)
        result = on_event("card_action", event) or {}
        callback = result.get("callback") if isinstance(result, dict) else None
        if not isinstance(callback, dict):
            callback = {"toast": {"type": "info", "content": "已处理"}}
        return P2CardActionTriggerResponse(callback)

    builder = lark.EventDispatcherHandler.builder("", "")
    builder = builder.register_p2_im_message_receive_v1(on_message)
    builder = builder.register_p2_card_action_trigger(on_card)
    p2p_name = "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1"
    menu_name = "register_p2_application_bot_menu_v6"
    if hasattr(builder, p2p_name):
        builder = getattr(builder, p2p_name)(on_p2p)
    else:
        logger.warning("SDK missing %s; p2p entered will not be handled via WS", p2p_name)
    if hasattr(builder, menu_name):
        builder = getattr(builder, menu_name)(on_menu)
    else:
        logger.warning("SDK missing %s; bot menu will not be handled via WS", menu_name)
    return builder.build()


def start_ws_client(
    *,
    app_id: str,
    app_secret: str,
    on_event: Callable[[str, Dict[str, Any]], Any],
) -> None:
    import lark_oapi as lark

    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_CHAT_APP_ID/SECRET missing; refuse to start WS")
    handler = build_event_handler(on_event)
    logger.warning(
        "starting Feishu long connection for app_id=%s — this must be the ONLY WS for this app",
        app_id,
    )
    client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )
    client.start()
