import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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
MAX_EVENT_LIMIT = int(os.getenv("LISTENER_MAX_EVENT_LIMIT", "1000"))
MAX_FIELD_SCAN_ROWS = int(os.getenv("LISTENER_MAX_FIELD_SCAN_ROWS", "50000"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("feishu-listener")

app = FastAPI(title="Feishu Listener")
state = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "ws_status": "not_started",
    "chat_ws_status": "not_started",
    "chat_ws_pid": None,
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
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # WAL survives abrupt container kills better than rollback journal;
        # avoid VACUUM-on-every-cleanup which rewrites the whole file under load.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
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
            "is_deleted": "alter table events add column is_deleted integer not null default 0",
            "deleted_record_ids": "alter table events add column deleted_record_ids text",
            "deleted_by_open_id": "alter table events add column deleted_by_open_id text",
            "deleted_by_union_id": "alter table events add column deleted_by_union_id text",
            "deleted_by_user_id": "alter table events add column deleted_by_user_id text",
            "deleted_field_count": "alter table events add column deleted_field_count integer not null default 0",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)
                columns.add(column)

        conn.execute("create index if not exists idx_events_status on events(status)")
        conn.execute("create index if not exists idx_events_route_name on events(route_name)")
        conn.execute("create index if not exists idx_events_event_type on events(event_type)")
        conn.execute("create index if not exists idx_events_table_id on events(table_id)")
        conn.execute("create index if not exists idx_events_record_id on events(record_id)")
        conn.execute("create index if not exists idx_events_is_deleted on events(is_deleted)")
        conn.execute("create index if not exists idx_events_deleted_by_open_id on events(deleted_by_open_id)")
        conn.execute("create index if not exists idx_events_deleted_by_user_id on events(deleted_by_user_id)")

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
        self.backfill_deletion_metadata(conn)

    def backfill_deletion_metadata(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            select id, normalized_json, raw_json
            from events
            where normalized_json like '%record_deleted%'
               or raw_json like '%record_deleted%'
            """
        ).fetchall()
        for row in rows:
            normalized = parse_json(row["normalized_json"])
            raw = parse_json(row["raw_json"])
            payload, _ = preferred_raw_payload(normalized, raw)
            deletion = extract_deletion_info(payload)
            if not deletion.get("is_deleted"):
                continue
            deleted_by = deletion.get("deleted_by") or {}
            conn.execute(
                """
                update events
                set is_deleted = 1,
                    deleted_record_ids = ?,
                    deleted_by_open_id = ?,
                    deleted_by_union_id = ?,
                    deleted_by_user_id = ?,
                    deleted_field_count = ?
                where id = ?
                """,
                (
                    ",".join(deletion.get("record_ids") or []),
                    deleted_by.get("open_id"),
                    deleted_by.get("union_id"),
                    deleted_by.get("user_id"),
                    int(deletion.get("field_count") or 0),
                    row["id"],
                ),
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
    ) -> Optional[int]:
        """Insert event. Returns None if event_id already exists (Feishu redelivery)."""
        now = datetime.now(timezone.utc).isoformat()
        resource = normalized.get("resource") or {}
        deletion = extract_deletion_info(normalized.get("raw") or raw)
        deleted_by = deletion.get("deleted_by") or {}
        eid = str(normalized.get("event_id") or "").strip()
        with self._lock, self.connect() as conn:
            if eid:
                exists = conn.execute(
                    "select 1 from events where event_id = ? limit 1",
                    (eid,),
                ).fetchone()
                if exists is not None:
                    return None
            cur = conn.execute(
                """
                insert into events (
                    event_id, event_type, route_name, status, dispatch_enabled,
                    normalized_json, raw_json, raw_mode, error, created_at,
                    app_token, table_id, record_id, chat_id, command,
                    is_deleted, deleted_record_ids, deleted_by_open_id,
                    deleted_by_union_id, deleted_by_user_id, deleted_field_count
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    1 if deletion.get("is_deleted") else 0,
                    ",".join(deletion.get("record_ids") or []),
                    deleted_by.get("open_id"),
                    deleted_by.get("union_id"),
                    deleted_by.get("user_id"),
                    int(deletion.get("field_count") or 0),
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
        app_token: Optional[str] = None,
        table_id: Optional[str] = None,
        record_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        command: Optional[str] = None,
        deleted_only: bool = False,
        deleted_record_id: Optional[str] = None,
        deleted_by: Optional[str] = None,
        created_from: Optional[str] = None,
        created_to: Optional[str] = None,
        q: Optional[str] = None,
        field_id: Optional[str] = None,
        field_path: Optional[str] = None,
        field_key: Optional[str] = None,
        field_value: Optional[str] = None,
        match_source: str = "all",
        scan_limit: int = 10000,
        sort: str = "desc",
    ) -> Dict[str, Any]:
        clauses = []
        params: List[Any] = []

        filters = {
            "e.status": status,
            "e.route_name": route_name,
            "e.event_type": event_type,
            "e.app_token": app_token,
            "e.table_id": table_id,
            "e.record_id": record_id,
            "e.chat_id": chat_id,
            "e.command": command,
        }
        for column, value in filters.items():
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)

        if deleted_only:
            clauses.append("e.is_deleted = 1")
        if deleted_record_id:
            clauses.append("e.deleted_record_ids like ?")
            params.append(f"%{deleted_record_id.strip()}%")
        if deleted_by:
            deleted_by_like = f"%{deleted_by.strip()}%"
            clauses.append(
                "(e.deleted_by_open_id like ? or e.deleted_by_union_id like ? or e.deleted_by_user_id like ?)"
            )
            params.extend([deleted_by_like, deleted_by_like, deleted_by_like])

        if created_from:
            clauses.append("e.created_at >= ?")
            params.append(normalize_datetime_filter(created_from))
        if created_to:
            clauses.append("e.created_at <= ?")
            params.append(normalize_datetime_filter(created_to))

        if q:
            q_like = f"%{q.strip()}%"
            search_columns = [
                "e.event_id",
                "e.event_type",
                "e.route_name",
                "e.status",
                "e.error",
                "e.app_token",
                "e.table_id",
                "e.record_id",
                "e.chat_id",
                "e.command",
                "e.deleted_record_ids",
                "e.deleted_by_open_id",
                "e.deleted_by_union_id",
                "e.deleted_by_user_id",
                "e.created_at",
                "e.normalized_json",
                "e.raw_json",
            ]
            clauses.append("(" + " or ".join(f"{column} like ?" for column in search_columns) + ")")
            params.extend([q_like] * len(search_columns))

        field_search_active = any(
            bool((value or "").strip())
            for value in (field_id, field_path, field_key, field_value)
        )
        if field_search_active:
            prefilter_clauses = []
            prefilter_params: List[Any] = []
            for term in (field_id, field_key, field_value):
                text = (term or "").strip()
                if not text:
                    continue
                prefilter_clauses.append("(e.normalized_json like ? or e.raw_json like ?)")
                prefilter_params.extend([f"%{text}%", f"%{text}%"])
            if prefilter_clauses:
                clauses.append("(" + " and ".join(prefilter_clauses) + ")")
                params.extend(prefilter_params)

        where = f"where {' and '.join(clauses)}" if clauses else ""
        order_direction = "asc" if str(sort).lower() == "asc" else "desc"
        row_columns = """
            e.id, e.event_id, e.event_type, e.route_name, e.status,
            e.dispatch_enabled, e.raw_mode, e.error, e.created_at,
            e.app_token, e.table_id, e.record_id, e.chat_id, e.command,
            e.is_deleted, e.deleted_record_ids, e.deleted_by_open_id,
            e.deleted_by_union_id, e.deleted_by_user_id, e.deleted_field_count,
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
        """

        with self.connect() as conn:
            total = conn.execute(f"select count(*) from events e {where}", params).fetchone()[0]
            if field_search_active:
                scan_rows = min(max(scan_limit, limit + offset), MAX_FIELD_SCAN_ROWS)
                rows = conn.execute(
                    f"""
                    select {row_columns}, e.normalized_json, e.raw_json
                    from events e
                    {where}
                    order by e.id {order_direction}
                    limit ?
                    """,
                    [*params, scan_rows],
                ).fetchall()
                matched_total = 0
                events: List[Dict[str, Any]] = []
                for row in rows:
                    row_data = dict(row)
                    normalized = parse_json(row_data.pop("normalized_json", "{}"))
                    raw = parse_json(row_data.pop("raw_json", "{}"))
                    matches = find_event_matches(
                        normalized=normalized,
                        raw=raw,
                        field_id=field_id,
                        field_path=field_path,
                        field_key=field_key,
                        field_value=field_value,
                        source=match_source,
                        limit=50,
                    )
                    if not matches:
                        continue
                    if matched_total >= offset and len(events) < limit:
                        row_data["match_count"] = len(matches)
                        row_data["field_matches"] = matches[:8]
                        events.append(row_data)
                    matched_total += 1

                return {
                    "total": matched_total,
                    "candidate_total": total,
                    "limit": limit,
                    "offset": offset,
                    "sort": order_direction,
                    "events": events,
                    "field_search": {
                        "active": True,
                        "source": match_source,
                        "scanned": len(rows),
                        "scan_limit": scan_rows,
                        "truncated": total > len(rows),
                    },
                }

            rows = conn.execute(
                f"""
                select {row_columns}
                from events e
                {where}
                order by e.id {order_direction}
                limit ? offset ?
                """,
                [*params, limit, offset],
            ).fetchall()

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "sort": order_direction,
            "events": [dict(row) for row in rows],
            "field_search": {
                "active": False,
                "source": match_source,
                "scanned": 0,
                "scan_limit": scan_limit,
                "truncated": False,
            },
        }

    def get_event(
        self,
        event_id: int,
        field_id: Optional[str] = None,
        field_path: Optional[str] = None,
        field_key: Optional[str] = None,
        field_value: Optional[str] = None,
        q: Optional[str] = None,
        match_source: str = "all",
    ) -> Dict[str, Any]:
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
        detail_raw, detail_root = preferred_raw_payload(data["normalized"], data["raw"])
        data["deletion"] = extract_deletion_info(detail_raw, root_path=detail_root)
        data["field_changes"] = extract_bitable_field_changes(detail_raw, root_path=detail_root)
        data["field_matches"] = find_event_matches(
            normalized=data["normalized"],
            raw=data["raw"],
            field_id=field_id,
            field_path=field_path,
            field_key=field_key,
            field_value=field_value,
            q=q,
            source=match_source,
            limit=300,
        )
        return data

    def stats(self) -> Dict[str, Any]:
        with self.connect() as conn:
            event_count = conn.execute("select count(*) from events").fetchone()[0]
            attempt_count = conn.execute("select count(*) from dispatch_attempts").fetchone()[0]
            deleted_count = conn.execute("select count(*) from events where is_deleted = 1").fetchone()[0]
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
            bounds = conn.execute(
                "select min(created_at) as first_event_at, max(created_at) as last_event_at from events"
            ).fetchone()

        return {
            "db_path": str(self.path),
            "db_size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "event_count": event_count,
            "dispatch_attempt_count": attempt_count,
            "deleted_event_count": deleted_count,
            "first_event_at": bounds["first_event_at"] if bounds else None,
            "last_event_at": bounds["last_event_at"] if bounds else None,
            "by_status": by_status,
            "by_route": by_route,
            "recent_dispatch_attempts": recent_attempts,
        }

    def cleanup(self, retention: Dict[str, Any]) -> Dict[str, Any]:
        max_days = int(retention.get("max_days", 7))
        max_events = int(retention.get("max_events", 5000))
        vacuum = bool(retention.get("vacuum_after_cleanup", False))
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

    def compact_ignored_raw(self, vacuum: bool = False) -> Dict[str, Any]:
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
    # Default off: VACUUM rewrites the whole DB and is unsafe under Docker recreate.
    data["retention"].setdefault("vacuum_after_cleanup", False)
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


