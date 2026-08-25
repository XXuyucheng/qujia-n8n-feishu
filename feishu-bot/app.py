"""feishu-bot HTTP 入口。

职责：接收 listener 转发的 IM 消息 → 路由 skill。
录入（upsert）走 Form Engine 写表；查询（search）只转发 n8n Agent，本进程不写表。
写表 / 发消息均使用「对话专用应用」凭证（FEISHU_CHAT_APP_*），与 n8n 主应用隔离。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from card_actions import CardActionHandler, parse_lifecycle, send_lifecycle_card
from cards import (
    build_overwrite_card,
    build_query_form_card,
    build_result_card,
    build_supplier_form_card,
    build_welcome_card,
)
from config_loader import (
    ConfigError,
    load_platform,
    resolve_config_dir,
    skill_to_runtime,
)
from dialog import (
    STATE_AWAIT_OVERWRITE,
    DialogEngine,
    should_handle_group,
)
from feishu_client import FeishuAPIError, FeishuClient
from query_proxy import call_query_agent
from router import resolve_skill_id
from session_store import SessionStore


def _env(*names: str, default: str = "") -> str:
    """按优先级读取环境变量，兼容旧名 SUPPLIER_BOT_*。"""
    for name in names:
        val = os.getenv(name)
        if val:
            return val
    return default


# ---------- 运行时路径与服务配置 ----------
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

app = FastAPI(title="Feishu Bot (skill platform)", version="0.3.0")
store = SessionStore(DB_PATH)


def _load() -> tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """加载 app.yaml + skills/*.yaml。"""
    return load_platform(CONFIG_DIR)


def _session_key(resource: Dict[str, Any]) -> str:
    """会话键：群聊按「用户+群」隔离，单聊按 open_id。"""
    open_id = str(resource.get("open_id") or "")
    chat_id = str(resource.get("chat_id") or "")
    chat_type = str(resource.get("chat_type") or "")
    if chat_type == "group":
        return f"{open_id}:{chat_id}"
    return open_id or chat_id


def _reply(feishu: FeishuClient, resource: Dict[str, Any], text: str) -> None:
    """优先 reply 原消息；无 message_id 时退化为向 chat 发文本。"""
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


def _send_card(feishu: FeishuClient, resource: Dict[str, Any], card: Dict[str, Any]) -> None:
    """发送互动卡片；失败时回退为标题文本。"""
    message_id = str(resource.get("message_id") or "")
    chat_id = str(resource.get("chat_id") or "")
    open_id = str(resource.get("open_id") or "")
    chat_type = str(resource.get("chat_type") or "")
    try:
        if message_id:
            feishu.reply_interactive(message_id, card)
            return
        if chat_type != "group" and open_id:
            feishu.send_interactive(open_id, card, receive_id_type="open_id")
            return
        if chat_id:
            feishu.send_interactive(chat_id, card)
            return
        logger.warning("no target to send card")
    except FeishuAPIError as exc:
        logger.error("send card failed: %s", exc.message)
        title = ((card.get("header") or {}).get("title") or {}).get("content") or "操作完成"
        _reply(feishu, resource, str(title))


def _strip_triggers(text: str, skill: Dict[str, Any]) -> str:
    t = (text or "").strip()
    for trig in sorted((skill.get("triggers") or []), key=len, reverse=True):
        trig = str(trig)
        if not trig:
            continue
        if t.startswith(trig):
            return t[len(trig) :].lstrip("：: ，, ").strip()
        if trig in t:
            return t.replace(trig, "", 1).strip()
    return t


def _make_feishu() -> FeishuClient:
    return FeishuClient()


card_handler = CardActionHandler(store, _load, _make_feishu)


@app.get("/health")
def health() -> Dict[str, Any]:
    """健康检查：配置是否可读、当前已加载的 skill 列表。"""
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
    """列出已启用 skill 的摘要（触发词、目标表）。"""
    _, skills = _load()
    return {
        "skills": [
            {
                "id": s["id"],
                "name": s.get("name"),
                "action": s.get("action") or "upsert_record",
                "triggers": s.get("triggers") or [],
                "table_id": (s.get("target") or {}).get("table_id"),
            }
            for s in skills.values()
        ]
    }


@app.post("/api/message")
def api_message(body: Dict[str, Any]) -> JSONResponse:
    """主入口：feishu-listener 转发的 IM 事件。

    流程：幂等去重 → 过期会话 → 群聊 @ 过滤 → 解析 skill →
    search 则转发 n8n（不下载图片、不入会话）→
    否则下载图片（如有）→ DialogEngine → 持久化会话 → 回复用户。
    """
    event = body.get("event") or body
    resource = event.get("resource") or {}
    sender_type = str(resource.get("sender_type") or "")
    # 忽略机器人自己发出的消息，避免循环
    if sender_type == "bot":
        return JSONResponse({"ok": True, "skipped": "bot_sender"})

    message_id = str(resource.get("message_id") or "")
    # 同一 message_id 只处理一次（飞书可能重推）
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
    # 群聊：无进行中会话且未 @ 机器人则忽略
    if not should_handle_group(
        chat_type=chat_type,
        mentions=mentions if isinstance(mentions, list) else [],
        bot_open_ids=None,
        has_session=bool(session),
    ):
        return JSONResponse({"ok": True, "skipped": "group_no_mention"})

    text = str(resource.get("text") or "")
    message_type = str(resource.get("message_type") or "text")
    # 图片可能来自 message_type=image，或 post 富文本里的 img
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
            # 未命中任何技能：欢迎卡片
            _send_card(
                feishu,
                resource,
                build_welcome_card(session_key=skey, open_id=str(resource.get("open_id") or "")),
            )
            return JSONResponse({"ok": True, "state": None, "skill_id": None})

        skill = skills[skill_id]
        # 只读查询：转发 n8n Agent，不进入 Form Engine、不写表
        if str(skill.get("action") or "") == "search":
            rest = _strip_triggers(text, skill)
            if not rest and not image_keys:
                _send_card(
                    feishu, resource, build_query_form_card(session_key=skey)
                )
                return JSONResponse(
                    {
                        "ok": True,
                        "state": None,
                        "skill_id": skill_id,
                        "action": "search",
                        "session_key": skey,
                    }
                )
            reply = call_query_agent(skill, text=text, resource=resource)
            _send_card(
                feishu,
                resource,
                build_result_card(
                    title="查询结果",
                    body=reply,
                    session_key=skey,
                    ok=True,
                ),
            )
            return JSONResponse(
                {
                    "ok": True,
                    "state": None,
                    "skill_id": skill_id,
                    "action": "search",
                    "session_key": skey,
                }
            )

        runtime = skill_to_runtime(skill, app_cfg)

        image_bytes: Optional[bytes] = None
        image_name = "attachment.png"
        if image_keys and message_id:
            key = image_keys[0]
            try:
                image_bytes = feishu.download_message_resource(
                    message_id, key, resource_type="image"
                )
                # 根据文件头猜测扩展名，便于后续上传
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

        # open_id 用于覆盖权限白名单判断
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
        # new_state 为 None 表示会话结束（取消 / 写入成功 / 拒绝覆盖等）
        if new_state is None:
            store.clear(skey)
        else:
            store.save(skey, new_state, payload or {"skill_id": skill_id})
        data = (payload or {}).get("data") or {}
        if new_state == STATE_AWAIT_OVERWRITE:
            _send_card(
                feishu,
                resource,
                build_overwrite_card(detail=reply, session_key=skey),
            )
        elif new_state in (None,):
            cancelled = "取消" in (reply or "")
            _send_card(
                feishu,
                resource,
                build_welcome_card(session_key=skey)
                if cancelled
                else build_result_card(
                    title="录入完成" if "成功" in reply or "已创建" in reply or "已更新" in reply else "提示",
                    body=reply,
                    session_key=skey,
                    ok="失败" not in reply and "权限" not in reply,
                ),
            )
        else:
            _send_card(
                feishu,
                resource,
                build_supplier_form_card(
                    runtime, data, session_key=skey, hint=reply
                ),
            )
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
    """调试接口：只做字段抽取 + 校验，不写会话、不写表。"""
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


@app.post("/api/card-action")
def api_card_action(body: Dict[str, Any]) -> JSONResponse:
    """飞书 card.action.trigger：须在约 3s 内返回 toast+card，写表/查询异步。"""
    try:
        result = card_handler.handle(body if isinstance(body, dict) else {})
    except Exception:
        logger.exception("card-action failed")
        result = {
            "toast": {
                "type": "error",
                "content": "卡片处理失败，请改用文字或稍后重试。",
            }
        }
    return JSONResponse(result)


@app.post("/api/lifecycle")
def api_lifecycle(body: Dict[str, Any]) -> JSONResponse:
    """p2p 进入 / 机器人菜单：异步发欢迎卡或表单卡。"""
    event = parse_lifecycle(body if isinstance(body, dict) else {})
    if not event.kind:
        return JSONResponse({"ok": True, "skipped": "unknown_lifecycle"})
    feishu = FeishuClient()
    try:
        sent = send_lifecycle_card(feishu, event, _load, store)
        return JSONResponse({"ok": True, "kind": event.kind, "sent": sent})
    except FeishuAPIError as exc:
        logger.error("lifecycle send failed: %s", exc.message)
        return JSONResponse({"ok": False, "error": exc.message}, status_code=502)
    except Exception:
        logger.exception("lifecycle failed")
        return JSONResponse({"ok": False, "error": "lifecycle_failed"}, status_code=500)
    finally:
        feishu.close()


if __name__ == "__main__":
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
