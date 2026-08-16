"""Schedule-a-clip: platforms -> date -> time -> confirm, and the reservation
it creates must be real (server-side validated, DB-uniqueness enforced) —
never something Telegram fabricates client-side.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import automation.service as automation_service
from automation.models import ClipState, GeneratedClip, PublishState, SourceState
from automation.ports import Runtime
from automation.service import AutopilotService
from autopilot_fakes import FakeClipGenerator, FakePublisher, FakeYouTubeClient, install_fake_vendor, make_record, run_async
from telegram_bot import persistence
from telegram_bot.handlers import publishing

NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)


class FakeQuery:
    def __init__(self, data=""):
        self.data = data
        self.edits = []
        self.answers = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))

    async def answer(self, *a, **kw):
        self.answers.append((a, kw))


class FakeContext:
    def __init__(self):
        self.user_data = {}

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
    svc.update_settings({
        "publishing": {"upload_post_user": pub.user, "platforms": ["youtube", "instagram"]},
        "schedule": {"publish_times": ["11:30", "16:30"]},
        "enabled": True,
    })
    automation_service._service = svc
    persistence.reset_store()
    yield svc
    automation_service.reset_service()
    persistence.reset_store()


def _pending_clip(service):
    run_async(service.orchestrator.run_discovery(now=NOW, force=True))
    eligible = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
    service.db.claim_source_for_processing(eligible.id, "job-1", "CREATIVE_COMMONS_ONLY")
    clip = GeneratedClip(source_id=eligible.id, job_id="job-1", clip_index=0,
                         filename="clip_0.mp4", title="A great clip",
                         start_seconds=0, end_seconds=20, rank=1, state=ClipState.PENDING)
    service.db.upsert_clip(clip)
    return service.db.list_clips(source_id=eligible.id)[0]


class TestFullFlow:
    def test_toggling_platforms_updates_the_checkbox_state(self, service):
        clip = _pending_clip(service)
        update, context = _update(), FakeContext()
        run_async(publishing.new(update, context, clip.id))
        assert context.user_data[publishing.SCHEDULE_KEY]["platforms"] == {"youtube", "instagram"}

        from telegram_bot.callbacks import Callback
        run_async(publishing.sched_toggle(update, context, Callback("publishing", "sched_toggle", ["youtube"])))
        assert context.user_data[publishing.SCHEDULE_KEY]["platforms"] == {"instagram"}

    def test_confirm_actually_creates_a_real_publish_attempt(self, service):
        clip = _pending_clip(service)
        update, context = _update(), FakeContext()
        run_async(publishing.new(update, context, clip.id))
        context.user_data[publishing.SCHEDULE_KEY]["day_token"] = "tomorrow"
        context.user_data[publishing.SCHEDULE_KEY]["hhmm"] = "16:30"

        run_async(publishing.sched_confirm(update, context))

        attempts = service.db.list_publish_attempts(states=[PublishState.PENDING], limit=10)
        assert len(attempts) == 1
        assert set(attempts[0].platforms) == {"youtube", "instagram"}
        assert service.db.get_clip(clip.id).state == ClipState.SCHEDULED
        assert publishing.SCHEDULE_KEY not in context.user_data

    def test_cannot_double_schedule_the_same_clip(self, service):
        clip = _pending_clip(service)
        update, context = _update(), FakeContext()
        run_async(publishing.new(update, context, clip.id))
        context.user_data[publishing.SCHEDULE_KEY]["day_token"] = "today"
        context.user_data[publishing.SCHEDULE_KEY]["hhmm"] = "16:30"
        run_async(publishing.sched_confirm(update, context))

        # Second attempt on the now-SCHEDULED clip must be refused up front.
        update2, context2 = _update(), FakeContext()
        run_async(publishing.new(update2, context2, clip.id))
        assert "not pending" in update2.callback_query.answers[-1][0][0].lower()

    def test_service_layer_refuses_a_platform_that_is_not_configured(self, service):
        """Defense in depth: even if a stale screen offered it, the service
        layer re-validates against the *current* configured platform set."""
        from datetime import date
        clip = _pending_clip(service)
        result = service.schedule_clip(clip.id, ["not_a_real_platform"], day=date(2099, 1, 1), hhmm="16:30")
        assert result["ok"] is False
        assert "platform" in result["reason"].lower()

    def test_cancel_clears_state_without_scheduling_anything(self, service):
        clip = _pending_clip(service)
        update, context = _update(), FakeContext()
        run_async(publishing.new(update, context, clip.id))
        run_async(publishing.sched_cancel(update, context))
        assert publishing.SCHEDULE_KEY not in context.user_data
        assert service.db.list_publish_attempts(limit=10) == []

    def test_expired_confirm_without_staged_state_is_refused(self, service):
        update, context = _update(), FakeContext()
        run_async(publishing.sched_confirm(update, context))
        assert "expired" in update.callback_query.answers[-1][0][0].lower()
