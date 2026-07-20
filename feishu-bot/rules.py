"""Skill YAML rules: keyword triggers, auto-set fields, name patterns."""

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
    """
    Apply skill rules against message text and current field data.

    Returns (data, hint_messages, active_rule_ids).
    sticky_rule_ids: rules already activated in this session (keep enforcing).
    """
    out = dict(data or {})
    hints: List[str] = []
    active: List[str] = list(sticky_rule_ids or [])
    blob = f"{text or ''}\n" + "\n".join(str(v) for v in out.values() if v not in (None, "", []))

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

        for key, val in (rule.get("set_fields") or {}).items():
            out[key] = val

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
