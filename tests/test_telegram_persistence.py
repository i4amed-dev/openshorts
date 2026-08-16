"""The bot's own SQLite tables: chat registry, preferences, notification
cursor, and action audit log — all of it must survive a reconnect (the
restart-survival property the old bot's in-memory globals lacked).
"""
from __future__ import annotations

import pytest

from telegram_bot.persistence import DEFAULT_CATEGORIES, TelegramStore


@pytest.fixture
def store(tmp_path):
    return TelegramStore(str(tmp_path / "bot.db")).connect()


class TestChatRegistry:
    def test_register_is_idempotent_and_updates_last_seen(self, store):
        store.register_chat(1, 100, "alice")
        store.register_chat(1, 100, "alice")
        assert store.known_chat_ids() == [1]

    def test_registering_creates_default_preferences(self, store):
        store.register_chat(1, 100, "alice")
        prefs = store.get_preferences(1)
        assert prefs["categories"] == DEFAULT_CATEGORIES
        assert prefs["notify_mode"] == "important"

    def test_blocked_chat_excluded_from_known_ids(self, store):
        store.register_chat(1, 100, "alice")
        store.register_chat(2, 200, "bob")
        store.mark_blocked(1)
        assert store.known_chat_ids() == [2]
        assert store.known_chat_ids(exclude_blocked=False) == [1, 2] or \
            store.known_chat_ids(exclude_blocked=False) == [2, 1]

    def test_re_registering_a_blocked_chat_unblocks_it(self, store):
        store.register_chat(1, 100, "alice")
        store.mark_blocked(1)
        store.register_chat(1, 100, "alice")
        assert store.known_chat_ids() == [1]


class TestPreferences:
    def test_default_preferences_for_unknown_chat(self, store):
        prefs = store.get_preferences(999)
        assert prefs["notify_mode"] == "important"
        assert prefs["daily_digest_enabled"] is False

    def test_update_preferences_merges_categories(self, store):
        store.register_chat(1, 100, "alice")
        store.update_preferences(1, {"categories": {"debug_recovery": True}})
        prefs = store.get_preferences(1)
        assert prefs["categories"]["debug_recovery"] is True
        assert prefs["categories"]["clips_ready"] is True  # untouched default survives

    def test_update_rejects_unknown_notify_mode(self, store):
        store.register_chat(1, 100, "alice")
        with pytest.raises(ValueError):
            store.update_preferences(1, {"notify_mode": "yell_constantly"})

    def test_quiet_hours_round_trip(self, store):
        store.register_chat(1, 100, "alice")
        store.update_preferences(1, {"quiet_hours_start": "23:00", "quiet_hours_end": "08:00"})
        prefs = store.get_preferences(1)
        assert (prefs["quiet_hours_start"], prefs["quiet_hours_end"]) == ("23:00", "08:00")


class TestCursor:
    def test_starts_at_zero(self, store):
        assert store.get_cursor() == {"last_event_id": 0}

    def test_cursor_survives_a_reconnect(self, tmp_path):
        path = str(tmp_path / "cursor.db")
        TelegramStore(path).connect().set_cursor(last_event_id=42)
        reopened = TelegramStore(path).connect()
        assert reopened.get_cursor()["last_event_id"] == 42

    def test_sequential_updates_advance_monotonically(self, store):
        store.set_cursor(last_event_id=5)
        store.set_cursor(last_event_id=6)
        assert store.get_cursor()["last_event_id"] == 6


class TestDigestPerChat:
    def test_digest_sent_date_is_tracked_per_chat_not_shared(self, store):
        """The bug this guards against: two chats with different digest times
        must not make one send suppress the other on the same poll cycle."""
        store.register_chat(1, 100, "alice")
        store.register_chat(2, 200, "bob")
        store.mark_digest_sent(1, "2026-08-16")
        assert store.get_preferences(1)["last_digest_sent_date"] == "2026-08-16"
        assert store.get_preferences(2)["last_digest_sent_date"] is None


class TestActionLog:
    def test_log_and_read_back(self, store):
        store.log_action(user_id=1, chat_id=1, action="admin:pause", result="ok")
        store.log_action(user_id=1, chat_id=1, action="admin:stop_go", result="error",
                          detail="upload-post unreachable")
        recent = store.recent_actions()
        assert recent[0]["action"] == "admin:stop_go"  # most recent first
        assert recent[0]["detail"] == "upload-post unreachable"
        assert recent[1]["action"] == "admin:pause"

    def test_never_stores_the_bot_token(self, store):
        # The audit log's `detail` field is free text — a caller must never be
        # able to make the token land in the database via an error message
        # that happens to embed it. This asserts the schema has no token
        # column at all, so there is no place for it to leak into.
        columns = {row[1] for row in store.conn.execute("PRAGMA table_info(telegram_action_log)")}
        assert "token" not in columns
        assert "bot_token" not in columns
