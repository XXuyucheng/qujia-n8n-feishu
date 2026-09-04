"""Dedicated process: Feishu chat-app IM websocket → POST into feishu-listener.

lark-oapi ws.Client cannot share a process with another Client (asyncio loop clash),
so the chat app connection runs here as a child process.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

try:
    import lark_oapi as lark
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"lark_oapi required: {exc}") from exc

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("feishu-listener.chat-ws")

INGEST_URL = os.getenv(
    "LISTENER_CHAT_INGEST_URL", "http://127.0.0.1:8010/internal/ingest"
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


def main() -> None:
    enabled = os.getenv("FEISHU_CHAT_WS_ENABLED", "true").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        logger.info("FEISHU_CHAT_WS_ENABLED is false; chat ws exiting")
        return
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

    verification_token = os.getenv(
        "FEISHU_CHAT_VERIFICATION_TOKEN",
        os.getenv("FEISHU_VERIFICATION_TOKEN", ""),
    )
    encrypt_key = os.getenv(
        "FEISHU_CHAT_ENCRYPT_KEY",
        os.getenv("FEISHU_ENCRYPT_KEY", ""),
    )

    def handle_im_message(data: Any) -> None:
        forward(data)

    builder = lark.EventDispatcherHandler.builder(verification_token, encrypt_key)
    builder = builder.register_p2_im_message_receive_v1(handle_im_message)
    handler = builder.build()

    logger.info("starting chat websocket for app_id=%s ingest=%s", app_id, INGEST_URL)
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
