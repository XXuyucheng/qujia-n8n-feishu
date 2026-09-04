"""官方四类入口：进会话 / 菜单 / IM 消息 / 卡片回传。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from card_engine import ACTION_OPEN_FORM, CardEngine, STATE_COLLECTING
from cards import build_supplier_form, build_welcome
from feishu_client import FeishuAPIError, FeishuClient
from session_store import SessionStore

logger = logging.getLogger("feishu-cardbot.handlers")


def session_key(open_id: str, chat_id: str = "", chat_type: str = "p2p") -> str:
    if chat_type == "group" and chat_id:
        return f"{open_id}:{chat_id}"
    return open_id or chat_id


def receive_target(open_id: str, chat_id: str = "", chat_type: str = "p2p") -> Tuple[str, str]:
    if chat_type == "group" and chat_id:
        return chat_id, "chat_id"
    return open_id, "open_id"


def parse_im_content(message: Dict[str, Any]) -> Tuple[str, list[str]]:
    """从飞书 message.content 抽出文本与 image_key。"""
    import json

    content_raw = message.get("content") or "{}"
    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
    except Exception:
        return str(content_raw), []
    if not isinstance(content, dict):
        return str(content_raw), []

    text = str(content.get("text") or "")
    image_keys: list[str] = []
    if content.get("image_key"):
        image_keys.append(str(content["image_key"]))

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("tag") == "img" and node.get("image_key"):
            image_keys.append(str(node["image_key"]))
        if node.get("tag") == "text" and node.get("text"):
            nonlocal text
            text = (text + " " + str(node["text"])).strip()
        for val in node.values():
            walk(val)

    if content.get("content"):
        walk(content["content"])
    return text, image_keys
    if chat_type == "group" and chat_id:
        return chat_id, "chat_id"
    return open_id, "open_id"


def _maybe_send(
    feishu: Optional[FeishuClient],
    *,
    send: bool,
    receive_id: str,
    receive_id_type: str,
    card: Dict[str, Any],
) -> Dict[str, Any]:
    if not send or feishu is None or not feishu.has_credentials:
        return {"sent": False, "skipped": "no_credentials_or_send_disabled"}
    try:
        data = feishu.send_card(receive_id, card, receive_id_type=receive_id_type)
        # region agent log
        from cards import _agent_log

        _agent_log(
            "B",
            "handlers.py:_maybe_send",
            "send_card ok",
            {"header": (card.get("header") or {}).get("title")},
        )
        # endregion
        return {"sent": True, "message": data}
    except FeishuAPIError as exc:
        logger.error("send card failed: %s", exc.message)
        # region agent log
        from cards import _agent_log

        _agent_log(
            "B",
            "handlers.py:_maybe_send",
            "send_card failed",
            {
                "error": exc.message[:500],
                "feishu_code": exc.feishu_code,
                "header": (card.get("header") or {}).get("title"),
            },
        )
        # endregion
        return {"sent": False, "error": exc.message}


def handle_p2p_entered(
    *,
    runtime: Dict[str, Any],
    open_id: str,
    feishu: Optional[FeishuClient],
    send: bool,
) -> Dict[str, Any]:
    card = build_welcome(open_id=open_id)
    dispatch = _maybe_send(
        feishu, send=send, receive_id=open_id, receive_id_type="open_id", card=card
    )
    return {"ok": True, "event": "p2p_entered", "card": card, **dispatch}


def handle_menu(
    *,
    runtime: Dict[str, Any],
    open_id: str,
    event_key: str,
    store: SessionStore,
    ttl_seconds: int,
    feishu: Optional[FeishuClient],
    send: bool,
) -> Dict[str, Any]:
    expected = str(runtime.get("menu_event_key") or "add_supplier")
    if event_key and event_key not in {expected, "add_supplier", "send_alarm"}:
        return {"ok": True, "skipped": "unknown_menu_key", "event_key": event_key}
    skey = session_key(open_id)
    store.expire_if_stale(skey, ttl_seconds)
    payload = {"skill_id": runtime.get("skill_id"), "data": {}, "active_rules": []}
    store.save(skey, STATE_COLLECTING, payload)
    card = build_supplier_form(runtime, data={})
    dispatch = _maybe_send(
        feishu, send=send, receive_id=open_id, receive_id_type="open_id", card=card
    )
    return {
        "ok": True,
        "event": "bot_menu",
        "state": STATE_COLLECTING,
        "card": card,
        **dispatch,
    }


def handle_message(
    *,
    runtime: Dict[str, Any],
    resource: Dict[str, Any],
    store: SessionStore,
    ttl_seconds: int,
    feishu: Optional[FeishuClient],
    send: bool,
    image_bytes: Optional[bytes] = None,
    image_name: str = "attachment.png",
) -> Dict[str, Any]:
    if str(resource.get("sender_type") or "") == "bot":
        return {"ok": True, "skipped": "bot_sender"}
    message_id = str(resource.get("message_id") or "")
    if not store.claim_message(message_id):
        return {"ok": True, "skipped": "duplicate_message"}

    open_id = str(resource.get("open_id") or "")
    chat_id = str(resource.get("chat_id") or "")
    chat_type = str(resource.get("chat_type") or "p2p")
    skey = session_key(open_id, chat_id, chat_type)
    store.expire_if_stale(skey, ttl_seconds)
    session = store.get(skey)
    rid, rid_type = receive_target(open_id, chat_id, chat_type)

    if image_bytes:
        if feishu is None or not feishu.has_credentials:
            return {"ok": False, "error": "cannot upload image without credentials"}
        try:
            file_token = feishu.upload_bitable_media(
                file_name=image_name,
                content=image_bytes,
                parent_node=str(runtime.get("base_id") or ""),
            )
        except FeishuAPIError as exc:
            return {"ok": False, "error": f"附件上传失败：{exc.message}"}
        engine = CardEngine(runtime, feishu, open_id=open_id, dry_run=True)
        card, state, payload = engine.after_image(session, file_token)
        store.save(skey, state, payload)
        dispatch = _maybe_send(
            feishu, send=send, receive_id=rid, receive_id_type=rid_type, card=card
        )
        return {
            "ok": True,
            "event": "image",
            "state": state,
            "card": card,
            **dispatch,
        }

    payload = {"skill_id": runtime.get("skill_id"), "data": {}, "active_rules": []}
    if session and isinstance((session.get("payload") or {}).get("data"), dict):
        payload["data"] = dict(session["payload"]["data"])
        payload["active_rules"] = list(session["payload"].get("active_rules") or [])
    store.save(skey, STATE_COLLECTING, payload)
    card = build_supplier_form(runtime, data=payload.get("data") or {})
    dispatch = _maybe_send(
        feishu, send=send, receive_id=rid, receive_id_type=rid_type, card=card
    )
    return {
        "ok": True,
        "event": "im_message",
        "state": STATE_COLLECTING,
        "card": card,
        **dispatch,
    }


def handle_card_action(
    *,
    runtime: Dict[str, Any],
    event: Dict[str, Any],
    store: SessionStore,
    ttl_seconds: int,
    feishu: Optional[FeishuClient],
    dry_run: bool,
) -> Dict[str, Any]:
    operator = event.get("operator") or {}
    context = event.get("context") or {}
    action_obj = event.get("action") or {}
    open_id = str(operator.get("open_id") or context.get("open_id") or "")
    chat_id = str(context.get("chat_id") or "")
    chat_type = str(context.get("chat_type") or "p2p")

    skey = session_key(open_id, chat_id, chat_type)
    store.expire_if_stale(skey, ttl_seconds)
    session = store.get(skey)

    value = action_obj.get("value") or {}
    action = str(value.get("action") or "")
    form_value = action_obj.get("form_value") or {}
    token = str(event.get("token") or action_obj.get("name") or "")
    if token and not store.claim_message(f"card:{token}"):
        return {
            "ok": True,
            "skipped": "duplicate_callback",
            "callback": {"toast": {"type": "info", "content": "请勿重复提交"}},
        }

    engine = CardEngine(runtime, feishu, open_id=open_id, dry_run=dry_run)
    callback, new_state, payload = engine.handle_action(
        action=action or ACTION_OPEN_FORM,
        session=session,
        form_value=form_value if isinstance(form_value, dict) else {},
    )
    if new_state is None:
        store.clear(skey)
    else:
        store.save(skey, new_state, payload or {"skill_id": runtime.get("skill_id")})
    return {
        "ok": True,
        "event": "card_action",
        "action": action,
        "state": new_state,
        "dry_run": dry_run,
        "callback": callback,
    }
