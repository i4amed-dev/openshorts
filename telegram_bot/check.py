"""python -m telegram_bot.check — safe, read-only startup diagnostic.

Performs: getMe, authorized-config validation, Autopilot reachability, and a
notification-persistence write check. Never discovers, processes, publishes,
changes settings, or executes Emergency Stop — see spec section 69.
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from telegram import Bot  # noqa: E402  (after load_dotenv, matching quality_probe.py's pattern)
from telegram.error import TelegramError  # noqa: E402

from . import auth, persistence  # noqa: E402


async def _check_telegram() -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN is not set.")
        return False
    try:
        async with Bot(token) as bot:
            me = await bot.get_me()
        print(f"✅ Telegram reachable — bot is @{me.username}")
        return True
    except TelegramError as exc:
        print(f"❌ Could not reach Telegram: {exc}")
        return False


def _check_auth() -> bool:
    if not auth.is_configured():
        print("❌ TELEGRAM_ADMIN_USER_IDS is not set — the bot will start but grant "
              "nobody any access.")
        return False
    print("✅ At least one admin is configured.")
    viewers = os.environ.get("TELEGRAM_VIEWER_USER_IDS", "")
    print(f"   Viewers configured: {'yes' if viewers.strip() else 'no'}")
    allowed = auth.allowed_chat_ids()
    print(f"   Allowed chats: {'any (unset)' if not allowed else len(allowed)}")
    return True


def _check_autopilot() -> bool:
    try:
        from automation.service import get_service
        status = get_service().status()
        print(f"✅ Autopilot reachable — engine status: {status['status']}")
        return True
    except Exception as exc:
        print(f"⚠️ Autopilot not reachable from this process: {exc}")
        return False


def _check_persistence() -> bool:
    try:
        store = persistence.get_store()
        store.get_cursor()
        print(f"✅ Notification persistence is writable at {store.path}")
        return True
    except Exception as exc:
        print(f"❌ Notification persistence is not writable: {exc}")
        return False


async def main() -> int:
    print("Klippo Telegram bot — diagnostic check\n")
    results = [
        await _check_telegram(),
        _check_auth(),
        _check_autopilot(),
        _check_persistence(),
    ]
    ok = all(results)
    print("\n" + ("✅ All checks passed." if ok else "❌ Some checks failed — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
