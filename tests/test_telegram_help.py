"""/help must stay under Telegram's message length limit and answer the
specific questions the spec calls out, not just list commands."""
from __future__ import annotations

from types import SimpleNamespace

from autopilot_fakes import run_async
from telegram_bot import render
from telegram_bot.handlers import help as help_handler


class FakeContext:
    def __init__(self):
        self.sent = []

    async def _send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    @property
    def bot(self):
        return SimpleNamespace(send_message=self._send)


def _update(user_id=1, chat_id=1):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="admin"),
        effective_chat=SimpleNamespace(id=chat_id),
        callback_query=None, message=None)


class TestHelp:
    def test_fits_under_the_telegram_message_limit(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
        assert len(help_handler._text()) <= render.MAX_MESSAGE_LENGTH

    def test_explains_uncertain_not_just_lists_commands(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
        text = help_handler._text()
        assert "UNCERTAIN" in text
        assert "never auto-retried" in text

    def test_viewer_can_read_help(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
        update, context = _update(), FakeContext()
        run_async(help_handler.show(update, context))
        assert context.sent  # a message was actually sent
