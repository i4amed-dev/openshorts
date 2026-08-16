"""Klippo Autopilot — Telegram command center.

Runs as an asyncio task inside the same process as FastAPI (`app.py`'s
lifespan starts/stops it) — no separate process or port needed.

To activate, set in .env:
    TELEGRAM_BOT_TOKEN=<BotFather token>
    TELEGRAM_ADMIN_USER_IDS=<comma-separated Telegram user ids>

Get your user id by messaging @userinfobot on Telegram. Without at least one
admin id configured, the bot starts but grants nobody any access — see
`telegram_bot.auth` for the fail-closed model.
"""
from __future__ import annotations

from .app import start_bot, stop_bot

__all__ = ["start_bot", "stop_bot"]
