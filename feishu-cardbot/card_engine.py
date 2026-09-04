"""卡片表单状态机：读 form_value → 校验 → 回包换卡；确认后查重写表。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from cards import (
    build_cancelled,
    build_confirm,
    build_denied,
    build_overwrite,
    build_success,
    build_supplier_form,
    callback_payload,
    field_label,
)
from feishu_client import FeishuAPIError, FeishuClient
from rules import apply_rules
from validator import (
    apply_field_patterns,
    build_bitable_fields,
    can_overwrite,
    is_blank_value,
    merge_fields,
    missing_payment_hint,
    missing_required,
    payment_satisfied,
    reconcile_qrcode_payment,
    resolve_select_fields,
    strip_blank_fields,
)

logger = logging.getLogger("feishu-cardbot.engine")

STATE_COLLECTING = "collecting"
STATE_CONFIRMING = "confirming"
STATE_AWAIT_OVERWRITE = "await_overwrite"

ACTION_OPEN_FORM = "open_form"
ACTION_SUBMIT_FORM = "submit_form"
ACTION_CONFIRM_WRITE = "confirm_write"
ACTION_BACK_TO_FORM = "back_to_form"
ACTION_OVERWRITE = "overwrite"
ACTION_CANCEL = "cancel"


def _prompt(config: Dict[str, Any], key: str, default: str = "", **kwargs: Any) -> str:
    template = str((config.get("prompts") or {}).get(key) or default)
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError):
            return template
    return template


def form_value_to_data(
    form_value: Optional[Dict[str, Any]], fields_cfg: Dict[str, Any]
) -> Dict[str, Any]:
    """只取 YAML 中的逻辑字段；附件不来自表单。"""
    out: Dict[str, Any] = {}
    if not isinstance(form_value, dict):
        return out
    for key, cfg in (fields_cfg or {}).items():
        if (cfg or {}).get("type") == "attachment":
            continue
        if key not in form_value:
            continue
        val = form_value[key]
        if isinstance(val, dict) and "value" in val:
            val = val.get("value")
        if val is None:
            continue
        out[key] = val
    return out


def normalize_data(
    config: Dict[str, Any],
    data: Dict[str, Any],
    *,
    sticky_rules: Optional[List[str]] = None,
    text: str = "",
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    fields_cfg = config.get("fields") or {}
    data = strip_blank_fields(data)
    problems: List[str] = []
    data, pattern_probs = apply_field_patterns(fields_cfg, data, config)
    problems.extend(pattern_probs)
    data, select_probs = resolve_select_fields(fields_cfg, data, config)
    problems.extend(select_probs)
    blob = text or " ".join(
        str(v) for v in data.values() if v not in (None, "", [])
    )
    data, rule_hints, active_rules = apply_rules(
        blob, data, config, sticky_rule_ids=sticky_rules
    )
    data = strip_blank_fields(data)
    data, select_probs2 = resolve_select_fields(fields_cfg, data, config)
    problems.extend(select_probs2)
    problems.extend(rule_hints)
    data, problems = reconcile_qrcode_payment(config, data, problems)
    return data, problems, active_rules


def collect_problems(config: Dict[str, Any], data: Dict[str, Any], problems: List[str]) -> List[str]:
    fields_cfg = config.get("fields") or {}
    out = list(problems)
    req = missing_required(fields_cfg, data)
    if req:
        labels = "、".join(field_label(config.get("fields") or {}, k) for k in req)
        out.append(f"还需要补充：{labels}")
    pay = missing_payment_hint(config, data)
    if pay:
        out.append(pay)
    return out


def _record_supplier_name(record: Dict[str, Any]) -> str:
    fields = record.get("fields") or {}
    val = fields.get("供应商")
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(str(item))
        return "".join(parts) or str(val)
    if val is None:
        return ""
    return str(val)


class CardEngine:
    def __init__(
        self,
        config: Dict[str, Any],
        feishu: Optional[FeishuClient] = None,
        *,
        open_id: str = "",
        dry_run: bool = True,
    ):
        self.config = config
        self.feishu = feishu
        self.open_id = open_id or ""
        self.dry_run = dry_run

    def handle_action(
        self,
        *,
        action: str,
        session: Optional[Dict[str, Any]],
        form_value: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], Optional[str], Optional[Dict[str, Any]]]:
        """处理一次卡片回传。

        返回：(官方回包 dict, 新状态或 None 表示结束会话, 新 payload)
        """
        payload = dict((session or {}).get("payload") or {})
        data = dict(payload.get("data") or {})
        sticky = list(payload.get("active_rules") or [])
        action = str(action or "").strip()

        if action == ACTION_CANCEL:
            return (
                callback_payload(card=build_cancelled(), toast_text="已取消", toast_type="info"),
                None,
                None,
            )

        if action == ACTION_OPEN_FORM:
            payload = {"skill_id": self.config.get("skill_id"), "data": {}, "active_rules": []}
            return (
                callback_payload(
                    card=build_supplier_form(self.config, data={}),
                    toast_text="开始录入",
                ),
                STATE_COLLECTING,
                payload,
            )

        if action == ACTION_BACK_TO_FORM:
            payload["data"] = data
            payload["active_rules"] = sticky
            return (
                callback_payload(
                    card=build_supplier_form(self.config, data=data),
                    toast_text="返回修改",
                ),
                STATE_COLLECTING,
                payload,
            )

        if action == ACTION_SUBMIT_FORM:
            incoming = form_value_to_data(form_value, self.config.get("fields") or {})
            merged = merge_fields(data, incoming)
            attach_key = str(self.config.get("attachment_field") or "qrcode")
            if data.get(attach_key) and attach_key not in merged:
                merged[attach_key] = data[attach_key]
            merged, problems, sticky = normalize_data(
                self.config, merged, sticky_rules=sticky
            )
            payload["data"] = merged
            payload["active_rules"] = sticky
            payload["skill_id"] = self.config.get("skill_id")
            blocking = collect_problems(self.config, merged, problems)
            if blocking:
                return (
                    callback_payload(
                        card=build_supplier_form(
                            self.config, data=merged, problems=blocking
                        ),
                        toast_text=blocking[0],
                        toast_type="error",
                    ),
                    STATE_COLLECTING,
                    payload,
                )
            return (
                callback_payload(
                    card=build_confirm(self.config, merged),
                    toast_text="请确认后写入",
                ),
                STATE_CONFIRMING,
                payload,
            )

        if action == ACTION_CONFIRM_WRITE:
            return self._write(data, payload, overwrite=False)

        if action == ACTION_OVERWRITE:
            if not can_overwrite(self.config, self.open_id):
                detail = str(payload.get("dedupe_detail") or "记录已存在。")
                return (
                    callback_payload(
                        card=build_denied(detail=detail),
                        toast_text="无覆盖权限",
                        toast_type="error",
                    ),
                    None,
                    None,
                )
            return self._write(data, payload, overwrite=True)

        return (
            callback_payload(
                card=build_supplier_form(self.config, data=data),
                toast_text="未知操作",
                toast_type="error",
            ),
            (session or {}).get("state") or STATE_COLLECTING,
            payload,
        )

    def after_image(
        self, session: Optional[Dict[str, Any]], file_token: str
    ) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
        """IM 图片写入草稿后，返回应发送的下一张卡片。"""
        payload = dict((session or {}).get("payload") or {})
        data = dict(payload.get("data") or {})
        sticky = list(payload.get("active_rules") or [])
        attach_key = str(self.config.get("attachment_field") or "qrcode")
        existing = data.get(attach_key) or []
        if not isinstance(existing, list):
            existing = [{"file_token": existing}] if existing else []
        existing.append({"file_token": file_token})
        data[attach_key] = existing
        data, problems, sticky = normalize_data(
            self.config, data, sticky_rules=sticky
        )
        payload["data"] = data
        payload["active_rules"] = sticky
        payload["skill_id"] = self.config.get("skill_id")
        blocking = collect_problems(self.config, data, problems)
        if blocking:
            return (
                build_supplier_form(self.config, data=data, problems=blocking),
                STATE_COLLECTING,
                payload,
            )
        return build_confirm(self.config, data), STATE_CONFIRMING, payload

    def _dedupe_search(
        self, *, app_token: str, table_id: str, data: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], str]:
        dedupe = self.config.get("dedupe") or {}
        if not dedupe.get("enabled", True):
            return [], ""
        field_specs = dedupe.get("fields")
        if not field_specs:
            match_key = str(dedupe.get("match_field_key") or "")
            field_specs = [{"key": match_key}] if match_key else []
        if not field_specs or self.feishu is None:
            return [], ""

        hits: List[Dict[str, Any]] = []
        details: List[str] = []
        seen_ids: set[str] = set()
        for spec in field_specs:
            key = str((spec or {}).get("key") or "")
            if not key or not data.get(key) or is_blank_value(data.get(key)):
                continue
            field_name = field_label(self.config.get("fields") or {}, key)
            value = str(data[key])
            found = self.feishu.search_records(
                app_token=app_token,
                table_id=table_id,
                field_name=field_name,
                value=value,
            )
            for rec in found:
                rid = str(rec.get("record_id") or rec.get("id") or "")
                if rid and rid in seen_ids:
                    continue
                if rid:
                    seen_ids.add(rid)
                hits.append(rec)
                other_name = _record_supplier_name(rec) or rid or "已有记录"
                details.append(f"{field_name}「{value}」已存在于「{other_name}」")
        detail = "检测到重复：\n- " + "\n- ".join(details) if details else ""
        return hits, detail

    def _write(
        self, data: Dict[str, Any], payload: Dict[str, Any], *, overwrite: bool
    ) -> Tuple[Dict[str, Any], Optional[str], Optional[Dict[str, Any]]]:
        fields_cfg = self.config.get("fields") or {}
        app_token = str(self.config.get("base_id") or "")
        table_id = str(self.config.get("table_id") or "")
        bitable_fields = build_bitable_fields(fields_cfg, data)
        dedupe = self.config.get("dedupe") or {}
        name_key = "supplier_name"
        if dedupe.get("fields"):
            name_key = str((dedupe["fields"][0] or {}).get("key") or "supplier_name")
        elif dedupe.get("match_field_key"):
            name_key = str(dedupe["match_field_key"])
        name = str(data.get(name_key) or "")

        existing: List[Dict[str, Any]] = []
        detail = str(payload.get("dedupe_detail") or "")
        if not self.dry_run:
            try:
                existing, detail = self._dedupe_search(
                    app_token=app_token, table_id=table_id, data=data
                )
            except FeishuAPIError as exc:
                payload["data"] = data
                return (
                    callback_payload(
                        card=build_confirm(self.config, data),
                        toast_text=str(exc.message),
                        toast_type="error",
                    ),
                    STATE_CONFIRMING,
                    payload,
                )

        if existing and not overwrite:
            allow = can_overwrite(self.config, self.open_id)
            payload["data"] = data
            payload["existing_record_id"] = existing[0].get("record_id") or existing[0].get(
                "id"
            )
            payload["dedupe_detail"] = detail
            if not allow:
                return (
                    callback_payload(
                        card=build_denied(detail=detail),
                        toast_text="记录已存在",
                        toast_type="error",
                    ),
                    None,
                    None,
                )
            if str(dedupe.get("on_hit") or "ask_overwrite") == "reject":
                return (
                    callback_payload(
                        card=build_denied(detail=detail),
                        toast_text="已拒绝写入",
                        toast_type="error",
                    ),
                    None,
                    None,
                )
            return (
                callback_payload(
                    card=build_overwrite(self.config, detail=detail),
                    toast_text="检测到重复",
                    toast_type="info",
                ),
                STATE_AWAIT_OVERWRITE,
                payload,
            )

        record_id = "dry_run"
        action_label = "将创建" if not overwrite else "将更新"
        if self.dry_run:
            return (
                callback_payload(
                    card=build_success(
                        action=action_label,
                        name=name,
                        record_id=record_id,
                        dry_run=True,
                    ),
                    toast_text="dry_run 未写表",
                ),
                None,
                None,
            )

        if self.feishu is None:
            payload["data"] = data
            return (
                callback_payload(
                    card=build_confirm(self.config, data),
                    toast_text="未配置飞书客户端",
                    toast_type="error",
                ),
                STATE_CONFIRMING,
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
                action_label = "已更新"
            elif existing and overwrite:
                rid = existing[0].get("record_id") or existing[0].get("id")
                rec = self.feishu.update_record(
                    app_token=app_token,
                    table_id=table_id,
                    record_id=str(rid),
                    fields=bitable_fields,
                )
                action_label = "已更新"
            else:
                rec = self.feishu.create_record(
                    app_token=app_token,
                    table_id=table_id,
                    fields=bitable_fields,
                )
                action_label = "已创建"
        except FeishuAPIError as exc:
            payload["data"] = data
            return (
                callback_payload(
                    card=build_confirm(self.config, data),
                    toast_text=f"写入失败：{exc.message}",
                    toast_type="error",
                ),
                STATE_CONFIRMING,
                payload,
            )

        rid = rec.get("record_id") or rec.get("id") or ""
        return (
            callback_payload(
                card=build_success(action=action_label, name=name, record_id=str(rid)),
                toast_text="写入成功",
                toast_type="info",
            ),
            None,
            None,
        )
