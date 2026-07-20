"""Intent router: sticky session skill, then trigger match."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def match_trigger(text: str, skills: Dict[str, Dict[str, Any]]) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return None
    # longer triggers first to avoid short substring surprises
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
    """
    1) Active session skill sticky
    2) Trigger match on text
    3) None → idle / menu
    """
    payload = (session or {}).get("payload") or {}
    sticky = payload.get("skill_id") or (session or {}).get("skill_id")
    state = (session or {}).get("state")
    if sticky and sticky in skills and state and state != "idle":
        return str(sticky)

    hit = match_trigger(text, skills)
    if hit:
        return hit

    # image with sticky collecting already handled; image alone without session → None
    if has_image and sticky and sticky in skills:
        return str(sticky)
    return None
