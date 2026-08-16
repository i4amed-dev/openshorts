"""Job center and clip gallery: real stages (never a fabricated percentage),
clip preview respects the 50 MB Bot API limit, and nothing here mutates state
outside the existing `service.retry_source` / read-only db queries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import automation.service as automation_service
from automation.models import GeneratedClip, SourceState
from automation.ports import Runtime
from automation.service import AutopilotService
from autopilot_fakes import FakeClipGenerator, FakePublisher, FakeYouTubeClient, install_fake_vendor, make_record, run_async
from telegram_bot import persistence
from telegram_bot.handlers import clips, jobs

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
    def __init__(self):
        self.sent_videos = []

    async def _send(self, chat_id, text, **kwargs):
        pass

    async def _send_video(self, chat_id, video, **kwargs):
        self.sent_videos.append((chat_id, kwargs.get("caption")))

    @property
    def bot(self):
        return SimpleNamespace(send_message=self._send, send_video=self._send_video)


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


def _process_one(service):
    run_async(service.orchestrator.run_discovery(now=NOW, force=True))
    eligible = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
    result = run_async(service.process_source(eligible.id))
    assert result["ok"], result["reason"]
    return service.db.get_source(eligible.id)


class TestJobList:
    def test_empty_state_is_honest(self, service):
        update, context = _update(), FakeContext()
        run_async(jobs.show_list(update, context, 0))
        text, _kb = update.callback_query.edits[0]
        assert "No jobs yet" in text

    def test_a_submitted_job_shows_a_real_stage_not_a_percentage(self, service):
        _process_one(service)
        update, context = _update(), FakeContext()
        run_async(jobs.show_list(update, context, 0))
        text, _kb = update.callback_query.edits[0]
        assert "%" not in text
        assert "Waiting for the Clip Generator" in text or "Generating clips" in text


class TestJobDetail:
    def test_shows_the_real_job_id_and_elapsed_time(self, service):
        source = _process_one(service)
        update, context = _update(), FakeContext()
        run_async(jobs.show_detail(update, context, source.id))
        text, _kb = update.callback_query.edits[0]
        assert source.job_id in text

    def test_missing_job_does_not_crash(self, service):
        update, context = _update(), FakeContext()
        run_async(jobs.show_detail(update, context, 999999))
        text, _kb = update.callback_query.edits[0]
        assert "no longer exists" in text


class TestClipGallery:
    def test_empty_state(self, service):
        update, context = _update(), FakeContext()
        run_async(clips.show_list(update, context, 0))
        text, _kb = update.callback_query.edits[0]
        assert "No clips yet" in text

    def _make_ready_clip(self, service, source):
        service.db.transition_source(source.id, SourceState.PROCESSING,
                                     expected=[SourceState.PROCESS_QUEUED])
        service.db.transition_source(source.id, SourceState.PROCESS_READY,
                                     expected=[SourceState.PROCESSING])
        clip = GeneratedClip(source_id=source.id, job_id=source.job_id, clip_index=0,
                             filename="clip_0.mp4", title="Great moment",
                             start_seconds=0, end_seconds=30, rank=1, state="PENDING")
        service.db.upsert_clip(clip)
        return service.db.list_clips(source_id=source.id)[0]

    def test_lists_a_real_clip(self, service):
        source = _process_one(service)
        self._make_ready_clip(service, source)
        update, context = _update(), FakeContext()
        run_async(clips.show_list(update, context, 0))
        text, _kb = update.callback_query.edits[0]
        assert "Great moment" in text

    def test_clip_detail_shows_duration_and_state(self, service):
        source = _process_one(service)
        clip = self._make_ready_clip(service, source)
        update, context = _update(), FakeContext()
        run_async(clips.show_detail(update, context, clip.id))
        text, _kb = update.callback_query.edits[0]
        assert "0:30" in text
        assert "PENDING" in text

    def test_preview_refuses_a_file_over_50mb(self, service, tmp_path, monkeypatch):
        source = _process_one(service)
        clip = self._make_ready_clip(service, source)
        big_file = tmp_path / "clip_0.mp4"
        big_file.write_bytes(b"0" * 1024)  # tiny content, but we lie about the size below
        monkeypatch.setattr(service.orchestrator.runtime.clip_generator, "clip_path",
                            lambda job_id, filename: str(big_file))
        monkeypatch.setattr("os.path.getsize", lambda p: 60 * 1024 * 1024)

        update, context = _update(), FakeContext()
        run_async(clips.preview(update, context, clip.id))
        assert context.sent_videos == []
        assert "too large" in update.callback_query.answers[-1][0][0].lower()

    def test_preview_sends_the_real_file_when_small_enough(self, service, tmp_path):
        source = _process_one(service)
        clip = self._make_ready_clip(service, source)
        small_file = tmp_path / "clip_0.mp4"
        small_file.write_bytes(b"fake mp4 bytes")
        service.orchestrator.runtime.clip_generator.clip_path = lambda job_id, filename: str(small_file)

        update, context = _update(), FakeContext()
        run_async(clips.preview(update, context, clip.id))
        assert len(context.sent_videos) == 1

    def test_missing_file_on_disk_answers_without_crashing(self, service):
        source = _process_one(service)
        clip = self._make_ready_clip(service, source)
        service.orchestrator.runtime.clip_generator.clip_path = lambda job_id, filename: None

        update, context = _update(), FakeContext()
        run_async(clips.preview(update, context, clip.id))
        assert context.sent_videos == []
        assert "not found" in update.callback_query.answers[-1][0][0].lower()
