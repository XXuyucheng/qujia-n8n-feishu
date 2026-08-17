"""只读查询：把用户问题转给 n8n Agent webhook，本进程不写表。"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("feishu-bot.query")


def _prompt(skill: Dict[str, Any], key: str, default: str) -> str:
    prompts = skill.get("prompts") or {}
    return str(prompts.get(key) or default)


def resolve_webhook_url(skill: Dict[str, Any]) -> str:
    target = skill.get("target") or {}
    url = str(target.get("webhook_url") or "").strip()
    if url:
        return url
    return str(os.getenv("N8N_QUERY_WEBHOOK_URL") or "").strip()


def parse_agent_reply(body: Any) -> str:
    """从 n8n 响应中取出给人看的文本。"""
    if body is None:
        return ""
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, list) and body:
        return parse_agent_reply(body[0])
    if not isinstance(body, dict):
        return str(body).strip()
    for key in ("reply", "text", "output", "message"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    inner = body.get("json")
    if inner is not None and inner is not body:
        return parse_agent_reply(inner)
    data = body.get("data")
    if data is not None:
        return parse_agent_reply(data)
    return ""


def call_query_agent(
    skill: Dict[str, Any],
    *,
    text: str,
    resource: Dict[str, Any],
    timeout: float = 20.0,
) -> str:
    """POST n8n；失败返回 skill prompts，永不写表。"""
    url = resolve_webhook_url(skill)
    if not url:
        return _prompt(
            skill,
            "unavailable",
            "查询服务未配置。",
        )

    payload = {
        "skill_id": skill.get("id") or "query",
        "text": text,
        "open_id": str(resource.get("open_id") or ""),
        "chat_id": str(resource.get("chat_id") or ""),
        "chat_type": str(resource.get("chat_type") or ""),
        "message_id": str(resource.get("message_id") or ""),
        "action": "search",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            try:
                body: Any = resp.json()
            except Exception:
                body = resp.text
    except Exception as exc:
        logger.warning("query agent failed url=%s err=%s", url, exc)
        return _prompt(skill, "timeout", "查询超时或 n8n 未响应，请稍后重试。")

    reply = parse_agent_reply(body)
    if not reply:
        return _prompt(skill, "empty", "没有查到可用结果。")
    return reply
