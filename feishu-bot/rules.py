"""Skill YAML 业务规则：关键词命中 → 自动设字段 / 名称 pattern 校验。

示例（供应商 skill）：消息含「客户退款」→ supplier_type=客户退款，
且 supplier_name 须匹配「客户退款+订单号数字」。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


def apply_rules(
    text: str,
    data: Dict[str, Any],
    config: Dict[str, Any],
    *,
    sticky_rule_ids: List[str] | None = None,
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """对当前消息与字段草稿应用 skill.rules。

    参数：
        sticky_rule_ids: 本会话已激活的规则 id（后续轮次即使不再提关键词也继续强制）

    返回：
        (更新后的 data, 提示文案列表, 当前激活的 rule id 列表)
    """
    out = dict(data or {})
    hints: List[str] = []
    active: List[str] = list(sticky_rule_ids or [])
    # 关键词既匹配原文，也匹配已填字段值（防用户只改名不提关键词）
    blob = f"{text or ''}\n" + "\n".join(
        str(v) for v in out.values() if v not in (None, "", [])
    )

    for rule in config.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rid = str(rule.get("id") or "")
        keywords = [str(k) for k in (rule.get("keywords") or []) if k]
        hit = rid in active or any(k and k in blob for k in keywords)
        if not hit:
            continue
        if rid and rid not in active:
            active.append(rid)

        # 自动写入字段（如类型=客户退款）
        for key, val in (rule.get("set_fields") or {}).items():
            out[key] = val

        # 名称必须符合正则，否则给出 hint（上层停留 collecting）
        pattern = rule.get("require_name_pattern")
        if pattern:
            name = str(out.get("supplier_name") or "").strip()
            try:
                ok = bool(re.search(str(pattern), name))
            except re.error:
                ok = False
            if not ok:
                hint = str(rule.get("hint") or f"名称需匹配规则 {rid}")
                if hint not in hints:
                    hints.append(hint)

    return out, hints, active
