"""Durable storage for the Telegram bot itself: which chats know the bot, their
notification preferences, the notification cursor, and the control-action audit
log.

Lives in the *same* SQLite file Autopilot already persists to — WAL mode is
built for exactly this, multiple connections/processes against one file — but
as its own connection and its own tables. That way `telegram_bot/` depends on
`automation/` only for its DB *path*, `automation/` never has to know Telegram
exists, and a bug in one schema can't corrupt the other's migration state.

Never stores the bot token or any secret.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_chat (
    chat_id       INTEGER PRIMARY KEY,
    user_id       INTEGER,
    username      TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    blocked       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS telegram_preferences (
    chat_id               INTEGER PRIMARY KEY
                          REFERENCES telegram_chat (chat_id) ON DELETE CASCADE,
    notify_mode           TEXT NOT NULL DEFAULT 'important',
    categories_json       TEXT NOT NULL DEFAULT '{}',
    quiet_hours_start     TEXT,
    quiet_hours_end       TEXT,
    daily_digest_enabled  INTEGER NOT NULL DEFAULT 0,
    daily_digest_time     TEXT NOT NULL DEFAULT '09:00',
    -- Per-chat, not shared: two chats with different digest times must not
    -- make one send suppress the other's on the same poll cycle.
    last_digest_sent_date TEXT,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_event_cursor (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_event_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS telegram_action_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    user_id INTEGER,
    chat_id INTEGER,
    action  TEXT NOT NULL,
    target  TEXT,
    result  TEXT NOT NULL DEFAULT 'ok',
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS ix_telegram_action_ts ON telegram_action_log (ts DESC);
"""

NOTIFY_MODES = ("critical_only", "important", "everything", "muted")

DEFAULT_CATEGORIES: Dict[str, bool] = {
    "critical_errors": True,
    "source_selected": True,
    "processing_started": True,
    "clips_ready": True,
    "publishing": True,
    "discovery_summary": True,
    "debug_recovery": False,
    "daily_summary": True,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class TelegramStore:
    """Thread-safe repository over the bot's own tables."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> "TelegramStore":
        with self._lock:
            if self._conn is not None:
                return self
            if self.path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0,
                                    isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.executescript(_SCHEMA)
            self._conn = conn
            return self

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    # --- chat registry ---------------------------------------------------------

    def register_chat(self, chat_id: int, user_id: Optional[int],
                       username: Optional[str]) -> None:
        with self._lock:
            now = iso()
            self.conn.execute(
                "INSERT INTO telegram_chat (chat_id, user_id, username, first_seen_at, "
                " last_seen_at, blocked) VALUES (?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "user_id = excluded.user_id, username = excluded.username, "
                "last_seen_at = excluded.last_seen_at, blocked = 0",
                (chat_id, user_id, username, now, now))
            self.conn.execute(
                "INSERT INTO telegram_preferences (chat_id, categories_json, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(chat_id) DO NOTHING",
                (chat_id, json.dumps(DEFAULT_CATEGORIES), now))

    def mark_blocked(self, chat_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE telegram_chat SET blocked = 1 WHERE chat_id = ?", (chat_id,))

    def known_chat_ids(self, *, exclude_blocked: bool = True) -> List[int]:
        sql = "SELECT chat_id FROM telegram_chat"
        if exclude_blocked:
            sql += " WHERE blocked = 0"
        with self._lock:
            return [row["chat_id"] for row in self.conn.execute(sql)]

    # --- preferences -------------------------------------------------------------

    def get_preferences(self, chat_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM telegram_preferences WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            return {
                "chat_id": chat_id, "notify_mode": "important",
                "categories": dict(DEFAULT_CATEGORIES),
                "quiet_hours_start": None, "quiet_hours_end": None,
                "daily_digest_enabled": False, "daily_digest_time": "09:00",
                "last_digest_sent_date": None,
            }
        try:
            categories = dict(DEFAULT_CATEGORIES, **json.loads(row["categories_json"]))
        except (TypeError, ValueError):
            categories = dict(DEFAULT_CATEGORIES)
        return {
            "chat_id": chat_id,
            "notify_mode": row["notify_mode"],
            "categories": categories,
            "quiet_hours_start": row["quiet_hours_start"],
            "quiet_hours_end": row["quiet_hours_end"],
            "daily_digest_enabled": bool(row["daily_digest_enabled"]),
            "daily_digest_time": row["daily_digest_time"],
            "last_digest_sent_date": row["last_digest_sent_date"],
        }

    def mark_digest_sent(self, chat_id: int, date_str: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE telegram_preferences SET last_digest_sent_date = ? WHERE chat_id = ?",
                (date_str, chat_id))

    def update_preferences(self, chat_id: int, patch: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_preferences(chat_id)
        if "categories" in patch:
            current["categories"].update(patch["categories"])
        for key in ("notify_mode", "quiet_hours_start", "quiet_hours_end",
                    "daily_digest_enabled", "daily_digest_time"):
            if key in patch:
                current[key] = patch[key]
        if current["notify_mode"] not in NOTIFY_MODES:
            raise ValueError(f"Unknown notify_mode: {current['notify_mode']!r}")
        with self._lock:
            self.conn.execute(
                "INSERT INTO telegram_preferences (chat_id, notify_mode, categories_json, "
                " quiet_hours_start, quiet_hours_end, daily_digest_enabled, "
                " daily_digest_time, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "notify_mode = excluded.notify_mode, categories_json = excluded.categories_json, "
                "quiet_hours_start = excluded.quiet_hours_start, "
                "quiet_hours_end = excluded.quiet_hours_end, "
                "daily_digest_enabled = excluded.daily_digest_enabled, "
                "daily_digest_time = excluded.daily_digest_time, updated_at = excluded.updated_at",
                (chat_id, current["notify_mode"], json.dumps(current["categories"]),
                 current["quiet_hours_start"], current["quiet_hours_end"],
                 int(current["daily_digest_enabled"]), current["daily_digest_time"], iso()))
        return current

    # --- notification cursor ------------------------------------------------------

    def get_cursor(self) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT last_event_id FROM telegram_event_cursor WHERE id = 1").fetchone()
        if row is None:
            return {"last_event_id": 0}
        return {"last_event_id": row["last_event_id"]}

    def set_cursor(self, *, last_event_id: Optional[int] = None) -> None:
        current = self.get_cursor()
        event_id = last_event_id if last_event_id is not None else current["last_event_id"]
        with self._lock:
            self.conn.execute(
                "INSERT INTO telegram_event_cursor (id, last_event_id) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET last_event_id = excluded.last_event_id",
                (event_id,))

    # --- action audit log ----------------------------------------------------------

    def log_action(self, *, user_id: Optional[int], chat_id: Optional[int], action: str,
                    target: Optional[str] = None, result: str = "ok",
                    detail: Optional[str] = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO telegram_action_log (ts, user_id, chat_id, action, target, "
                "result, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (iso(), user_id, chat_id, action, target, result, (detail or "")[:500] or None))

    def recent_actions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM telegram_action_log ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(row) for row in rows]


_store: Optional[TelegramStore] = None


def get_store() -> TelegramStore:
    global _store
    if _store is None:
        from automation.db import DEFAULT_DB_PATH
        path = os.environ.get("AUTOPILOT_DB_PATH", DEFAULT_DB_PATH)
        _store = TelegramStore(path).connect()
    return _store


def reset_store() -> None:
    global _store
    if _store is not None:
        _store.close()
    _store = None
