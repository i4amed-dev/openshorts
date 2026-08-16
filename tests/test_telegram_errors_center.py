"""The error center: real deep links to the source/job/publish attempt an
error actually happened on — never a generic dump, and never a crash on a
malformed/legacy event row missing those ids."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import automation.service as automation_service
from automation.ports import Runtime
from automation.service import AutopilotService
from autopilot_fakes import FakeClipGenerator, FakePublisher, FakeYouTubeClient, install_fake_vendor, make_record, run_async
from telegram_bot import persistence
from telegram_bot.handlers import errors as errors_center

NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)


class FakeQuery:
    def __init__(self):
        self.edits = []
        self.answers = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))

    async def answer(self, *a, **kw):
        self.answers.append((a, kw))


class FakeContext:
    async def _send(self, chat_id, text, **kwargs):
        pass

    @property
    def bot(self):
        return SimpleNamespace(send_message=self._send)


def _update(user_id=1, chat_id=1):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="admin"),
        effective_chat=SimpleNamespace(id=chat_id),
        callback_query=FakeQuery(), message=None)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_DATA_API_KEY", "test-yt-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-up-key")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
    monkeypatch.setenv("AUTOPILOT_DB_PATH", str(tmp_path / "bot.db"))
    svc = AutopilotService(db_path=str(tmp_path / "svc.db")).open()
    clip_gen, pub = FakeClipGenerator(), FakePublisher()
    youtube = FakeYouTubeClient([make_record("vid0000000x", now=NOW)])
    svc._orchestrator.runtime = Runtime(clip_generator=clip_gen.port(), publisher=pub.port())
    svc._orchestrator._client_factory = lambda: youtube
    install_fake_vendor(monkeypatch, pub)
    svc.update_settings({"publishing": {"upload_post_user": pub.user}, "enabled": True})
    automation_service._service = svc
    persistence.reset_store()
    yield svc
    automation_service.reset_service()
    persistence.reset_store()


class TestErrorsCenter:
    def test_no_errors_is_honest(self, service):
        update, context = _update(), FakeContext()
        run_async(errors_center.show(update, context))
        text, _kb = update.callback_query.edits[0]
        assert "No recent errors" in text

    def test_source_error_links_to_the_candidate(self, service):
        service.db.log_event("selection", "Could not submit source: boom",
                             level="error", source_id=42, youtube_video_id="vidX")
        update, context = _update(), FakeContext()
        run_async(errors_center.show(update, context))
        _text, kb = update.callback_query.edits[0]
        found = [b.callback_data for row in kb.inline_keyboard for b in row
                 if b.callback_data.startswith("candidates:show:")]
        assert found == ["candidates:show:42"]

    def test_publish_error_links_to_the_publish_attempt(self, service):
        service.db.log_event("publishing", "Cancellation failed", level="error",
                             source_id=7, publish_attempt_id=99)
        update, context = _update(), FakeContext()
        run_async(errors_center.show(update, context))
        _text, kb = update.callback_query.edits[0]
        found = [b.callback_data for row in kb.inline_keyboard for b in row
                 if b.callback_data.startswith("publishing:show:")]
        assert found == ["publishing:show:99"]

    def test_error_with_no_ids_at_all_does_not_crash(self, service):
        service.db.log_event("engine", "generic breaker trip", level="error")
        update, context = _update(), FakeContext()
        run_async(errors_center.show(update, context))  # must not raise
        text, _kb = update.callback_query.edits[0]
        assert "generic breaker trip" in text
