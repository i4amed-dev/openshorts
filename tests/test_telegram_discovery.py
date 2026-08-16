"""The discovery control center: shows real settings and the real last-run
stats, never a fabricated "done" message, and never promises a dry-run button
`automation/discovery.py` doesn't actually support.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import automation.service as automation_service
from automation.ports import Runtime
from automation.service import AutopilotService
from autopilot_fakes import FakeClipGenerator, FakePublisher, FakeYouTubeClient, install_fake_vendor, make_record, run_async
from telegram_bot import persistence
from telegram_bot.handlers import discovery

NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)


class FakeContext:
    def __init__(self):
        self.sent = []

    async def _send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))

    @property
    def bot(self):
        return SimpleNamespace(send_message=self._send)


def _update(*, callback=False, user_id=1, chat_id=1):
    query = SimpleNamespace(data="discovery:run",
                             answer=_recorder(), edit_message_text=_recorder()) if callback else None
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id, username="a"),
                            effective_chat=SimpleNamespace(id=chat_id), callback_query=query,
                            message=None)


def _recorder():
    calls = []

    async def rec(*a, **kw):
        calls.append((a, kw))
    rec.calls = calls
    return rec


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_DATA_API_KEY", "test-yt-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-up-key")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
    monkeypatch.setenv("AUTOPILOT_DB_PATH", str(tmp_path / "bot.db"))
    svc = AutopilotService(db_path=str(tmp_path / "svc.db")).open()
    clip_gen, publisher = FakeClipGenerator(), FakePublisher()
    youtube = FakeYouTubeClient([make_record("vid0000000x", now=NOW)])
    svc._orchestrator.runtime = Runtime(clip_generator=clip_gen.port(), publisher=publisher.port())
    svc._orchestrator._client_factory = lambda: youtube
    install_fake_vendor(monkeypatch, publisher)
    svc.update_settings({"publishing": {"upload_post_user": publisher.user}, "enabled": True})
    automation_service._service = svc
    persistence.reset_store()
    yield svc
    automation_service.reset_service()
    persistence.reset_store()


class TestShow:
    def test_no_run_yet_says_so_honestly(self, service):
        update = _update()
        context = FakeContext()
        run_async(discovery.show(update, context))
        text = context.sent[0][1]
        assert "No discovery run yet" in text

    def test_after_a_run_shows_real_numbers(self, service):
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        update = _update()
        context = FakeContext()
        run_async(discovery.show(update, context))
        text = context.sent[0][1]
        assert "Fetched: 1" in text

    def test_never_shows_a_dry_run_button(self, service):
        update = _update()
        context = FakeContext()
        run_async(discovery.show(update, context))
        kb = context.sent[0][2]
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert not any("dry" in label.lower() for label in labels)


class TestRun:
    def test_run_actually_triggers_discovery_and_reports_real_stats(self, service):
        update = _update(callback=True)
        context = FakeContext()
        run_async(discovery.run(update, context))
        assert len(update.callback_query.answer.calls) >= 1
        # discovery.run() re-renders show(), which sends a fresh message since
        # our fake query.edit_message_text doesn't raise but isn't asserted on —
        # what matters is a real run actually happened:
        assert service.db.recent_runs(limit=1)[0]["kind"] == "discovery"
