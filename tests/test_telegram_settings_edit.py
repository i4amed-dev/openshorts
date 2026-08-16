"""Settings guided editing: every mutation goes through `service.update_settings`
(never a parallel validation path in Telegram), rights-policy changes are
never silent, and platform toggling can never leave zero platforms active.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import automation.service as automation_service
from automation.service import AutopilotService
from autopilot_fakes import FakePublisher, install_fake_vendor, run_async
from telegram_bot import persistence
from telegram_bot.callbacks import Callback
from telegram_bot.handlers import settings


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)


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
        self.user_data = {}

    async def _send(self, chat_id, text, **kwargs):
        pass

    @property
    def bot(self):
        return SimpleNamespace(send_message=self._send)


def _update(*, text=None, has_query=False, user_id=1, chat_id=1):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="admin"),
        effective_chat=SimpleNamespace(id=chat_id),
        message=FakeMessage(text) if text is not None else None,
        callback_query=FakeQuery() if has_query else None)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_DATA_API_KEY", "test-yt-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-up-key")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
    monkeypatch.setenv("AUTOPILOT_DB_PATH", str(tmp_path / "bot.db"))
    svc = AutopilotService(db_path=str(tmp_path / "svc.db")).open()
    pub = FakePublisher()
    install_fake_vendor(monkeypatch, pub)
    svc.update_settings({"publishing": {"upload_post_user": pub.user}})
    automation_service._service = svc
    persistence.reset_store()
    yield svc
    automation_service.reset_service()
    persistence.reset_store()


class TestTopics:
    def test_add_topic_via_prompt(self, service):
        update, context = _update(has_query=True), FakeContext()
        run_async(settings._prompt(update, context, "topic_add", "Send a topic"))
        assert context.user_data[settings.PROMPT_KEY]["kind"] == "topic_add"

        msg_update = _update(text="machine learning")
        consumed = run_async(settings.handle_message(msg_update, context))
        assert consumed is True
        assert "machine learning" in service.get_settings()["discovery"]["topics"]
        assert settings.PROMPT_KEY not in context.user_data

    def test_remove_topic(self, service):
        service.update_settings({"discovery": {"topics": ["a", "b", "c"]}})
        update, context = _update(has_query=True), FakeContext()
        run_async(settings.topic_remove(update, context, 1))
        assert service.get_settings()["discovery"]["topics"] == ["a", "c"]

    def test_message_outside_any_prompt_is_ignored(self, service):
        update, context = _update(text="random chatter"), FakeContext()
        consumed = run_async(settings.handle_message(update, context))
        assert consumed is False


class TestRightsPolicy:
    def test_changing_policy_requires_confirmation_first(self, service):
        original = service.get_settings()["rights"]["policy"]
        update, context = _update(has_query=True), FakeContext()
        run_async(settings.rights_policy_confirm_screen(update, context, "OWNED_OR_ALLOWLISTED_CHANNELS"))
        # Nothing applied yet — only the confirmation screen was shown.
        assert service.get_settings()["rights"]["policy"] == original

    def test_confirming_actually_applies_it(self, service):
        service.update_settings({"rights": {"allowlisted_channel_ids": ["UC" + "x" * 22]}})
        update, context = _update(has_query=True), FakeContext()
        run_async(settings.rights_policy_apply(update, context, "OWNED_OR_ALLOWLISTED_CHANNELS"))
        assert service.get_settings()["rights"]["policy"] == "OWNED_OR_ALLOWLISTED_CHANNELS"

    def test_invalid_policy_change_is_rejected_not_silently_applied(self, service):
        """Owned/allowlisted with zero approved channels is invalid — the
        change must be refused, never silently weakening rights."""
        update, context = _update(has_query=True), FakeContext()
        run_async(settings.rights_policy_apply(update, context, "OWNED_OR_ALLOWLISTED_CHANNELS"))
        assert service.get_settings()["rights"]["policy"] != "OWNED_OR_ALLOWLISTED_CHANNELS"
        assert "show_alert" in update.callback_query.answers[-1][1]

    def test_add_and_remove_allowlisted_channel(self, service):
        update, context = _update(text="UC" + "y" * 22), FakeContext()
        context.user_data[settings.PROMPT_KEY] = {"kind": "allow_add"}
        run_async(settings.handle_message(update, context))
        ids = service.get_settings()["rights"]["allowlisted_channel_ids"]
        assert ids == ["UC" + "y" * 22]

        update2, context2 = _update(has_query=True), FakeContext()
        run_async(settings.allow_remove(update2, context2, 0))
        assert service.get_settings()["rights"]["allowlisted_channel_ids"] == []


class TestSchedule:
    def test_add_publish_time_slot(self, service):
        update, context = _update(text="13:45"), FakeContext()
        context.user_data[settings.PROMPT_KEY] = {"kind": "time_add"}
        run_async(settings.handle_message(update, context))
        assert "13:45" in service.get_settings()["schedule"]["publish_times"]

    def test_remove_publish_time_slot(self, service):
        times = list(service.get_settings()["schedule"]["publish_times"])
        update, context = _update(has_query=True), FakeContext()
        run_async(settings.time_remove(update, context, 0))
        assert service.get_settings()["schedule"]["publish_times"] == times[1:]

    def test_max_posts_per_day_rejects_non_numeric_input(self, service):
        original = service.get_settings()["schedule"]["max_posts_per_day"]
        update, context = _update(text="not a number"), FakeContext()
        context.user_data[settings.PROMPT_KEY] = {"kind": "max_posts"}
        run_async(settings.handle_message(update, context))
        assert "valid number" in update.message.sent[0].lower()
        assert service.get_settings()["schedule"]["max_posts_per_day"] == original

    def test_max_posts_per_day_updates_with_valid_input(self, service):
        update, context = _update(text="5"), FakeContext()
        context.user_data[settings.PROMPT_KEY] = {"kind": "max_posts"}
        run_async(settings.handle_message(update, context))
        assert service.get_settings()["schedule"]["max_posts_per_day"] == 5


class TestPlatforms:
    def test_toggle_off_and_back_on(self, service):
        active = set(service.get_settings()["publishing"]["platforms"])
        target = next(iter(active))
        update, context = _update(has_query=True), FakeContext()
        run_async(settings.platform_toggle(update, context, target))
        assert target not in service.get_settings()["publishing"]["platforms"]

        run_async(settings.platform_toggle(update, context, target))
        assert target in service.get_settings()["publishing"]["platforms"]

    def test_cannot_toggle_off_the_last_platform(self, service):
        # Drive down to exactly one active platform first.
        from automation.config import PLATFORMS
        service.update_settings({"publishing": {"platforms": [PLATFORMS[0]]}})
        update, context = _update(has_query=True), FakeContext()
        run_async(settings.platform_toggle(update, context, PLATFORMS[0]))
        # Rejected by config validation — still has the one platform.
        assert service.get_settings()["publishing"]["platforms"] == [PLATFORMS[0]]


class TestViewerCannotEdit:
    def test_viewer_cannot_add_a_topic(self, service, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "999")  # caller (1) becomes nobody
        update, context = _update(has_query=True), FakeContext()
        run_async(settings.discovery_edit(update, context))
        assert update.callback_query.edits == []
