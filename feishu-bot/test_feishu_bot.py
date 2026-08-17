"""feishu-bot 单元测试：配置加载、路由、抽取、校验、规则、对话与查重权限。"""

import copy
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from config_loader import load_config, load_platform, skill_to_runtime
from dialog import (
    DialogEngine,
    is_trigger,
    STATE_AWAIT_OVERWRITE,
    STATE_COLLECTING,
    STATE_CONFIRMING,
)
from extractor import extract_by_rules, extract_fields
from query_proxy import call_query_agent, parse_agent_reply, resolve_webhook_url
from router import match_trigger, resolve_skill_id
from rules import apply_rules
from validator import (
    apply_field_patterns,
    build_bitable_fields,
    can_overwrite,
    fuzzy_match_option,
    is_blank_value,
    missing_required,
    payment_satisfied,
    reconcile_qrcode_payment,
    resolve_select_fields,
)

CONFIG_DIR = Path(__file__).resolve().parent / "config"
APP, SKILLS = load_platform(CONFIG_DIR)
CONFIG = skill_to_runtime(SKILLS["supplier"], APP)


class ConfigSkillTests(unittest.TestCase):
    """平台配置与 skill runtime 加载。"""

    def test_platform_loads_supplier(self):
        self.assertIn("supplier", SKILLS)
        self.assertEqual(CONFIG["skill_id"], "supplier")
        self.assertTrue(CONFIG["table_id"])
        self.assertTrue(CONFIG["require_any_group"])
        self.assertEqual(CONFIG.get("action") or "upsert_record", "upsert_record")
        self.assertIsNot(CONFIG["ai"].get("extract_enabled"), False)
        self.assertFalse(CONFIG["ai"].get("chat_enabled"))

    def test_platform_loads_query_skill(self):
        self.assertIn("query", SKILLS)
        q = SKILLS["query"]
        self.assertEqual(q["action"], "search")
        self.assertEqual((q.get("target") or {}).get("type"), "n8n_agent")
        self.assertFalse(q.get("fields"))

    def test_load_config_compat(self):
        cfg = load_config(CONFIG_DIR)
        self.assertEqual(cfg["skill_id"], "supplier")


class RouterTests(unittest.TestCase):
    """触发词匹配与会话粘性 skill。"""

    def test_match_trigger(self):
        self.assertEqual(match_trigger("添加供应商：测试", SKILLS), "supplier")
        self.assertIsNone(match_trigger("你好", SKILLS))

    def test_match_query_triggers(self):
        self.assertEqual(match_trigger("查供应商 德清茶歇", SKILLS), "query")
        self.assertEqual(match_trigger("查价差 团餐", SKILLS), "query")
        self.assertEqual(match_trigger("/查 某某", SKILLS), "query")

    def test_sticky_session(self):
        session = {
            "state": STATE_COLLECTING,
            "payload": {"skill_id": "supplier", "data": {}},
        }
        self.assertEqual(
            resolve_skill_id(text="补联系方式", session=session, skills=SKILLS),
            "supplier",
        )

    def test_sticky_session_blocks_query_until_cancel(self):
        session = {
            "state": STATE_COLLECTING,
            "payload": {"skill_id": "supplier", "data": {}},
        }
        self.assertEqual(
            resolve_skill_id(text="查供应商 德清", session=session, skills=SKILLS),
            "supplier",
        )


