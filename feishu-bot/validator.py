"""字段校验、选项归一化、组合必填、覆盖权限判断。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


# 用户常用来表示「没有 / 空」的字面量（去空白后比对）
_BLANK_TOKENS = {
    "无",
    "没有",
    "没",
    "无有",
    "暂无",
    "没有了",
    "不填",
    "不写",
    "空",
    "空着",
    "空白",
    "无无",
    "无。",
    "不详",
    "未知",
    "没有账号",
    "无账号",
    "没有户名",
    "无户名",
    "没有银行",
    "无银行",
    "/",
    "／",
    "\\",
    "-",
    "－",
    "—",
    "–",
    "~",
    "～",
    "*",
    "＊",
    ".",
    "。",
    "n/a",
    "na",
    "null",
    "none",
    "nil",
    "undefined",
}


def is_blank_value(value: Any) -> bool:
    """判断是否应视为空值（不写入、不校验）。"""
    if value is None:
        return True
    if value == [] or value == {}:
        return True
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return True
    # 去掉常见包裹符号后再判
    s2 = s.strip("（）()【】[]「」\"'“”")
    if not s2:
        return True
    low = s2.lower()
    if low in _BLANK_TOKENS or s2 in _BLANK_TOKENS:
        return True
    # 纯占位符重复：---、///、……
    if re.fullmatch(r"[\-－—–/~～./／。.*＊\\]+", s2):
        return True
    return False


def _norm(s: str) -> str:
    """去空白并转小写，用于模糊匹配。"""
    return re.sub(r"\s+", "", str(s or "")).lower()


def fuzzy_match_option(value: str, options: List[str]) -> Optional[str]:
    """将用户输入匹配到 options 中的标准值。

    顺序：精确 → 银行简称别名表 → 包含关系模糊匹配。
    """
    if is_blank_value(value):
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
    """账号去空白（空格、换行等）。"""
    return re.sub(r"\s+", "", str(value or ""))


def strip_blank_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """去掉值为空占位符的字段。"""
    out: Dict[str, Any] = {}
    for key, val in (data or {}).items():
        if is_blank_value(val):
            continue
        out[key] = val
    return out


def merge_fields(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """合并字段草稿：incoming 非空覆盖；空占位符则清除该键。"""
    out = dict(existing or {})
    for key, val in (incoming or {}).items():
        if val is None or val == []:
            continue
        if is_blank_value(val):
            out.pop(key, None)
            continue
        out[key] = val
    return out


def missing_required(fields_cfg: Dict[str, Any], data: Dict[str, Any]) -> List[str]:
    """返回仍缺失的必填逻辑键列表。"""
    missing = []
    for key, cfg in fields_cfg.items():
        if not cfg.get("required"):
            continue
        if not data.get(key) or is_blank_value(data.get(key)):
            missing.append(key)
    return missing


def payment_satisfied(config: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """组合必填：require_any_group 中任一组字段全部有值即通过；未配置则视为通过。"""
    groups = config.get("require_any_group") or config.get("payment_groups") or []
    if not groups:
        return True
    for group in groups:
        if all(data.get(k) and not is_blank_value(data.get(k)) for k in group):
            return True
    return False


PAYMENT_BANK_KEYS = ("account_name", "account_no", "bank_name")


def attachment_payment_ok(config: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """是否已具备二维码/附件收款（attachment_field 或 qrcode）。"""
    key = str(config.get("attachment_field") or "qrcode")
    return bool(data.get(key) or data.get("qrcode"))


def bank_transfer_complete(data: Dict[str, Any]) -> bool:
    """户名+账号+银行是否齐全。"""
    return all(data.get(k) and not is_blank_value(data.get(k)) for k in PAYMENT_BANK_KEYS)


def reconcile_qrcode_payment(
    config: Dict[str, Any],
    data: Dict[str, Any],
    problems: List[str],
) -> Tuple[Dict[str, Any], List[str]]:
    """收款二选一：已有收款码且银行三件套未齐时，银行字段可空。"""
    if not attachment_payment_ok(config, data):
        return data, problems
    if bank_transfer_complete(data):
        return data, problems

    out = dict(data)
    fields_cfg = config.get("fields") or {}
    labels: List[str] = []
    for key in PAYMENT_BANK_KEYS:
        if key in out:
            del out[key]
        label = str((fields_cfg.get(key) or {}).get("name") or key)
        labels.append(label)

    filtered: List[str] = []
    for msg in problems:
        if any(lab and lab in msg for lab in labels):
            continue
        if "纯数字" in msg and ("账号" in msg or "account" in msg.lower()):
            continue
        filtered.append(msg)
    return out, filtered


def missing_payment_hint(config: Dict[str, Any], data: Dict[str, Any]) -> str:
    """组合必填未满足时的提示文案。"""
    if payment_satisfied(config, data):
        return ""
    prompts = config.get("prompts") or {}
    return str(
        prompts.get("group_hint")
        or "仍需满足组合必填条件（见技能配置 require_any_group）"
    )


def suggested_missing(fields_cfg: Dict[str, Any], data: Dict[str, Any]) -> List[str]:
    """建议填写但非强制的字段（suggested: true）。"""
    missing = []
    for key, cfg in fields_cfg.items():
        if not cfg.get("suggested"):
            continue
        if not data.get(key) or is_blank_value(data.get(key)):
            missing.append(key)
    return missing


def _option_missing_msg(config: Dict[str, Any], label: str, value: str) -> str:
    """选项不存在时的硬提示（可配置 prompts.option_missing）。"""
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
    """按 field.pattern 校验；空占位符直接丢弃；非法值删除并记入 problems。"""
    out = dict(data)
    problems: List[str] = []
    prompts = (config or {}).get("prompts") or {}
    for key, cfg in fields_cfg.items():
        if key not in out:
            continue
        if is_blank_value(out[key]):
            del out[key]
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
    """单选/多选归一化；空占位符丢弃；无法匹配 options 时硬提示并丢弃该值。"""
    out = dict(data)
    problems: List[str] = []
    cfg_root = config or {}
    for key, cfg in fields_cfg.items():
        ftype = cfg.get("type")
        options = cfg.get("options") or []
        if key not in out or not options:
            continue
        if is_blank_value(out[key]):
            del out[key]
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
                if not part or is_blank_value(part):
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
    """逻辑键 → 飞书字段名，组装 create/update 所需 fields 对象。"""
    result: Dict[str, Any] = {}
    for key, cfg in fields_cfg.items():
        if key not in data or is_blank_value(data[key]):
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
    """是否允许覆盖：open_id 须在 permissions.overwrite_open_ids 白名单内。"""
    perms = config.get("permissions") or {}
    allowed = perms.get("overwrite_open_ids") or []
    if not allowed:
        return False
    return str(open_id or "") in {str(x) for x in allowed}
