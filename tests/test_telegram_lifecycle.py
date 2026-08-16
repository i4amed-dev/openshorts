"""Startup/shutdown must be deterministic and must never take the FastAPI
backend down with them. Tests here only exercise paths that touch no network —
anything that would call the real Telegram API belongs in a live smoke test
(spec section 68: automated tests never contact real Telegram), not here.
"""
from __future__ import annotations

import importlib

from autopilot_fakes import run_async


class TestNoTokenIsANoOp:
    def test_start_bot_without_a_token_does_nothing(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        import telegram_bot.app as bot_app
        importlib.reload(bot_app)  # picks up the cleared env var as module-level TOKEN
        assert bot_app._app is None
        run_async(bot_app.start_bot())
        assert bot_app._app is None  # never built an Application, never touched the network

    def test_stop_bot_when_never_started_is_a_no_op(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        import telegram_bot.app as bot_app
        importlib.reload(bot_app)
        run_async(bot_app.stop_bot())  # must not raise
        assert bot_app._app is None


class TestRateLimiterFallback:
    def test_missing_aiolimiter_extra_does_not_crash_startup(self, monkeypatch):
        """`python-telegram-bot[rate-limiter]` may not be installed everywhere
        this bot runs. AIORateLimiter() raises RuntimeError in that case —
        the builder must fall back cleanly, never propagate it. Forced
        deterministically here regardless of whether this environment
        actually has the extra installed."""
        import telegram.ext as ext
        from telegram_bot.app import _with_rate_limiter

        def _boom(*a, **kw):
            raise RuntimeError("extra not installed")

        monkeypatch.setattr(ext, "AIORateLimiter", _boom)

        class StubBuilder:
            def rate_limiter(self, rl):
                raise AssertionError("must not attach a rate limiter that can't construct")

        stub = StubBuilder()
        assert _with_rate_limiter(stub) is stub  # unchanged, no exception

    def test_available_aiolimiter_is_attached(self, monkeypatch):
        import telegram.ext as ext
        from telegram_bot.app import _with_rate_limiter

        sentinel = object()
        monkeypatch.setattr(ext, "AIORateLimiter", lambda: sentinel)

        class StubBuilder:
            def rate_limiter(self, rl):
                assert rl is sentinel
                return "configured"

        assert _with_rate_limiter(StubBuilder()) == "configured"


class TestRouteTableIntegrity:
    """The package importing at all without a RuntimeError already proves no
    handler module double-registers a namespace — see Router.register()."""

    def test_every_phase_0_screen_has_a_registered_route(self):
        from telegram_bot.callbacks import router
        for ns in ("home", "status", "settings", "admin", "health", "errors", "noop"):
            assert ns in router._routes, f"missing callback route: {ns}"

    def test_command_list_has_no_duplicate_commands(self):
        from telegram_bot.app import _COMMANDS
        names = [c.command for c in _COMMANDS]
        assert len(names) == len(set(names))