class ExtractorTests(unittest.TestCase):
    """规则抽取：标签行、触发词+名称、空占位符。"""

    def test_labeled_lines(self):
        text = (
            "添加供应商\n供应商：德清茶歇A\n户名：张三\n账号：6222 001\n"
            "银行：工行\n结算类型：月结\n地域：德清"
        )
        data = extract_by_rules(text, CONFIG)
        self.assertEqual(data["supplier_name"], "德清茶歇A")
        self.assertEqual(data["account_name"], "张三")
        self.assertEqual(data["account_no"], "6222001")
        self.assertEqual(data["bank_name"], "工行")
        self.assertEqual(data["settlement_type"], "月结")
        self.assertEqual(data["region"], "德清")

    def test_trigger_name(self):
        data = extract_by_rules("添加供应商 杭州某某民宿", CONFIG)
        self.assertEqual(data.get("supplier_name"), "杭州某某民宿")

    def test_blank_tokens_wu_meiyou_slash(self):
        text = (
            "添加供应商\n供应商名称：导游唐唐\n户名：无\n账号：没有\n银行：/\n"
            "地域：杭州\n类型：导游\n结算类型：现结\n联系方式：15658110209"
        )
        data = extract_by_rules(text, CONFIG)
        self.assertEqual(data.get("supplier_name"), "导游唐唐")
        self.assertNotIn("account_name", data)
        self.assertNotIn("account_no", data)
        self.assertNotIn("bank_name", data)
        self.assertEqual(data.get("region"), "杭州")
        self.assertFalse(is_blank_value("导游唐唐"))
        self.assertTrue(is_blank_value("无"))
        self.assertTrue(is_blank_value("/"))

    def test_empty_label_values_not_swallow_next_line(self):
        text = (
            "添加供应商\n供应商名称：导游唐唐\n户名：\n账号：\n银行：\n"
            "地域：杭州\n类型：导游\n结算类型：现结\n联系方式：15658110209"
        )
        data = extract_by_rules(text, CONFIG)
        self.assertEqual(data.get("supplier_name"), "导游唐唐")
        self.assertNotIn("account_name", data)
        self.assertNotIn("account_no", data)
        self.assertNotIn("bank_name", data)
        self.assertEqual(data.get("region"), "杭州")
        self.assertEqual(data.get("contact"), "15658110209")


