import json
import unittest
from copy import deepcopy
from pathlib import Path

from app import (
    _value_matches,
    actions_match,
    canonical_action_name,
    normalize_event,
    route_matches,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "files" / "quote-raw-event-test.json"

QUOTE_EDITED_RAW = {
    "schema": "2.0",
    "header": {
        "event_id": "debug-quote-generation-test-001",
        "create_time": "1779873600000",
        "event_type": "drive.file.bitable_record_changed_v1",
        "tenant_key": "debug",
        "app_id": "cli_a94bb777f63a5bce",
    },
    "event": {
        "file_token": "Y2ZgbUGWqa0MB4sLmXxc89T5n0P",
        "file_type": "bitable",
        "table_id": "tbl91gZyDPCLhlva",
        "update_time": 1779873600,
        "action_list": [
            {
                "action": "record_edited",
                "record_id": "recTestQuoteRoute001",
                "after_value": [
                    {"field_id": "fldGQVeaKb", "field_value": "optDK1JeDR"},
                ],
            }
        ],
    },
}


def load_quote_edited_event():
    if FIXTURE_PATH.exists():
        with FIXTURE_PATH.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = QUOTE_EDITED_RAW
    return normalize_event(raw)


def make_delete_event():
    event = load_quote_edited_event()
    raw = deepcopy(event["raw"])
    raw["event"]["action_list"] = [
        {
            "action": "record_deleted",
            "record_id": "recDeleted001",
            "before_value": [
                {
                    "field_id": "fldGQVeaKb",
                    "field_value": "optDK1JeDR",
                }
            ],
        }
    ]
    return normalize_event(raw)


def make_added_event():
    event = load_quote_edited_event()
    raw = deepcopy(event["raw"])
    raw["event"]["action_list"] = [
        {
            "action": "record_added",
            "record_id": "recAdded001",
            "after_value": [
                {
                    "field_id": "fldGQVeaKb",
                    "field_value": "optDK1JeDR",
                }
            ],
        }
    ]
    return normalize_event(raw)


def base_route(**overrides):
    route = {
        "name": "test-route",
        "enabled": True,
        "dispatch_enabled": True,
        "event_type": "drive.file.bitable_record_changed_v1",
        "app_token": "Y2ZgbUGWqa0MB4sLmXxc89T5n0P",
        "table_id": "tbl91gZyDPCLhlva",
        "n8n_webhook_url": "http://n8n:5678/webhook/test",
    }
    route.update(overrides)
    return route


class CanonicalActionNameTests(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(canonical_action_name("record_added"), "record_added")
        self.assertEqual(canonical_action_name("added"), "record_added")
        self.assertEqual(canonical_action_name("new"), "record_added")
        self.assertEqual(canonical_action_name("record_edited"), "record_edited")
        self.assertEqual(canonical_action_name("edited"), "record_edited")
        self.assertEqual(canonical_action_name("update"), "record_edited")
        self.assertEqual(canonical_action_name("record_deleted"), "record_deleted")
        self.assertEqual(canonical_action_name("deleted"), "record_deleted")
        self.assertEqual(canonical_action_name("delete"), "record_deleted")


class RouteActionsMatchTests(unittest.TestCase):
    def setUp(self):
        self.edited_event = load_quote_edited_event()
        self.added_event = make_added_event()
        self.deleted_event = make_delete_event()

    def test_backward_compatible_without_actions(self):
        route = base_route()
        self.assertTrue(route_matches(route, self.edited_event))

    def test_record_added_rejects_edited_event(self):
        route = base_route(actions="record_added")
        self.assertFalse(route_matches(route, self.edited_event))

    def test_record_edited_matches_fixture(self):
        route = base_route(actions="record_edited")
        self.assertTrue(route_matches(route, self.edited_event))

    def test_record_edited_with_field_conditions_matches_quote_rule(self):
        route = base_route(
            actions="record_edited",
            field_conditions=[
                {"field_id": "fldGQVeaKb", "equals": "optDK1JeDR"},
            ],
        )
        self.assertTrue(route_matches(route, self.edited_event))

    def test_record_added_with_field_conditions_rejects_edited_event(self):
        route = base_route(
            actions="record_added",
            field_conditions=[
                {"field_id": "fldGQVeaKb", "equals": "optDK1JeDR"},
            ],
        )
        self.assertFalse(route_matches(route, self.edited_event))

    def test_record_added_with_field_conditions_matches_added_event(self):
        route = base_route(
            actions="record_added",
            field_conditions=[
                {"field_id": "fldGQVeaKb", "equals": "optDK1JeDR"},
            ],
        )
        self.assertTrue(route_matches(route, self.added_event))

    def test_alias_actions_match(self):
        route = base_route(actions="edited")
        self.assertTrue(actions_match(route, self.edited_event))
        route = base_route(actions=["added"])
        self.assertTrue(actions_match(route, self.added_event))

    def test_record_deleted_matches_delete_event(self):
        route = base_route(actions="record_deleted")
        self.assertTrue(route_matches(route, self.deleted_event))

    def test_record_deleted_field_condition_uses_before_value(self):
        route = base_route(
            actions="record_deleted",
            field_conditions=[
                {"field_id": "fldGQVeaKb", "equals": "optDK1JeDR"},
            ],
        )
        self.assertTrue(route_matches(route, self.deleted_event))

    def test_multiple_actions_or_semantics(self):
        route = base_route(actions=["record_added", "record_edited"])
        self.assertTrue(route_matches(route, self.edited_event))
        self.assertTrue(route_matches(route, self.added_event))
        self.assertFalse(route_matches(route, self.deleted_event))


class ValueMatchesTests(unittest.TestCase):
    def test_single_string_match(self):
        self.assertTrue(_value_matches("abc", "abc"))

    def test_single_string_mismatch(self):
        self.assertFalse(_value_matches("abc", "xyz"))

    def test_list_match(self):
        self.assertTrue(_value_matches(["a", "b", "c"], "b"))

    def test_list_mismatch(self):
        self.assertFalse(_value_matches(["a", "b", "c"], "d"))

    def test_list_with_int_converts_to_str(self):
        self.assertTrue(_value_matches([1, 2, 3], "2"))

    def test_none_actual_with_string_expected(self):
        self.assertFalse(_value_matches("abc", None))

    def test_none_actual_with_list_expected(self):
        self.assertFalse(_value_matches(["abc"], None))


class MultiTableIdTests(unittest.TestCase):
    def setUp(self):
        self.edited_event = load_quote_edited_event()

    def test_single_table_id_backward_compatible(self):
        route = base_route(table_id="tbl91gZyDPCLhlva")
        self.assertTrue(route_matches(route, self.edited_event))

    def test_list_table_id_matches_when_in_list(self):
        route = base_route(table_id=["tblNd9zgN7ALihwj", "tbl91gZyDPCLhlva"])
        self.assertTrue(route_matches(route, self.edited_event))

    def test_list_table_id_mismatches_when_not_in_list(self):
        route = base_route(table_id=["tblNd9zgN7ALihwj", "tblAnotherOne"])
        self.assertFalse(route_matches(route, self.edited_event))

    def test_list_app_token_matches(self):
        route = base_route(app_token=["Y2ZgbUGWqa0MB4sLmXxc89T5n0P", "other"])
        self.assertTrue(route_matches(route, self.edited_event))

    def test_list_app_token_mismatches(self):
        route = base_route(app_token=["other1", "other2"])
        self.assertFalse(route_matches(route, self.edited_event))


class AppIdAndImNormalizeTests(unittest.TestCase):
    def test_app_id_route_match(self):
        event = load_quote_edited_event()
        route = base_route(app_id="cli_a94bb777f63a5bce")
        self.assertTrue(route_matches(route, event))
        route_bad = base_route(app_id="cli_other")
        self.assertFalse(route_matches(route_bad, event))

    def test_im_message_normalize(self):
        raw = {
            "schema": "2.0",
            "header": {
                "event_id": "im-001",
                "event_type": "im.message.receive_v1",
                "app_id": "cli_a940966ee8385bd7",
                "create_time": "1",
            },
            "event": {
                "sender": {
                    "sender_type": "user",
                    "sender_id": {"open_id": "ou_test"},
                },
                "message": {
                    "message_id": "om_test",
                    "chat_id": "oc_test",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": '{"text":"添加供应商 测试店"}',
                },
            },
        }
        normalized = normalize_event(raw)
        self.assertEqual(normalized["event_type"], "im.message.receive_v1")
        self.assertEqual(normalized["app_id"], "cli_a940966ee8385bd7")
        self.assertEqual(normalized["resource"]["open_id"], "ou_test")
        self.assertEqual(normalized["resource"]["text"], "添加供应商 测试店")
        self.assertEqual(normalized["resource"]["message_type"], "text")

    def test_post_with_embedded_image_normalize(self):
        post = {
            "title": "",
            "content": [
                [
                    {
                        "tag": "text",
                        "text": "录入二维码，结算类型改为月结",
                        "style": [],
                    }
                ],
                [
                    {
                        "tag": "img",
                        "image_key": "img_v3_0213l_testkey",
                        "width": 100,
                        "height": 100,
                    }
                ],
            ],
        }
        raw = {
            "schema": "2.0",
            "header": {
                "event_id": "im-post-001",
                "event_type": "im.message.receive_v1",
                "app_id": "cli_a940966ee8385bd7",
            },
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_x"}},
                "message": {
                    "message_id": "om_post",
                    "chat_id": "oc_x",
                    "chat_type": "p2p",
                    "message_type": "post",
                    "content": json.dumps(post, ensure_ascii=False),
                },
            },
        }
        normalized = normalize_event(raw)
        self.assertEqual(normalized["resource"]["message_type"], "post")
        self.assertIn("录入二维码", normalized["resource"]["text"])
        self.assertEqual(normalized["resource"]["image_key"], "img_v3_0213l_testkey")
        self.assertEqual(
            normalized["resource"]["image_keys"], ["img_v3_0213l_testkey"]
        )


if __name__ == "__main__":
    unittest.main()
