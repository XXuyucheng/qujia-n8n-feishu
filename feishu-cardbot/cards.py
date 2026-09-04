"""飞书卡片 schema 2.0：欢迎 / 表单 / 确认 / 覆盖 / 成功 / 取消。"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


def _plain(text: str) -> Dict[str, str]:
    return {"tag": "plain_text", "content": str(text or "")}


def _md(text: str) -> Dict[str, str]:
    return {"tag": "markdown", "content": str(text or "")}


def _option(label: str, value: Optional[str] = None) -> Dict[str, Any]:
    val = value if value is not None else label
    return {"text": _plain(label), "value": str(val)}


def _header(title: str, template: str = "blue") -> Dict[str, Any]:
    return {"title": _plain(title), "template": template}


def _callback_button(
    *,
    name: str,
    text: str,
    action: str,
    button_type: str = "default",
    extra: Optional[Dict[str, Any]] = None,
    form_action_type: Optional[str] = None,
) -> Dict[str, Any]:
    value = {"action": action}
    if extra:
        value.update(extra)
    btn: Dict[str, Any] = {
        "tag": "button",
        "name": name,
        "text": _plain(text),
        "type": button_type,
        "behaviors": [{"type": "callback", "value": value}],
    }
    if form_action_type:
        btn["form_action_type"] = form_action_type
    return btn


def wrap_card(
    *,
    header_title: str,
    elements: List[Dict[str, Any]],
    template: str = "blue",
) -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": _header(header_title, template),
        "body": {"elements": elements},
    }


def callback_payload(
    *,
    card: Dict[str, Any],
    toast_text: str = "",
    toast_type: str = "info",
) -> Dict[str, Any]:
    """官方 card.action.trigger 回包：toast + raw 卡片。"""
    out: Dict[str, Any] = {"card": {"type": "raw", "data": card}}
    if toast_text:
        out["toast"] = {
            "type": toast_type,
            "content": toast_text,
            "i18n": {"zh_cn": toast_text},
        }
    return out


def field_label(fields_cfg: Dict[str, Any], key: str) -> str:
    return str((fields_cfg.get(key) or {}).get("name") or key)


def format_value(value: Any) -> str:
    if value is None or value == "" or value == []:
        return ""
    if isinstance(value, list):
        if value and isinstance(value[0], dict) and value[0].get("file_token"):
            return "[已附图片]"
        return "、".join(str(x) for x in value)
    return str(value)


def _input_element(key: str, cfg: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    el: Dict[str, Any] = {
        "tag": "input",
        "name": key,
        "placeholder": _plain(f"请输入{cfg.get('name') or key}"),
        "label": _plain(str(cfg.get("name") or key)),
        "label_position": "top",
        "width": "fill",
        "required": bool(cfg.get("required")),
    }
    current = data.get(key)
    if current not in (None, "", []):
        el["default_value"] = str(current)
    return el


def _select_element(key: str, cfg: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    options = [_option(str(opt)) for opt in (cfg.get("options") or [])]
    ftype = str(cfg.get("type") or "")
    label = str(cfg.get("name") or key)
    if ftype == "multi_select":
        el: Dict[str, Any] = {
            "tag": "multi_select_static",
            "name": key,
            "placeholder": _plain(f"请选择{label}"),
            "label": _plain(label),
            "label_position": "top",
            "width": "fill",
            "options": options,
        }
        current = data.get(key)
        if isinstance(current, list) and current:
            el["selected_values"] = [str(x) for x in current]
        elif current not in (None, ""):
            el["selected_values"] = [str(current)]
        return el
    el = {
        "tag": "select_static",
        "name": key,
        "placeholder": _plain(f"请选择{label}"),
        "label": _plain(label),
        "label_position": "top",
        "width": "fill",
        "options": options,
    }
    current = data.get(key)
    if isinstance(current, list) and current:
        el["initial_option"] = str(current[0])
    elif current not in (None, ""):
        el["initial_option"] = str(current)
    return el


def form_field_keys(fields_cfg: Dict[str, Any]) -> List[str]:
    """表单展示顺序：跳过附件字段。"""
    keys: List[str] = []
    for key, cfg in (fields_cfg or {}).items():
        if (cfg or {}).get("type") == "attachment":
            continue
        keys.append(key)
    return keys


def build_welcome(*, open_id: str = "") -> Dict[str, Any]:
    mention = f"<at id={open_id}></at>" if open_id else "你好"
    return wrap_card(
        header_title="供应商录入助手",
        template="blue",
        elements=[
            _md(f"{mention}，欢迎使用供应商录入。在卡片上填写信息后提交，确认无误再写入多维表。"),
            _md("收款码请在打开表单后 **发送图片消息**（卡片无法上传文件）。"),
            {
                "tag": "column_set",
                "flex_mode": "stretch",
                "columns": [
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            _callback_button(
                                name="open_form",
                                text="录入供应商",
                                action="open_form",
                                button_type="primary",
                            )
                        ],
                    }
                ],
            },
        ],
    )


def build_supplier_form(
    runtime: Dict[str, Any],
    *,
    data: Optional[Dict[str, Any]] = None,
    problems: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    fields_cfg = runtime.get("fields") or {}
    data = data or {}
    attach_key = str(runtime.get("attachment_field") or "qrcode")
    notes: List[str] = []
    if problems:
        notes.extend(str(p) for p in problems if p)
    if data.get(attach_key):
        notes.append(f"{field_label(fields_cfg, attach_key)}：已附图片")
    else:
        notes.append("支付信息：户名+账号+银行，或发送收款码图片。")

    form_elements: List[Dict[str, Any]] = []
    for key in form_field_keys(fields_cfg):
        cfg = fields_cfg.get(key) or {}
        ftype = str(cfg.get("type") or "text")
        if ftype in {"single_select", "multi_select"}:
            form_elements.append(_select_element(key, cfg, data))
        else:
            form_elements.append(_input_element(key, cfg, data))
    form_elements.append(
        _callback_button(
            name="submit_form",
            text="提交",
            action="submit_form",
            button_type="primary",
            form_action_type="submit",
        )
    )
    form_elements.append(
        _callback_button(
            name="cancel",
            text="取消",
            action="cancel",
            button_type="default",
        )
    )

    elements: List[Dict[str, Any]] = []
    if notes:
        elements.append(_md("\n\n".join(f"- {n}" for n in notes)))
    elements.append({"tag": "form", "name": "supplier_form", "elements": form_elements})
    return wrap_card(header_title="供应商录入", template="blue", elements=elements)


def build_confirm(runtime: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    fields_cfg = runtime.get("fields") or {}
    lines = ["**请确认以下信息，确认后写入多维表：**"]
    attach_key = str(runtime.get("attachment_field") or "qrcode")
    for key, cfg in fields_cfg.items():
        if key not in data or data[key] in (None, "", []):
            continue
        val = format_value(data[key]) if key != attach_key else "[已附图片]"
        lines.append(f"- {cfg.get('name', key)}：{val}")
    return wrap_card(
        header_title="确认写入",
        template="turquoise",
        elements=[
            _md("\n".join(lines)),
            {
                "tag": "column_set",
                "flex_mode": "stretch",
                "columns": [
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            _callback_button(
                                name="confirm_write",
                                text="确认写入",
                                action="confirm_write",
                                button_type="primary",
                            )
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            _callback_button(
                                name="back_to_form",
                                text="返回修改",
                                action="back_to_form",
                            )
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            _callback_button(
                                name="cancel",
                                text="取消",
                                action="cancel",
                                button_type="danger",
                            )
                        ],
                    },
                ],
            },
        ],
    )


def build_overwrite(runtime: Dict[str, Any], *, detail: str) -> Dict[str, Any]:
    return wrap_card(
        header_title="检测到重复记录",
        template="orange",
        elements=[
            _md(detail or "记录已存在。"),
            _md("你有覆盖权限：确认覆盖将更新已有记录。"),
            {
                "tag": "column_set",
                "flex_mode": "stretch",
                "columns": [
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            _callback_button(
                                name="overwrite",
                                text="覆盖",
                                action="overwrite",
                                button_type="primary",
                            )
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            _callback_button(
                                name="cancel",
                                text="取消",
                                action="cancel",
                                button_type="danger",
                            )
                        ],
                    },
                ],
            },
        ],
    )


def build_denied(*, detail: str) -> Dict[str, Any]:
    return wrap_card(
        header_title="无法覆盖",
        template="red",
        elements=[
            _md(detail or "记录已存在。"),
            _md("你没有覆盖权限，请修改后重新提交，或联系管理员。"),
            _callback_button(
                name="open_form",
                text="重新录入",
                action="open_form",
                button_type="primary",
            ),
        ],
    )


def build_success(
    *,
    action: str,
    name: str,
    record_id: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    suffix = "（dry_run，未写表）" if dry_run else ""
    return wrap_card(
        header_title="录入完成",
        template="green",
        elements=[
            _md(f"{action}供应商「{name}」成功{suffix}。\n记录 ID：`{record_id}`"),
            _callback_button(
                name="open_form",
                text="再录一条",
                action="open_form",
                button_type="primary",
            ),
        ],
    )


def build_cancelled() -> Dict[str, Any]:
    return wrap_card(
        header_title="已取消",
        template="grey",
        elements=[
            _md("已取消本次供应商录入。"),
            _callback_button(
                name="open_form",
                text="重新录入",
                action="open_form",
                button_type="primary",
            ),
        ],
    )


def collect_select_options(card: Dict[str, Any]) -> Dict[str, List[str]]:
    """测试辅助：抽出表单里各 select 的 option value。"""
    out: Dict[str, List[str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        name = str(node.get("name") or "")
        if node.get("tag") in {"select_static", "multi_select_static"} and name:
            out[name] = [str(o.get("value")) for o in (node.get("options") or [])]
        for val in node.values():
            walk(val)

    walk(card)
    return out
