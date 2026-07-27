"""从自由文本抽取业务字段：规则优先，必要时再调 LLM 补全。

LLM 使用 VISION_API_*（与合同识别共用豆包/OpenAI 兼容接口），
仅做字段整理，不开启独立 Agent 对话；确认前仍展示给人审。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional

import httpx

from validator import is_blank_value, normalize_account_no

logger = logging.getLogger("feishu-bot.extractor")

# 「标签：值」行；值可为空白（户名：）
LABEL_LINE = re.compile(
    r"^(?P<label>[\u4e00-\u9fffA-Za-z/]{1,12})\s*[:：]\s*(?P<value>.*)$"
)

# 行内抽取到的值若像「下一字段标签：」则视为误匹配
_LOOKS_LIKE_LABEL = re.compile(r"^[\u4e00-\u9fffA-Za-z/]{1,12}[:：]")


def _clean_extracted_value(value: str) -> Optional[str]:
    """清洗抽取值；空占位符返回 None。"""
    val = (value or "").strip()
    if is_blank_value(val):
        return None
    if _LOOKS_LIKE_LABEL.match(val):
        return None
    return val


def extract_by_rules(text: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """基于 aliases 与触发词的规则抽取（不调用 LLM）。"""
    aliases: Dict[str, str] = config.get("aliases") or {}
    fields_cfg: Dict[str, Any] = config.get("fields") or {}
    found: Dict[str, Any] = {}
    if not text or not text.strip():
        return found

    # 1) 按行解析「标签：值」（允许值为空 → 视为未填）
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    for line in lines:
        m = LABEL_LINE.match(line)
        if not m:
            continue
        label = m.group("label").strip()
        value = _clean_extracted_value(m.group("value") or "")
        key = aliases.get(label)
        if not key or key not in fields_cfg:
            continue
        if fields_cfg[key].get("type") == "attachment":
            continue
        if value is None:
            continue
        if key == "account_no":
            value = normalize_account_no(value)
            if is_blank_value(value):
                continue
        found[key] = value

    # 2) 行内「户名xxx 账号xxx」：不跨行吞下一标签（空白不含换行）
    compact = text
    for label, key in aliases.items():
        if key in found or key not in fields_cfg:
            continue
        if fields_cfg[key].get("type") == "attachment":
            continue
        pat = re.compile(
            rf"(?:^|[ \t,，；;]){re.escape(label)}[ \t]*[:：]?[ \t]*([^\s,，；;]+)"
        )
        m = pat.search(compact)
        if m:
            val = _clean_extracted_value(m.group(1))
            if val is None:
                continue
            if key == "account_no":
                val = normalize_account_no(val)
                if is_blank_value(val):
                    continue
            found[key] = val

    # 3) 「添加供应商 某某店」：触发词后整段当作名称
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
                if "：" in rest or ":" in rest:
                    break
                name = re.split(r"[,，；;\n]", rest)[0].strip()
                name = _clean_extracted_value(name) or ""
                if name and len(name) <= 40:
                    found["supplier_name"] = name
                break

    return found


def _llm_enabled() -> bool:
    return bool(os.getenv("VISION_API_KEY"))


def extract_by_llm(text: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """调用 LLM 抽取 JSON 字段；失败则返回空 dict。"""
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
            # Ark Responses API vs OpenAI chat/completions 两种形态
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
        if isinstance(v, str):
            v = _clean_extracted_value(v)
            if v is None:
                continue
        if is_blank_value(v):
            continue
        if k == "account_no":
            v = normalize_account_no(str(v))
            if is_blank_value(v):
                continue
        cleaned[k] = v
    return cleaned


def _parse_llm_content(body: Dict[str, Any]) -> str:
    """从不同厂商响应结构中取出文本内容。"""
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
    """规则抽取 + 条件触发 LLM；冲突时规则结果优先覆盖。

    触发 LLM 的条件（可配置 ai.*）：
    - 缺必填 / 组合必填未满足（extract_on_incomplete）
    - 或原文较长（> long_text_chars，默认 40）
    """
    ruled = extract_by_rules(text, config)
    if not use_llm:
        return ruled

    ai_cfg = config.get("ai") or {}
    extract_on_incomplete = bool(ai_cfg.get("extract_on_incomplete", True))
    long_chars = int(ai_cfg.get("long_text_chars") or 40)

    fields_cfg = config.get("fields") or {}
    from validator import missing_required, payment_satisfied

    incomplete = bool(missing_required(fields_cfg, ruled)) or not payment_satisfied(
        config, ruled
    )
    long_text = len((text or "").strip()) > long_chars
    already_rich = ruled.get("supplier_name") and (
        (ruled.get("account_no") and ruled.get("bank_name")) or len(ruled) >= 4
    )

    # 规则已抽得很全且非长文 → 不浪费 LLM 调用
    if already_rich and not (extract_on_incomplete and incomplete) and not long_text:
        return ruled
    if not extract_on_incomplete and already_rich:
        return ruled

    llm = extract_by_llm(text, config)
    merged = dict(llm)
    merged.update(ruled)  # 规则优先
    # 最终再滤一遍空占位符
    return {k: v for k, v in merged.items() if not is_blank_value(v)}
