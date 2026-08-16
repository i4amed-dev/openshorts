"""The publishing center: every lifecycle state gets the right (and only the
right) actions, and nothing here is ever shown as "Published" without a real
vendor confirmation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import automation.service as automation_service
from automation.db import iso
from automation.models import PublishState
from automation.ports import Runtime
from automation.service import AutopilotService
from autopilot_fakes import FakeClipGenerator, FakePublisher, FakeYouTubeClient, install_fake_vendor, make_record, run_async
from telegram_bot import persistence
from telegram_bot.handlers import publishing

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
    youtube = FakeYouTubeClient([make_record(f"vid0000000{i}", now=NOW) for i in range(1, 3)])
    svc._orchestrator.runtime = Runtime(clip_generator=clip_gen.port(), publisher=pub.port())
    svc._orchestrator._client_factory = lambda: youtube
    svc.clip_gen, svc.publisher = clip_gen, pub
    install_fake_vendor(monkeypatch, pub)
    svc.update_settings({"publishing": {"upload_post_user": pub.user}, "enabled": True})
    automation_service._service = svc
    persistence.reset_store()
    yield svc
    automation_service.reset_service()
    persistence.reset_store()


def _run_to_scheduled(service, now=NOW):
    run_async(service.orchestrator.run_discovery(now=now, force=True))
    run_async(service.orchestrator._start_next_source(service.get_settings(), now=now))
    job_id = service.clip_gen.submissions[0]["job_id"]
    service.clip_gen.complete(job_id)
    active = service.db.active_processing_source()
    run_async(service.orchestrator._reconcile_active(active, service.get_settings(), now=now))
    run_async(service.orchestrator._schedule_ready_sources(service.get_settings(), now=now))
    return job_id


def _submitted_attempt(service, *, hours_ahead=6, now=NOW):
    _run_to_scheduled(service, now=now)
    attempt = service.db.list_publish_attempts(states=[PublishState.PENDING], limit=1)[0]
    service.db.execute("UPDATE publish_attempt SET scheduled_for_utc = ? WHERE id = ?",
                       (iso(now + timedelta(hours=hours_ahead)), attempt.id))
    service.db.set_publish_state(attempt.id, PublishState.IN_FLIGHT)
    service.db.set_publish_state(attempt.id, PublishState.SUBMITTED,
                                 vendor_job_id=f"scheduler_job_{attempt.id}",
                                 vendor_request_id=f"klippo-{attempt.id}")
    return service.db.get_publish_attempt(attempt.id)


class TestList:
    def test_empty_state(self, service):
        update, context = _update(), FakeContext()
        run_async(publishing.show_list(update, context, 0))
        text, _kb = update.callback_query.edits[0]
        assert "Nothing scheduled" in text

    def test_lists_real_attempts(self, service):
        _run_to_scheduled(service)
        update, context = _update(), FakeContext()
        run_async(publishing.show_list(update, context, 0))
        text, _kb = update.callback_query.edits[0]
        assert "Publishing" in text


class TestPendingCancel:
    def test_pending_offers_only_cancel(self, service):
        _run_to_scheduled(service)
        attempt = service.db.list_publish_attempts(states=[PublishState.PENDING], limit=1)[0]
        update, context = _update(), FakeContext()
        run_async(publishing.show_detail(update, context, attempt.id))
        _text, kb = update.callback_query.edits[0]
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert any("Cancel" in l for l in labels)
        assert not any("Retry" in l for l in labels)

    def test_cancel_pending_actually_cancels(self, service):
        _run_to_scheduled(service)
        attempt = service.db.list_publish_attempts(states=[PublishState.PENDING], limit=1)[0]
        update, context = _update(), FakeContext()
        run_async(publishing.cancel_pending(update, context, attempt.id))
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.CANCELED


class TestFailedRetry:
    def test_failed_offers_retry_and_it_works(self, service):
        _run_to_scheduled(service)
        attempt = service.db.list_publish_attempts(states=[PublishState.PENDING], limit=1)[0]
        service.db.set_publish_state(attempt.id, PublishState.FAILED, error="boom")
        update, context = _update(), FakeContext()
        run_async(publishing.retry(update, context, attempt.id))
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.PENDING


class TestUncertain:
    def test_uncertain_offers_check_resolve_and_force_retry(self, service):
        attempt = _submitted_attempt(service)
        service.db.set_publish_state(attempt.id, PublishState.UNCERTAIN)
        update, context = _update(), FakeContext()
        run_async(publishing.show_detail(update, context, attempt.id))
        _text, kb = update.callback_query.edits[0]
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert any("Check Status" in l for l in labels)
        assert any("Mark Published" in l for l in labels)
        assert any("Retry" in l for l in labels)

    def test_mark_published_records_operator_confirmation_not_vendor_confirmation(self, service):
        attempt = _submitted_attempt(service)
        service.db.set_publish_state(attempt.id, PublishState.UNCERTAIN)
        update, context = _update(), FakeContext()
        run_async(publishing.resolve(update, context, attempt.id))
        refreshed = service.db.get_publish_attempt(attempt.id)
        assert refreshed.state == PublishState.PUBLISHED
        assert "operator" in (refreshed.error or "").lower()

    def test_force_retry_never_double_sent_for_a_merely_failed_attempt(self, service):
        """force_retry is UNCERTAIN-only — a plain FAILED attempt must use retry."""
        _run_to_scheduled(service)
        attempt = service.db.list_publish_attempts(states=[PublishState.PENDING], limit=1)[0]
        service.db.set_publish_state(attempt.id, PublishState.FAILED)
        update, context = _update(), FakeContext()
        run_async(publishing.force_retry(update, context, attempt.id))
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.FAILED  # unchanged


class TestPartialFailure:
    def test_offers_accept_or_abandon_never_a_plain_retry(self, service):
        attempt = _submitted_attempt(service)
        service.db.set_publish_state(attempt.id, PublishState.PARTIAL_FAILED)
        update, context = _update(), FakeContext()
        run_async(publishing.show_detail(update, context, attempt.id))
        _text, kb = update.callback_query.edits[0]
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert any("Accept" in l for l in labels)
        assert any("Abandon" in l for l in labels)
        assert not any(l == "🔁 Retry" for l in labels)

    def test_abandon_never_resends_the_platforms_that_already_succeeded(self, service):
        attempt = _submitted_attempt(service)
        service.db.set_publish_state(attempt.id, PublishState.PARTIAL_FAILED)
        update, context = _update(), FakeContext()
        run_async(publishing.abandon(update, context, attempt.id))
        refreshed = service.db.get_publish_attempt(attempt.id)
        assert refreshed.state == PublishState.FAILED
        # Never re-queued as PENDING — abandoning must not trigger a resend.
        pending_ids = {a.id for a in service.db.list_publish_attempts(states=[PublishState.PENDING], limit=50)}
        assert attempt.id not in pending_ids


class TestPublishedIsTerminal:
    def test_published_offers_no_retry_or_cancel(self, service):
        attempt = _submitted_attempt(service)
        service.db.set_publish_state(attempt.id, PublishState.PUBLISHED)
        update, context = _update(), FakeContext()
        run_async(publishing.show_detail(update, context, attempt.id))
        _text, kb = update.callback_query.edits[0]
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert not any(l for l in labels if "Retry" in l or "Cancel" in l)


class TestSubmittedCancelVendor:
    def test_submitted_with_a_future_slot_offers_cancel_scheduled(self, service):
        attempt = _submitted_attempt(service, hours_ahead=6)
        update, context = _update(), FakeContext()
        run_async(publishing.show_detail(update, context, attempt.id))
        _text, kb = update.callback_query.edits[0]
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert any("Cancel Scheduled" in l for l in labels)

    def test_cancel_vendor_only_reports_success_on_vendor_confirmation(self, service):
        attempt = _submitted_attempt(service, hours_ahead=6)
        service.publisher.cancel_outcome = "not_found"
        update, context = _update(), FakeContext()
        run_async(publishing.cancel_vendor(update, context, attempt.id))
        refreshed = service.db.get_publish_attempt(attempt.id)
        # A 404 is never reported as cancelled — it may already be live.
        assert refreshed.state == PublishState.UNCERTAIN

    def test_cancel_vendor_succeeds_on_real_confirmation(self, service):
        attempt = _submitted_attempt(service, hours_ahead=6)
        update, context = _update(), FakeContext()
        run_async(publishing.cancel_vendor(update, context, attempt.id))
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.CANCELED


class TestNoDuplicateUpload:
    def test_retry_only_ever_works_on_a_failed_attempt(self, service):
        attempt = _submitted_attempt(service)  # SUBMITTED, not FAILED
        update, context = _update(), FakeContext()
        run_async(publishing.retry(update, context, attempt.id))
        # retry_publish() refuses anything but FAILED — state is untouched.
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.SUBMITTED
