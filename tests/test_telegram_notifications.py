"""Notification preferences and the delivery-decision logic: category
filters, quiet hours, muted/critical-only modes, and durable per-chat digest
tracking that survives a restart.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from autopilot_fakes import run_async
from telegram_bot import notifications, persistence
from telegram_bot.handlers import notifications as notify_ui


class FakeQuery:
    def __init__(self):
        self.edits = []
        self.answers = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))

    async def answer(self, *a, **kw):
        self.answers.append((a, kw))


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)


class FakeContext:
    def __init__(self):
        self.user_data = {}

    async def _send(self, chat_id, text, **kwargs):
        pass

    @property
    def bot(self):
        return SimpleNamespace(send_message=self._send)


def _update(*, has_query=False, text=None, user_id=1, chat_id=1):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="admin"),
        effective_chat=SimpleNamespace(id=chat_id),
        callback_query=FakeQuery() if has_query else None,
        message=FakeMessage(text) if text is not None else None)


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
    monkeypatch.setenv("AUTOPILOT_DB_PATH", str(tmp_path / "bot.db"))
    persistence.reset_store()
    yield
    persistence.reset_store()


class TestShouldNotify:
    def test_error_is_always_critical(self):
        notify, emoji, category = notifications._should_notify("anything", "error", "boom")
        assert (notify, category) == (True, "critical_errors")

    def test_engine_warning_is_critical(self):
        notify, _emoji, category = notifications._should_notify("engine", "warn", "circuit breaker tripped")
        assert (notify, category) == (True, "critical_errors")

    def test_selection_maps_to_source_selected(self):
        notify, _emoji, category = notifications._should_notify("selection", "info", "picked one")
        assert (notify, category) == (True, "source_selected")

    def test_discovery_without_eligible_keyword_is_silent(self):
        notify, _emoji, _category = notifications._should_notify("discovery", "info", "fetched 40 candidates")
        assert notify is False

    def test_discovery_with_eligible_keyword_notifies(self):
        notify, _emoji, category = notifications._should_notify("discovery", "info", "12 eligible")
        assert (notify, category) == (True, "discovery_summary")

    def test_unmapped_stage_is_silent(self):
        notify, _emoji, _category = notifications._should_notify("housekeeping", "info", "pruned old rows")
        assert notify is False


class TestQuietHours:
    def test_no_quiet_hours_configured_never_suppresses(self):
        assert notifications._in_quiet_hours({}, "23:30") is False

    def test_overnight_window_wraps_past_midnight(self):
        prefs = {"quiet_hours_start": "23:00", "quiet_hours_end": "08:00"}
        assert notifications._in_quiet_hours(prefs, "23:30") is True
        assert notifications._in_quiet_hours(prefs, "03:00") is True
        assert notifications._in_quiet_hours(prefs, "12:00") is False

    def test_same_day_window(self):
        prefs = {"quiet_hours_start": "09:00", "quiet_hours_end": "17:00"}
        assert notifications._in_quiet_hours(prefs, "12:00") is True
        assert notifications._in_quiet_hours(prefs, "20:00") is False


class TestWants:
    def _prefs(self, **overrides):
        base = {"notify_mode": "important", "categories": dict(persistence.DEFAULT_CATEGORIES),
                "quiet_hours_start": None, "quiet_hours_end": None}
        base.update(overrides)
        return base

    def test_muted_blocks_everything(self):
        assert notifications._wants(self._prefs(notify_mode="muted"), "critical_errors") is False

    def test_critical_only_blocks_non_critical_categories(self):
        prefs = self._prefs(notify_mode="critical_only")
        assert notifications._wants(prefs, "source_selected") is False
        assert notifications._wants(prefs, "critical_errors") is True

    def test_disabled_category_is_blocked_even_in_important_mode(self):
        prefs = self._prefs()
        prefs["categories"]["source_selected"] = False
        assert notifications._wants(prefs, "source_selected") is False

    def test_critical_errors_bypass_quiet_hours(self, monkeypatch):
        monkeypatch.setattr(notifications, "_configured_timezone", lambda: "UTC")
        monkeypatch.setattr(notifications, "_local_hhmm", lambda tz, now=None: "23:30")
        prefs = self._prefs(quiet_hours_start="22:00", quiet_hours_end="06:00")
        assert notifications._wants(prefs, "critical_errors") is True
        assert notifications._wants(prefs, "source_selected") is False


class TestPreferencesScreen:
    def test_toggle_mode_persists(self):
        update, context = _update(has_query=True), FakeContext()
        run_async(notify_ui.set_mode(update, context, "critical_only"))
        prefs = persistence.get_store().get_preferences(1)
        assert prefs["notify_mode"] == "critical_only"

    def test_toggle_category_flips_it(self):
        update, context = _update(has_query=True), FakeContext()
        run_async(notify_ui.toggle_category(update, context, "debug_recovery"))
        prefs = persistence.get_store().get_preferences(1)
        assert prefs["categories"]["debug_recovery"] is True  # default False -> True

    def test_quiet_hours_set_via_text_prompt(self):
        update, context = _update(has_query=True), FakeContext()
        run_async(notify_ui.quiet_prompt(update, context))
        assert context.user_data[notify_ui.PROMPT_KEY] is True

        msg_update = _update(text="23:00-08:00")
        consumed = run_async(notify_ui.handle_message(msg_update, context))
        assert consumed is True
        prefs = persistence.get_store().get_preferences(1)
        assert (prefs["quiet_hours_start"], prefs["quiet_hours_end"]) == ("23:00", "08:00")

    def test_invalid_quiet_hours_text_is_rejected(self):
        context = FakeContext()
        context.user_data[notify_ui.PROMPT_KEY] = True
        msg_update = _update(text="not a time range")
        run_async(notify_ui.handle_message(msg_update, context))
        assert "Use HH:MM-HH:MM" in msg_update.message.sent[0]
        assert persistence.get_store().get_preferences(1)["quiet_hours_start"] is None


class TestDigestNeverDoubleSends:
    def test_digest_is_per_chat_across_a_restart(self):
        store = persistence.get_store()
        store.register_chat(1, 1, "admin")
        store.update_preferences(1, {"daily_digest_enabled": True, "daily_digest_time": "00:00"})
        store.mark_digest_sent(1, "2026-08-16")

        # Simulate a restart: new TelegramStore instance, same file.
        persistence.reset_store()
        reopened = persistence.get_store()
        assert reopened.get_preferences(1)["last_digest_sent_date"] == "2026-08-16"
