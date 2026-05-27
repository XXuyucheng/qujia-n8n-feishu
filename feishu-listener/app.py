import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

try:
    import lark_oapi as lark
except Exception:  # pragma: no cover - lets local tests run without the SDK
    lark = None


CONFIG_PATH = Path(os.getenv("LISTENER_CONFIG_PATH", "/app/config.yaml"))
DB_PATH = Path(os.getenv("LISTENER_DB_PATH", "/data/events.sqlite"))
HOST = os.getenv("LISTENER_HOST", "0.0.0.0")
PORT = int(os.getenv("LISTENER_PORT", "8010"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("feishu-listener")

app = FastAPI(title="Feishu Listener")
state = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "ws_status": "not_started",
    "last_event_at": None,
    "last_error": None,
}


class EventStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                create table if not exists events (
                    id integer primary key autoincrement,
                    event_id text,
                    event_type text,
                    route_name text,
                    status text not null,
                    dispatch_enabled integer not null default 0,
                    normalized_json text not null,
                    raw_json text not null,
                    error text,
                    created_at text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists dispatch_attempts (
                    id integer primary key autoincrement,
                    event_row_id integer not null,
                    route_name text not null,
                    webhook_url text not null,
                    attempt integer not null,
                    status text not null,
                    status_code integer,
                    elapsed_ms integer,
                    error text,
                    created_at text not null,
                    foreign key(event_row_id) references events(id)
                )
                """
            )
            conn.execute("create index if not exists idx_events_created_at on events(created_at)")
            conn.execute("create index if not exists idx_events_event_id on events(event_id)")
            self.migrate(conn)

    def migrate(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("pragma table_info(events)").fetchall()}
        migrations = {
            "raw_mode": "alter table events add column raw_mode text not null default 'full'",
            "app_token": "alter table events add column app_token text",
            "table_id": "alter table events add column table_id text",
            "record_id": "alter table events add column record_id text",
            "chat_id": "alter table events add column chat_id text",
            "command": "alter table events add column command text",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)

        conn.execute("create index if not exists idx_events_status on events(status)")
        conn.execute("create index if not exists idx_events_route_name on events(route_name)")
        conn.execute("create index if not exists idx_events_event_type on events(event_type)")
        conn.execute("create index if not exists idx_events_table_id on events(table_id)")
        conn.execute("create index if not exists idx_events_record_id on events(record_id)")

        conn.execute(
            """
            update events
            set app_token = coalesce(app_token, json_extract(normalized_json, '$.resource.app_token')),
                table_id = coalesce(table_id, json_extract(normalized_json, '$.resource.table_id')),
                record_id = coalesce(record_id, json_extract(normalized_json, '$.resource.record_id')),
                chat_id = coalesce(chat_id, json_extract(normalized_json, '$.resource.chat_id')),
                command = coalesce(command, json_extract(normalized_json, '$.command'))
            where app_token is null
               or table_id is null
               or record_id is null
               or chat_id is null
               or command is null
            """
        )

    def insert_event(
        self,
        normalized: Dict[str, Any],
        raw: Dict[str, Any],
        route_name: Optional[str],
        status: str,
        dispatch_enabled: bool,
        raw_mode: str,
        error: Optional[str] = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        resource = normalized.get("resource") or {}
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                """
                insert into events (
                    event_id, event_type, route_name, status, dispatch_enabled,
                    normalized_json, raw_json, raw_mode, error, created_at,
                    app_token, table_id, record_id, chat_id, command
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.get("event_id"),
                    normalized.get("event_type"),
                    route_name,
                    status,
                    1 if dispatch_enabled else 0,
                    json.dumps(normalized, ensure_ascii=False),
                    json.dumps(raw, ensure_ascii=False),
                    raw_mode,
                    error,
                    now,
                    resource.get("app_token") or resource.get("file_token"),
                    resource.get("table_id"),
                    resource.get("record_id"),
                    resource.get("chat_id"),
                    normalized.get("command"),
                ),
            )
            return int(cur.lastrowid)

    def insert_attempt(
        self,
        event_row_id: int,
        route_name: str,
        webhook_url: str,
        attempt: int,
        status: str,
        status_code: Optional[int],
        elapsed_ms: Optional[int],
        error: Optional[str],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                insert into dispatch_attempts (
                    event_row_id, route_name, webhook_url, attempt, status,
                    status_code, elapsed_ms, error, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_row_id,
                    route_name,
                    webhook_url,
                    attempt,
                    status,
                    status_code,
                    elapsed_ms,
                    error,
                    now,
                ),
            )

    def list_events(
        self,
        limit: int,
        offset: int,
        status: Optional[str] = None,
        route_name: Optional[str] = None,
        event_type: Optional[str] = None,
        table_id: Optional[str] = None,
        record_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        clauses = []
        params: List[Any] = []

        filters = {
            "status": status,
            "route_name": route_name,
            "event_type": event_type,
            "table_id": table_id,
            "record_id": record_id,
        }
        for column, value in filters.items():
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)

        where = f"where {' and '.join(clauses)}" if clauses else ""

        with self.connect() as conn:
            total = conn.execute(f"select count(*) from events {where}", params).fetchone()[0]
            rows = conn.execute(
                f"""
                select e.id, e.event_id, e.event_type, e.route_name, e.status,
                       e.dispatch_enabled, e.raw_mode, e.error, e.created_at,
                       e.app_token, e.table_id, e.record_id, e.chat_id, e.command,
                       (
                         select a.status
                         from dispatch_attempts a
                         where a.event_row_id = e.id
                         order by a.id desc
                         limit 1
                       ) as dispatch_status,
                       (
                         select a.status_code
                         from dispatch_attempts a
                         where a.event_row_id = e.id
                         order by a.id desc
                         limit 1
                       ) as dispatch_status_code
                from events e
                {where}
                order by e.id desc
                limit ? offset ?
                """,
                [*params, limit, offset],
            ).fetchall()

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "events": [dict(row) for row in rows],
        }

    def get_event(self, event_id: int) -> Dict[str, Any]:
        with self.connect() as conn:
            event = conn.execute("select * from events where id = ?", (event_id,)).fetchone()
            if event is None:
                raise KeyError(event_id)
            attempts = conn.execute(
                "select * from dispatch_attempts where event_row_id = ? order by id asc",
                (event_id,),
            ).fetchall()

        data = dict(event)
        data["normalized"] = parse_json(data.pop("normalized_json", "{}"))
        data["raw"] = parse_json(data.pop("raw_json", "{}"))
        data["dispatch_attempts"] = [dict(row) for row in attempts]
        return data

    def stats(self) -> Dict[str, Any]:
        with self.connect() as conn:
            event_count = conn.execute("select count(*) from events").fetchone()[0]
            attempt_count = conn.execute("select count(*) from dispatch_attempts").fetchone()[0]
            by_status = [
                dict(row)
                for row in conn.execute(
                    "select status, count(*) as count from events group by status order by count desc"
                ).fetchall()
            ]
            by_route = [
                dict(row)
                for row in conn.execute(
                    """
                    select coalesce(route_name, '(none)') as route_name, count(*) as count
                    from events
                    group by route_name
                    order by count desc
                    """
                ).fetchall()
            ]
            recent_attempts = [
                dict(row)
                for row in conn.execute(
                    """
                    select a.id, a.event_row_id, e.event_id, a.route_name, a.status,
                           a.status_code, a.elapsed_ms, a.error, a.created_at
                    from dispatch_attempts a
                    left join events e on e.id = a.event_row_id
                    order by a.id desc
                    limit 10
                    """
                ).fetchall()
            ]

        return {
            "db_path": str(self.path),
            "db_size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "event_count": event_count,
            "dispatch_attempt_count": attempt_count,
            "by_status": by_status,
            "by_route": by_route,
            "recent_dispatch_attempts": recent_attempts,
        }

    def cleanup(self, retention: Dict[str, Any]) -> Dict[str, Any]:
        max_days = int(retention.get("max_days", 7))
        max_events = int(retention.get("max_events", 5000))
        vacuum = bool(retention.get("vacuum_after_cleanup", True))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).isoformat()

        with self._lock, self.connect() as conn:
            before = conn.execute("select count(*) from events").fetchone()[0]
            ids_to_delete = {
                row["id"]
                for row in conn.execute("select id from events where created_at < ?", (cutoff,)).fetchall()
            }

            if max_events > 0:
                overflow_rows = conn.execute(
                    """
                    select id from events
                    where id not in (
                      select id from events order by id desc limit ?
                    )
                    """,
                    (max_events,),
                ).fetchall()
                ids_to_delete.update(row["id"] for row in overflow_rows)

            deleted_attempts = 0
            deleted_events = 0
            if ids_to_delete:
                ids = sorted(ids_to_delete)
                for chunk in chunked(ids, 500):
                    placeholders = ",".join("?" for _ in chunk)
                    cur = conn.execute(
                        f"delete from dispatch_attempts where event_row_id in ({placeholders})",
                        chunk,
                    )
                    deleted_attempts += cur.rowcount if cur.rowcount != -1 else 0
                    cur = conn.execute(f"delete from events where id in ({placeholders})", chunk)
                    deleted_events += cur.rowcount if cur.rowcount != -1 else 0

            after = conn.execute("select count(*) from events").fetchone()[0]

        if vacuum and deleted_events:
            with self._lock, self.connect() as conn:
                conn.execute("vacuum")

        return {
            "before_events": before,
            "after_events": after,
            "deleted_events": deleted_events,
            "deleted_dispatch_attempts": deleted_attempts,
            "max_days": max_days,
            "max_events": max_events,
            "vacuum": vacuum and bool(deleted_events),
        }

    def compact_ignored_raw(self, vacuum: bool = True) -> Dict[str, Any]:
        converted = 0
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """
                select id, normalized_json, raw_json
                from events
                where status = 'ignored'
                  and raw_mode = 'full'
                """
            ).fetchall()
            for row in rows:
                normalized = parse_json(row["normalized_json"])
                raw = parse_json(row["raw_json"])
                summary = summarize_raw_event(raw, normalized)
                conn.execute(
                    "update events set raw_json = ?, raw_mode = 'summary' where id = ?",
                    (json.dumps(summary, ensure_ascii=False), row["id"]),
                )
                converted += 1

        if vacuum and converted:
            with self._lock, self.connect() as conn:
                conn.execute("vacuum")

        return {
            "converted_events": converted,
            "vacuum": vacuum and bool(converted),
            "db_size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def is_writable(self) -> bool:
        try:
            with self.connect() as conn:
                conn.execute("select 1")
            return True
        except Exception:
            return False


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config file not found: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("routes", [])
    data.setdefault("retention", {})
    data["retention"].setdefault("max_days", 7)
    data["retention"].setdefault("max_events", 5000)
    data["retention"].setdefault("vacuum_after_cleanup", True)
    data.setdefault("storage", {})
    data["storage"].setdefault("ignored_raw_mode", "summary")
    data["storage"].setdefault("matched_raw_mode", "full")
    return data


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_json(value: str) -> Any:
    try:
        return json.loads(value or "{}")
    except Exception:
        return {}


def chunked(items: List[int], size: int) -> List[List[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if lark is not None:
        try:
            return json.loads(lark.JSON.marshal(value))
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return value.__dict__
    return {"value": str(value)}


def nested_get(data: Dict[str, Any], *path: str) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def normalize_event(raw_input: Any) -> Dict[str, Any]:
    raw = as_dict(raw_input)
    header = raw.get("header") or {}
    event = raw.get("event") or {}
    message = event.get("message") or {}

    event_type = (
        header.get("event_type")
        or raw.get("event_type")
        or raw.get("type")
        or event.get("type")
        or ""
    )
    event_id = (
        header.get("event_id")
        or raw.get("event_id")
        or raw.get("uuid")
        or event.get("event_id")
        or ""
    )

    app_token = (
        event.get("app_token")
        or event.get("file_token")
        or event.get("token")
        or nested_get(event, "base", "app_token")
        or ""
    )
    file_token = event.get("file_token") or app_token

    record_id = event.get("record_id") or ""
    record_ids = event.get("record_ids") or []
    action_list = event.get("action_list") or []
    if not record_id and isinstance(action_list, list) and action_list:
        first_action = action_list[0]
        if isinstance(first_action, dict):
            record_id = first_action.get("record_id") or ""
    if not record_id and isinstance(record_ids, list) and record_ids:
        record_id = record_ids[0]

    text = ""
    content = message.get("content")
    if isinstance(content, str):
        try:
            text = json.loads(content).get("text", "")
        except Exception:
            text = content

    command = text.strip().split()[0] if text.strip().startswith("/") else ""

    return {
        "event_id": str(event_id),
        "event_type": str(event_type),
        "event_time": str(header.get("create_time") or raw.get("ts") or ""),
        "source": "feishu",
        "tenant_key": str(header.get("tenant_key") or raw.get("tenant_key") or ""),
        "app_id": str(header.get("app_id") or raw.get("app_id") or ""),
        "resource": {
            "app_token": str(app_token),
            "file_token": str(file_token),
            "table_id": str(event.get("table_id") or ""),
            "record_id": str(record_id),
            "record_ids": record_ids,
            "chat_id": str(event.get("chat_id") or message.get("chat_id") or ""),
            "message_id": str(message.get("message_id") or ""),
        },
        "command": command,
        "raw": raw,
    }


def summarize_raw_event(raw: Dict[str, Any], normalized: Dict[str, Any]) -> Dict[str, Any]:
    event = raw.get("event") or {}
    header = raw.get("header") or {}
    actions = event.get("action_list") or []
    action_summary = []

    if isinstance(actions, list):
        for action in actions[:10]:
            if not isinstance(action, dict):
                continue
            after = action.get("after_value") or []
            before = action.get("before_value") or []
            action_summary.append(
                {
                    "action": action.get("action"),
                    "record_id": action.get("record_id"),
                    "after_field_ids": [
                        item.get("field_id")
                        for item in after
                        if isinstance(item, dict) and item.get("field_id")
                    ][:100],
                    "before_field_ids": [
                        item.get("field_id")
                        for item in before
                        if isinstance(item, dict) and item.get("field_id")
                    ][:100],
                    "after_count": len(after) if isinstance(after, list) else 0,
                    "before_count": len(before) if isinstance(before, list) else 0,
                }
            )

    return {
        "summary": True,
        "schema": raw.get("schema"),
        "header": {
            "event_id": header.get("event_id"),
            "event_type": header.get("event_type"),
            "create_time": header.get("create_time"),
            "tenant_key": header.get("tenant_key"),
            "app_id": header.get("app_id"),
        },
        "event": {
            "file_token": event.get("file_token") or event.get("app_token"),
            "file_type": event.get("file_type"),
            "table_id": event.get("table_id"),
            "update_time": event.get("update_time"),
            "revision": event.get("revision"),
            "operator_id": event.get("operator_id"),
            "action_list": action_summary,
        },
        "normalized_resource": normalized.get("resource", {}),
    }


def choose_raw_payload(
    raw: Dict[str, Any],
    normalized: Dict[str, Any],
    status: str,
    config: Dict[str, Any],
) -> Tuple[Dict[str, Any], str]:
    storage = config.get("storage", {})
    mode = storage.get("matched_raw_mode", "full")
    if status == "ignored":
        mode = storage.get("ignored_raw_mode", "summary")

    if mode == "summary":
        return summarize_raw_event(raw, normalized), "summary"
    if mode == "none":
        return {}, "none"
    return raw, "full"


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def format_event_row(row: Dict[str, Any]) -> str:
    status = escape(str(row.get("status") or ""))
    dispatch_status = escape(str(row.get("dispatch_status") or ""))
    return (
        "<tr>"
        f"<td><a href='#' onclick='showDetail({int(row.get('id'))}); return false;'>{int(row.get('id'))}</a></td>"
        f"<td><code>{escape(str(row.get('created_at') or ''))}</code></td>"
        f"<td><span class='pill {escape(str(row.get('status') or ''))}'>{status}</span></td>"
        f"<td><span class='pill {escape(str(row.get('dispatch_status') or ''))}'>{dispatch_status}</span></td>"
        f"<td>{escape(str(row.get('route_name') or ''))}</td>"
        f"<td><code>{escape(str(row.get('table_id') or ''))}</code></td>"
        f"<td><code>{escape(str(row.get('record_id') or ''))}</code></td>"
        f"<td>{escape(str(row.get('event_type') or ''))}</td>"
        "</tr>"
    )


def extract_field_tokens(value: Any) -> List[str]:
    tokens: List[str] = []

    def add(token: Any) -> None:
        if token is None:
            return
        text = str(token).strip()
        if text and text not in tokens:
            tokens.append(text)

    def walk(cur: Any) -> None:
        if cur is None:
            return
        if isinstance(cur, str):
            text = cur.strip()
            add(text)
            if text and text[0] in "[{":
                parsed = parse_json(text)
                if parsed not in ({}, []):
                    walk(parsed)
            return
        if isinstance(cur, (int, float, bool)):
            add(cur)
            return
        if isinstance(cur, list):
            for item in cur:
                walk(item)
            return
        if isinstance(cur, dict):
            for key in ("id", "name", "enName", "en_name", "text", "value", "userId"):
                if key in cur:
                    walk(cur.get(key))
            for key in ("data", "users"):
                if key in cur:
                    walk(cur.get(key))
            return

    walk(value)
    return tokens


def action_field_tokens(event: Dict[str, Any], field_id: str) -> List[str]:
    raw_event = (event.get("raw") or {}).get("event") or {}
    actions = raw_event.get("action_list") or []
    tokens: List[str] = []

    if not isinstance(actions, list):
        return tokens

    for action in actions:
        if not isinstance(action, dict):
            continue
        after_values = action.get("after_value") or []
        if not isinstance(after_values, list):
            continue
        for field in after_values:
            if not isinstance(field, dict):
                continue
            if str(field.get("field_id") or "") != str(field_id):
                continue
            values = extract_field_tokens(field.get("field_value"))
            values.extend(extract_field_tokens(field.get("field_identity_value")))
            for value in values:
                if value not in tokens:
                    tokens.append(value)

    return tokens


def field_condition_matches(event: Dict[str, Any], condition: Dict[str, Any]) -> bool:
    field_id = condition.get("field_id")
    if not field_id:
        return False

    actual_tokens = action_field_tokens(event, str(field_id))
    if "exists" in condition:
        return bool(actual_tokens) is bool(condition.get("exists"))

    expected = condition.get("equals", condition.get("value"))
    if expected is not None:
        expected_tokens = [str(item) for item in expected] if isinstance(expected, list) else [str(expected)]
        return any(token in actual_tokens for token in expected_tokens)

    contains = condition.get("contains")
    if contains is not None:
        return any(str(contains) in token for token in actual_tokens)

    return bool(actual_tokens)


def field_conditions_match(route: Dict[str, Any], event: Dict[str, Any]) -> bool:
    conditions = route.get("field_conditions") or []
    if isinstance(conditions, dict):
        conditions = [conditions]
    if not isinstance(conditions, list):
        return False

    for condition in conditions:
        if not isinstance(condition, dict):
            return False
        if not field_condition_matches(event, condition):
            return False
    return True


def route_matches(route: Dict[str, Any], event: Dict[str, Any]) -> bool:
    if not route.get("enabled", True):
        return False

    resource = event.get("resource") or {}
    checks: List[Tuple[str, Any, Any]] = [
        ("event_type", route.get("event_type"), event.get("event_type")),
        ("app_token", route.get("app_token"), resource.get("app_token") or resource.get("file_token")),
        ("file_token", route.get("file_token"), resource.get("file_token")),
        ("table_id", route.get("table_id"), resource.get("table_id")),
        ("chat_id", route.get("chat_id"), resource.get("chat_id")),
        ("command", route.get("command"), event.get("command")),
    ]
    for _, expected, actual in checks:
        if expected is None:
            continue
        if str(expected) != str(actual):
            return False
    return field_conditions_match(route, event)


def find_route(event: Dict[str, Any], routes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for route in routes:
        if route_matches(route, event):
            return route
    return None


def dispatch_to_n8n(store: EventStore, event_row_id: int, route: Dict[str, Any], event: Dict[str, Any]) -> None:
    url = route.get("n8n_webhook_url")
    if not url:
        store.insert_attempt(
            event_row_id,
            route.get("name", "unnamed"),
            "",
            1,
            "skipped",
            None,
            None,
            "missing n8n_webhook_url",
        )
        return

    delays = [1, 3, 10]
    max_attempts = int(route.get("max_attempts", 3))
    timeout = float(route.get("timeout_seconds", 10))

    for attempt in range(1, max_attempts + 1):
        started = now_ms()
        error = None
        status_code = None
        status = "failed"
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    url,
                    json={
                        "route_name": route.get("name"),
                        "event": event,
                    },
                )
            status_code = response.status_code
            response.raise_for_status()
            status = "success"
        except Exception as exc:
            error = str(exc)

        store.insert_attempt(
            event_row_id,
            route.get("name", "unnamed"),
            url,
            attempt,
            status,
            status_code,
            now_ms() - started,
            error,
        )

        if status == "success":
            return
        if attempt < max_attempts:
            time.sleep(delays[min(attempt - 1, len(delays) - 1)])


store = EventStore(DB_PATH)
cleanup_state = {
    "last_cleanup_at": None,
    "last_cleanup_result": None,
}


def maybe_cleanup(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    now = time.time()
    last = cleanup_state.get("last_cleanup_at")
    if last and now - float(last) < 300:
        return None
    result = store.cleanup(config.get("retention", {}))
    cleanup_state["last_cleanup_at"] = now
    cleanup_state["last_cleanup_result"] = result
    return result


def process_event(raw_input: Any) -> Dict[str, Any]:
    raw = as_dict(raw_input)
    normalized = normalize_event(raw)
    config = load_config()
    route = find_route(normalized, config.get("routes", []))
    route_name = route.get("name") if route else None
    dispatch_enabled = bool(route and route.get("dispatch_enabled", False))
    status = "ignored"

    if route:
        status = "matched_dispatch_disabled"
        if dispatch_enabled:
            status = "dispatching"

    raw_payload, raw_mode = choose_raw_payload(raw, normalized, status, config)
    event_row_id = store.insert_event(
        normalized=normalized,
        raw=raw_payload,
        route_name=route_name,
        status=status,
        dispatch_enabled=dispatch_enabled,
        raw_mode=raw_mode,
    )

    state["last_event_at"] = datetime.now(timezone.utc).isoformat()

    if route and dispatch_enabled:
        try:
            dispatch_to_n8n(store, event_row_id, route, normalized)
        except Exception as exc:
            state["last_error"] = str(exc)
            logger.exception("dispatch failed")

    logger.info(
        "event processed event_id=%s event_type=%s route=%s status=%s",
        normalized.get("event_id"),
        normalized.get("event_type"),
        route_name,
        status,
    )
    cleanup_result = maybe_cleanup(config)
    return {
        "event_row_id": event_row_id,
        "status": status,
        "route_name": route_name,
        "dispatch_enabled": dispatch_enabled,
        "cleanup": cleanup_result,
        "normalized": normalized,
    }


def build_lark_event_handler() -> Any:
    if lark is None:
        raise RuntimeError("lark_oapi is not installed")

    verification_token = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
    encrypt_key = os.getenv("FEISHU_ENCRYPT_KEY", "")

    def handle_custom_event(data: Any) -> None:
        process_event(data)

    builder = lark.EventDispatcherHandler.builder(verification_token, encrypt_key)
    builder = builder.register_p2_customized_event(
        "drive.file.bitable_record_changed_v1",
        handle_custom_event,
    )
    builder = builder.register_p1_customized_event(
        "drive.file.bitable_record_changed_v1",
        handle_custom_event,
    )
    return builder.build()


def start_ws_client() -> None:
    if os.getenv("LISTENER_DISABLE_WS", "").lower() in {"1", "true", "yes"}:
        state["ws_status"] = "disabled"
        logger.info("websocket listener disabled by LISTENER_DISABLE_WS")
        return

    app_id = os.getenv("FEISHU_APP_ID", "")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        state["ws_status"] = "missing_credentials"
        logger.warning("FEISHU_APP_ID or FEISHU_APP_SECRET is missing; websocket not started")
        return

    try:
        handler = build_lark_event_handler()
        state["ws_status"] = "starting"
        ws_client = lark.ws.Client(
            app_id=app_id,
            app_secret=app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )
        state["ws_status"] = "running"
        ws_client.start()
    except Exception as exc:
        state["ws_status"] = "error"
        state["last_error"] = str(exc)
        logger.exception("websocket listener failed")


@app.on_event("startup")
def on_startup() -> None:
    load_config()
    thread = threading.Thread(target=start_ws_client, name="feishu-ws", daemon=True)
    thread.start()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": store.is_writable(),
        "service": "feishu-listener",
        "started_at": state["started_at"],
        "ws_status": state["ws_status"],
        "last_event_at": state["last_event_at"],
        "last_error": state["last_error"],
        "db_path": str(DB_PATH),
        "last_cleanup_at": cleanup_state["last_cleanup_at"],
        "last_cleanup_result": cleanup_state["last_cleanup_result"],
    }


@app.get("/routes")
def routes() -> Dict[str, Any]:
    config = load_config()
    sanitized_routes = []
    for route in config.get("routes", []):
        sanitized = dict(route)
        secret = sanitized.pop("webhook_secret", "")
        if secret:
            sanitized["n8n_webhook_url"] = sanitized.get("n8n_webhook_url", "").replace(secret, "***")
        sanitized_routes.append(sanitized)

    return {
        "routes": sanitized_routes
    }


@app.get("/events")
def events(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = None,
    route_name: Optional[str] = None,
    event_type: Optional[str] = None,
    table_id: Optional[str] = None,
    record_id: Optional[str] = None,
) -> Dict[str, Any]:
    return store.list_events(
        limit=limit,
        offset=offset,
        status=status,
        route_name=route_name,
        event_type=event_type,
        table_id=table_id,
        record_id=record_id,
    )


@app.get("/events/{event_row_id}")
def event_detail(event_row_id: int) -> Dict[str, Any]:
    try:
        return store.get_event(event_row_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="event not found")


@app.get("/stats")
def stats() -> Dict[str, Any]:
    config = load_config()
    return {
        **store.stats(),
        "retention": config.get("retention", {}),
        "storage": config.get("storage", {}),
        "last_cleanup_at": cleanup_state["last_cleanup_at"],
        "last_cleanup_result": cleanup_state["last_cleanup_result"],
    }


@app.post("/admin/cleanup")
def admin_cleanup() -> Dict[str, Any]:
    config = load_config()
    result = store.cleanup(config.get("retention", {}))
    cleanup_state["last_cleanup_at"] = time.time()
    cleanup_state["last_cleanup_result"] = result
    return result


@app.post("/admin/compact-ignored")
def admin_compact_ignored() -> Dict[str, Any]:
    config = load_config()
    vacuum = bool(config.get("retention", {}).get("vacuum_after_cleanup", True))
    return store.compact_ignored_raw(vacuum=vacuum)


@app.post("/debug/normalize")
def debug_normalize(payload: Dict[str, Any]) -> Dict[str, Any]:
    if os.getenv("ENABLE_DEBUG_ENDPOINTS", "true").lower() not in {"1", "true", "yes"}:
        raise HTTPException(status_code=404, detail="debug endpoints disabled")
    return process_event(payload)


@app.get("/ui", response_class=HTMLResponse)
def ui() -> str:
    stats_data = stats()
    route_data = routes()["routes"]
    event_data = store.list_events(limit=100, offset=0)

    status_cards = "".join(
        f"<div class='metric'><span>{escape(str(row['status']))}</span><strong>{row['count']}</strong></div>"
        for row in stats_data["by_status"]
    )
    route_rows = "".join(
        "<tr>"
        f"<td>{escape(str(route.get('name', '')))}</td>"
        f"<td>{'yes' if route.get('enabled', True) else 'no'}</td>"
        f"<td>{'yes' if route.get('dispatch_enabled') else 'no'}</td>"
        f"<td>{escape(str(route.get('event_type', '')))}</td>"
        f"<td>{escape(str(route.get('table_id', '')))}</td>"
        f"<td>{escape(str(route.get('n8n_webhook_url', '')))}</td>"
        "</tr>"
        for route in route_data
    )
    event_rows = "".join(format_event_row(row) for row in event_data["events"])

    return f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feishu Listener</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --text: #20242c;
      --muted: #626b7a;
      --line: #dfe4ec;
      --accent: #176b87;
      --ok: #1b7f4b;
      --warn: #9a5b00;
      --bad: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
    }}
    header {{
      padding: 20px 28px 14px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }}
    h1 {{ margin: 0 0 6px; font-size: 22px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 12px; font-size: 16px; }}
    main {{ padding: 20px 28px 32px; display: grid; gap: 18px; }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      overflow: hidden;
    }}
    .subtle {{ color: var(--muted); }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }}
    .metric {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 12px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 44px;
    }}
    .metric strong {{ font-size: 18px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; border-bottom: 1px solid var(--line); padding: 8px 9px; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; font-weight: 600; background: #fbfcfe; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
    .scroll {{ overflow-x: auto; }}
    .pill {{ display: inline-block; padding: 2px 7px; border-radius: 999px; background: #eef2f6; }}
    .pill.dispatching, .pill.success {{ color: var(--ok); background: #eaf7ef; }}
    .pill.ignored {{ color: var(--muted); }}
    .pill.failed, .pill.error {{ color: var(--bad); background: #fff0ee; }}
    .filters {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin-bottom: 12px; }}
    input, button {{
      width: 100%;
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 9px;
      background: #fff;
      color: var(--text);
    }}
    button {{ cursor: pointer; background: var(--accent); color: #fff; border-color: var(--accent); }}
    .detail {{
      white-space: pre-wrap;
      max-height: 520px;
      overflow: auto;
      background: #111827;
      color: #f9fafb;
      padding: 12px;
      border-radius: 6px;
      display: none;
    }}
    a {{ color: var(--accent); }}
  </style>
</head>
<body>
  <header>
    <h1>Feishu Listener</h1>
    <div class="subtle">ws_status={escape(str(state["ws_status"]))} · last_event_at={escape(str(state["last_event_at"]))}</div>
  </header>
  <main>
    <section>
      <h2>Overview</h2>
      <div class="grid">
        <div class="metric"><span>Events</span><strong>{stats_data["event_count"]}</strong></div>
        <div class="metric"><span>Dispatch Attempts</span><strong>{stats_data["dispatch_attempt_count"]}</strong></div>
        <div class="metric"><span>DB Size</span><strong>{format_bytes(stats_data["db_size_bytes"])}</strong></div>
        <div class="metric"><span>Retention</span><strong>{escape(str(stats_data["retention"].get("max_days")))}d / {escape(str(stats_data["retention"].get("max_events")))}</strong></div>
        {status_cards}
      </div>
    </section>

    <section>
      <h2>Routes</h2>
      <div class="scroll">
        <table>
          <thead><tr><th>Name</th><th>Enabled</th><th>Dispatch</th><th>Event Type</th><th>Table</th><th>Webhook</th></tr></thead>
          <tbody>{route_rows}</tbody>
        </table>
      </div>
    </section>

    <section>
      <h2>Events</h2>
      <div class="filters">
        <input id="status" placeholder="status">
        <input id="route_name" placeholder="route_name">
        <input id="table_id" placeholder="table_id">
        <input id="record_id" placeholder="record_id">
        <button onclick="loadEvents()">Filter</button>
      </div>
      <div class="scroll">
        <table>
          <thead><tr><th>ID</th><th>Created</th><th>Status</th><th>Dispatch</th><th>Route</th><th>Table</th><th>Record</th><th>Event</th></tr></thead>
          <tbody id="events">{event_rows}</tbody>
        </table>
      </div>
    </section>

    <section>
      <h2>Detail</h2>
      <pre id="detail" class="detail"></pre>
    </section>
  </main>
  <script>
    function cls(v) {{ return String(v || '').replace(/[^a-zA-Z0-9_-]/g, ''); }}
    function esc(v) {{
      return String(v ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
    }}
    async function loadEvents() {{
      const params = new URLSearchParams({{limit: '100'}});
      for (const key of ['status', 'route_name', 'table_id', 'record_id']) {{
        const value = document.getElementById(key).value.trim();
        if (value) params.set(key, value);
      }}
      const res = await fetch('/events?' + params.toString());
      const data = await res.json();
      document.getElementById('events').innerHTML = data.events.map(row => `
        <tr>
          <td><a href="#" onclick="showDetail(${{row.id}}); return false;">${{row.id}}</a></td>
          <td><code>${{esc(row.created_at)}}</code></td>
          <td><span class="pill ${{cls(row.status)}}">${{esc(row.status)}}</span></td>
          <td><span class="pill ${{cls(row.dispatch_status)}}">${{esc(row.dispatch_status || '')}}</span></td>
          <td>${{esc(row.route_name || '')}}</td>
          <td><code>${{esc(row.table_id || '')}}</code></td>
          <td><code>${{esc(row.record_id || '')}}</code></td>
          <td>${{esc(row.event_type || '')}}</td>
        </tr>
      `).join('');
    }}
    async function showDetail(id) {{
      const res = await fetch('/events/' + id);
      const data = await res.json();
      const el = document.getElementById('detail');
      el.style.display = 'block';
      el.textContent = JSON.stringify(data, null, 2);
      el.scrollIntoView({{behavior: 'smooth', block: 'start'}});
    }}
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
