"""Authorization must fail closed: with no admin configured, nobody — not even
a configured viewer, not even someone in an allowed chat — gets anything.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from autopilot_fakes import run_async
from telegram_bot import auth, persistence


def _update(*, user_id=None, chat_id=None):
    user = SimpleNamespace(id=user_id, username="tester") if user_id is not None else None
    chat = SimpleNamespace(id=chat_id) if chat_id is not None else None
    return SimpleNamespace(effective_user=user, effective_chat=chat, message=None, callback_query=None)


class TestFailClosed:
    def test_no_admin_configured_denies_everyone(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
        monkeypatch.delenv("TELEGRAM_VIEWER_USER_IDS", raising=False)
        assert auth.is_configured() is False
        assert auth.role_for(12345) is None

    def test_no_admin_configured_denies_even_a_would_be_viewer(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
        monkeypatch.setenv("TELEGRAM_VIEWER_USER_IDS", "999")
        assert auth.role_for(999) is None

    def test_unknown_user_denied(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
        assert auth.role_for(2) is None

    def test_viewer_role_granted(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
        monkeypatch.setenv("TELEGRAM_VIEWER_USER_IDS", "2")
        assert auth.role_for(2) is auth.Role.VIEWER
        assert auth.role_for(1) is auth.Role.ADMIN

    def test_viewer_cannot_satisfy_admin_requirement(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
        monkeypatch.setenv("TELEGRAM_VIEWER_USER_IDS", "2")
        update = _update(user_id=2, chat_id=2)
        assert auth.has_role(update, auth.Role.VIEWER) is True
        assert auth.has_role(update, auth.Role.ADMIN) is False

    def test_admin_satisfies_viewer_requirement(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
        monkeypatch.delenv("TELEGRAM_VIEWER_USER_IDS", raising=False)
        update = _update(user_id=1, chat_id=1)
        assert auth.has_role(update, auth.Role.VIEWER) is True
        assert auth.has_role(update, auth.Role.ADMIN) is True

    def test_allowed_group_does_not_promote_every_member_to_admin(self, monkeypatch):
        """An allowed chat is a destination policy, never a privilege grant."""
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-100555")
        random_member = _update(user_id=54321, chat_id=-100555)
        assert auth.has_role(random_member, auth.Role.VIEWER) is False

    def test_chat_not_in_allowlist_denies_even_the_admin(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
        update = _update(user_id=1, chat_id=999)
        assert auth.has_role(update, auth.Role.ADMIN) is False

    def test_empty_allowlist_allows_any_chat_for_an_authorized_user(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
        monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
        update = _update(user_id=1, chat_id=999)
        assert auth.has_role(update, auth.Role.ADMIN) is True

    def test_denial_message_distinguishes_not_configured_from_not_authorized(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
        update = _update(user_id=1, chat_id=1)
        assert auth.denial_message(update, auth.Role.VIEWER) == auth.NOT_CONFIGURED_MESSAGE

        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
        update = _update(user_id=2, chat_id=2)
        assert auth.denial_message(update, auth.Role.VIEWER) == auth.NOT_AUTHORIZED_MESSAGE

        update = _update(user_id=1, chat_id=1)
        monkeypatch.setenv("TELEGRAM_VIEWER_USER_IDS", "1")
        # user 1 is actually admin (set above), so re-derive a pure viewer:
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "999")
        assert auth.denial_message(update, auth.Role.ADMIN) == auth.ADMIN_REQUIRED_MESSAGE


class TestGuard:
    @pytest.fixture(autouse=True)
    def _isolated_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTOPILOT_DB_PATH", str(tmp_path / "bot.db"))
        persistence.reset_store()
        yield
        persistence.reset_store()

    def test_denied_call_is_audited_and_does_not_register_the_chat(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
        update = _update(user_id=7, chat_id=7)

        class FakeMessage:
            def __init__(self):
                self.replies = []

            async def reply_text(self, text):
                self.replies.append(text)

        update.message = FakeMessage()
        allowed = run_async(auth.guard(update, auth.Role.VIEWER, action="test:denied"))
        assert allowed is False
        assert update.message.replies == [auth.NOT_CONFIGURED_MESSAGE]
        assert persistence.get_store().known_chat_ids() == []
        actions = persistence.get_store().recent_actions()
        assert actions[0]["action"] == "test:denied"
        assert actions[0]["result"] == "denied"

    def test_allowed_call_registers_the_chat(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "7")
        update = _update(user_id=7, chat_id=7)
        allowed = run_async(auth.guard(update, auth.Role.ADMIN, action="test:allowed"))
        assert allowed is True
        assert persistence.get_store().known_chat_ids() == [7]
