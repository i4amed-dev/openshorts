"""python -m telegram_bot.send_test --chat-id <id> — sends exactly one
explicit test message. Never invoked automatically; a human runs this by hand
to confirm outbound delivery to a specific chat — see spec section 69.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from telegram import Bot  # noqa: E402
from telegram.error import TelegramError  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-id", required=True, type=int,
                        help="Telegram chat id to send the test message to")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN is not set.")
        return 1

    try:
        async with Bot(token) as bot:
            await bot.send_message(
                args.chat_id,
                "✅ Klippo Telegram bot — this is a manual test message. "
                "If you can read this, outbound delivery to this chat works.")
        print(f"✅ Sent to chat {args.chat_id}.")
        return 0
    except TelegramError as exc:
        print(f"❌ Could not send to chat {args.chat_id}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
