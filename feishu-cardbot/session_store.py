"""SQLite 会话存储 + 消息幂等去重。

表：
- sessions：多轮对话状态与 payload（字段草稿、规则 id 等）
- processed_messages：已处理的飞书 message_id，防止重推重复执行
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


class SessionStore:
    """线程安全的简易会话仓库（单进程内用锁即可）。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        """建表（若不存在）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                create table if not exists sessions (
                    session_key text primary key,
                    state text not null,
                    payload_json text not null,
                    updated_at real not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists processed_messages (
                    message_id text primary key,
                    processed_at real not null
                )
                """
            )

    def claim_message(self, message_id: str) -> bool:
        """认领消息：首次返回 True；重复 message_id 返回 False。"""
        if not message_id:
            return True
        now = time.time()
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "insert into processed_messages(message_id, processed_at) values (?, ?)",
                    (message_id, now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get(self, session_key: str) -> Optional[Dict[str, Any]]:
        """读取会话；不存在返回 None。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "select state, payload_json, updated_at from sessions where session_key = ?",
                (session_key,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        return {
            "state": row["state"],
            "payload": payload,
            "updated_at": row["updated_at"],
        }

    def save(self, session_key: str, state: str, payload: Dict[str, Any]) -> None:
        """写入或更新会话状态。"""
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into sessions(session_key, state, payload_json, updated_at)
                values (?, ?, ?, ?)
                on conflict(session_key) do update set
                    state = excluded.state,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (session_key, state, json.dumps(payload, ensure_ascii=False), now),
            )

    def clear(self, session_key: str) -> None:
        """删除会话（取消 / 写表成功后调用）。"""
        with self._lock, self._connect() as conn:
            conn.execute("delete from sessions where session_key = ?", (session_key,))

    def expire_if_stale(self, session_key: str, ttl_seconds: float) -> bool:
        """若会话超过 TTL 则清除；返回是否因过期而清除。"""
        row = self.get(session_key)
        if not row:
            return False
        if time.time() - float(row["updated_at"]) > ttl_seconds:
            self.clear(session_key)
            return True
        return False
