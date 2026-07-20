"""Skill-based Feishu chat bot — multi-turn dialog → bitable."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config_loader import (
    ConfigError,
    load_platform,
    resolve_config_dir,
    skill_to_runtime,
)
from dialog import DialogEngine, should_handle_group
from feishu_client import FeishuAPIError, FeishuClient
from router import resolve_skill_id
from session_store import SessionStore

def _env(*names: str, default: str = "") -> str:
    for name in names:
        val = os.getenv(name)
        if val:
            return val
    return default


CONFIG_DIR = resolve_config_dir(
    Path(_env("FEISHU_BOT_CONFIG_DIR", "SUPPLIER_BOT_CONFIG_DIR"))
    if _env("FEISHU_BOT_CONFIG_DIR", "SUPPLIER_BOT_CONFIG_DIR")
    else None
)
DB_PATH = Path(
    _env("FEISHU_BOT_DB_PATH", "SUPPLIER_BOT_DB_PATH", default="/data/sessions.sqlite")
)
HOST = _env("FEISHU_BOT_HOST", "SUPPLIER_BOT_HOST", default="0.0.0.0")
PORT = int(_env("FEISHU_BOT_PORT", "SUPPLIER_BOT_PORT", default="8040"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("feishu-bot")

app = FastAPI(title="Feishu Bot (skill platform)", version="0.2.0")
store = SessionStore(DB_PATH)


def _load() -> tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    return load_platform(CONFIG_DIR)


def _session_key(resource: Dict[str, Any]) -> str:
    open_id = str(resource.get("open_id") or "")
    chat_id = str(resource.get("chat_id") or "")
    chat_type = str(resource.get("chat_type") or "")
    if chat_type == "group":
        return f"{open_id}:{chat_id}"
    return open_id or chat_id


def _reply(feishu: FeishuClient, resource: Dict[str, Any], text: str) -> None:
    message_id = str(resource.get("message_id") or "")
    chat_id = str(resource.get("chat_id") or "")
    try:
        if message_id:
            feishu.reply_text(message_id, text)
        elif chat_id:
            feishu.send_text(chat_id, text)
        else:
            logger.warning("no message_id/chat_id to reply")
    except FeishuAPIError as exc:
        logger.error("reply failed: %s", exc.message)


@app.get("/health")
def health() -> Dict[str, Any]:
    try:
        app_cfg, skills = _load()
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
        "service": "feishu-bot",
        "config_dir": str(CONFIG_DIR),
        "skills": skill_ids,
        "session_ttl_minutes": app_cfg.get("session_ttl_minutes"),
        "detail": detail,
        "db_path": str(DB_PATH),
    }


@app.get("/skills")
def list_skills() -> Dict[str, Any]:
    _, skills = _load()
    return {
        "skills": [
            {
                "id": s["id"],
                "name": s.get("name"),
                "triggers": s.get("triggers") or [],
                "table_id": (s.get("target") or {}).get("table_id"),
            }
            for s in skills.values()
        ]
    }


@app.post("/api/message")
def api_message(body: Dict[str, Any]) -> JSONResponse:
    event = body.get("event") or body
    resource = event.get("resource") or {}
    sender_type = str(resource.get("sender_type") or "")
    if sender_type == "bot":
        return JSONResponse({"ok": True, "skipped": "bot_sender"})

    message_id = str(resource.get("message_id") or "")
    if not store.claim_message(message_id):
        return JSONResponse({"ok": True, "skipped": "duplicate_message"})

    try:
        app_cfg, skills = _load()
    except ConfigError as exc:
        logger.error("config error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    ttl = int(app_cfg.get("session_ttl_minutes") or 30) * 60
    skey = _session_key(resource)
    store.expire_if_stale(skey, ttl)
    session = store.get(skey)

    chat_type = str(resource.get("chat_type") or "p2p")
    mentions = resource.get("mentions") or []
    if not should_handle_group(
        chat_type=chat_type,
        mentions=mentions if isinstance(mentions, list) else [],
        bot_open_ids=None,
        has_session=bool(session),
    ):
        return JSONResponse({"ok": True, "skipped": "group_no_mention"})

    text = str(resource.get("text") or "")
    message_type = str(resource.get("message_type") or "text")
    image_keys: list[str] = []
    raw_keys = resource.get("image_keys") or []
    if isinstance(raw_keys, list):
        image_keys.extend(str(k) for k in raw_keys if k)
    single = str(resource.get("image_key") or "")
    if single and single not in image_keys:
        image_keys.append(single)

    skill_id = resolve_skill_id(
        text=text,
        session=session,
        skills=skills,
        has_image=bool(image_keys),
    )

    feishu = FeishuClient()
    try:
        if not skill_id:
            # idle help / menu
            help_text = str(app_cfg.get("idle_help") or app_cfg.get("router", {}).get("ambiguous_reply") or "")
            if not help_text:
                help_text = "请发送业务指令，例如：添加供应商"
            _reply(feishu, resource, help_text)
            return JSONResponse({"ok": True, "state": None, "skill_id": None})

        skill = skills[skill_id]
        runtime = skill_to_runtime(skill, app_cfg)

        image_bytes: Optional[bytes] = None
        image_name = "attachment.png"
        if image_keys and message_id:
            key = image_keys[0]
            try:
                image_bytes = feishu.download_message_resource(
                    message_id, key, resource_type="image"
                )
                if image_bytes[:3] == b"\xff\xd8\xff":
                    image_name = "attachment.jpg"
                elif image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                    image_name = "attachment.png"
                logger.info(
                    "downloaded image skill=%s message_id=%s key=%s bytes=%s",
                    skill_id,
                    message_id,
                    key,
                    len(image_bytes),
                )
            except FeishuAPIError as exc:
                _reply(feishu, resource, f"下载图片失败：{exc.message}")
                return JSONResponse({"ok": False, "error": exc.message})

        open_id = str(resource.get("open_id") or "")
        engine = DialogEngine(runtime, feishu, open_id=open_id)
        reply, new_state, payload = engine.handle(
            session=session,
            text=text,
            message_type=message_type,
            image_bytes=image_bytes,
            image_name=image_name,
        )
        if payload is not None:
            payload["skill_id"] = skill_id
        if new_state is None:
            store.clear(skey)
        else:
            store.save(skey, new_state, payload or {"skill_id": skill_id})
        _reply(feishu, resource, reply)
        return JSONResponse(
            {
                "ok": True,
                "state": new_state,
                "skill_id": skill_id,
                "session_key": skey,
            }
        )
    finally:
        feishu.close()


@app.post("/api/preview-extract")
def preview_extract(body: Dict[str, Any]) -> Dict[str, Any]:
    from extractor import extract_fields
    from validator import apply_field_patterns, resolve_select_fields

    app_cfg, skills = _load()
    skill_id = str(body.get("skill_id") or "supplier")
    skill = skills.get(skill_id) or next(iter(skills.values()))
    runtime = skill_to_runtime(skill, app_cfg)
    text = str(body.get("text") or "")
    use_llm = bool(body.get("use_llm", False))
    data = extract_fields(text, runtime, use_llm=use_llm)
    data, problems = apply_field_patterns(runtime.get("fields") or {}, data, runtime)
    data, select_probs = resolve_select_fields(
        runtime.get("fields") or {}, data, runtime
    )
    problems.extend(select_probs)
    return {"skill_id": runtime.get("skill_id"), "fields": data, "problems": problems}


if __name__ == "__main__":
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
