"""Form Engine：YAML 驱动的多轮对话状态机。

状态：idle → collecting → confirming →（查重）await_overwrite → 写表结束。
查重：对 dedupe.fields 中每个非空字段各调一次 bitable search（OR 语义汇总），
命中后按 open_id 白名单决定拒绝或询问覆盖。供应商 skill 默认查「供应商名称」与「账号」（不含户名）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from extractor import extract_fields
from feishu_client import FeishuAPIError, FeishuClient
from rules import apply_rules
from validator import (
    apply_field_patterns,
    build_bitable_fields,
    can_overwrite,
    merge_fields,
    missing_payment_hint,
    missing_required,
    payment_satisfied,
    reconcile_qrcode_payment,
    resolve_select_fields,
    suggested_missing,
)

logger = logging.getLogger("feishu-bot.dialog")

# 会话状态常量
STATE_IDLE = "idle"
STATE_COLLECTING = "collecting"
STATE_CONFIRMING = "confirming"
STATE_AWAIT_OVERWRITE = "await_overwrite"


def _prompt(config: Dict[str, Any], key: str, default: str = "", **kwargs: Any) -> str:
    """读取 skill prompts 并做 {占位符} 格式化。"""
    prompts = config.get("prompts") or {}
    template = str(prompts.get(key) or default)
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError):
            return template
    return template


def _field_label(config: Dict[str, Any], key: str) -> str:
    """逻辑键 → 飞书字段中文名。"""
    return ((config.get("fields") or {}).get(key) or {}).get("name") or key


def _attachment_key(config: Dict[str, Any]) -> str:
    return str(config.get("attachment_field") or "qrcode")


def _format_summary(config: Dict[str, Any], data: Dict[str, Any]) -> str:
    """确认前展示的字段汇总。"""
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
    """用户消息是否包含某组口令词（确认/取消/覆盖等）。"""
    t = (text or "").strip().lower()
    for w in words:
        if w.lower() in t:
            return True
    return False


def is_trigger(text: str, config: Dict[str, Any]) -> bool:
    """当前 skill 触发词是否出现在文本中。"""
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
    """群聊是否处理：有进行中会话，或消息 @ 了机器人。"""
    if chat_type != "group":
        return True
    if has_session:
        return True
    if not mentions:
        return False
    if not bot_open_ids:
        # 未配置 bot open_id 时：有任意 @ 即处理
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


def _normalize_data(
    config: Dict[str, Any],
    data: Dict[str, Any],
    text: str,
    sticky_rules: List[str],
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """抽取后统一校验：空占位 → pattern → 选项 → 业务 rules。

    若已有收款码且银行转账未齐，则银行字段可空，并丢掉其校验错误。
    """
    from validator import strip_blank_fields

    fields_cfg = config.get("fields") or {}
    data = strip_blank_fields(data)
    problems: List[str] = []
    data, pattern_probs = apply_field_patterns(fields_cfg, data, config)
    problems.extend(pattern_probs)
    data, select_probs = resolve_select_fields(fields_cfg, data, config)
    problems.extend(select_probs)
    data, rule_hints, active_rules = apply_rules(
        text, data, config, sticky_rule_ids=sticky_rules
    )
    data = strip_blank_fields(data)
    # rules 可能 set_fields 写入单选值，再跑一遍选项归一化
    data, select_probs2 = resolve_select_fields(fields_cfg, data, config)
    problems.extend(select_probs2)
    problems.extend(rule_hints)
    # 二维码收款优先：不因半截/非法银行字段卡住
    data, problems = reconcile_qrcode_payment(config, data, problems)
    return data, problems, active_rules


def _record_supplier_name(record: Dict[str, Any]) -> str:
    """从查重返回的 record 中尽量取出「供应商」展示名。"""
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


class DialogEngine:
    """单 skill 的多轮对话引擎。"""

    def __init__(
        self,
        config: Dict[str, Any],
        feishu: FeishuClient,
        *,
        open_id: str = "",
    ):
        self.config = config
        self.feishu = feishu
        self.open_id = open_id or ""  # 用于覆盖权限

    def handle(
        self,
        *,
        session: Optional[Dict[str, Any]],
        text: str,
        message_type: str,
        image_bytes: Optional[bytes] = None,
        image_name: str = "qrcode.png",
    ) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
        """处理一轮用户输入。

        返回：(回复文案, 新状态或 None 表示结束会话, 新 payload)
        """
        text = (text or "").strip()
        fields_cfg = self.config.get("fields") or {}
        attach_key = _attachment_key(self.config)

        if session and _word_in(text, self.config.get("cancel_words") or []):
            return _prompt(self.config, "cancel", "已取消。"), None, None

        state = (session or {}).get("state") or STATE_IDLE
        payload = _ensure_skill_payload(
            dict((session or {}).get("payload") or {}), self.config
        )
        data = dict(payload.get("data") or {})
        sticky_rules = list(payload.get("active_rules") or [])

        # ---------- 图片：上传为多维表附件 token ----------
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
                data, problems, sticky_rules = _normalize_data(
                    self.config, data, text, sticky_rules
                )
                payload["data"] = data
                payload["active_rules"] = sticky_rules
                if problems:
                    return "\n".join(problems), STATE_COLLECTING, payload
                return self._after_merge(data, payload, problems=[])

            data, _, sticky_rules = _normalize_data(
                self.config, data, text, sticky_rules
            )
            payload["data"] = data
            payload["active_rules"] = sticky_rules

        # ---------- 等待覆盖确认 ----------
        if state == STATE_AWAIT_OVERWRITE:
            if not can_overwrite(self.config, self.open_id):
                return (
                    _prompt(
                        self.config,
                        "dedupe_denied",
                        "无覆盖权限，请联系管理员。\n{detail}",
                        detail=payload.get("dedupe_detail") or "",
                    ),
                    None,
                    None,
                )
            if _word_in(text, self.config.get("overwrite_words") or []):
                return self._write(data, payload, overwrite=True)
            if _word_in(text, self.config.get("cancel_words") or []):
                return _prompt(self.config, "cancel_overwrite", "已取消。"), None, None
            return (
                _prompt(
                    self.config,
                    "dedupe_ask",
                    "{detail}\n回复「覆盖」或「取消」。",
                    detail=payload.get("dedupe_detail") or "记录已存在。",
                ),
                STATE_AWAIT_OVERWRITE,
                payload,
            )

        # ---------- 确认阶段 ----------
        if state == STATE_CONFIRMING:
            if _word_in(text, self.config.get("confirm_words") or []):
                return self._write(data, payload, overwrite=False)
            if _word_in(text, self.config.get("skip_words") or []):
                return self._confirm_prompt(data, payload)
            # 确认阶段仍可继续改字段
            extracted = extract_fields(text, self.config)
            data = merge_fields(data, extracted)
            data, problems, sticky_rules = _normalize_data(
                self.config, data, text, sticky_rules
            )
            payload["data"] = data
            payload["active_rules"] = sticky_rules
            return self._after_merge(data, payload, problems)

        # ---------- idle：未触发则给帮助 ----------
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
                _prompt(
                    self.config,
                    "image_before_start",
                    "请先开始对应业务录入，再发送图片。",
                ),
                None,
                {"data": data, "skill_id": self.config.get("skill_id")}
                if data.get(attach_key)
                else None,
            )

        # collecting 下「跳过」：跳过建议项，直接进确认（必填仍须齐）
        if state == STATE_COLLECTING and _word_in(
            text, self.config.get("skip_words") or []
        ):
            data, problems, sticky_rules = _normalize_data(
                self.config, data, text, sticky_rules
            )
            payload["active_rules"] = sticky_rules
            if (
                problems
                or missing_required(fields_cfg, data)
                or not payment_satisfied(self.config, data)
            ):
                return self._after_merge(data, payload, problems=problems)
            payload["skipped_suggested"] = True
            return self._confirm_prompt(data, payload)

        # ---------- 默认：抽取 + 合并 + 校验 ----------
        extracted = extract_fields(text, self.config) if text else {}
        data = merge_fields(data, extracted)
        data, problems, sticky_rules = _normalize_data(
            self.config, data, text, sticky_rules
        )
        payload["data"] = data
        payload["active_rules"] = sticky_rules
        return self._after_merge(data, payload, problems)

    def _after_merge(
        self,
        data: Dict[str, Any],
        payload: Dict[str, Any],
        problems: List[str],
    ) -> Tuple[str, str, Dict[str, Any]]:
        """合并后：有问题/缺必填则继续收集；仅缺建议项可提示跳过；否则进确认。"""
        fields_cfg = self.config.get("fields") or {}
        attach_key = _attachment_key(self.config)
        parts: List[str] = []
        if problems:
            parts.extend(problems)

        req = missing_required(fields_cfg, data)
        pay = missing_payment_hint(self.config, data)
        if problems or req or pay:
            filled = [
                f"{_field_label(self.config, k)}：{data[k]}"
                for k in data
                if data[k] not in (None, "", []) and k != attach_key
            ]
            if data.get(attach_key):
                filled.append(
                    _prompt(
                        self.config,
                        "attachment_filled",
                        f"{_field_label(self.config, attach_key)}：[已附]",
                    )
                )
            if filled:
                parts.append("已识别：\n- " + "\n- ".join(filled))
            need = [_field_label(self.config, k) for k in req]
            if pay:
                need.append(pay)
            if need:
                parts.append("还需要补充：\n- " + "\n- ".join(need))
            parts.append(
                _prompt(
                    self.config, "need_more_hint", "可继续补充。回复「取消」结束。"
                )
            )
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
        """进入确认态，展示汇总。"""
        payload["data"] = data
        payload["skipped_suggested"] = True
        text = (
            _format_summary(self.config, data)
            + "\n\n"
            + _prompt(
                self.config,
                "confirm_hint",
                "请回复「确认」写入，或继续补充；「取消」放弃。",
            )
        )
        return text, STATE_CONFIRMING, payload

    def _dedupe_search(
        self, *, app_token: str, table_id: str, data: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], str]:
        """多字段查重（OR）：对每个非空 dedupe 字段各 search 一次，按 record_id 去重汇总。

        返回 (命中记录列表, 给人看的重复明细文案)。
        """
        dedupe = self.config.get("dedupe") or {}
        if not dedupe.get("enabled", True):
            return [], ""

        field_specs = dedupe.get("fields")
        if not field_specs:
            # 兼容旧配置：单一 match_field_key
            match_key = str(dedupe.get("match_field_key") or "")
            if match_key:
                field_specs = [{"key": match_key}]
            else:
                return [], ""

        hits: List[Dict[str, Any]] = []
        details: List[str] = []
        seen_ids: set[str] = set()

        for spec in field_specs:
            key = str((spec or {}).get("key") or "")
            if not key or not data.get(key):
                continue
            field_name = _field_label(self.config, key)
            value = str(data[key])
            try:
                found = self.feishu.search_records(
                    app_token=app_token,
                    table_id=table_id,
                    field_name=field_name,
                    value=value,
                )
            except FeishuAPIError as exc:
                raise FeishuAPIError(f"查重失败（{field_name}）：{exc.message}") from exc

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
    ) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
        """写表：先查重，再按权限 create 或 update。"""
        fields_cfg = self.config.get("fields") or {}
        app_token = str(
            self.config.get("base_id")
            or __import__("os").getenv("FEISHU_BASE_ID", "")
        )
        table_id = str(self.config.get("table_id"))
        bitable_fields = build_bitable_fields(fields_cfg, data)

        dedupe = self.config.get("dedupe") or {}
        name_key = "supplier_name"
        if dedupe.get("fields"):
            name_key = str((dedupe["fields"][0] or {}).get("key") or "supplier_name")
        elif dedupe.get("match_field_key"):
            name_key = str(dedupe["match_field_key"])
        name = str(data.get(name_key) or "")

        existing: List[Dict[str, Any]] = []
        detail = ""
        try:
            existing, detail = self._dedupe_search(
                app_token=app_token, table_id=table_id, data=data
            )
        except FeishuAPIError as exc:
            return f"{exc.message}", STATE_CONFIRMING, payload

        # 查重命中且本轮不是「确认覆盖」
        if existing and not overwrite:
            allow = can_overwrite(self.config, self.open_id)
            payload["data"] = data
            payload["existing_record_id"] = existing[0].get("record_id") or existing[
                0
            ].get("id")
            payload["dedupe_detail"] = detail
            if not allow:
                # 普通人：直接拒绝，结束会话
                return (
                    _prompt(
                        self.config,
                        "dedupe_denied",
                        "{detail}\n无覆盖权限，请联系管理员。",
                        detail=detail,
                    ),
                    None,
                    None,
                )
            on_hit = str(dedupe.get("on_hit") or "ask_overwrite")
            if on_hit == "reject":
                return (
                    _prompt(
                        self.config,
                        "dedupe_denied",
                        "{detail}\n已拒绝写入。",
                        detail=detail,
                    ),
                    None,
                    None,
                )
            # 白名单：询问是否覆盖
            return (
                _prompt(
                    self.config,
                    "dedupe_ask",
                    "{detail}\n回复「覆盖」或「取消」。",
                    detail=detail,
                    name=name,
                ),
                STATE_AWAIT_OVERWRITE,
                payload,
            )

        try:
            if overwrite and payload.get("existing_record_id"):
                if not can_overwrite(self.config, self.open_id):
                    return (
                        _prompt(
                            self.config,
                            "dedupe_denied",
                            "无覆盖权限，请联系管理员。\n{detail}",
                            detail=payload.get("dedupe_detail") or detail,
                        ),
                        None,
                        None,
                    )
                rec = self.feishu.update_record(
                    app_token=app_token,
                    table_id=table_id,
                    record_id=str(payload["existing_record_id"]),
                    fields=bitable_fields,
                )
                action = "已更新"
            elif existing and overwrite:
                if not can_overwrite(self.config, self.open_id):
                    return (
                        _prompt(
                            self.config,
                            "dedupe_denied",
                            "无覆盖权限，请联系管理员。\n{detail}",
                            detail=detail,
                        ),
                        None,
                        None,
                    )
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
            return (
                f"写入失败：{exc.message}\n可修改后再次「确认」。",
                STATE_CONFIRMING,
                payload,
            )

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
