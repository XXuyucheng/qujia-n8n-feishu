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


def _option_missing_msg(config: Dict[str, Any], label: str, value: str) -> str:
    prompts = (config or {}).get("prompts") or {}
    template = str(
        prompts.get("option_missing")
        or "「{label}」选项「{value}」不存在，请核对或联系管理员。"
    )
    try:
        return template.format(label=label, value=value)
    except (KeyError, ValueError):
        return f"「{label}」选项「{value}」不存在，请核对或联系管理员。"


def apply_field_patterns(
    fields_cfg: Dict[str, Any],
    data: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Validate pattern constraints (e.g. digits). Invalid values are dropped."""
    out = dict(data)
    problems: List[str] = []
    prompts = (config or {}).get("prompts") or {}
    for key, cfg in fields_cfg.items():
        if key not in out or out[key] in (None, "", []):
            continue
        pattern = cfg.get("pattern")
        if not pattern:
            continue
        raw = out[key]
        if pattern == "digits":
            cleaned = normalize_account_no(str(raw))
            if not cleaned or not re.fullmatch(r"\d+", cleaned):
                label = cfg.get("name", key)
                template = str(
                    prompts.get("account_invalid")
                    or "{label}必须为纯数字，当前值「{value}」无效。"
                )
                try:
                    problems.append(template.format(label=label, value=raw))
                except (KeyError, ValueError):
                    problems.append(f"{label}必须为纯数字，当前值「{raw}」无效。")
                del out[key]
            else:
                out[key] = cleaned
    return out, problems


def resolve_select_fields(
    fields_cfg: Dict[str, Any],
    data: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Normalize select values; unmatched options become hard errors (value dropped)."""
    out = dict(data)
    problems: List[str] = []
    cfg_root = config or {}
    for key, cfg in fields_cfg.items():
        ftype = cfg.get("type")
        options = cfg.get("options") or []
        if key not in out or not out[key] or not options:
            continue
        if ftype == "single_select":
            raw_val = str(out[key])
            matched = fuzzy_match_option(raw_val, options)
            if matched:
                out[key] = matched
            else:
                label = cfg.get("name", key)
                problems.append(_option_missing_msg(cfg_root, label, raw_val))
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
                    problems.append(_option_missing_msg(cfg_root, label, part))
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


def can_overwrite(config: Dict[str, Any], open_id: str) -> bool:
    perms = config.get("permissions") or {}
    allowed = perms.get("overwrite_open_ids") or []
    if not allowed:
        return False
    return str(open_id or "") in {str(x) for x in allowed}
