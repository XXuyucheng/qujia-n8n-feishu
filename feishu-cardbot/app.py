"""feishu-cardbot HTTP 入口。

默认不启飞书长连接（CARDBOT_ENABLE_WS=false），避免抢生产 chat_ws。
开发用 /api/inject/* 模拟官方事件；写表默认 dry_run。
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config_loader import ConfigError, load_platform, load_supplier_runtime, resolve_config_dir
from feishu_client import FeishuAPIError, FeishuClient
from handlers import (
    handle_card_action,
    handle_menu,
    handle_message,
    handle_p2p_entered,
    parse_im_content,
)
from session_store import SessionStore

CONFIG_DIR = resolve_config_dir(
    Path(os.getenv("FEISHU_CARDBOT_CONFIG_DIR") or "") or None
)
DB_PATH = Path(os.getenv("CARDBOT_DB_PATH") or "/data/sessions.sqlite")
HOST = os.getenv("CARDBOT_HOST") or "0.0.0.0"
PORT = int(os.getenv("CARDBOT_PORT") or os.getenv("FEISHU_CARDBOT_PORT") or "8050")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


WS_ENABLED = _env_bool("CARDBOT_ENABLE_WS", False)
INJECT_ENABLED = _env_bool("CARDBOT_ENABLE_INJECT", True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("feishu-cardbot")

app = FastAPI(title="Feishu Card Bot", version="0.1.0")
store = SessionStore(DB_PATH)
_ws_started = False


def _runtime() -> Dict[str, Any]:
    return load_supplier_runtime(CONFIG_DIR)


def _ttl_seconds() -> int:
    app_cfg, _ = load_platform(CONFIG_DIR)
    return int(app_cfg.get("session_ttl_minutes") or 30) * 60


def _inject_allowed() -> Optional[JSONResponse]:
    if INJECT_ENABLED:
        return None
    return JSONResponse({"ok": False, "error": "inject disabled"}, status_code=403)


@app.get("/health")
def health() -> Dict[str, Any]:
    try:
        app_cfg, skills = load_platform(CONFIG_DIR)
        ok = True
        detail = None
        skill_ids = list(skills.keys())
    except Exception as exc:
        ok = False
        app_cfg = {}
        skill_ids = []
        detail = str(exc)
    return {
        "ok": ok,
        "service": "feishu-cardbot",
        "config_dir": str(CONFIG_DIR),
        "skills": skill_ids,
        "session_ttl_minutes": app_cfg.get("session_ttl_minutes"),
        "detail": detail,
        "db_path": str(DB_PATH),
        "ws_enabled": WS_ENABLED,
        "inject_enabled": INJECT_ENABLED,
    }


@app.post("/api/inject/p2p-entered")
def inject_p2p(body: Dict[str, Any]) -> JSONResponse:
    blocked = _inject_allowed()
    if blocked:
        return blocked
    try:
        runtime = _runtime()
    except ConfigError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    send = bool(body.get("send"))
    open_id = str(body.get("open_id") or "")
    feishu = FeishuClient()
    try:
        result = handle_p2p_entered(
            runtime=runtime, open_id=open_id, feishu=feishu, send=send
        )
        return JSONResponse(result)
    finally:
        feishu.close()


@app.post("/api/inject/menu")
def inject_menu(body: Dict[str, Any]) -> JSONResponse:
    blocked = _inject_allowed()
    if blocked:
        return blocked
    try:
        runtime = _runtime()
    except ConfigError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    feishu = FeishuClient()
    try:
        result = handle_menu(
            runtime=runtime,
            open_id=str(body.get("open_id") or ""),
            event_key=str(body.get("event_key") or runtime.get("menu_event_key") or ""),
            store=store,
            ttl_seconds=_ttl_seconds(),
            feishu=feishu,
            send=bool(body.get("send")),
        )
        return JSONResponse(result)
    finally:
        feishu.close()


@app.post("/api/inject/message")
def inject_message(body: Dict[str, Any]) -> JSONResponse:
    blocked = _inject_allowed()
    if blocked:
        return blocked
    try:
        runtime = _runtime()
    except ConfigError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    resource = dict(body.get("resource") or body)
    image_bytes = None
    b64 = body.get("image_base64")
    if b64:
        image_bytes = base64.b64decode(b64)
    feishu = FeishuClient()
    try:
        result = handle_message(
            runtime=runtime,
            resource=resource,
            store=store,
            ttl_seconds=_ttl_seconds(),
            feishu=feishu,
            send=bool(body.get("send")),
            image_bytes=image_bytes,
            image_name=str(body.get("image_name") or "attachment.png"),
        )
        return JSONResponse(result)
    finally:
        feishu.close()


@app.post("/api/inject/card-action")
def inject_card_action(body: Dict[str, Any]) -> JSONResponse:
    blocked = _inject_allowed()
    if blocked:
        return blocked
    try:
        runtime = _runtime()
    except ConfigError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    event = dict(body.get("event") or body)
    dry_run = True if "dry_run" not in body else bool(body.get("dry_run"))
    feishu = FeishuClient()
    try:
        result = handle_card_action(
            runtime=runtime,
            event=event,
            store=store,
            ttl_seconds=_ttl_seconds(),
            feishu=feishu,
            dry_run=dry_run,
        )
        return JSONResponse(result)
    finally:
        feishu.close()


def _ws_on_event(kind: str, event: Dict[str, Any]) -> Any:
    runtime = _runtime()
    ttl = _ttl_seconds()
    feishu = FeishuClient()
    try:
        if kind == "p2p_entered":
            operator = event.get("operator_id") or event.get("operatorId") or {}
            open_id = str(
                operator.get("open_id")
                or operator.get("openId")
                or event.get("operator", {}).get("open_id")
                or ""
            )
            return handle_p2p_entered(
                runtime=runtime, open_id=open_id, feishu=feishu, send=True
            )
        if kind == "bot_menu":
            operator = (event.get("operator") or {}).get("operator_id") or {}
            open_id = str(operator.get("open_id") or "")
            event_key = str(event.get("event_key") or event.get("eventKey") or "")
            return handle_menu(
                runtime=runtime,
                open_id=open_id,
                event_key=event_key,
                store=store,
                ttl_seconds=ttl,
                feishu=feishu,
                send=True,
            )
        if kind == "im_message":
            message = event.get("message") or {}
            sender = event.get("sender") or {}
            sender_id = sender.get("sender_id") or sender.get("senderId") or {}
            text, image_keys = parse_im_content(message)
            resource = {
                "open_id": sender_id.get("open_id") or sender_id.get("openId") or "",
                "chat_id": message.get("chat_id") or message.get("chatId") or "",
                "chat_type": message.get("chat_type") or message.get("chatType") or "p2p",
                "message_id": message.get("message_id") or message.get("messageId") or "",
                "message_type": message.get("message_type") or message.get("messageType") or "text",
                "sender_type": sender.get("sender_type") or sender.get("senderType") or "",
                "text": text,
            }
            image_bytes = None
            image_name = "attachment.png"
            msg_id = str(resource["message_id"])
            if image_keys and msg_id:
                try:
                    image_bytes = feishu.download_message_resource(
                        msg_id, image_keys[0], resource_type="image"
                    )
                    if image_bytes[:3] == b"\xff\xd8\xff":
                        image_name = "attachment.jpg"
                except FeishuAPIError as exc:
                    logger.error("download image failed: %s", exc.message)
            return handle_message(
                runtime=runtime,
                resource=resource,
                store=store,
                ttl_seconds=ttl,
                feishu=feishu,
                send=True,
                image_bytes=image_bytes,
                image_name=image_name,
            )
        if kind == "card_action":
            return handle_card_action(
                runtime=runtime,
                event=event,
                store=store,
                ttl_seconds=ttl,
                feishu=feishu,
                dry_run=False,
            )
        logger.warning("unknown ws event kind=%s", kind)
        return {"ok": False, "error": "unknown_event"}
    finally:
        feishu.close()


@app.on_event("startup")
def on_startup() -> None:
    global _ws_started
    if not WS_ENABLED:
        logger.info("CARDBOT_ENABLE_WS=false; long connection not started")
        return
    if _ws_started:
        return
    _ws_started = True
    app_id = os.getenv("FEISHU_CHAT_APP_ID", "")
    app_secret = os.getenv("FEISHU_CHAT_APP_SECRET", "")

    def runner() -> None:
        from ws_client import start_ws_client

        start_ws_client(app_id=app_id, app_secret=app_secret, on_event=_ws_on_event)

    threading.Thread(target=runner, name="feishu-cardbot-ws", daemon=True).start()


if __name__ == "__main__":
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
