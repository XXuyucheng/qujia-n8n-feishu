"""Form-engine dialog state machine (skill-config driven)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from extractor import extract_fields
from feishu_client import FeishuAPIError, FeishuClient
from validator import (
    build_bitable_fields,
    merge_fields,
    missing_payment_hint,
    missing_required,
    payment_satisfied,
    resolve_select_fields,
    suggested_missing,
)

logger = logging.getLogger("supplier-bot.dialog")

STATE_IDLE = "idle"
STATE_COLLECTING = "collecting"
STATE_CONFIRMING = "confirming"
STATE_AWAIT_OVERWRITE = "await_overwrite"


def _prompt(config: Dict[str, Any], key: str, default: str = "", **kwargs: Any) -> str:
    prompts = config.get("prompts") or {}
    template = str(prompts.get(key) or default)
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError):
            return template
    return template


def _field_label(config: Dict[str, Any], key: str) -> str:
    return ((config.get("fields") or {}).get(key) or {}).get("name") or key


def _attachment_key(config: Dict[str, Any]) -> str:
    return str(config.get("attachment_field") or "qrcode")


def _format_summary(config: Dict[str, Any], data: Dict[str, Any]) -> str:
    title = _prompt(config, "summary_title", "已整理信息")
    lines = [f"{title}："]
    attach_key = _attachment_key(config)
    for key, cfg in (config.get("fields") or {}).items():
        if key not in data or data[key] in (None, "", []):
            continue
        val = data[key]
        if key == attach_key:
            val = _prompt(config, "attachment_filled", "[已附图片]")
        elif isinstance(val, list):
            val = "、".join(str(x) for x in val)
        lines.append(f"- {cfg.get('name', key)}：{val}")
    return "\n".join(lines)


def _word_in(text: str, words: List[str]) -> bool:
    t = (text or "").strip().lower()
    for w in words:
        if w.lower() in t:
            return True
    return False


def is_trigger(text: str, config: Dict[str, Any]) -> bool:
    t = (text or "").strip()
    for trig in config.get("triggers") or []:
        if trig in t:
            return True
    return False


def should_handle_group(
    *,
    chat_type: str,
    mentions: List[Any],
    bot_open_ids: Optional[set],
    has_session: bool,
) -> bool:
    if chat_type != "group":
        return True
    if has_session:
        return True
    if not mentions:
        return False
    if not bot_open_ids:
        return True
    for m in mentions:
        if not isinstance(m, dict):
            continue
        oid = ((m.get("id") or {}) if isinstance(m.get("id"), dict) else {})
        open_id = m.get("open_id") or oid.get("open_id") or ""
        if open_id in bot_open_ids:
            return True
    return False


def _ensure_skill_payload(payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    payload["skill_id"] = config.get("skill_id")
    return payload


class DialogEngine:
    def __init__(self, config: Dict[str, Any], feishu: FeishuClient):
        self.config = config
        self.feishu = feishu

    def handle(
        self,
        *,
        session: Optional[Dict[str, Any]],
        text: str,
        message_type: str,
        image_bytes: Optional[bytes] = None,
        image_name: str = "qrcode.png",
    ) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
        text = (text or "").strip()
        fields_cfg = self.config.get("fields") or {}
        attach_key = _attachment_key(self.config)

        if session and _word_in(text, self.config.get("cancel_words") or []):
            return _prompt(self.config, "cancel", "已取消。"), None, None

        state = (session or {}).get("state") or STATE_IDLE
        payload = _ensure_skill_payload(dict((session or {}).get("payload") or {}), self.config)
        data = dict(payload.get("data") or {})

        if image_bytes:
            try:
                file_token = self.feishu.upload_bitable_media(
                    file_name=image_name,
                    content=image_bytes,
                    parent_node=str(
                        self.config.get("base_id")
                        or __import__("os").getenv("FEISHU_BASE_ID", "")
                    ),
                )
                existing = data.get(attach_key) or []
                if not isinstance(existing, list):
                    existing = [{"file_token": existing}] if existing else []
                existing.append({"file_token": file_token})
                data[attach_key] = existing
            except FeishuAPIError as exc:
                return f"附件上传失败：{exc.message}", state, {
                    "data": data,
                    **{k: v for k, v in payload.items() if k != "data"},
                }

            if not text:
                data, problems = resolve_select_fields(fields_cfg, data)
                payload["data"] = data
                if problems:
                    return "\n".join(problems), STATE_COLLECTING, payload
                return self._after_merge(data, payload, problems=[])

            data, _ = resolve_select_fields(fields_cfg, data)
            payload["data"] = data

        if state == STATE_AWAIT_OVERWRITE:
            if _word_in(text, self.config.get("overwrite_words") or []):
                return self._write(data, payload, overwrite=True)
            if _word_in(text, self.config.get("cancel_words") or []):
                return _prompt(self.config, "cancel_overwrite", "已取消。"), None, None
            name = str(data.get((self.config.get("dedupe") or {}).get("match_field_key") or "") or "")
            return (
                _prompt(self.config, "dedupe_ask", "记录已存在。回复「覆盖」或「取消」。", name=name),
                STATE_AWAIT_OVERWRITE,
                payload,
            )

        if state == STATE_CONFIRMING:
            if _word_in(text, self.config.get("confirm_words") or []):
                return self._write(data, payload, overwrite=False)
            if _word_in(text, self.config.get("skip_words") or []):
                return self._confirm_prompt(data, payload)
            extracted = extract_fields(text, self.config)
            data = merge_fields(data, extracted)
            data, problems = resolve_select_fields(fields_cfg, data)
            payload["data"] = data
            return self._after_merge(data, payload, problems)

        if (
            state == STATE_IDLE
            and not is_trigger(text, self.config)
            and not image_bytes
            and message_type != "image"
        ):
            help_text = _prompt(self.config, "start", "")
            example = _prompt(self.config, "help_example", "")
            idle = str(self.config.get("idle_help") or "")
            parts = [p for p in (idle, help_text, example) if p]
            return "\n\n".join(parts) or "请发送业务指令开始。", None, None

        if state == STATE_IDLE and not is_trigger(text, self.config) and image_bytes:
            return (
                _prompt(self.config, "image_before_start", "请先开始对应业务录入，再发送图片。"),
                None,
                {"data": data, "skill_id": self.config.get("skill_id")} if data.get(attach_key) else None,
            )

        if state == STATE_COLLECTING and _word_in(text, self.config.get("skip_words") or []):
            if missing_required(fields_cfg, data) or not payment_satisfied(self.config, data):
                return self._after_merge(data, payload, problems=[])
            payload["skipped_suggested"] = True
            return self._confirm_prompt(data, payload)

        extracted = extract_fields(text, self.config) if text else {}
        data = merge_fields(data, extracted)
        data, problems = resolve_select_fields(fields_cfg, data)
        payload["data"] = data
        return self._after_merge(data, payload, problems)

    def _after_merge(
        self,
        data: Dict[str, Any],
        payload: Dict[str, Any],
        problems: List[str],
    ) -> Tuple[str, str, Dict[str, Any]]:
        fields_cfg = self.config.get("fields") or {}
        attach_key = _attachment_key(self.config)
        parts: List[str] = []
        if problems:
            parts.extend(problems)

        req = missing_required(fields_cfg, data)
        pay = missing_payment_hint(self.config, data)
        if req or pay:
            filled = [
                f"{_field_label(self.config, k)}：{data[k]}"
                for k in data
                if data[k] not in (None, "", []) and k != attach_key
            ]
            if data.get(attach_key):
                filled.append(
                    _prompt(self.config, "attachment_filled", f"{_field_label(self.config, attach_key)}：[已附]")
                )
            if filled:
                parts.append("已识别：\n- " + "\n- ".join(filled))
            need = [_field_label(self.config, k) for k in req]
            if pay:
                need.append(pay)
            parts.append("还需要补充：\n- " + "\n- ".join(need))
            parts.append(_prompt(self.config, "need_more_hint", "可继续补充。回复「取消」结束。"))
            payload["data"] = data
            return "\n\n".join(parts), STATE_COLLECTING, payload

        sug = suggested_missing(fields_cfg, data)
        if sug and not payload.get("skipped_suggested"):
            labels = "、".join(_field_label(self.config, k) for k in sug)
            parts.append(_format_summary(self.config, data))
            parts.append(
                _prompt(
                    self.config,
                    "suggest_hint",
                    "建议补充：{labels}（可继续发送，或回复「跳过」进入确认）",
                    labels=labels,
                )
            )
            payload["data"] = data
            return "\n\n".join(parts), STATE_COLLECTING, payload

        return self._confirm_prompt(data, payload)

    def _confirm_prompt(
        self, data: Dict[str, Any], payload: Dict[str, Any]
    ) -> Tuple[str, str, Dict[str, Any]]:
        payload["data"] = data
        payload["skipped_suggested"] = True
        text = (
            _format_summary(self.config, data)
            + "\n\n"
            + _prompt(self.config, "confirm_hint", "请回复「确认」写入，或继续补充；「取消」放弃。")
        )
        return text, STATE_CONFIRMING, payload

    def _write(
        self, data: Dict[str, Any], payload: Dict[str, Any], *, overwrite: bool
    ) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
        fields_cfg = self.config.get("fields") or {}
        app_token = str(
            self.config.get("base_id")
            or __import__("os").getenv("FEISHU_BASE_ID", "")
        )
        table_id = str(self.config.get("table_id"))
        bitable_fields = build_bitable_fields(fields_cfg, data)

        dedupe = self.config.get("dedupe") or {}
        match_key = str(dedupe.get("match_field_key") or "")
        name = str(data.get(match_key) or "") if match_key else ""
        match_field_name = _field_label(self.config, match_key) if match_key else ""

        existing: List[Dict[str, Any]] = []
        if dedupe.get("enabled", True) and match_field_name and name:
            try:
                existing = self.feishu.search_records(
                    app_token=app_token,
                    table_id=table_id,
                    field_name=match_field_name,
                    value=name,
                )
            except FeishuAPIError as exc:
                return f"查重失败：{exc.message}", STATE_CONFIRMING, payload

            if existing and not overwrite:
                on_hit = str(dedupe.get("on_hit") or "ask_overwrite")
                if on_hit == "reject":
                    return f"已存在同名「{name}」，已拒绝写入。", None, None
                payload["data"] = data
                payload["existing_record_id"] = existing[0].get("record_id") or existing[0].get(
                    "id"
                )
                return (
                    _prompt(self.config, "dedupe_ask", "已存在同名记录。回复「覆盖」或「取消」。", name=name),
                    STATE_AWAIT_OVERWRITE,
                    payload,
                )

        try:
            if overwrite and payload.get("existing_record_id"):
                rec = self.feishu.update_record(
                    app_token=app_token,
                    table_id=table_id,
                    record_id=str(payload["existing_record_id"]),
                    fields=bitable_fields,
                )
                action = "已更新"
            elif existing and overwrite:
                rid = existing[0].get("record_id") or existing[0].get("id")
                rec = self.feishu.update_record(
                    app_token=app_token,
                    table_id=table_id,
                    record_id=str(rid),
                    fields=bitable_fields,
                )
                action = "已更新"
            else:
                rec = self.feishu.create_record(
                    app_token=app_token,
                    table_id=table_id,
                    fields=bitable_fields,
                )
                action = "已创建"
        except FeishuAPIError as exc:
            return f"写入失败：{exc.message}\n可修改后再次「确认」。", STATE_CONFIRMING, payload

        rid = rec.get("record_id") or rec.get("id") or ""
        return (
            _prompt(
                self.config,
                "success",
                "{action}「{name}」成功。记录 ID：{record_id}",
                action=action,
                name=name or self.config.get("skill_name") or "",
                record_id=rid,
            ),
            None,
            None,
        )
