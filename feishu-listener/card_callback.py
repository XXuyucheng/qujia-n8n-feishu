"""卡片回传：把 feishu-bot 的 JSON 转成飞书 card.action.trigger 响应体。"""

from __future__ import annotations

from typing import Any, Dict


def bot_card_response_to_sdk(body: Dict[str, Any]) -> Dict[str, Any]:
    """bot /api/card-action → 飞书回调 toast + raw card。"""
    out: Dict[str, Any] = {}
    toast = body.get("toast")
    if isinstance(toast, dict):
        out["toast"] = toast
    card = body.get("card")
    if isinstance(card, dict):
        out["card"] = {"type": "raw", "data": card}
    return out
