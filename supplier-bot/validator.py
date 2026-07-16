"""Field validation and payment-group completeness."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def fuzzy_match_option(value: str, options: List[str]) -> Optional[str]:
    if not value:
        return None
    raw = str(value).strip()
    for opt in options:
        if opt == raw:
            return opt
    n = _norm(raw)
    aliases = {
        "工行": "中国工商银行",
        "工商银行": "中国工商银行",
        "农行": "中国农业银行股份有限公司",
        "农业银行": "中国农业银行股份有限公司",
        "建行": "中国建设银行股份有限公司总行",
        "建设银行": "中国建设银行股份有限公司总行",
        "中行": "中国银行总行",
        "中国银行": "中国银行总行",
        "招行": "招商银行股份有限公司",
        "招商银行": "招商银行股份有限公司",
        "交行": "交通银行股份有限公司",
        "交通银行": "交通银行股份有限公司",
        "邮储": "中国邮政储蓄银行有限责任公司",
        "民生": "中国民生银行",
        "光大": "中国光大银行",
        "浦发": "上海浦东发展银行",
        "兴业": "兴业银行总行",
        "中信": "中信银行股份有限公司",
        "平安": "平安银行（原深圳发展银行）",
        "浙商": "浙商银行",
        "杭州银行": "杭州银行股份有限公司",
        "宁波银行": "宁波银行股份有限公司",
        "农商": "浙江农商银行",
    }
    if raw in aliases:
        target = aliases[raw]
        if target in options:
            return target
    for opt in options:
        on = _norm(opt)
        if n and (n in on or on in n):
            return opt
    return None


def normalize_account_no(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def merge_fields(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(existing or {})
    for key, val in (incoming or {}).items():
        if val is None or val == "" or val == []:
            continue
        out[key] = val
    return out


def missing_required(fields_cfg: Dict[str, Any], data: Dict[str, Any]) -> List[str]:
    missing = []
    for key, cfg in fields_cfg.items():
        if not cfg.get("required"):
            continue
        if not data.get(key):
            missing.append(key)
    return missing


def payment_satisfied(config: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """True if any require_any_group / payment_groups is fully filled (or no groups configured)."""
    groups = config.get("require_any_group") or config.get("payment_groups") or []
    if not groups:
        return True
    for group in groups:
        if all(data.get(k) for k in group):
            return True
    return False


def missing_payment_hint(config: Dict[str, Any], data: Dict[str, Any]) -> str:
    if payment_satisfied(config, data):
        return ""
    prompts = config.get("prompts") or {}
    return str(prompts.get("group_hint") or "仍需满足组合必填条件（见技能配置 require_any_group）")


def suggested_missing(fields_cfg: Dict[str, Any], data: Dict[str, Any]) -> List[str]:
    missing = []
    for key, cfg in fields_cfg.items():
        if not cfg.get("suggested"):
            continue
        if not data.get(key):
            missing.append(key)
    return missing


def resolve_select_fields(
    fields_cfg: Dict[str, Any], data: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[str]]:
    """Normalize select values; return (data, unresolved_messages)."""
    out = dict(data)
    problems: List[str] = []
    for key, cfg in fields_cfg.items():
        ftype = cfg.get("type")
        options = cfg.get("options") or []
        if key not in out or not out[key] or not options:
            continue
        if ftype == "single_select":
            matched = fuzzy_match_option(str(out[key]), options)
            if matched:
                out[key] = matched
            else:
                sample = "、".join(options[:8])
                label = cfg.get("name", key)
                problems.append(
                    f"{label}无法匹配「{out[key]}」，可选如：{sample}…"
                )
                del out[key]
        elif ftype == "multi_select":
            raw = out[key]
            parts = raw if isinstance(raw, list) else re.split(r"[,，/、\s]+", str(raw))
            resolved = []
            for part in parts:
                part = str(part).strip()
                if not part:
                    continue
                matched = fuzzy_match_option(part, options)
                if matched:
                    resolved.append(matched)
                else:
                    label = cfg.get("name", key)
                    problems.append(
                        f"{label}中的「{part}」无法匹配，可选：{'、'.join(options)}"
                    )
            if resolved:
                out[key] = resolved
            else:
                del out[key]
    return out, problems


def build_bitable_fields(fields_cfg: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    """Map logical keys to Feishu field names for create/update."""
    result: Dict[str, Any] = {}
    for key, cfg in fields_cfg.items():
        if key not in data or data[key] in (None, "", []):
            continue
        name = cfg["name"]
        ftype = cfg.get("type")
        val = data[key]
        if ftype == "attachment":
            if isinstance(val, str):
                result[name] = [{"file_token": val}]
            elif isinstance(val, list):
                tokens = []
                for item in val:
                    if isinstance(item, dict) and item.get("file_token"):
                        tokens.append({"file_token": item["file_token"]})
                    elif isinstance(item, str):
                        tokens.append({"file_token": item})
                if tokens:
                    result[name] = tokens
        elif ftype == "multi_select":
            result[name] = val if isinstance(val, list) else [val]
        else:
            result[name] = val
    return result