class ValidatorTests(unittest.TestCase):
    """选项模糊匹配、组合必填、账号 digits、覆盖白名单。"""

    def test_bank_fuzzy(self):
        opts = CONFIG["fields"]["bank_name"]["options"]
        self.assertEqual(fuzzy_match_option("工行", opts), "中国工商银行")
        self.assertEqual(fuzzy_match_option("中国工商银行", opts), "中国工商银行")

    def test_zj_nongshang_sub_banks(self):
        """浙江农商下属行归并到选项「浙江农商银行」。"""
        opts = CONFIG["fields"]["bank_name"]["options"]
        self.assertIn("浙江农商银行", opts)
        self.assertEqual(
            fuzzy_match_option("浙江义乌农村商业银行", opts), "浙江农商银行"
        )
        self.assertEqual(
            fuzzy_match_option("义乌农村商业银行", opts), "浙江农商银行"
        )
        self.assertEqual(fuzzy_match_option("浙农商", opts), "浙江农商银行")
        # 非浙江农商体系仍不匹配
        self.assertIsNone(fuzzy_match_option("北京农村商业银行", opts))

    def test_payment_bank_group(self):
        data = {
            "supplier_name": "A",
            "account_name": "张三",
            "account_no": "123",
            "bank_name": "中国工商银行",
        }
        self.assertTrue(payment_satisfied(CONFIG, data))
        self.assertEqual(missing_required(CONFIG["fields"], data), [])

    def test_payment_qrcode(self):
        data = {"supplier_name": "A", "qrcode": [{"file_token": "tok"}]}
        self.assertTrue(payment_satisfied(CONFIG, data))

    def test_reconcile_qrcode_ignores_bad_bank(self):
        """有收款码时，非法银行选项不阻断（字段清空、问题丢弃）。"""
        data = {
            "supplier_name": "A",
            "bank_name": "火星银行",
            "qrcode": [{"file_token": "tok"}],
        }
        data, problems = resolve_select_fields(CONFIG["fields"], data, CONFIG)
        self.assertNotIn("bank_name", data)
        self.assertTrue(problems)
        data, problems = reconcile_qrcode_payment(CONFIG, data, problems)
        self.assertEqual(problems, [])
        self.assertIn("qrcode", data)
        self.assertTrue(payment_satisfied(CONFIG, data))

    def test_resolve_select(self):
        data = {"bank_name": "工行", "settlement_type": "月结", "region": "德清"}
        out, problems = resolve_select_fields(CONFIG["fields"], data, CONFIG)
        self.assertEqual(out["bank_name"], "中国工商银行")
        self.assertEqual(out["settlement_type"], ["月结"])
        self.assertEqual(out["region"], "德清")
        self.assertEqual(problems, [])

    def test_supplier_type_is_multi_select(self):
        self.assertEqual(CONFIG["fields"]["supplier_type"]["type"], "multi_select")

    def test_resolve_supplier_type_multi(self):
        out, problems = resolve_select_fields(
            CONFIG["fields"], {"supplier_type": "餐厅"}, CONFIG
        )
        self.assertEqual(out["supplier_type"], ["餐厅"])
        self.assertEqual(problems, [])

        out2, problems2 = resolve_select_fields(
            CONFIG["fields"], {"supplier_type": "餐厅、民宿"}, CONFIG
        )
        self.assertEqual(out2["supplier_type"], ["餐厅", "民宿"])
        self.assertEqual(problems2, [])

        out3, problems3 = resolve_select_fields(
            CONFIG["fields"], {"supplier_type": ["客户退款"]}, CONFIG
        )
        self.assertEqual(out3["supplier_type"], ["客户退款"])
        self.assertEqual(problems3, [])

    def test_option_missing_hard(self):
        data = {"bank_name": "火星银行不存在"}
        out, problems = resolve_select_fields(CONFIG["fields"], data, CONFIG)
        self.assertNotIn("bank_name", out)
        self.assertTrue(any("不存在" in p and "管理员" in p for p in problems))

    def test_account_digits(self):
        out, problems = apply_field_patterns(
            CONFIG["fields"], {"account_no": "6222abc"}, CONFIG
        )
        self.assertNotIn("account_no", out)
        self.assertTrue(any("纯数字" in p for p in problems))
        out2, problems2 = apply_field_patterns(
            CONFIG["fields"], {"account_no": "6222 001"}, CONFIG
        )
        self.assertEqual(out2["account_no"], "6222001")
        self.assertEqual(problems2, [])

    def test_can_overwrite(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["permissions"] = {"overwrite_open_ids": ["ou_admin"]}
        self.assertTrue(can_overwrite(cfg, "ou_admin"))
        self.assertFalse(can_overwrite(cfg, "ou_other"))
        self.assertFalse(can_overwrite(CONFIG, "ou_anyone"))

    def test_build_fields(self):
        data = {
            "supplier_name": "A",
            "bank_name": "中国工商银行",
            "settlement_type": ["月结"],
            "supplier_type": ["餐厅", "民宿"],
            "qrcode": [{"file_token": "ftoken"}],
        }
        fields = build_bitable_fields(CONFIG["fields"], data)
        self.assertEqual(fields["供应商"], "A")
        self.assertEqual(fields["银行名称"], "中国工商银行")
        self.assertEqual(fields["结算类型"], ["月结"])
        self.assertEqual(fields["类型"], ["餐厅", "民宿"])
        self.assertEqual(fields["支付宝/微信/二维码"], [{"file_token": "ftoken"}])


class FakeFeishu:
    """测试用假飞书客户端：可控查重命中，记录 create/update 调用。"""

    def __init__(self, hits=None):
        self.hits = hits or []
        self.search_calls = []
        self.created = None
        self.updated = None

    def upload_bitable_media(self, **kwargs):
        return "file_token_x"

    def search_records(self, **kwargs):
        self.search_calls.append(kwargs)
        field = kwargs.get("field_name")
        value = kwargs.get("value")
        out = []
        for hit in self.hits:
            if hit.get("match_field") == field and hit.get("match_value") == value:
                out.append(hit["record"])
        return out

    def create_record(self, **kwargs):
        self.created = kwargs
        return {"record_id": "rec_new"}

    def update_record(self, **kwargs):
        self.updated = kwargs
        return {"record_id": "rec_upd"}


class RulesTests(unittest.TestCase):
    """客户退款关键词规则。"""

    def test_customer_refund_sets_type_and_hint(self):
        data, hints, active = apply_rules(
            "客户退款 需要处理",
            {"supplier_name": "普通店"},
            CONFIG,
        )
        self.assertEqual(data["supplier_type"], "客户退款")
        self.assertIn("customer_refund", active)
        self.assertTrue(any("客户退款+订单号" in h for h in hints))

    def test_customer_refund_name_ok(self):
        data, hints, active = apply_rules(
            "客户退款",
            {"supplier_name": "客户退款26071001"},
            CONFIG,
        )
        self.assertEqual(data["supplier_type"], "客户退款")
        self.assertEqual(hints, [])
        self.assertIn("customer_refund", active)

    def test_customer_refund_name_order_suffix(self):
        """支持「订单号+客户退款」格式。"""
        data, hints, active = apply_rules(
            "客户退款",
            {"supplier_name": "26071001客户退款"},
            CONFIG,
        )
        self.assertEqual(data["supplier_type"], "客户退款")
        self.assertEqual(hints, [])
        self.assertIn("customer_refund", active)


class DialogTests(unittest.TestCase):
    """多轮对话、退款拦截、查重权限、附件上传。"""

    def test_blank_payment_fields_no_false_errors(self):
        """户名/账号/银行填「无」时不应产生账号/选项报错。"""
        engine = DialogEngine(CONFIG, FakeFeishu())
        text = (
            "添加供应商\n供应商名称：导游唐唐\n户名：无\n账号：无\n银行：无\n"
            "地域：杭州\n类型：导游\n结算类型：现结\n联系方式：15658110209"
        )
        reply, state, payload = engine.handle(
            session=None, text=text, message_type="text"
        )
        self.assertEqual(state, STATE_COLLECTING)
        self.assertNotIn("纯数字", reply)
        self.assertNotIn("不存在", reply)
        self.assertNotIn("account_no", payload["data"])
        self.assertNotIn("bank_name", payload["data"])
        self.assertIn("还需要", reply)
        self.assertIn("支付信息", reply)

    def test_empty_colon_payment_fields_no_false_errors(self):
        """户名：/账号：/银行：为空时，不应把下一行标签吞进字段。"""
        engine = DialogEngine(CONFIG, FakeFeishu())
        text = (
            "添加供应商\n供应商名称：导游唐唐\n户名：\n账号：\n银行：\n"
            "地域：杭州\n类型：导游\n结算类型：现结\n联系方式：15658110209"
        )
        reply, state, payload = engine.handle(
            session=None, text=text, message_type="text"
        )
        self.assertEqual(state, STATE_COLLECTING)
        self.assertNotIn("纯数字", reply)
        self.assertNotIn("不存在", reply)
        data = payload["data"]
        self.assertEqual(data.get("supplier_name"), "导游唐唐")
        self.assertEqual(data.get("region"), "杭州")
        self.assertNotIn("account_name", data)
        self.assertNotIn("account_no", data)
        self.assertNotIn("bank_name", data)

    def test_trigger(self):
        self.assertTrue(is_trigger("添加供应商 测试", CONFIG))

    def test_collect_to_confirm(self):
        engine = DialogEngine(CONFIG, FakeFeishu())
        text = (
            "添加供应商\n供应商：单元测试店\n户名：李四\n账号：6222001\n"
            "银行：工行\n地域：德清\n类型：餐厅\n结算类型：月结\n"
            "联系方式：13800000000\n开户支行：某某支行"
        )
        reply, state, payload = engine.handle(
            session=None, text=text, message_type="text"
        )
        self.assertEqual(state, STATE_CONFIRMING)
        self.assertIn("确认", reply)
        self.assertEqual(payload["data"]["supplier_name"], "单元测试店")
        self.assertEqual(payload["data"]["supplier_type"], ["餐厅"])
        self.assertEqual(payload.get("skill_id"), "supplier")

    def test_missing_payment(self):
        engine = DialogEngine(CONFIG, FakeFeishu())
        reply, state, payload = engine.handle(
            session=None,
            text="添加供应商：只有名字的店",
            message_type="text",
        )
        self.assertEqual(state, STATE_COLLECTING)
        self.assertIn("还需要", reply)

    def test_refund_blocks_until_name_pattern(self):
        engine = DialogEngine(CONFIG, FakeFeishu())
        text = (
            "添加供应商\n客户退款\n供应商：随便店\n户名：李四\n账号：6222001\n"
            "银行：工行\n地域：德清\n结算类型：月结"
        )
        reply, state, payload = engine.handle(
            session=None, text=text, message_type="text"
        )
        self.assertEqual(state, STATE_COLLECTING)
        self.assertEqual(payload["data"]["supplier_type"], ["客户退款"])
        self.assertIn("客户退款+订单号", reply)

    def test_account_invalid_in_dialog(self):
        engine = DialogEngine(CONFIG, FakeFeishu())
        text = (
            "添加供应商\n供应商：账号测\n户名：李四\n账号：6222ABCD\n银行：工行"
        )
        reply, state, payload = engine.handle(
            session=None, text=text, message_type="text"
        )
        self.assertEqual(state, STATE_COLLECTING)
        self.assertNotIn("account_no", payload["data"])
        self.assertIn("纯数字", reply)

    def test_confirm_writes(self):
        engine = DialogEngine(CONFIG, FakeFeishu())
        session = {
            "state": STATE_CONFIRMING,
            "payload": {
                "skill_id": "supplier",
                "data": {
                    "supplier_name": "写入店",
                    "account_name": "王五",
                    "account_no": "123",
                    "bank_name": "中国工商银行",
                },
                "skipped_suggested": True,
            },
        }
        reply, state, payload = engine.handle(
            session=session, text="确认", message_type="text"
        )
        self.assertIsNone(state)
        self.assertIn("已创建", reply)

    def test_confirm_writes_supplier_type_as_list(self):
        """旧会话里类型仍是字符串时，写表须包成多选数组。"""
        feishu = FakeFeishu()
        engine = DialogEngine(CONFIG, feishu)
        session = {
            "state": STATE_CONFIRMING,
            "payload": {
                "skill_id": "supplier",
                "data": {
                    "supplier_name": "类型多选店",
                    "account_name": "王五",
                    "account_no": "123",
                    "bank_name": "中国工商银行",
                    "supplier_type": "餐厅",
                },
                "skipped_suggested": True,
            },
        }
        reply, state, payload = engine.handle(
            session=session, text="确认", message_type="text"
        )
        self.assertIsNone(state)
        self.assertIn("已创建", reply)
        self.assertEqual(feishu.created["fields"]["类型"], ["餐厅"])

    def test_dedupe_ignores_account_name_only(self):
        """仅户名相同不查重；须撞供应商名或账号才触发。"""
        feishu = FakeFeishu(
            hits=[
                {
                    "match_field": "户名",
                    "match_value": "王五",
                    "record": {
                        "record_id": "rec_old",
                        "fields": {"供应商": "别人店", "户名": "王五"},
                    },
                }
            ]
        )
        engine = DialogEngine(CONFIG, feishu, open_id="ou_normal")
        session = {
            "state": STATE_CONFIRMING,
            "payload": {
                "skill_id": "supplier",
                "data": {
                    "supplier_name": "新店不撞名",
                    "account_name": "王五",
                    "account_no": "111222333",
                    "bank_name": "中国工商银行",
                },
                "skipped_suggested": True,
            },
        }
        reply, state, payload = engine.handle(
            session=session, text="确认", message_type="text"
        )
        self.assertIsNone(state)
        self.assertIn("已创建", reply)
        self.assertIsNotNone(feishu.created)
        # 不应去搜「户名」
        searched = {c.get("field_name") for c in feishu.search_calls}
        self.assertNotIn("户名", searched)
        self.assertIn("供应商", searched)
        self.assertIn("账号", searched)

    def test_dedupe_denied_without_permission(self):
        feishu = FakeFeishu(
            hits=[
                {
                    "match_field": "供应商",
                    "match_value": "撞名店",
                    "record": {
                        "record_id": "rec_old",
                        "fields": {"供应商": "撞名店"},
                    },
                }
            ]
        )
        engine = DialogEngine(CONFIG, feishu, open_id="ou_normal")
        session = {
            "state": STATE_CONFIRMING,
            "payload": {
                "skill_id": "supplier",
                "data": {
                    "supplier_name": "撞名店",
                    "account_name": "王五",
                    "account_no": "123",
                    "bank_name": "中国工商银行",
                },
                "skipped_suggested": True,
            },
        }
        reply, state, payload = engine.handle(
            session=session, text="确认", message_type="text"
        )
        self.assertIsNone(state)
        self.assertIsNone(payload)
        self.assertIn("覆盖权限", reply)
        self.assertIn("撞名店", reply)
        self.assertIsNone(feishu.created)

    def test_dedupe_ask_with_permission(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["permissions"] = {"overwrite_open_ids": ["ou_admin"]}
        feishu = FakeFeishu(
            hits=[
                {
                    "match_field": "账号",
                    "match_value": "999888",
                    "record": {
                        "record_id": "rec_old",
                        "fields": {"供应商": "已有供应商"},
                    },
                }
            ]
        )
        engine = DialogEngine(cfg, feishu, open_id="ou_admin")
        session = {
            "state": STATE_CONFIRMING,
            "payload": {
                "skill_id": "supplier",
                "data": {
                    "supplier_name": "新店",
                    "account_name": "王五",
                    "account_no": "999888",
                    "bank_name": "中国工商银行",
                },
                "skipped_suggested": True,
            },
        }
        reply, state, payload = engine.handle(
            session=session, text="确认", message_type="text"
        )
        self.assertEqual(state, STATE_AWAIT_OVERWRITE)
        self.assertIn("覆盖", reply)
        self.assertIn("已有供应商", reply)

        reply2, state2, _ = engine.handle(
            session={"state": state, "payload": payload},
            text="覆盖",
            message_type="text",
        )
        self.assertIsNone(state2)
        self.assertIn("已更新", reply2)
        self.assertIsNotNone(feishu.updated)

    def test_image_upload_in_collecting(self):
        engine = DialogEngine(CONFIG, FakeFeishu())
        session = {
            "state": STATE_COLLECTING,
            "payload": {
                "skill_id": "supplier",
                "data": {
                    "supplier_name": "图测店",
                    "account_name": "赵六",
                    "account_no": "999",
                    "bank_name": "中国工商银行",
                },
            },
        }
        reply, state, payload = engine.handle(
            session=session,
            text="结算类型：月结",
            message_type="post",
            image_bytes=b"\x89PNG\r\n\x1a\nfake",
            image_name="qrcode.png",
        )
        self.assertIn("qrcode", payload["data"])
        self.assertEqual(payload["data"]["qrcode"][0]["file_token"], "file_token_x")
        self.assertIn(state, (STATE_COLLECTING, STATE_CONFIRMING))
        self.assertTrue(reply)

    def test_qrcode_only_reaches_confirm(self):
        """仅供应商名 + 收款码图片即可进入确认，无需户名账号银行。"""
        engine = DialogEngine(CONFIG, FakeFeishu())
        session = {
            "state": STATE_COLLECTING,
            "payload": {
                "skill_id": "supplier",
                "data": {"supplier_name": "仅收款码店"},
                "skipped_suggested": True,
            },
        }
        reply, state, payload = engine.handle(
            session=session,
            text="",
            message_type="image",
            image_bytes=b"\x89PNG\r\n\x1a\nfake",
            image_name="qrcode.png",
        )
        self.assertEqual(state, STATE_CONFIRMING)
        self.assertIn("qrcode", payload["data"])
        self.assertNotIn("account_no", payload["data"])
        self.assertIn("确认", reply)

    def test_qrcode_with_invalid_bank_still_confirms(self):
        """已有收款码时，会话里残留的非法银行名不应再拦住确认。"""
        engine = DialogEngine(CONFIG, FakeFeishu())
        session = {
            "state": STATE_COLLECTING,
            "payload": {
                "skill_id": "supplier",
                "data": {
                    "supplier_name": "码优先店",
                    "bank_name": "火星银行不存在",
                },
                "skipped_suggested": True,
            },
        }
        reply, state, payload = engine.handle(
            session=session,
            text="",
            message_type="image",
            image_bytes=b"\x89PNG\r\n\x1a\nfake",
            image_name="qrcode.png",
        )
        self.assertEqual(state, STATE_CONFIRMING)
        self.assertIn("qrcode", payload["data"])
        self.assertNotIn("bank_name", payload["data"])
        self.assertNotIn("不存在", reply)


class ExtractAiGuardTests(unittest.TestCase):
    """录入 AI 只抽字段，可关闭 LLM。"""

    def test_extract_disabled_skips_llm(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["ai"] = {**(cfg.get("ai") or {}), "extract_enabled": False}
        long_text = (
            "添加供应商\n供应商：德清茶歇\n"
            + ("补充说明若干字。" * 20)
        )
        with patch("extractor.extract_by_llm") as llm:
            data = extract_fields(long_text, cfg, use_llm=True)
        llm.assert_not_called()
        self.assertEqual(data.get("supplier_name"), "德清茶歇")


class QueryProxyTests(unittest.TestCase):
    """只读查询：解析 n8n 回复、缺 URL、HTTP 失败；不写表。"""

    def test_parse_agent_reply_shapes(self):
        self.assertEqual(parse_agent_reply({"reply": "ok"}), "ok")
        self.assertEqual(parse_agent_reply({"text": "hi"}), "hi")
        self.assertEqual(parse_agent_reply({"output": "out"}), "out")
        self.assertEqual(parse_agent_reply({"message": "msg"}), "msg")
        self.assertEqual(parse_agent_reply({"json": {"reply": "nested"}}), "nested")
        self.assertEqual(parse_agent_reply({"data": {"text": "d"}}), "d")
        self.assertEqual(parse_agent_reply([{"reply": "list"}]), "list")
        self.assertEqual(parse_agent_reply("plain"), "plain")

    def test_resolve_webhook_url(self):
        self.assertEqual(
            resolve_webhook_url({"target": {"webhook_url": "http://n8n/x"}}),
            "http://n8n/x",
        )
        with patch.dict(os.environ, {"N8N_QUERY_WEBHOOK_URL": "http://from-env"}):
            self.assertEqual(resolve_webhook_url({"target": {}}), "http://from-env")

    def test_missing_webhook_returns_unavailable(self):
        skill = {"prompts": {"unavailable": "查询服务未配置。"}, "target": {}}
        with patch.dict(os.environ, {"N8N_QUERY_WEBHOOK_URL": ""}, clear=False):
            with patch("query_proxy.os.getenv", return_value=""):
                reply = call_query_agent(skill, text="查供应商 A", resource={})
        self.assertIn("未配置", reply)

    def test_mocked_post_returns_reply(self):
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"reply": "德清茶歇 月结"}

            text = ""

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, json=None):
                captured["url"] = url
                captured["json"] = json
                return FakeResp()

        skill = {
            "id": "query",
            "target": {"webhook_url": "http://n8n:5678/webhook/feishu-bot-query"},
        }
        with patch("query_proxy.httpx.Client", return_value=FakeClient()):
            reply = call_query_agent(
                skill,
                text="查供应商 德清茶歇",
                resource={"open_id": "ou_1", "chat_id": "oc_1", "message_id": "om_1"},
            )
        self.assertEqual(reply, "德清茶歇 月结")
        self.assertEqual(captured["url"], "http://n8n:5678/webhook/feishu-bot-query")
        self.assertEqual(captured["json"]["action"], "search")
        self.assertEqual(captured["json"]["open_id"], "ou_1")
        self.assertNotIn("fields", captured["json"])

    def test_http_error_returns_timeout_prompt(self):
        class BoomClient:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, json=None):
                raise RuntimeError("n8n down")

        skill = {
            "target": {"webhook_url": "http://n8n/x"},
            "prompts": {"timeout": "查询超时或 n8n 未响应，请稍后重试。"},
        }
        with patch("query_proxy.httpx.Client", return_value=BoomClient()):
            reply = call_query_agent(skill, text="查价差", resource={})
        self.assertIn("超时", reply)


if __name__ == "__main__":
    unittest.main()
