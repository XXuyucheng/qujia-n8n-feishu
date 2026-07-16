"""Extract supplier fields from free-form text (rules + optional LLM)."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional

import httpx

from validator import normalize_account_no

logger = logging.getLogger("supplier-bot.extractor")

LABEL_LINE = re.compile(
    r"^(?P<label>[\u4e00-\u9fffA-Za-z/]{1,12})\s*[:：]\s*(?P<value>.+)$"
)


def extract_by_rules(text: str, config: Dict[str, Any]) -> Dict[str, Any]:
    aliases: Dict[str, str] = config.get("aliases") or {}
    fields_cfg: Dict[str, Any] = config.get("fields") or {}
    found: Dict[str, Any] = {}
    if not text or not text.strip():
        return found

    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    for line in lines:
        m = LABEL_LINE.match(line)
        if not m:
            continue
        label = m.group("label").strip()
        value = m.group("value").strip()
        key = aliases.get(label)
        if not key or key not in fields_cfg:
            continue
        if fields_cfg[key].get("type") == "attachment":
            continue
        if key == "account_no":
            value = normalize_account_no(value)
        found[key] = value

    # inline patterns: 户名xxx 账号xxx
    compact = text
    for label, key in aliases.items():
        if key in found or key not in fields_cfg:
            continue
        if fields_cfg[key].get("type") == "attachment":
            continue
        pat = re.compile(
            rf"(?:^|[\s,，；;]){re.escape(label)}\s*[:：]?\s*([^\s,，；;]+)"
        )
        m = pat.search(compact)
        if m:
            val = m.group(1).strip()
            if key == "account_no":
                val = normalize_account_no(val)
            found[key] = val

    # bare trigger + name: "添加供应商 某某店"
    if "supplier_name" not in found:
        for trigger in config.get("triggers") or []:
            if trigger.startswith("/"):
                continue
            m = re.search(
                rf"{re.escape(trigger)}\s*[:：]?\s*(.+)$",
                text.strip(),
                re.S,
            )
            if m:
                rest = m.group(1).strip()
                # if rest looks like labeled dump, skip assigning whole rest
                if "：" in rest or ":" in rest:
                    break
                # take first segment before comma if long
                name = re.split(r"[,，；;\n]", rest)[0].strip()
                if name and len(name) <= 40:
                    found["supplier_name"] = name
                break

    return found


def _llm_enabled() -> bool:
    return bool(os.getenv("VISION_API_KEY"))


def extract_by_llm(text: str, config: Dict[str, Any]) -> Dict[str, Any]:
    if not _llm_enabled() or not text.strip():
        return {}

    field_keys = [
        k
        for k, cfg in (config.get("fields") or {}).items()
        if cfg.get("type") != "attachment"
    ]
    prompt = (
        "你是供应商信息抽取助手。从用户消息中提取字段，只返回 JSON 对象，"
        "不要 markdown。未知字段省略。字段键："
        + ", ".join(field_keys)
        + "。\n\n用户消息：\n"
        + text
    )
    api_url = os.getenv(
        "VISION_API_URL", "https://ark.cn-beijing.volces.com/api/v3/responses"
    )
    model = os.getenv("VISION_MODEL", "gpt-4o-mini")
    api_key = os.getenv("VISION_API_KEY", "")

    try:
        with httpx.Client(timeout=30.0) as client:
            # Prefer OpenAI-compatible chat if URL looks like responses; fallback chat/completions
            if api_url.rstrip("/").endswith("/responses"):
                resp = client.post(
                    api_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "input": [{"role": "user", "content": prompt}],
                    },
                )
            else:
                chat_url = api_url
                if not chat_url.endswith("/chat/completions"):
                    chat_url = chat_url.rstrip("/") + "/chat/completions"
                resp = client.post(
                    chat_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0,
                    },
                )
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        logger.warning("LLM extract failed: %s", exc)
        return {}

    content = _parse_llm_content(body)
    if not content:
        return {}
    try:
        # strip code fences
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        data = json.loads(content)
    except Exception:
        logger.warning("LLM returned non-JSON: %s", content[:200])
        return {}

    if not isinstance(data, dict):
        return {}

    allowed = set(field_keys)
    cleaned: Dict[str, Any] = {}
    for k, v in data.items():
        if k not in allowed or v is None or v == "":
            continue
        if k == "account_no":
            v = normalize_account_no(str(v))
        cleaned[k] = v
    return cleaned


def _parse_llm_content(body: Dict[str, Any]) -> str:
    # Ark responses API shapes vary
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    output = body.get("output")
    if isinstance(output, list):
        chunks = []
        for item in output:
            if not isinstance(item, dict):
                continue
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("text"):
                    chunks.append(str(c["text"]))
        if chunks:
            return "\n".join(chunks)
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        msg = (choices[0] or {}).get("message") or {}
        return str(msg.get("content") or "")
    return ""


def extract_fields(
    text: str,
    config: Dict[str, Any],
    *,
    use_llm: bool = True,
) -> Dict[str, Any]:
    ruled = extract_by_rules(text, config)
    if not use_llm:
        return ruled
    # If we already have supplier_name + some payment fields, skip LLM
    if ruled.get("supplier_name") and (
        (ruled.get("account_no") and ruled.get("bank_name")) or len(ruled) >= 4
    ):
        return ruled
    llm = extract_by_llm(text, config)
    merged = dict(llm)
    merged.update(ruled)  # rules win on conflicts
    return merged
