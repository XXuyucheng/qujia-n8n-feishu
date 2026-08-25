"""卡片回传与生命周期：3s 内只做本地校验/组卡；写表与 n8n 查询异步。"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from cards import (
    ACTION_CANCEL_SUPPLIER,
    ACTION_OPEN_QUERY,
    ACTION_OPEN_SUPPLIER,
    ACTION_OVERWRITE,
    ACTION_SUBMIT_QUERY,
    ACTION_SUBMIT_SUPPLIER,
    build_overwrite_card,
    build_processing_card,
    build_query_form_card,
    build_result_card,
    build_supplier_form_card,
    build_welcome_card,
    card_for_send,
    make_toast,
)
from config_loader import skill_to_runtime
from dialog import (
    STATE_AWAIT_OVERWRITE,
    STATE_COLLECTING,
    STATE_CONFIRMING,
    DialogEngine,
    form_value_to_data,
    validate_entry,
)
from feishu_client import FeishuAPIError, FeishuClient
from query_proxy import call_query_agent
from session_store import SessionStore
from validator import missing_payment_hint, missing_required, payment_satisfied

logger = logging.getLogger("feishu-bot.card-actions")

LoadFn = Callable[[], tuple]
MakeFeishu = Callable[[], FeishuClient]
SpawnFn = Callable[[Callable[[], None]], None]


def _default_spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


def _as_mapping(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    return {}


def _parse_value(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


@dataclass
class CardEvent:
    action: str = ""
    value: Dict[str, Any] = field(default_factory=dict)
    form_value: Dict[str, Any] = field(default_factory=dict)
    open_id: str = ""
    chat_id: str = ""
    message_id: str = ""
    session_key: str = ""
    skill_id: str = ""


def parse_card_event(body: Dict[str, Any]) -> CardEvent:
    """兼容 listener as_dict 后的 card.action.trigger 信封。"""
    event = _as_mapping(body.get("event") if isinstance(body.get("event"), dict) else body)
    if not event.get("action") and isinstance(body.get("action"), dict):
        event = body
    operator = _as_mapping(event.get("operator"))
    context = _as_mapping(event.get("context"))
    action_obj = _as_mapping(event.get("action"))
    value = _parse_value(action_obj.get("value"))
    form_value = _as_mapping(action_obj.get("form_value"))
    open_id = str(operator.get("open_id") or "")
    chat_id = str(context.get("open_chat_id") or "")
    message_id = str(context.get("open_message_id") or "")
    session_key = str(value.get("session_key") or open_id or "")
    skill_id = str(value.get("skill_id") or "")
    return CardEvent(
        action=str(value.get("action") or ""),
        value=value,
        form_value=form_value,
        open_id=open_id,
        chat_id=chat_id,
        message_id=message_id,
        session_key=session_key,
        skill_id=skill_id,
    )


@dataclass
class LifecycleEvent:
    kind: str = ""  # p2p_entered | bot_menu
    open_id: str = ""
    chat_id: str = ""
    event_key: str = ""


def parse_lifecycle(body: Dict[str, Any]) -> LifecycleEvent:
    header = _as_mapping(body.get("header"))
    event = _as_mapping(body.get("event"))
    et = str(header.get("event_type") or body.get("event_type") or "")
    operator_id = _as_mapping(event.get("operator_id"))
    operator = _as_mapping(event.get("operator"))
    op_id = _as_mapping(operator.get("operator_id"))
    open_id = str(
        operator_id.get("open_id")
        or op_id.get("open_id")
        or operator.get("open_id")
        or ""
    )
    chat_id = str(event.get("chat_id") or "")
    event_key = str(event.get("event_key") or "")
    kind = ""
    if "bot_p2p_chat_entered" in et:
        kind = "p2p_entered"
    elif "bot.menu" in et or "bot_menu" in et:
        kind = "bot_menu"
    return LifecycleEvent(
        kind=kind, open_id=open_id, chat_id=chat_id, event_key=event_key
    )


def _entry_in_progress(session: Optional[Dict[str, Any]]) -> bool:
    if not session:
        return False
    state = session.get("state")
    skill = (session.get("payload") or {}).get("skill_id")
    return bool(state and state != "idle" and skill == "supplier")


def _hint_for(config: Dict[str, Any], data: Dict[str, Any], problems: List[str]) -> str:
    parts = list(problems)
    fields_cfg = config.get("fields") or {}
    req = missing_required(fields_cfg, data)
    pay = missing_payment_hint(config, data)
    labels = []
    for key in req:
        labels.append(((fields_cfg.get(key) or {}).get("name")) or key)
    if pay:
        labels.append(pay)
    if labels:
        parts.append("还需要补充：" + "、".join(str(x) for x in labels))
    return "\n".join(parts)


def _ok(toast: Optional[Dict[str, Any]] = None, card: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if toast:
        out["toast"] = toast
    if card:
        out["card"] = card_for_send(card)
    return out


class CardActionHandler:
    """同步返回 toast+card；写表/查询丢进 spawn。"""

    def __init__(
        self,
        store: SessionStore,
        load_fn: LoadFn,
        make_feishu: MakeFeishu,
        spawn: Optional[SpawnFn] = None,
    ):
        self.store = store
        self.load_fn = load_fn
        self.make_feishu = make_feishu
        self.spawn = spawn or _default_spawn

    def handle(self, body: Dict[str, Any]) -> Dict[str, Any]:
        ev = parse_card_event(body)
        if not ev.action:
            return _ok(make_toast("warning", "未识别的卡片操作"))
        if ev.action == ACTION_OPEN_SUPPLIER:
            return self._open_supplier(ev)
        if ev.action == ACTION_OPEN_QUERY:
            return self._open_query(ev)
        if ev.action == ACTION_SUBMIT_SUPPLIER:
            return self._submit_supplier(ev)
        if ev.action == ACTION_CANCEL_SUPPLIER:
            return self._cancel(ev)
        if ev.action == ACTION_OVERWRITE:
            return self._overwrite(ev)
        if ev.action == ACTION_SUBMIT_QUERY:
            return self._submit_query(ev)
        return _ok(make_toast("warning", f"未知操作：{ev.action}"))

    def _runtime_supplier(self) -> Dict[str, Any]:
        app_cfg, skills = self.load_fn()
        return skill_to_runtime(skills["supplier"], app_cfg)

    def _skill_query(self) -> Dict[str, Any]:
        _, skills = self.load_fn()
        return skills["query"]

    def _open_supplier(self, ev: CardEvent) -> Dict[str, Any]:
        session = self.store.get(ev.session_key)
        runtime = self._runtime_supplier()
        data: Dict[str, Any] = {}
        if _entry_in_progress(session):
            data = dict((session or {}).get("payload", {}).get("data") or {})
        payload = {
            "skill_id": "supplier",
            "data": data,
            "card_message_id": ev.message_id,
        }
        self.store.save(ev.session_key, STATE_COLLECTING, payload)
        return _ok(
            make_toast("info", "请填写供应商信息"),
            build_supplier_form_card(runtime, data, session_key=ev.session_key),
        )

    def _open_query(self, ev: CardEvent) -> Dict[str, Any]:
        session = self.store.get(ev.session_key)
        if _entry_in_progress(session):
            return _ok(
                make_toast("warning", "请先取消当前供应商录入，再查询。"),
            )
        return _ok(
            make_toast("info", "请输入查询内容"),
            build_query_form_card(session_key=ev.session_key),
        )

    def _cancel(self, ev: CardEvent) -> Dict[str, Any]:
        self.store.clear(ev.session_key)
        return _ok(
            make_toast("info", "已取消"),
            build_welcome_card(session_key=ev.session_key, open_id=ev.open_id),
        )

    def _submit_supplier(self, ev: CardEvent) -> Dict[str, Any]:
        runtime = self._runtime_supplier()
        session = self.store.get(ev.session_key)
        payload = dict((session or {}).get("payload") or {})
        data = form_value_to_data(
            runtime, dict(payload.get("data") or {}), ev.form_value
        )
        text_blob = " ".join(
            str(v) for v in ev.form_value.values() if isinstance(v, str)
        )
        data, problems, sticky = validate_entry(
            runtime,
            data,
            text=text_blob,
            sticky_rules=list(payload.get("active_rules") or []),
        )
        payload["data"] = data
        payload["active_rules"] = sticky
        payload["skill_id"] = "supplier"
        payload["card_message_id"] = ev.message_id or payload.get("card_message_id")
        payload["skipped_suggested"] = True

        fields_cfg = runtime.get("fields") or {}
        if (
            problems
            or missing_required(fields_cfg, data)
            or not payment_satisfied(runtime, data)
        ):
            hint = _hint_for(runtime, data, problems) or "请补全必填项后再提交。"
            self.store.save(ev.session_key, STATE_COLLECTING, payload)
            return _ok(
                make_toast("error", hint.split("\n")[0][:100]),
                build_supplier_form_card(
                    runtime, data, session_key=ev.session_key, hint=hint
                ),
            )

        self.store.save(ev.session_key, STATE_CONFIRMING, payload)
        self._spawn_write(
            session_key=ev.session_key,
            open_id=ev.open_id,
            chat_id=ev.chat_id,
            message_id=str(payload.get("card_message_id") or ev.message_id),
            data=data,
            payload=payload,
            overwrite=False,
        )
        return _ok(
            make_toast("info", "正在查重并写入，请稍候…"),
            build_processing_card(message="正在查重并写入多维表，请稍候…"),
        )

    def _overwrite(self, ev: CardEvent) -> Dict[str, Any]:
        session = self.store.get(ev.session_key)
        if not session:
            return _ok(make_toast("error", "会话已过期，请重新录入。"))
        payload = dict(session.get("payload") or {})
        data = dict(payload.get("data") or {})
        self._spawn_write(
            session_key=ev.session_key,
            open_id=ev.open_id,
            chat_id=ev.chat_id,
            message_id=str(payload.get("card_message_id") or ev.message_id),
            data=data,
            payload=payload,
            overwrite=True,
        )
        return _ok(
            make_toast("info", "正在覆盖写入…"),
            build_processing_card(message="正在覆盖已有记录，请稍候…"),
        )

    def _submit_query(self, ev: CardEvent) -> Dict[str, Any]:
        session = self.store.get(ev.session_key)
        if _entry_in_progress(session):
            return _ok(make_toast("warning", "请先取消当前供应商录入，再查询。"))
        text = str(ev.form_value.get("query_text") or ev.value.get("query_text") or "").strip()
        if not text:
            return _ok(
                make_toast("error", "请填写查询内容"),
                build_query_form_card(session_key=ev.session_key),
            )
        resource = {
            "open_id": ev.open_id,
            "chat_id": ev.chat_id,
            "message_id": ev.message_id,
            "chat_type": "p2p",
        }
        self._spawn_query(
            session_key=ev.session_key,
            chat_id=ev.chat_id,
            open_id=ev.open_id,
            message_id=ev.message_id,
            text=text,
            resource=resource,
        )
        return _ok(
            make_toast("info", "正在查询…"),
            build_processing_card(message="正在查询，请稍候…"),
        )

    def _deliver_card(
        self,
        feishu: FeishuClient,
        *,
        message_id: str,
        chat_id: str,
        open_id: str,
        card: Dict[str, Any],
    ) -> None:
        try:
            if message_id:
                feishu.patch_interactive(message_id, card)
                return
        except FeishuAPIError as exc:
            logger.warning("patch card failed: %s", exc.message)
        try:
            if open_id:
                feishu.send_interactive(open_id, card, receive_id_type="open_id")
            elif chat_id:
                feishu.send_interactive(chat_id, card)
        except FeishuAPIError as exc:
            logger.error("send result card failed: %s", exc.message)

    def _spawn_write(
        self,
        *,
        session_key: str,
        open_id: str,
        chat_id: str,
        message_id: str,
        data: Dict[str, Any],
        payload: Dict[str, Any],
        overwrite: bool,
    ) -> None:
        def job() -> None:
            feishu = self.make_feishu()
            try:
                runtime = self._runtime_supplier()
                engine = DialogEngine(runtime, feishu, open_id=open_id)
                reply, new_state, new_payload = engine._write(
                    data, dict(payload), overwrite=overwrite
                )
                if new_state == STATE_AWAIT_OVERWRITE:
                    card = build_overwrite_card(
                        detail=reply,
                        session_key=session_key,
                    )
                    self.store.save(session_key, new_state, new_payload or payload)
                elif new_state is None:
                    ok = "失败" not in reply and "权限" not in reply
                    title = "录入完成" if ok else "未能写入"
                    card = build_result_card(
                        title=title,
                        body=reply,
                        session_key=session_key,
                        ok=ok,
                    )
                    self.store.clear(session_key)
                else:
                    card = build_supplier_form_card(
                        runtime,
                        (new_payload or payload).get("data") or data,
                        session_key=session_key,
                        hint=reply,
                    )
                    self.store.save(
                        session_key, new_state, new_payload or payload
                    )
                self._deliver_card(
                    feishu,
                    message_id=message_id,
                    chat_id=chat_id,
                    open_id=open_id,
                    card=card,
                )
            except Exception:
                logger.exception("async write failed session=%s", session_key)
                try:
                    self._deliver_card(
                        feishu,
                        message_id=message_id,
                        chat_id=chat_id,
                        open_id=open_id,
                        card=build_result_card(
                            title="写入失败",
                            body="写入时出错，请稍后重试。",
                            session_key=session_key,
                            ok=False,
                        ),
                    )
                except Exception:
                    logger.exception("deliver error card failed")
            finally:
                feishu.close()

        self.spawn(job)

    def _spawn_query(
        self,
        *,
        session_key: str,
        chat_id: str,
        open_id: str,
        message_id: str,
        text: str,
        resource: Dict[str, Any],
    ) -> None:
        def job() -> None:
            feishu = self.make_feishu()
            try:
                skill = self._skill_query()
                reply = call_query_agent(skill, text=text, resource=resource)
                card = build_result_card(
                    title="查询结果",
                    body=reply or "没有查到可用结果。",
                    session_key=session_key,
                    ok=True,
                )
                self._deliver_card(
                    feishu,
                    message_id=message_id,
                    chat_id=chat_id,
                    open_id=open_id,
                    card=card,
                )
            except Exception:
                logger.exception("async query failed")
                try:
                    self._deliver_card(
                        feishu,
                        message_id=message_id,
                        chat_id=chat_id,
                        open_id=open_id,
                        card=build_result_card(
                            title="查询失败",
                            body="查询超时或失败，请稍后重试。",
                            session_key=session_key,
                            ok=False,
                        ),
                    )
                except Exception:
                    logger.exception("deliver query error card failed")
            finally:
                feishu.close()

        self.spawn(job)


def send_lifecycle_card(
    feishu: FeishuClient,
    event: LifecycleEvent,
    load_fn: LoadFn,
    store: SessionStore,
) -> str:
    """p2p 进入发欢迎卡；菜单按 event_key 打开表单。"""
    if not event.open_id:
        return "missing_open_id"
    skey = event.open_id
    if event.kind == "bot_menu" and event.event_key == "supplier_entry":
        app_cfg, skills = load_fn()
        runtime = skill_to_runtime(skills["supplier"], app_cfg)
        store.save(skey, STATE_COLLECTING, {"skill_id": "supplier", "data": {}})
        feishu.send_interactive(
            event.open_id,
            build_supplier_form_card(runtime, {}, session_key=skey),
            receive_id_type="open_id",
        )
        return "supplier_form"
    if event.kind == "bot_menu" and event.event_key == "supplier_query":
        feishu.send_interactive(
            event.open_id,
            build_query_form_card(session_key=skey),
            receive_id_type="open_id",
        )
        return "query_form"
    feishu.send_interactive(
        event.open_id,
        build_welcome_card(session_key=skey, open_id=event.open_id),
        receive_id_type="open_id",
    )
    return "welcome"