def normalize_datetime_filter(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return text
    if dt.tzinfo is None:
        return dt.isoformat()
    return dt.astimezone(timezone.utc).isoformat()


def contains_ci(haystack: Any, needle: Optional[str]) -> bool:
    text = str(needle or "").strip()
    if not text:
        return True
    return text.casefold() in str(haystack or "").casefold()


def trim_text(value: str, limit: int = 260) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


def json_preview(value: Any, limit: int = 260) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return trim_text(value.strip(), limit)
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return trim_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), limit)
    except Exception:
        return trim_text(str(value), limit)


def path_leaf(path: str) -> str:
    if not path:
        return ""
    segment = path.rsplit(".", 1)[-1]
    if "[" in segment and not segment.startswith("["):
        return segment.split("[", 1)[0]
    return segment


def iter_json_paths(value: Any, path: str) -> List[Tuple[str, str, Any]]:
    items: List[Tuple[str, str, Any]] = [(path, path_leaf(path), value)]
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            items.extend(iter_json_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            items.extend(iter_json_paths(child, child_path))
    return items


def find_json_matches(
    normalized: Dict[str, Any],
    raw: Dict[str, Any],
    field_path: Optional[str] = None,
    field_key: Optional[str] = None,
    field_value: Optional[str] = None,
    q: Optional[str] = None,
    source: str = "all",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    if not any(bool((value or "").strip()) for value in (field_path, field_key, field_value, q)):
        return []

    source = source if source in {"all", "normalized", "raw"} else "all"
    roots: List[Tuple[str, Dict[str, Any]]] = []
    if source in {"all", "normalized"}:
        roots.append(("normalized", normalized))
    if source in {"all", "raw"}:
        roots.append(("raw", raw))

    matches: List[Dict[str, Any]] = []
    for root_name, root_value in roots:
        for path, key, value in iter_json_paths(root_value, root_name):
            preview = json_preview(value)
            is_container = isinstance(value, (dict, list))
            path_ok = contains_ci(path, field_path)
            key_ok = contains_ci(key, field_key) or contains_ci(path, field_key)
            value_ok = contains_ci(preview, field_value)
            q_ok = contains_ci(path, q) or contains_ci(key, q) or contains_ci(preview, q)
            if not (path_ok and key_ok and value_ok and q_ok):
                continue
            if is_container and not (field_path or field_key):
                continue
            matches.append(
                {
                    "kind": "json_path",
                    "source": root_name,
                    "path": path,
                    "key": key,
                    "value": preview,
                    "value_type": type(value).__name__,
                }
            )
            if len(matches) >= limit:
                return matches
    return matches


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


def _extract_post_text_and_media(
    content_obj: Dict[str, Any],
) -> Tuple[str, List[str], List[str]]:
    """Parse Feishu post (rich text) into plain text, image_keys, and file_keys."""
    texts: List[str] = []
    image_keys: List[str] = []
    file_keys: List[str] = []
    title = str(content_obj.get("title") or "").strip()
    if title:
        texts.append(title)
    rows = content_obj.get("content")
    if not isinstance(rows, list):
        return "\n".join(texts), image_keys, file_keys
    for row in rows:
        if not isinstance(row, list):
            continue
        line_parts: List[str] = []
        for item in row:
            if not isinstance(item, dict):
                continue
            tag = item.get("tag")
            if tag == "text":
                line_parts.append(str(item.get("text") or ""))
            elif tag == "a":
                line_parts.append(str(item.get("text") or item.get("href") or ""))
            elif tag == "at":
                line_parts.append(str(item.get("user_name") or ""))
            elif tag == "img":
                key = str(item.get("image_key") or "").strip()
                if key and key not in image_keys:
                    image_keys.append(key)
            elif tag == "file":
                key = str(item.get("file_key") or "").strip()
                if key and key not in file_keys:
                    file_keys.append(key)
            elif tag == "media":
                file_key = str(item.get("file_key") or "").strip()
                image_key = str(item.get("image_key") or "").strip()
                if file_key:
                    if file_key not in file_keys:
                        file_keys.append(file_key)
                elif image_key and image_key not in image_keys:
                    image_keys.append(image_key)
        line = "".join(line_parts).strip()
        if line:
            texts.append(line)
    return "\n".join(texts), image_keys, file_keys


def _extract_post_text_and_images(content_obj: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Backward-compatible wrapper: plain text + image_key list."""
    text, image_keys, _file_keys = _extract_post_text_and_media(content_obj)
    return text, image_keys


def normalize_event(raw_input: Any) -> Dict[str, Any]:
    raw = as_dict(raw_input)
    header = raw.get("header") or {}
    event = raw.get("event") or {}
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}

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
    image_keys: List[str] = []
    file_keys: List[str] = []
    content = message.get("content")
    content_obj: Dict[str, Any] = {}
    message_type = str(message.get("message_type") or "")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                content_obj = parsed
                text = str(parsed.get("text") or "")
            else:
                text = content
        except Exception:
            text = content
    elif isinstance(content, dict):
        content_obj = content
        text = str(content.get("text") or "")

    if content_obj.get("image_key"):
        image_keys.append(str(content_obj["image_key"]))

    if message_type == "file":
        key = str(content_obj.get("file_key") or "").strip()
        if key and key not in file_keys:
            file_keys.append(key)

    if message_type == "post" or (
        isinstance(content_obj.get("content"), list) and not text
    ):
        post_text, post_images, post_files = _extract_post_text_and_media(content_obj)
        if post_text:
            text = post_text
        for key in post_images:
            if key not in image_keys:
                image_keys.append(key)
        for key in post_files:
            if key not in file_keys:
                file_keys.append(key)

    # Some clients put file_key on top-level content without message_type=file
    top_file_key = str(content_obj.get("file_key") or "").strip()
    if top_file_key and top_file_key not in file_keys and message_type != "image":
        file_keys.append(top_file_key)

    command = text.strip().split()[0] if text.strip().startswith("/") else ""
    mentions = message.get("mentions") or []
    if not isinstance(mentions, list):
        mentions = []

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
            "chat_type": str(message.get("chat_type") or ""),
            "message_id": str(message.get("message_id") or ""),
            "message_type": message_type,
            "image_key": image_keys[0] if image_keys else "",
            "image_keys": image_keys,
            "file_key": file_keys[0] if file_keys else "",
            "file_keys": file_keys,
            "open_id": str(
                sender_id.get("open_id")
                or sender.get("open_id")
                or event.get("open_id")
                or ""
            ),
            "sender_type": str(sender.get("sender_type") or ""),
            "mentions": mentions,
            "text": text,
            "content": content_obj or content,
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


def format_route_actions(actions: Any) -> str:
    if actions is None:
        return "-"
    if isinstance(actions, list):
        text = ", ".join(str(item) for item in actions if str(item).strip())
        return text or "-"
    text = str(actions).strip()
    return text or "-"


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
    match_count = row.get("match_count") or ""
    deleted_pill = "<span class='pill deleted'>deleted</span>" if row.get("is_deleted") else ""
    deleted_by = (
        row.get("deleted_by_user_id")
        or row.get("deleted_by_open_id")
        or row.get("deleted_by_union_id")
        or ""
    )
    return (
        f"<tr class='{'deleted-row' if row.get('is_deleted') else ''}'>"
        f"<td><a href='#' onclick='showDetail({int(row.get('id'))}); return false;'>{int(row.get('id'))}</a></td>"
        f"<td><code>{escape(str(row.get('created_at') or ''))}</code></td>"
        f"<td><span class='pill {escape(str(row.get('status') or ''))}'>{status}</span></td>"
        f"<td>{deleted_pill}</td>"
        f"<td><code>{escape(str(deleted_by))}</code></td>"
        f"<td><span class='pill {escape(str(row.get('dispatch_status') or ''))}'>{dispatch_status}</span></td>"
        f"<td>{escape(str(row.get('route_name') or ''))}</td>"
        f"<td><code>{escape(str(row.get('table_id') or ''))}</code></td>"
        f"<td><code>{escape(str(row.get('record_id') or ''))}</code></td>"
        f"<td>{escape(str(row.get('event_type') or ''))}</td>"
        f"<td>{escape(str(match_count))}</td>"
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


def field_value_text(value: Any) -> str:
    tokens = extract_field_tokens(value)
    if tokens:
        return trim_text(", ".join(tokens[:12]), 320)
    return json_preview(value, 320)


def preferred_raw_payload(normalized: Dict[str, Any], raw: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    normalized_raw = (normalized or {}).get("raw")
    if isinstance(normalized_raw, dict) and isinstance(normalized_raw.get("event"), dict):
        return normalized_raw, "normalized.raw"
    return raw or {}, "raw"


def operator_identity(raw: Dict[str, Any]) -> Dict[str, Any]:
    event = (raw or {}).get("event") or {}
    operator = event.get("operator_id") or event.get("operator") or {}
    if not isinstance(operator, dict):
        return {"value": str(operator)}
    user_id = operator.get("user_id")
    if isinstance(user_id, dict):
        user_id = user_id.get("user_id") or user_id.get("open_id") or user_id.get("union_id")
    identity = {
        "open_id": operator.get("open_id"),
        "union_id": operator.get("union_id"),
        "user_id": user_id,
        "name": operator.get("name") or operator.get("en_name") or operator.get("enName"),
    }
    display = identity.get("name") or identity.get("user_id") or identity.get("open_id") or identity.get("union_id") or ""
    identity["display"] = display
    return identity


def is_delete_action(action: Dict[str, Any]) -> bool:
    action_name = str(action.get("action") or action.get("type") or "").casefold()
    return action_name in {"record_deleted", "record_delete", "delete_record"} or (
        "delete" in action_name and "record" in action_name
    )


def extract_record_fields(
    action: Dict[str, Any],
    action_index: int,
    value_key: str,
    summary_key: str,
    root_path: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    fields: List[Dict[str, Any]] = []
    values = action.get(value_key)
    if isinstance(values, list) and values:
        for field_index, field in enumerate(values):
            if not isinstance(field, dict):
                continue
            raw_value = field.get("field_value")
            identity_value = field.get("field_identity_value")
            fields.append(
                {
                    "field_index": field_index,
                    "field_id": str(field.get("field_id") or ""),
                    "path": f"{root_path}.event.action_list[{action_index}].{value_key}[{field_index}]",
                    "value_text": field_value_text(raw_value),
                    "identity_text": field_value_text(identity_value),
                    "raw_value": raw_value,
                    "identity_value": identity_value,
                    "summary": False,
                }
            )
        return fields, False

    field_ids = action.get(summary_key) or []
    if isinstance(field_ids, list) and field_ids:
        for field_index, field_id in enumerate(field_ids):
            fields.append(
                {
                    "field_index": field_index,
                    "field_id": str(field_id or ""),
                    "path": f"{root_path}.event.action_list[{action_index}].{summary_key}[{field_index}]",
                    "value_text": "summary only",
                    "identity_text": "",
                    "raw_value": None,
                    "identity_value": None,
                    "summary": True,
                }
            )
    return fields, True


def extract_deletion_info(raw: Dict[str, Any], root_path: str = "raw") -> Dict[str, Any]:
    raw_event = (raw or {}).get("event") or {}
    actions = raw_event.get("action_list") or []
    deleted_records: List[Dict[str, Any]] = []

    if isinstance(actions, list):
        for action_index, action in enumerate(actions):
            if not isinstance(action, dict) or not is_delete_action(action):
                continue
            before_fields, before_summary = extract_record_fields(
                action,
                action_index,
                "before_value",
                "before_field_ids",
                root_path,
            )
            after_fields, after_summary = extract_record_fields(
                action,
                action_index,
                "after_value",
                "after_field_ids",
                root_path,
            )
            deleted_records.append(
                {
                    "action_index": action_index,
                    "action": action.get("action") or "",
                    "record_id": str(action.get("record_id") or ""),
                    "before_field_count": len(before_fields),
                    "after_field_count": len(after_fields),
                    "before_summary": before_summary,
                    "after_summary": after_summary,
                    "before_fields": before_fields,
                    "after_fields": after_fields,
                    "path": f"{root_path}.event.action_list[{action_index}]",
                }
            )

    record_ids = [record["record_id"] for record in deleted_records if record.get("record_id")]
    return {
        "is_deleted": bool(deleted_records),
        "record_ids": record_ids,
        "record_count": len(deleted_records),
        "field_count": sum(int(record.get("before_field_count") or 0) for record in deleted_records),
        "deleted_by": operator_identity(raw),
        "table_id": raw_event.get("table_id"),
        "file_token": raw_event.get("file_token") or raw_event.get("app_token"),
        "revision": raw_event.get("revision"),
        "update_time": raw_event.get("update_time"),
        "records": deleted_records,
        "source_path": root_path,
    }


def extract_bitable_field_changes(
    raw: Dict[str, Any],
    max_changes: int = 1000,
    root_path: str = "raw",
) -> List[Dict[str, Any]]:
    raw_event = (raw or {}).get("event") or {}
    actions = raw_event.get("action_list") or []
    changes: List[Dict[str, Any]] = []

    if not isinstance(actions, list):
        return changes

    for action_index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        for value_key, side, summary_key in (
            ("before_value", "before", "before_field_ids"),
            ("after_value", "after", "after_field_ids"),
        ):
            values = action.get(value_key)
            if isinstance(values, list) and values:
                for field_index, field in enumerate(values):
                    if not isinstance(field, dict):
                        continue
                    raw_value = field.get("field_value")
                    identity_value = field.get("field_identity_value")
                    changes.append(
                        {
                            "kind": "bitable_field",
                            "action_index": action_index,
                            "field_index": field_index,
                            "action": action.get("action") or "",
                            "record_id": action.get("record_id") or "",
                            "side": side,
                            "field_id": str(field.get("field_id") or ""),
                            "path": f"{root_path}.event.action_list[{action_index}].{value_key}[{field_index}]",
                            "value_text": field_value_text(raw_value),
                            "identity_text": field_value_text(identity_value),
                            "raw_value": raw_value,
                            "identity_value": identity_value,
                            "summary": False,
                        }
                    )
                    if len(changes) >= max_changes:
                        return changes
                continue

            field_ids = action.get(summary_key) or []
            if not isinstance(field_ids, list):
                continue
            for field_index, field_id in enumerate(field_ids):
                changes.append(
                    {
                        "kind": "bitable_field",
                        "action_index": action_index,
                        "field_index": field_index,
                        "action": action.get("action") or "",
                        "record_id": action.get("record_id") or "",
                        "side": side,
                        "field_id": str(field_id or ""),
                        "path": f"{root_path}.event.action_list[{action_index}].{summary_key}[{field_index}]",
                        "value_text": "summary only",
                        "identity_text": "",
                        "raw_value": None,
                        "identity_value": None,
                        "summary": True,
                    }
                )
                if len(changes) >= max_changes:
                    return changes

    return changes


def bitable_change_matches(
    change: Dict[str, Any],
    field_id: Optional[str] = None,
    field_path: Optional[str] = None,
    field_key: Optional[str] = None,
    field_value: Optional[str] = None,
    q: Optional[str] = None,
) -> bool:
    value_blob = " ".join(
        [
            str(change.get("value_text") or ""),
            str(change.get("identity_text") or ""),
            json_preview(change.get("raw_value"), 500),
            json_preview(change.get("identity_value"), 500),
        ]
    )
    field_blob = " ".join(
        [
            str(change.get("field_id") or ""),
            str(change.get("path") or ""),
            str(change.get("action") or ""),
            str(change.get("record_id") or ""),
            str(change.get("side") or ""),
            value_blob,
        ]
    )

    if field_id and not contains_ci(change.get("field_id"), field_id):
        return False
    if field_path and not contains_ci(change.get("path"), field_path):
        return False
    if field_key and not (
        contains_ci(change.get("field_id"), field_key)
        or contains_ci("field_id", field_key)
        or contains_ci("field_value", field_key)
        or contains_ci(change.get("path"), field_key)
    ):
        return False
    if field_value and not contains_ci(value_blob, field_value):
        return False
    if q and not contains_ci(field_blob, q):
        return False
    return True


def find_event_matches(
    normalized: Dict[str, Any],
    raw: Dict[str, Any],
    field_id: Optional[str] = None,
    field_path: Optional[str] = None,
    field_key: Optional[str] = None,
    field_value: Optional[str] = None,
    q: Optional[str] = None,
    source: str = "all",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    if not any(bool((value or "").strip()) for value in (field_id, field_path, field_key, field_value, q)):
        return []

    matches: List[Dict[str, Any]] = []
    if source in {"all", "raw"}:
        for change in extract_bitable_field_changes(raw):
            if not bitable_change_matches(
                change,
                field_id=field_id,
                field_path=field_path,
                field_key=field_key,
                field_value=field_value,
                q=q,
            ):
                continue
            matches.append(
                {
                    "kind": "bitable_field",
                    "source": "raw",
                    "path": change["path"],
                    "key": change["field_id"],
                    "value": change["value_text"],
                    "identity": change["identity_text"],
                    "action": change["action"],
                    "record_id": change["record_id"],
                    "side": change["side"],
                    "summary": change["summary"],
                }
            )
            if len(matches) >= limit:
                return matches

    remaining = limit - len(matches)
    if remaining <= 0:
        return matches
    matches.extend(
        find_json_matches(
            normalized=normalized,
            raw=raw,
            field_path=field_path,
            field_key=field_key,
            field_value=field_value or field_id,
            q=q,
            source=source,
            limit=remaining,
        )
    )
    return matches


ACTION_ADDED_ALIASES = {"record_added", "added", "new"}
ACTION_EDITED_ALIASES = {"record_edited", "edited", "update", "updated"}
ACTION_DELETED_ALIASES = {"record_deleted", "record_delete", "delete_record", "deleted", "delete"}


def canonical_action_name(raw: str) -> str:
    name = str(raw or "").strip().casefold()
    if not name:
        return ""
    if name in ACTION_ADDED_ALIASES:
        return "record_added"
    if name in ACTION_EDITED_ALIASES:
        return "record_edited"
    if name in ACTION_DELETED_ALIASES:
        return "record_deleted"
    if "delete" in name and "record" in name:
        return "record_deleted"
    if "add" in name and "record" in name:
        return "record_added"
    if "edit" in name and "record" in name:
        return "record_edited"
    return name


def action_dict_name(action: Dict[str, Any]) -> str:
    if not isinstance(action, dict):
        return ""
    if is_delete_action(action):
        return "record_deleted"
    raw_name = str(action.get("action") or action.get("type") or "").strip()
    return canonical_action_name(raw_name)


def route_allowed_actions(route: Dict[str, Any]) -> Optional[Set[str]]:
    configured = route.get("actions")
    if configured is None:
        return None
    items = configured if isinstance(configured, list) else [configured]
    allowed: Set[str] = set()
    for item in items:
        name = canonical_action_name(str(item))
        if name:
            allowed.add(name)
    return allowed if allowed else None


def event_action_names(event: Dict[str, Any]) -> List[str]:
    raw_event = (event.get("raw") or {}).get("event") or {}
    actions = raw_event.get("action_list") or []
    names: List[str] = []
    if not isinstance(actions, list):
        return names
    for action in actions:
        if not isinstance(action, dict):
            continue
        name = action_dict_name(action)
        if name and name not in names:
            names.append(name)
    return names


def actions_match(route: Dict[str, Any], event: Dict[str, Any]) -> bool:
    allowed = route_allowed_actions(route)
    if allowed is None:
        return True
    event_names = event_action_names(event)
    return any(name in allowed for name in event_names)


def _collect_field_tokens_from_values(
    values: Any,
    field_id: str,
    tokens: List[str],
) -> None:
    if not isinstance(values, list):
        return
    for field in values:
        if not isinstance(field, dict):
            continue
        if str(field.get("field_id") or "") != str(field_id):
            continue
        for value in extract_field_tokens(field.get("field_value")):
            if value not in tokens:
                tokens.append(value)
        for value in extract_field_tokens(field.get("field_identity_value")):
            if value not in tokens:
                tokens.append(value)


def action_field_tokens(
    event: Dict[str, Any],
    field_id: str,
    allowed_actions: Optional[Set[str]] = None,
) -> List[str]:
    raw_event = (event.get("raw") or {}).get("event") or {}
    actions = raw_event.get("action_list") or []
    tokens: List[str] = []

    if not isinstance(actions, list):
        return tokens

    for action in actions:
        if not isinstance(action, dict):
            continue
        action_name = action_dict_name(action)
        if allowed_actions is not None and action_name not in allowed_actions:
            continue
        _collect_field_tokens_from_values(action.get("after_value"), field_id, tokens)
        if action_name == "record_deleted":
            _collect_field_tokens_from_values(action.get("before_value"), field_id, tokens)

    return tokens


def field_condition_matches(
    event: Dict[str, Any],
    condition: Dict[str, Any],
    allowed_actions: Optional[Set[str]] = None,
) -> bool:
    field_id = condition.get("field_id")
    if not field_id:
        return False

    actual_tokens = action_field_tokens(event, str(field_id), allowed_actions)
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
    if not conditions:
        return True

    allowed_actions = route_allowed_actions(route)
    for condition in conditions:
        if not isinstance(condition, dict):
            return False
        if not field_condition_matches(event, condition, allowed_actions):
            return False
    return True


def _value_matches(expected: Any, actual: Any) -> bool:
    """Return True if actual matches expected (scalar or allowed-list)."""
    if expected is None:
        return True
    if isinstance(expected, (list, tuple, set)):
        allowed = {str(item) for item in expected}
        return str(actual) in allowed
    return str(expected) == str(actual)


def route_matches(route: Dict[str, Any], event: Dict[str, Any]) -> bool:
    if not route.get("enabled", True):
        return False

    resource = event.get("resource") or {}
    checks: List[Tuple[str, Any, Any]] = [
        ("event_type", route.get("event_type"), event.get("event_type")),
        ("app_id", route.get("app_id"), event.get("app_id")),
        ("app_token", route.get("app_token"), resource.get("app_token") or resource.get("file_token")),
        ("file_token", route.get("file_token"), resource.get("file_token")),
        ("table_id", route.get("table_id"), resource.get("table_id")),
        ("chat_id", route.get("chat_id"), resource.get("chat_id")),
        ("command", route.get("command"), event.get("command")),
    ]
    for _, expected, actual in checks:
        if expected is None:
            continue
        # support either a single expected value or a list/tuple/set of allowed values
        if isinstance(expected, (list, tuple, set)):
            allowed = {str(item) for item in expected}
            if str(actual) not in allowed:
                return False
        else:
            if str(expected) != str(actual):
                return False

    text_contains = route.get("text_contains")
    if text_contains is not None and str(text_contains) != "":
        haystack = str(resource.get("text") or "")
        if str(text_contains) not in haystack:
            return False

    if not actions_match(route, event):
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
    event_id = str(normalized.get("event_id") or "").strip()

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
    # Deduplicate Feishu redeliveries of the same event_id (common when the
    # WS handler previously blocked on a slow n8n webhook before ACKing).
    event_row_id = store.insert_event(
        normalized=normalized,
        raw=raw_payload,
        route_name=route_name,
        status=status,
        dispatch_enabled=dispatch_enabled,
        raw_mode=raw_mode,
    )
    if event_row_id is None:
        logger.info(
            "duplicate event skipped event_id=%s event_type=%s",
            event_id,
            normalized.get("event_type"),
        )
        return {
            "event_row_id": None,
            "status": "duplicate",
            "route_name": route_name,
            "dispatch_enabled": False,
            "cleanup": None,
            "normalized": normalized,
        }

    state["last_event_at"] = datetime.now(timezone.utc).isoformat()

    if route and dispatch_enabled:
        # Return from WS handler ASAP so Feishu ACKs the event; dispatch in background.
        def _bg_dispatch() -> None:
            try:
                dispatch_to_n8n(store, event_row_id, route, normalized)
            except Exception as exc:
                state["last_error"] = str(exc)
                logger.exception("dispatch failed event_id=%s", event_id)

        threading.Thread(
            target=_bg_dispatch,
            name=f"dispatch-{route_name or 'route'}-{event_row_id}",
            daemon=True,
        ).start()

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

    def handle_im_message(data: Any) -> None:
        # Main app (n8n) may also receive IM in groups where it is the bot.
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
    builder = builder.register_p2_im_message_receive_v1(handle_im_message)
    return builder.build()


def build_chat_lark_event_handler() -> Any:
    """Handler for the dedicated chat app: IM messages only."""
    if lark is None:
        raise RuntimeError("lark_oapi is not installed")

    verification_token = os.getenv(
        "FEISHU_CHAT_VERIFICATION_TOKEN",
        os.getenv("FEISHU_VERIFICATION_TOKEN", ""),
    )
    encrypt_key = os.getenv(
        "FEISHU_CHAT_ENCRYPT_KEY",
        os.getenv("FEISHU_ENCRYPT_KEY", ""),
    )

    def handle_im_message(data: Any) -> None:
        process_event(data)

    builder = lark.EventDispatcherHandler.builder(verification_token, encrypt_key)
    builder = builder.register_p2_im_message_receive_v1(handle_im_message)
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


_chat_ws_proc: Optional[Any] = None


def _chat_ws_enabled() -> bool:
    """对话应用长连接开关。切流到 feishu-cardbot 时设 FEISHU_CHAT_WS_ENABLED=false。"""
    explicit = os.getenv("FEISHU_CHAT_WS_ENABLED")
    if explicit is not None and explicit.strip() != "":
        return explicit.strip().lower() in {"1", "true", "yes", "on"}
    return os.getenv("LISTENER_DISABLE_CHAT_WS", "").lower() not in {"1", "true", "yes"}


def start_chat_ws_client() -> None:
    """Spawn chat-app WS in a child process (lark SDK cannot run two Clients in one process)."""
    global _chat_ws_proc
    if not _chat_ws_enabled():
        state["chat_ws_status"] = "disabled"
        logger.info(
            "chat websocket listener disabled by FEISHU_CHAT_WS_ENABLED/LISTENER_DISABLE_CHAT_WS"
        )
        return

    app_id = os.getenv("FEISHU_CHAT_APP_ID", "")
    app_secret = os.getenv("FEISHU_CHAT_APP_SECRET", "")
    if not app_id or not app_secret:
        state["chat_ws_status"] = "missing_credentials"
        logger.warning(
            "FEISHU_CHAT_APP_ID or FEISHU_CHAT_APP_SECRET is missing; chat websocket not started"
        )
        return

    try:
        import subprocess
        import sys

        script = Path(__file__).resolve().parent / "chat_ws.py"
        state["chat_ws_status"] = "starting"
        _chat_ws_proc = subprocess.Popen(
            [sys.executable, str(script)],
            env=os.environ.copy(),
        )
        state["chat_ws_status"] = "running"
        state["chat_ws_pid"] = _chat_ws_proc.pid
        logger.info(
            "chat websocket subprocess started pid=%s app_id=%s",
            _chat_ws_proc.pid,
            app_id,
        )
    except Exception as exc:
        state["chat_ws_status"] = "error"
        state["last_error"] = str(exc)
        logger.exception("chat websocket subprocess failed to start")


@app.on_event("startup")
def on_startup() -> None:
    load_config()
    thread = threading.Thread(target=start_ws_client, name="feishu-ws", daemon=True)
    thread.start()
    chat_thread = threading.Thread(
        target=start_chat_ws_client, name="feishu-chat-ws-launcher", daemon=True
    )
    chat_thread.start()


@app.get("/health")
def health() -> Dict[str, Any]:
    chat_status = state["chat_ws_status"]
    pid = state.get("chat_ws_pid")
    if _chat_ws_proc is not None and _chat_ws_proc.poll() is not None:
        chat_status = "exited"
        state["chat_ws_status"] = chat_status
    return {
        "ok": store.is_writable(),
        "service": "feishu-listener",
        "started_at": state["started_at"],
        "ws_status": state["ws_status"],
        "chat_ws_status": chat_status,
        "chat_ws_pid": pid,
        "last_event_at": state["last_event_at"],
        "last_error": state["last_error"],
        "db_path": str(DB_PATH),
        "last_cleanup_at": cleanup_state["last_cleanup_at"],
        "last_cleanup_result": cleanup_state["last_cleanup_result"],
    }


@app.post("/internal/ingest")
def internal_ingest(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Receive events from chat_ws subprocess (same host)."""
    return process_event(payload)


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
    limit: int = Query(100, ge=1, le=MAX_EVENT_LIMIT),
    offset: int = Query(0, ge=0),
    status: Optional[str] = None,
    route_name: Optional[str] = None,
    event_type: Optional[str] = None,
    app_token: Optional[str] = None,
    table_id: Optional[str] = None,
    record_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    command: Optional[str] = None,
    deleted_only: bool = False,
    deleted_record_id: Optional[str] = None,
    deleted_by: Optional[str] = None,
    created_from: Optional[str] = None,
    created_to: Optional[str] = None,
    q: Optional[str] = None,
    field_id: Optional[str] = None,
    field_path: Optional[str] = None,
    field_key: Optional[str] = None,
    field_value: Optional[str] = None,
    match_source: str = "all",
    scan_limit: int = Query(10000, ge=100, le=MAX_FIELD_SCAN_ROWS),
    sort: str = "desc",
) -> Dict[str, Any]:
    return store.list_events(
        limit=limit,
        offset=offset,
        status=status,
        route_name=route_name,
        event_type=event_type,
        app_token=app_token,
        table_id=table_id,
        record_id=record_id,
        chat_id=chat_id,
        command=command,
        deleted_only=deleted_only,
        deleted_record_id=deleted_record_id,
        deleted_by=deleted_by,
        created_from=created_from,
        created_to=created_to,
        q=q,
        field_id=field_id,
        field_path=field_path,
        field_key=field_key,
        field_value=field_value,
        match_source=match_source,
        scan_limit=scan_limit,
        sort=sort,
    )


@app.get("/events/{event_row_id}")
def event_detail(
    event_row_id: int,
    q: Optional[str] = None,
    field_id: Optional[str] = None,
    field_path: Optional[str] = None,
    field_key: Optional[str] = None,
    field_value: Optional[str] = None,
    match_source: str = "all",
) -> Dict[str, Any]:
    try:
        return store.get_event(
            event_row_id,
            q=q,
            field_id=field_id,
            field_path=field_path,
            field_key=field_key,
            field_value=field_value,
            match_source=match_source,
        )
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
    vacuum = bool(config.get("retention", {}).get("vacuum_after_cleanup", False))
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
    initial_json = json.dumps(
        {
            "events": event_data,
            "stats": stats_data,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")

    status_cards = "".join(
        f"<div class='metric'><span>{escape(str(row['status']))}</span><strong>{row['count']}</strong></div>"
        for row in stats_data["by_status"]
    )
    route_rows = "".join(
        "<tr>"
        f"<td>{escape(str(route.get('name', '')))}</td>"
        f"<td>{'yes' if route.get('enabled', True) else 'no'}</td>"
        f"<td>{'yes' if route.get('dispatch_enabled') else 'no'}</td>"
        f"<td>{escape(format_route_actions(route.get('actions')))}</td>"
        f"<td>{escape(str(route.get('event_type', '')))}</td>"
        f"<td>{escape(str(route.get('table_id', '')))}</td>"
        f"<td>{escape(str(route.get('n8n_webhook_url', '')))}</td>"
        "</tr>"
        for route in route_data
    )
    event_rows = "".join(format_event_row(row) for row in event_data["events"])
    limit_options = "".join(
        f"<option value='{value}' {'selected' if value == 100 else ''}>{value}</option>"
        for value in (25, 50, 100, 250, 500, 1000)
        if value <= MAX_EVENT_LIMIT
    )
    scan_options = "".join(
        f"<option value='{value}' {'selected' if value == 10000 else ''}>{value}</option>"
        for value in (1000, 5000, 10000, 25000, 50000)
        if value <= MAX_FIELD_SCAN_ROWS
    )

    html = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feishu Listener</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef1f4;
      --panel: #ffffff;
      --panel-soft: #f7f9fb;
      --text: #20242c;
      --muted: #626b7a;
      --line: #dfe4ec;
      --accent: #176b87;
      --accent-soft: #e8f4f7;
      --violet: #6457a6;
      --ok: #1b7f4b;
      --warn: #9a5b00;
      --bad: #b42318;
      --mark: #fff1a8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
    }
    header {
      padding: 18px 28px 14px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-end;
      flex-wrap: wrap;
    }
    h1 { margin: 0 0 6px; font-size: 22px; letter-spacing: 0; }
    h2 { margin: 0 0 12px; font-size: 16px; }
    h3 { margin: 0 0 10px; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0; }
    main { padding: 0 0 36px; }
    section {
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      padding: 18px 28px;
      overflow: hidden;
    }
    .subtle { color: var(--muted); }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; }
    .metric {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 12px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 44px;
      background: var(--panel);
    }
    .metric strong { font-size: 18px; }
    .metric span { color: var(--muted); }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; border-bottom: 1px solid var(--line); padding: 8px 9px; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; font-weight: 600; background: #fbfcfe; position: sticky; top: 0; z-index: 1; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .scroll { overflow: auto; }
    .event-scroll { max-height: 520px; border: 1px solid var(--line); }
    .event-table tr.selected { background: var(--accent-soft); }
    .pill { display: inline-block; padding: 2px 7px; border-radius: 999px; background: #eef2f6; white-space: nowrap; }
    .pill.dispatching, .pill.success { color: var(--ok); background: #eaf7ef; }
    .pill.matched_dispatch_disabled { color: var(--warn); background: #fff5df; }
    .pill.ignored { color: var(--muted); }
    .pill.failed, .pill.error { color: var(--bad); background: #fff0ee; }
    .pill.deleted { color: var(--bad); background: #fff0ee; font-weight: 700; }
    .pill.match { color: var(--violet); background: #f0edff; }
    .deleted-row { background: #fff8f7; }
    .filters {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; }
    .check-label { display: flex; align-items: center; gap: 8px; min-height: 34px; padding-top: 18px; }
    .check-label input { width: 16px; height: 16px; padding: 0; }
    input, select, button {
      width: 100%;
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 9px;
      background: #fff;
      color: var(--text);
    }
    button { cursor: pointer; background: var(--accent); color: #fff; border-color: var(--accent); font-weight: 600; }
    button.secondary { background: #fff; color: var(--accent); }
    button.ghost { background: var(--panel-soft); color: var(--text); border-color: var(--line); }
    button.small { width: auto; height: 28px; padding: 0 8px; font-size: 12px; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .link-btn { width: auto; height: auto; border: 0; background: transparent; color: var(--accent); padding: 0; font: inherit; text-decoration: underline; }
    a { color: var(--accent); }
    .detail-layout { display: grid; grid-template-columns: minmax(280px, 360px) minmax(0, 1fr); gap: 16px; align-items: start; }
    .detail-side, .detail-main { min-width: 0; }
    .summary-list { display: grid; gap: 8px; }
    .summary-row { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 8px; border-bottom: 1px solid var(--line); padding-bottom: 7px; }
    .summary-row span { color: var(--muted); }
    .summary-row code, .summary-row strong { overflow-wrap: anywhere; }
    .tabs { display: flex; gap: 8px; border-bottom: 1px solid var(--line); margin-bottom: 12px; overflow-x: auto; }
    .tab { width: auto; background: transparent; color: var(--muted); border: 0; border-bottom: 2px solid transparent; border-radius: 0; padding: 0 6px 9px; }
    .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }
    .empty {
      border: 1px dashed var(--line);
      background: var(--panel-soft);
      color: var(--muted);
      padding: 18px;
      border-radius: 6px;
    }
    .mini-table { border: 1px solid var(--line); max-height: 420px; overflow: auto; }
    .field-row.located, .json-leaf.located, details.located > summary { background: #fff7cf; }
    .match-list { display: grid; gap: 8px; }
    .match-item {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      display: grid;
      gap: 6px;
      background: #fff;
    }
    .match-top { display: flex; gap: 8px; align-items: center; justify-content: space-between; flex-wrap: wrap; }
    .match-path { color: var(--muted); overflow-wrap: anywhere; }
    mark { background: var(--mark); padding: 0 2px; border-radius: 3px; }
    .json-tools { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
    .json-tree {
      border: 1px solid var(--line);
      background: #fbfcfe;
      max-height: 560px;
      overflow: auto;
      padding: 10px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }
    .json-tree details { margin-left: 14px; }
    .json-tree summary { cursor: pointer; padding: 2px 4px; border-radius: 4px; overflow-wrap: anywhere; }
    .json-leaf { margin-left: 18px; padding: 2px 4px; border-radius: 4px; overflow-wrap: anywhere; }
    .json-key { color: #365f7c; }
    .json-type { color: var(--muted); }
    .json-value { color: #3b3f46; }
    .row-note { color: var(--muted); font-size: 12px; margin-top: 8px; }
    .wide { grid-column: span 2; }
    @media (max-width: 900px) {
      header, section { padding-left: 16px; padding-right: 16px; }
      .detail-layout { grid-template-columns: 1fr; }
      .wide { grid-column: span 1; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Feishu Listener</h1>
      <div class="subtle">ws_status=__WS_STATUS__ | last_event_at=__LAST_EVENT_AT__</div>
    </div>
    <div class="subtle">Field-level history search</div>
  </header>
  <main>
    <section>
      <h2>Overview</h2>
      <div class="grid">
        <div class="metric"><span>Events</span><strong>__EVENT_COUNT__</strong></div>
        <div class="metric"><span>Dispatch Attempts</span><strong>__ATTEMPT_COUNT__</strong></div>
        <div class="metric"><span>Deleted Events</span><strong>__DELETED_COUNT__</strong></div>
        <div class="metric"><span>DB Size</span><strong>__DB_SIZE__</strong></div>
        <div class="metric"><span>Retention</span><strong>__RETENTION__</strong></div>
        <div class="metric"><span>First Event</span><strong><code>__FIRST_EVENT_AT__</code></strong></div>
        <div class="metric"><span>Last Event</span><strong><code>__LAST_DB_EVENT_AT__</code></strong></div>
        __STATUS_CARDS__
      </div>
    </section>

    <section>
      <div class="toolbar">
        <h2>Routes</h2>
      </div>
      <div class="scroll">
        <table>
          <thead><tr><th>Name</th><th>Enabled</th><th>Dispatch</th><th>Actions</th><th>Event Type</th><th>Table</th><th>Webhook</th></tr></thead>
          <tbody>__ROUTE_ROWS__</tbody>
        </table>
      </div>
    </section>

    <section>
      <div class="toolbar">
        <h2>Events</h2>
        <div class="actions">
          <button class="secondary small" onclick="showDeletedOnly()">Deleted only</button>
          <button class="secondary small" id="newerBtn" onclick="pageRelative(-1)">Newer page</button>
          <button class="secondary small" id="olderBtn" onclick="pageRelative(1)">Older page</button>
        </div>
      </div>
      <div class="filters">
        <label>Keyword
          <input id="q" placeholder="event_id, error, value">
        </label>
        <label>Status
          <input id="status" placeholder="ignored / dispatching">
        </label>
        <label>Route
          <input id="route_name" placeholder="route_name">
        </label>
        <label>Event Type
          <input id="event_type" placeholder="drive.file...">
        </label>
        <label>App Token
          <input id="app_token" placeholder="app_token">
        </label>
        <label>Table ID
          <input id="table_id" placeholder="table_id">
        </label>
        <label>Record ID
          <input id="record_id" placeholder="record_id">
        </label>
        <label>Chat ID
          <input id="chat_id" placeholder="chat_id">
        </label>
        <label>Command
          <input id="command" placeholder="/command">
        </label>
        <label class="check-label">
          <input id="deleted_only" type="checkbox">
          Deleted only
        </label>
        <label>Deleted Record
          <input id="deleted_record_id" placeholder="deleted record_id">
        </label>
        <label>Deleted By
          <input id="deleted_by" placeholder="open_id / user_id / union_id">
        </label>
        <label>Created From
          <input id="created_from" type="datetime-local">
        </label>
        <label>Created To
          <input id="created_to" type="datetime-local">
        </label>
        <label>Field ID
          <input id="field_id" placeholder="fld...">
        </label>
        <label>Field Path
          <input id="field_path" placeholder="raw.event.action_list">
        </label>
        <label>Field Key
          <input id="field_key" placeholder="field_value / users">
        </label>
        <label class="wide">Field Value
          <input id="field_value" placeholder="customer, option id, phone, user name">
        </label>
        <label>Source
          <select id="match_source">
            <option value="all">all</option>
            <option value="raw">raw</option>
            <option value="normalized">normalized</option>
          </select>
        </label>
        <label>Limit
          <select id="limit">__LIMIT_OPTIONS__</select>
        </label>
        <label>Field Scan Rows
          <select id="scan_limit">__SCAN_OPTIONS__</select>
        </label>
        <label>Sort
          <select id="sort">
            <option value="desc" selected>newest first</option>
            <option value="asc">oldest first</option>
          </select>
        </label>
        <button onclick="applySearch()">Search</button>
        <button class="ghost" onclick="resetSearch()">Reset</button>
      </div>
      <div id="resultInfo" class="subtle"></div>
      <div class="scroll event-scroll">
        <table class="event-table">
          <thead><tr><th>ID</th><th>Created</th><th>Status</th><th>Deleted</th><th>Deleted By</th><th>Dispatch</th><th>Route</th><th>Table</th><th>Record</th><th>Event</th><th>Matches</th></tr></thead>
          <tbody id="events">__EVENT_ROWS__</tbody>
        </table>
      </div>
      <div id="scanNote" class="row-note"></div>
    </section>

    <section>
      <h2>Detail</h2>
      <div id="emptyDetail" class="empty">Select an event ID to inspect field changes, matched paths, and JSON tree.</div>
      <div id="detailContent" class="detail-layout" style="display:none">
        <aside class="detail-side">
          <h3>Selected Event</h3>
          <div id="detailSummary" class="summary-list"></div>
        </aside>
        <div class="detail-main">
          <div class="tabs">
            <button class="tab active" data-tab="summary">Summary</button>
            <button class="tab" data-tab="deleted">Deleted Record</button>
            <button class="tab" data-tab="fields">Field Changes</button>
            <button class="tab" data-tab="matches">Search Matches</button>
            <button class="tab" data-tab="json">JSON Tree</button>
          </div>
          <div id="tab-summary" class="tab-panel active"></div>
          <div id="tab-deleted" class="tab-panel"></div>
          <div id="tab-fields" class="tab-panel"></div>
          <div id="tab-matches" class="tab-panel"></div>
          <div id="tab-json" class="tab-panel"></div>
        </div>
      </div>
    </section>
  </main>
  <script>
    const INITIAL_DATA = __INITIAL_DATA__;
    const filterKeys = [
      'q', 'status', 'route_name', 'event_type', 'app_token', 'table_id',
      'record_id', 'chat_id', 'command', 'deleted_record_id', 'deleted_by',
      'field_id', 'field_path', 'field_key', 'field_value'
    ];
    let currentOffset = 0;
    let currentTotal = INITIAL_DATA.events.total || 0;
    let selectedEventId = null;

    function cls(v) { return String(v || '').replace(/[^a-zA-Z0-9_-]/g, ''); }
    function esc(v) {
      return String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function attr(v) { return esc(v).replace(/`/g, '&#96;'); }
    function escapeRegExp(v) { return String(v).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
    function terms() {
      return ['q', 'deleted_record_id', 'deleted_by', 'field_id', 'field_key', 'field_value']
        .map(key => document.getElementById(key)?.value.trim())
        .filter(Boolean)
        .slice(0, 6);
    }
    function highlight(v) {
      let output = esc(v ?? '');
      for (const term of terms()) {
        const safe = esc(term);
        if (!safe) continue;
        output = output.replace(new RegExp(escapeRegExp(safe), 'ig'), '<mark>$&</mark>');
      }
      return output;
    }
    function localIso(id) {
      const value = document.getElementById(id).value;
      return value ? new Date(value).toISOString() : '';
    }
    function displayTime(value) {
      if (!value) return '';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString();
    }
    function addParam(params, key, value) {
      const text = String(value ?? '').trim();
      if (text) params.set(key, text);
    }
    function eventParams(includePaging = true) {
      const params = new URLSearchParams();
      const limit = document.getElementById('limit').value || '100';
      params.set('limit', limit);
      params.set('sort', document.getElementById('sort').value || 'desc');
      params.set('match_source', document.getElementById('match_source').value || 'all');
      params.set('scan_limit', document.getElementById('scan_limit').value || '10000');
      if (includePaging) params.set('offset', String(currentOffset));
      for (const key of filterKeys) addParam(params, key, document.getElementById(key).value);
      if (document.getElementById('deleted_only').checked) params.set('deleted_only', 'true');
      addParam(params, 'created_from', localIso('created_from'));
      addParam(params, 'created_to', localIso('created_to'));
      return params;
    }
    function detailParams() {
      const params = new URLSearchParams();
      for (const key of ['q', 'field_id', 'field_path', 'field_key', 'field_value']) {
        addParam(params, key, document.getElementById(key).value);
      }
      params.set('match_source', document.getElementById('match_source').value || 'all');
      return params;
    }
    function renderEvents(data) {
      currentTotal = data.total || 0;
      const rows = data.events || [];
      document.getElementById('events').innerHTML = rows.map(row => `
        <tr class="${row.is_deleted ? 'deleted-row' : ''}">
          <td><a href="#" onclick="showDetail(${row.id}); return false;">${row.id}</a></td>
          <td><code>${esc(displayTime(row.created_at))}</code></td>
          <td><span class="pill ${cls(row.status)}">${esc(row.status)}</span></td>
          <td>${row.is_deleted ? '<span class="pill deleted">deleted</span>' : ''}</td>
          <td><code>${highlight(row.deleted_by_user_id || row.deleted_by_open_id || row.deleted_by_union_id || '')}</code></td>
          <td><span class="pill ${cls(row.dispatch_status)}">${esc(row.dispatch_status || '')}</span></td>
          <td>${highlight(row.route_name || '')}</td>
          <td><code>${highlight(row.table_id || '')}</code></td>
          <td><code>${highlight(row.record_id || '')}</code></td>
          <td>${highlight(row.event_type || '')}</td>
          <td>${row.match_count ? `<span class="pill match">${row.match_count}</span>` : ''}</td>
        </tr>
      `).join('');
      document.querySelectorAll('.event-table tbody tr').forEach(row => {
        const id = row.querySelector('a')?.textContent;
        if (String(selectedEventId || '') === id) row.classList.add('selected');
      });
      const start = rows.length ? currentOffset + 1 : 0;
      const end = currentOffset + rows.length;
      document.getElementById('resultInfo').textContent = `Showing ${start}-${end} of ${currentTotal} matching events`;
      document.getElementById('newerBtn').disabled = currentOffset <= 0;
      document.getElementById('olderBtn').disabled = end >= currentTotal;
      const fieldSearch = data.field_search || {};
      document.getElementById('scanNote').textContent = fieldSearch.active
        ? `Field search scanned ${fieldSearch.scanned} of ${data.candidate_total || 0} candidate rows${fieldSearch.truncated ? '; raise Field Scan Rows to inspect deeper history.' : '.'}`
        : '';
    }
    async function loadEvents() {
      const res = await fetch('/events?' + eventParams(true).toString());
      const data = await res.json();
      renderEvents(data);
    }
    function applySearch() {
      currentOffset = 0;
      loadEvents();
    }
    function resetSearch() {
      for (const key of filterKeys) document.getElementById(key).value = '';
      document.getElementById('created_from').value = '';
      document.getElementById('created_to').value = '';
      document.getElementById('deleted_only').checked = false;
      document.getElementById('match_source').value = 'all';
      document.getElementById('sort').value = 'desc';
      currentOffset = 0;
      loadEvents();
    }
    function showDeletedOnly() {
      document.getElementById('deleted_only').checked = true;
      currentOffset = 0;
      loadEvents();
    }
    function pageRelative(direction) {
      const limit = Number(document.getElementById('limit').value || 100);
      currentOffset = Math.max(0, currentOffset + direction * limit);
      loadEvents();
    }
    async function showDetail(id) {
      selectedEventId = id;
      const detailUrl = '/events/' + id + '?' + detailParams().toString();
      const detailRes = await fetch(detailUrl);
      const data = await detailRes.json();
      renderDetail(data);
      document.querySelectorAll('.event-table tbody tr').forEach(row => {
        row.classList.toggle('selected', row.querySelector('a')?.textContent === String(id));
      });
      document.getElementById('detailContent').scrollIntoView({behavior: 'smooth', block: 'start'});
    }
    function summaryRow(label, value, code = false) {
      return `<div class="summary-row"><span>${esc(label)}</span>${code ? `<code>${highlight(value || '')}</code>` : `<strong>${highlight(value || '')}</strong>`}</div>`;
    }
    function renderDetail(data) {
      document.getElementById('emptyDetail').style.display = 'none';
      document.getElementById('detailContent').style.display = 'grid';
      const resource = data.normalized?.resource || {};
      document.getElementById('detailSummary').innerHTML = [
        summaryRow('Row ID', data.id, true),
        summaryRow('Created', displayTime(data.created_at), true),
        summaryRow('Status', data.status),
        summaryRow('Deleted', data.deletion?.is_deleted ? 'yes' : 'no'),
        summaryRow('Deleted By', deletedByText(data.deletion), true),
        summaryRow('Route', data.route_name || ''),
        summaryRow('Event Type', data.event_type || ''),
        summaryRow('App Token', data.app_token || resource.app_token || '', true),
        summaryRow('Table', data.table_id || resource.table_id || '', true),
        summaryRow('Record', data.record_id || resource.record_id || '', true),
        summaryRow('Raw Mode', data.raw_mode || '')
      ].join('');
      renderSummaryTab(data);
      renderDeletedTab(data.deletion || {});
      renderFieldsTab(data.field_changes || []);
      renderMatchesTab(data.field_matches || []);
      renderJsonTab(data);
      activateTab(data.deletion?.is_deleted ? 'deleted' : ((data.field_matches || []).length ? 'matches' : 'fields'));
    }
    function deletedByText(deletion) {
      if (!deletion?.is_deleted) return '';
      const by = deletion?.deleted_by || {};
      return by.display || by.user_id || by.open_id || by.union_id || '';
    }
    function renderSummaryTab(data) {
      const attempts = data.dispatch_attempts || [];
      const attemptRows = attempts.length ? attempts.map(item => `
        <tr>
          <td>${esc(item.attempt)}</td>
          <td><span class="pill ${cls(item.status)}">${esc(item.status)}</span></td>
          <td>${esc(item.status_code || '')}</td>
          <td>${esc(item.elapsed_ms || '')}</td>
          <td>${highlight(item.error || '')}</td>
          <td><code>${esc(displayTime(item.created_at))}</code></td>
        </tr>
      `).join('') : '<tr><td colspan="6" class="subtle">No dispatch attempts.</td></tr>';
      document.getElementById('tab-summary').innerHTML = `
        <div class="mini-table">
          <table>
            <thead><tr><th>Attempt</th><th>Status</th><th>HTTP</th><th>ms</th><th>Error</th><th>Created</th></tr></thead>
            <tbody>${attemptRows}</tbody>
          </table>
        </div>`;
    }
    function renderDeletedTab(deletion) {
      if (!deletion?.is_deleted) {
        document.getElementById('tab-deleted').innerHTML = '<div class="empty">This event is not a bitable record deletion.</div>';
        return;
      }
      const recordRows = (deletion.records || []).map(record => `
        <tr>
          <td><code>${highlight(record.record_id || '')}</code></td>
          <td>${highlight(record.action || '')}</td>
          <td>${esc(record.before_field_count || 0)}</td>
          <td>${record.before_summary ? 'summary' : 'full'}</td>
          <td><code>${highlight(record.path || '')}</code></td>
        </tr>
      `).join('');
      const fieldRows = (deletion.records || []).flatMap(record =>
        (record.before_fields || []).map(field => `
          <tr class="field-row" data-change-path="${attr(field.path)}">
            <td><button class="secondary small" data-field-path="${attr(field.path)}">Locate</button></td>
            <td><code>${highlight(record.record_id || '')}</code></td>
            <td><code>${highlight(field.field_id || '')}</code></td>
            <td>${highlight(field.value_text || '')}</td>
            <td>${highlight(field.identity_text || '')}</td>
            <td><code>${highlight(field.path || '')}</code></td>
          </tr>
        `)
      ).join('');
      document.getElementById('tab-deleted').innerHTML = `
        <div class="summary-list" style="margin-bottom:12px">
          ${summaryRow('Deleted By', deletedByText(deletion), true)}
          ${summaryRow('Open ID', deletion.deleted_by?.open_id || '', true)}
          ${summaryRow('Union ID', deletion.deleted_by?.union_id || '', true)}
          ${summaryRow('User ID', deletion.deleted_by?.user_id || '', true)}
          ${summaryRow('Record Count', deletion.record_count || 0)}
          ${summaryRow('Field Count', deletion.field_count || 0)}
          ${summaryRow('Revision', deletion.revision || '', true)}
        </div>
        <h3>Deleted Records</h3>
        <div class="mini-table" style="margin-bottom:12px">
          <table>
            <thead><tr><th>Record</th><th>Action</th><th>Before Fields</th><th>Mode</th><th>Path</th></tr></thead>
            <tbody>${recordRows || '<tr><td colspan="5" class="subtle">No deleted records found.</td></tr>'}</tbody>
          </table>
        </div>
        <h3>Deleted Record Content</h3>
        <div class="mini-table">
          <table>
            <thead><tr><th></th><th>Record</th><th>Field ID</th><th>Deleted Value</th><th>Identity</th><th>Path</th></tr></thead>
            <tbody>${fieldRows || '<tr><td colspan="6" class="subtle">Only field IDs were captured for this deleted record.</td></tr>'}</tbody>
          </table>
        </div>`;
      bindLocateButtons(document.getElementById('tab-deleted'));
    }
    function renderFieldsTab(changes) {
      const body = changes.length ? changes.map(change => `
        <tr class="field-row" data-change-path="${attr(change.path)}">
          <td><button class="secondary small" data-field-path="${attr(change.path)}">Locate</button></td>
          <td>${esc(change.side || '')}</td>
          <td>${highlight(change.action || '')}</td>
          <td><code>${highlight(change.record_id || '')}</code></td>
          <td><code>${highlight(change.field_id || '')}</code></td>
          <td>${highlight(change.value_text || '')}</td>
          <td>${highlight(change.identity_text || '')}</td>
          <td><code>${highlight(change.path || '')}</code></td>
        </tr>
      `).join('') : '<tr><td colspan="8" class="subtle">No bitable field changes in this event payload.</td></tr>';
      document.getElementById('tab-fields').innerHTML = `
        <div class="mini-table">
          <table>
            <thead><tr><th></th><th>Side</th><th>Action</th><th>Record</th><th>Field ID</th><th>Value</th><th>Identity</th><th>Path</th></tr></thead>
            <tbody>${body}</tbody>
          </table>
        </div>`;
      bindLocateButtons(document.getElementById('tab-fields'));
    }
    function renderMatchesTab(matches) {
      document.getElementById('tab-matches').innerHTML = matches.length ? `
        <div class="match-list">
          ${matches.map(match => `
            <div class="match-item">
              <div class="match-top">
                <span><span class="pill match">${esc(match.kind || 'match')}</span> <code>${highlight(match.key || '')}</code></span>
                <button class="secondary small" data-field-path="${attr(match.path || '')}">Locate</button>
              </div>
              <div>${highlight(match.value || '')}</div>
              ${match.identity ? `<div class="subtle">${highlight(match.identity)}</div>` : ''}
              <div class="match-path"><code>${highlight(match.path || '')}</code></div>
            </div>
          `).join('')}
        </div>` : '<div class="empty">No field-level matches for the current search terms.</div>';
      bindLocateButtons(document.getElementById('tab-matches'));
    }
    function renderJsonTab(data) {
      document.getElementById('tab-json').innerHTML = `
        <div class="json-tools">
          <button class="secondary small" onclick="expandJson(true)">Expand</button>
          <button class="secondary small" onclick="expandJson(false)">Collapse</button>
        </div>
        <div id="jsonTree" class="json-tree">
          ${renderJsonTree(data.normalized || {}, 'normalized', 0)}
          ${renderJsonTree(data.raw || {}, 'raw', 0)}
        </div>`;
    }
    function renderJsonTree(value, path, depth) {
      const type = Array.isArray(value) ? 'array' : typeof value;
      if (value && typeof value === 'object') {
        const entries = Array.isArray(value)
          ? value.map((item, index) => [String(index), item, `${path}[${index}]`])
          : Object.entries(value).map(([key, item]) => [key, item, `${path}.${key}`]);
        return `<details ${depth < 2 ? 'open' : ''} data-json-path="${attr(path)}">
          <summary><span class="json-key">${highlight(path)}</span> <span class="json-type">${type} ${entries.length}</span></summary>
          ${entries.map(([key, item, childPath]) => renderJsonTree(item, childPath, depth + 1)).join('')}
        </details>`;
      }
      return `<div class="json-leaf" data-json-path="${attr(path)}"><span class="json-key">${highlight(path)}</span>: <span class="json-value">${highlight(value)}</span></div>`;
    }
    function bindLocateButtons(root) {
      root.querySelectorAll('[data-field-path]').forEach(button => {
        button.addEventListener('click', () => locatePath(button.dataset.fieldPath || ''));
      });
    }
    function locatePath(path) {
      if (!path) return;
      document.querySelectorAll('.located').forEach(item => item.classList.remove('located'));
      const fieldRow = Array.from(document.querySelectorAll('[data-change-path]')).find(item => item.dataset.changePath === path);
      if (fieldRow) {
        activateTab('fields');
        fieldRow.classList.add('located');
        fieldRow.scrollIntoView({behavior: 'smooth', block: 'center'});
        return;
      }
      activateTab('json');
      const node = Array.from(document.querySelectorAll('[data-json-path]')).find(item => item.dataset.jsonPath === path);
      if (!node) return;
      let parent = node.parentElement;
      while (parent) {
        if (parent.tagName === 'DETAILS') parent.open = true;
        parent = parent.parentElement;
      }
      node.classList.add('located');
      node.scrollIntoView({behavior: 'smooth', block: 'center'});
    }
    function expandJson(open) {
      document.querySelectorAll('#jsonTree details').forEach(item => { item.open = open; });
    }
    function activateTab(name) {
      document.querySelectorAll('.tab').forEach(tab => tab.classList.toggle('active', tab.dataset.tab === name));
      document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.toggle('active', panel.id === 'tab-' + name));
    }
    document.querySelectorAll('.tab').forEach(tab => {
      tab.addEventListener('click', () => activateTab(tab.dataset.tab));
    });
    document.querySelectorAll('.filters input').forEach(input => {
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter') applySearch();
      });
    });
    renderEvents(INITIAL_DATA.events);
  </script>
</body>
</html>
"""
    return (
        html.replace("__WS_STATUS__", escape(str(state["ws_status"])))
        .replace("__LAST_EVENT_AT__", escape(str(state["last_event_at"])))
        .replace("__EVENT_COUNT__", escape(str(stats_data["event_count"])))
        .replace("__ATTEMPT_COUNT__", escape(str(stats_data["dispatch_attempt_count"])))
        .replace("__DELETED_COUNT__", escape(str(stats_data.get("deleted_event_count", 0))))
        .replace("__DB_SIZE__", escape(format_bytes(stats_data["db_size_bytes"])))
        .replace(
            "__RETENTION__",
            escape(
                f"{stats_data['retention'].get('max_days')}d / {stats_data['retention'].get('max_events')}"
            ),
        )
        .replace("__FIRST_EVENT_AT__", escape(str(stats_data.get("first_event_at") or "-")))
        .replace("__LAST_DB_EVENT_AT__", escape(str(stats_data.get("last_event_at") or "-")))
        .replace("__STATUS_CARDS__", status_cards)
        .replace("__ROUTE_ROWS__", route_rows)
        .replace("__EVENT_ROWS__", event_rows)
        .replace("__LIMIT_OPTIONS__", limit_options)
        .replace("__SCAN_OPTIONS__", scan_options)
        .replace("__INITIAL_DATA__", initial_json)
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
