"""意图路由：进行中会话粘性 skill，否则按触发词匹配。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def match_trigger(text: str, skills: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """在全部 skill 的 triggers 中做子串匹配；长触发词优先，减少误命中。"""
    t = (text or "").strip()
    if not t:
        return None
    ranked: List[Tuple[int, str, str]] = []
    for sid, skill in skills.items():
        for trig in skill.get("triggers") or []:
            ranked.append((len(str(trig)), str(trig), sid))
    ranked.sort(key=lambda x: -x[0])
    for _, trig, sid in ranked:
        if trig and trig in t:
            return sid
    return None


def resolve_skill_id(
    *,
    text: str,
    session: Optional[Dict[str, Any]],
    skills: Dict[str, Dict[str, Any]],
    has_image: bool = False,
) -> Optional[str]:
    """解析本轮应使用的 skill_id。

    优先级：
    1. 非 idle 的进行中会话 → 粘性沿用原 skill
    2. 文本命中触发词
    3. 仅有图片但会话里已有 sticky skill
    4. 否则返回 None（由上层展示 idle 菜单）
    """
    payload = (session or {}).get("payload") or {}
    sticky = payload.get("skill_id") or (session or {}).get("skill_id")
    state = (session or {}).get("state")
    if sticky and sticky in skills and state and state != "idle":
        return str(sticky)

    hit = match_trigger(text, skills)
    if hit:
        return hit

    if has_image and sticky and sticky in skills:
        return str(sticky)
    return None
