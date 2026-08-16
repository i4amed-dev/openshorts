"""The candidate explorer end to end: it must read through `automation.service`
(never raw SQL), let an operator process a *specific* candidate out of score
order, and never offer Process on a rights-blocked one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import automation.service as automation_service
from automation.models import SourceState
from automation.ports import Runtime
from automation.service import AutopilotService
from autopilot_fakes import (
    FakeClipGenerator, FakePublisher, FakeYouTubeClient, install_fake_vendor, make_record,
    run_async,
)
from telegram_bot import auth, persistence
from telegram_bot.handlers import candidates

NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.edits = []
        self.answers = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))

    async def answer(self, *a, **kw):
        self.answers.append((a, kw))


class FakeContext:
    def __init__(self):
        self.bot = SimpleNamespace(send_message=self._send)
        self.sent = []

    async def _send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))


def _update(*, callback_data=None, user_id=1, chat_id=1):
    query = FakeQuery(callback_data) if callback_data else None
    message = None if query else FakeMessage()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="admin"),
        effective_chat=SimpleNamespace(id=chat_id),
        callback_query=query, message=message)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_DATA_API_KEY", "test-yt-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-up-key")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
    monkeypatch.setenv("AUTOPILOT_DB_PATH", str(tmp_path / "bot.db"))

    svc = AutopilotService(db_path=str(tmp_path / "svc.db")).open()
    clip_gen = FakeClipGenerator()
    publisher = FakePublisher()
    youtube = FakeYouTubeClient([make_record(f"vid0000000{i}", now=NOW) for i in range(1, 4)])
    svc._orchestrator.runtime = Runtime(clip_generator=clip_gen.port(), publisher=publisher.port())
    svc._orchestrator._client_factory = lambda: youtube
    install_fake_vendor(monkeypatch, publisher)
    svc.update_settings({"publishing": {"upload_post_user": publisher.user}, "enabled": True})

    automation_service._service = svc
    persistence.reset_store()
    yield svc
    automation_service.reset_service()
    persistence.reset_store()


class TestCandidateList:
    def test_empty_queue_explains_why_with_real_counts(self, service):
        update = _update()
        context = FakeContext()
        run_async(candidates.show_list(update, context, page=0))
        text = context.sent[0][1]
        assert "No candidate is ready yet" in text

    def test_lists_eligible_candidates_with_pagination(self, service):
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        update = _update()
        context = FakeContext()
        run_async(candidates.show_list(update, context, page=0))
        text = context.sent[0][1]
        assert "Candidates" in text
        assert "of 3" in text  # 3 fake records from the fixture


class TestCandidateDetail:
    def test_eligible_candidate_offers_process_and_skip(self, service):
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        eligible = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        update = _update(callback_data=f"candidates:show:{eligible.id}")
        context = FakeContext()
        run_async(candidates.show_detail(update, context, eligible.id))
        text, kb = update.callback_query.edits[0]
        assert "Opportunity" in text
        assert "✅ Eligible" in text
        button_labels = [b.text for row in kb.inline_keyboard for b in row]
        assert any("Process" in label for label in button_labels)
        assert any("Skip" in label for label in button_labels)

    def test_a_rights_blocked_candidate_never_offers_process(self, service):
        """The whole point of the rights-blocked state: it must be viewable
        but never carry an action that would bypass the policy."""
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        eligible = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        service.db.transition_source(eligible.id, SourceState.FILTERED,
                                     expected=[SourceState.ELIGIBLE],
                                     rejection_reason="rights_policy")
        blocked = service.db.get_source(eligible.id)

        update = _update(callback_data=f"candidates:show:{blocked.id}")
        context = FakeContext()
        run_async(candidates.show_detail(update, context, blocked.id))
        text, kb = update.callback_query.edits[0]
        assert "🔒 Blocked" in text
        button_labels = [b.text for row in kb.inline_keyboard for b in row]
        assert not any("Process" in label for label in button_labels)


class TestProcessSpecificCandidate:
    def test_process_starts_the_chosen_candidate(self, service):
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        eligible = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        update = _update(callback_data=f"candidates:process:{eligible.id}")
        context = FakeContext()
        run_async(candidates.process(update, context, eligible.id))

        refreshed = service.db.get_source(eligible.id)
        assert refreshed.state == SourceState.PROCESS_QUEUED
        text, _kb = update.callback_query.edits[0]
        assert "Submitted" in text

    def test_viewer_cannot_process(self, service, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "999")  # caller (id=1) is now nobody
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        eligible = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        update = _update(callback_data=f"candidates:process:{eligible.id}", user_id=1)
        context = FakeContext()
        run_async(candidates.process(update, context, eligible.id))

        refreshed = service.db.get_source(eligible.id)
        assert refreshed.state == SourceState.ELIGIBLE  # untouched


class TestSkip:
    def test_skip_removes_it_from_the_queue(self, service):
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        eligible = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        update = _update(callback_data=f"candidates:skip:{eligible.id}")
        context = FakeContext()
        run_async(candidates.skip(update, context, eligible.id))
        assert service.db.get_source(eligible.id).state == SourceState.SKIPPED
