"""飞书 JSON 2.0 互动卡片（代码生成，不用 CardKit 模板）。

listener 把本模块产出的 card JSON 包成 type=raw 回给飞书。
按钮 value.action 与 card_actions 分流约定一致。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

ACTION_OPEN_SUPPLIER = "open_supplier_form"
ACTION_OPEN_QUERY = "open_query_form"
ACTION_SUBMIT_SUPPLIER = "submit_supplier"
ACTION_CANCEL_SUPPLIER = "cancel_supplier"
ACTION_OVERWRITE = "overwrite_supplier"
ACTION_SUBMIT_QUERY = "submit_query"


def make_toast(kind: str, content: str) -> Dict[str, Any]:
    """飞书回调 toast。kind: info / success / error / warning。"""
    return {
        "type": kind,
        "content": content,
        "i18n": {"zh_cn": content},
    }


def callback_value(
    action: str,
    *,
    session_key: str = "",
    skill_id: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    value: Dict[str, Any] = {"action": action}
    if session_key:
        value["session_key"] = session_key
    if skill_id:
        value["skill_id"] = skill_id
    if extra:
        value.update(extra)
    return value


def _plain(text: str) -> Dict[str, Any]:
    return {"tag": "plain_text", "content": text}


def _markdown(content: str) -> Dict[str, Any]:
    return {"tag": "markdown", "content": content}


def _button(
    *,
    name: str,
    text: str,
    action: str,
    session_key: str = "",
    skill_id: str = "",
    btn_type: str = "default",
    form_action_type: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    btn: Dict[str, Any] = {
        "tag": "button",
        "name": name,
        "text": _plain(text),
        "type": btn_type,
        "width": "default",
        "behaviors": [
            {
                "type": "callback",
                "value": callback_value(
                    action, session_key=session_key, skill_id=skill_id, extra=extra
                ),
            }
        ],
    }
    if form_action_type:
        btn["form_action_type"] = form_action_type
    return btn


def _base_card(
    *,
    title: str,
    elements: List[Dict[str, Any]],
    template: str = "blue",
) -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
        },
        "header": {
            "title": _plain(title),
            "template": template,
        },
        "body": {"elements": elements},
    }


def build_welcome_card(*, session_key: str = "", open_id: str = "") -> Dict[str, Any]:
    """进入单聊 / idle：录入与查询入口。"""
    skey = session_key or open_id
    return _base_card(
        title="供应商助手",
        elements=[
            _markdown(
                "请选择要办理的业务。也可直接发送「添加供应商 …」或「查供应商 …」。\n"
                "收款码请在对话里发图片（卡片无法上传附件）。"
            ),
            _button(
                name="btn_open_supplier",
                text="录入供应商",
                action=ACTION_OPEN_SUPPLIER,
                session_key=skey,
                skill_id="supplier",
                btn_type="primary",
            ),
            _button(
                name="btn_open_query",
                text="查询资源",
                action=ACTION_OPEN_QUERY,
                session_key=skey,
                skill_id="query",
            ),
        ],
    )


def _option(text: str, *, selected: bool = False) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "text": _plain(text),
        "value": text,
    }
    if selected:
        item["selected"] = True
    return item


def _selected_set(value: Any) -> set:
    if value is None or value == "" or value == []:
        return set()
    if isinstance(value, list):
        return {str(v) for v in value if v not in (None, "")}
    return {str(value)}


def _field_element(key: str, cfg: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    """按 YAML 字段类型生成表单项；name = 逻辑键。"""
    label = str(cfg.get("name") or key)
    ftype = str(cfg.get("type") or "text")
    required = bool(cfg.get("required"))
    current = data.get(key)
    options = [str(x) for x in (cfg.get("options") or [])]

    if ftype == "single_select":
        selected = _selected_set(current)
        el: Dict[str, Any] = {
            "tag": "select_static",
            "name": key,
            "required": required,
            "placeholder": _plain(f"请选择{label}"),
            "label": _plain(label),
            "width": "fill",
            "options": [_option(opt, selected=opt in selected) for opt in options],
        }
        if selected:
            first = next(iter(selected))
            el["initial_option"] = first
        return el

    if ftype == "multi_select":
        selected = _selected_set(current)
        return {
            "tag": "multi_select_static",
            "name": key,
            "required": required,
            "placeholder": _plain(f"请选择{label}"),
            "label": _plain(label),
            "width": "fill",
            "options": [_option(opt, selected=opt in selected) for opt in options],
        }

    default = ""
    if current not in (None, "", []):
        if isinstance(current, list):
            default = "、".join(str(x) for x in current)
        else:
            default = str(current)
    return {
        "tag": "input",
        "name": key,
        "required": required,
        "placeholder": _plain(f"请输入{label}"),
        "label": _plain(label),
        "width": "fill",
        "default_value": default,
    }


def build_supplier_form_card(
    config: Dict[str, Any],
    data: Optional[Dict[str, Any]] = None,
    *,
    session_key: str,
    hint: str = "",
    skill_id: str = "supplier",
) -> Dict[str, Any]:
    """录入表单：字段来自 skill YAML；附件字段不进表单。"""
    data = dict(data or {})
    fields_cfg: Dict[str, Any] = config.get("fields") or {}
    attach_key = str(config.get("attachment_field") or "qrcode")
    qrcode_on = bool(data.get(attach_key))
    elements: List[Dict[str, Any]] = []

    intro = hint.strip() if hint else "请填写供应商信息后点「提交」。"
    if qrcode_on:
        intro += "\n已附收款码图片。"
    else:
        intro += "\n支付信息：户名+账号+银行，**或**在对话中发送收款码图片。"
    elements.append(_markdown(intro))

    form_items: List[Dict[str, Any]] = []
    for key, cfg in fields_cfg.items():
        if not isinstance(cfg, dict):
            continue
        if key == attach_key or str(cfg.get("type") or "") == "attachment":
            continue
        form_items.append(_field_element(key, cfg, data))

    form_items.append(
        _button(
            name="btn_submit_supplier",
            text="提交",
            action=ACTION_SUBMIT_SUPPLIER,
            session_key=session_key,
            skill_id=skill_id,
            btn_type="primary",
            form_action_type="submit",
        )
    )
    elements.append(
        {
            "tag": "form",
            "name": "supplier_form",
            "elements": form_items,
        }
    )
    elements.append(
        _button(
            name="btn_cancel_supplier",
            text="取消",
            action=ACTION_CANCEL_SUPPLIER,
            session_key=session_key,
            skill_id=skill_id,
        )
    )
    return _base_card(title="供应商录入", elements=elements, template="turquoise")


def strip_private(card: Dict[str, Any]) -> Dict[str, Any]:
    """去掉仅测试/内部使用的键，避免发给飞书。"""
    out = {k: v for k, v in card.items() if not str(k).startswith("_")}
    return out


def form_names(card: Dict[str, Any]) -> List[str]:
    """抽出表单内交互组件 name（跳过按钮）。"""
    names: List[str] = []
    for el in iter_elements(card):
        if not isinstance(el, dict):
            continue
        tag = str(el.get("tag") or "")
        name = str(el.get("name") or "")
        if tag in ("input", "select_static", "multi_select_static") and name:
            names.append(name)
    return names


def iter_elements(node: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(node, dict):
        if "tag" in node:
            yield node
        for val in node.values():
            yield from iter_elements(val)
    elif isinstance(node, list):
        for item in node:
            yield from iter_elements(item)


def find_submit_button(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for el in iter_elements(card):
        if el.get("tag") == "button" and el.get("form_action_type") == "submit":
            return el
    return None


def build_query_form_card(
    *,
    session_key: str,
    default_text: str = "",
    skill_id: str = "query",
) -> Dict[str, Any]:
    return _base_card(
        title="资源查询",
        template="wathet",
        elements=[
            _markdown("只读查询，不会改多维表。可查供应商或价差。"),
            {
                "tag": "form",
                "name": "query_form",
                "elements": [
                    {
                        "tag": "input",
                        "name": "query_text",
                        "required": True,
                        "placeholder": _plain("例如：查供应商 德清某某茶歇"),
                        "label": _plain("查询内容"),
                        "width": "fill",
                        "default_value": default_text,
                    },
                    _button(
                        name="btn_submit_query",
                        text="查询",
                        action=ACTION_SUBMIT_QUERY,
                        session_key=session_key,
                        skill_id=skill_id,
                        btn_type="primary",
                        form_action_type="submit",
                    ),
                ],
            },
        ],
    )


def build_processing_card(*, message: str = "处理中，请稍候…") -> Dict[str, Any]:
    return _base_card(
        title="处理中",
        template="grey",
        elements=[_markdown(message)],
    )


def build_overwrite_card(
    *,
    detail: str,
    session_key: str,
    skill_id: str = "supplier",
) -> Dict[str, Any]:
    return _base_card(
        title="发现重复记录",
        template="orange",
        elements=[
            _markdown(detail or "记录已存在。"),
            _button(
                name="btn_overwrite",
                text="覆盖已有记录",
                action=ACTION_OVERWRITE,
                session_key=session_key,
                skill_id=skill_id,
                btn_type="danger",
            ),
            _button(
                name="btn_cancel_overwrite",
                text="取消",
                action=ACTION_CANCEL_SUPPLIER,
                session_key=session_key,
                skill_id=skill_id,
            ),
        ],
    )


def build_result_card(
    *,
    title: str,
    body: str,
    session_key: str = "",
    ok: bool = True,
) -> Dict[str, Any]:
    template = "green" if ok else "red"
    elements: List[Dict[str, Any]] = [_markdown(body)]
    if session_key:
        elements.extend(
            [
                _button(
                    name="btn_again_supplier",
                    text="再录一条",
                    action=ACTION_OPEN_SUPPLIER,
                    session_key=session_key,
                    skill_id="supplier",
                    btn_type="primary",
                ),
                _button(
                    name="btn_again_query",
                    text="再查一次",
                    action=ACTION_OPEN_QUERY,
                    session_key=session_key,
                    skill_id="query",
                ),
            ]
        )
    return _base_card(title=title, template=template, elements=elements)


def card_for_send(card: Dict[str, Any]) -> Dict[str, Any]:
    """发给飞书 / 回调的卡片（去掉内部键）。"""
    return strip_private(card)
