"""Application lifecycle: build, start, stop. One bot instance, one polling
loop, one JobQueue, no duplicate handlers across a restart — `stop_bot()`
always tears the previous `Application` down completely before `start_bot()`
is allowed to build a new one.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from . import auth, callbacks, notifications, persistence
from . import errors as delivery
from .handlers import (
    admin, candidates, clips, discovery, errors as errors_handler, health, help as help_handler,
    home, jobs, new_video, notifications as notify_ui, publishing, settings, status,
)

# --- multi-step text conversations ------------------------------------------------
#
# Both `/new` (paste a URL) and the Settings edit prompts (add a topic, a
# channel id, a time slot…) collect one line of free text. Rather than one
# MessageHandler per flow — ambiguous about which fires first — there is a
# single text handler that asks each stateful flow in turn whether it owns
# this message, and a single /cancel that clears whichever one is pending.


async def _dispatch_text(update, context) -> None:
    if await settings.handle_message(update, context):
        return
    if await notify_ui.handle_message(update, context):
        return
    await new_video.handle_message(update, context)


async def _cancel_any(update, context) -> None:
    had_state = bool(context.user_data.get(new_video.STAGE_KEY)
                     or context.user_data.get(settings.PROMPT_KEY)
                     or context.user_data.get(publishing.SCHEDULE_KEY)
                     or context.user_data.get(notify_ui.PROMPT_KEY))
    video_state = context.user_data.pop(new_video.STAGE_KEY, None)
    if video_state:
        new_video._cleanup_upload(video_state)  # drop any downloaded-but-unused file
    context.user_data.pop(settings.PROMPT_KEY, None)
    context.user_data.pop(publishing.SCHEDULE_KEY, None)
    context.user_data.pop(notify_ui.PROMPT_KEY, None)
    await update.message.reply_text("❌ Cancelled." if had_state else "Nothing to cancel.")

log = logging.getLogger("telegram_bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

_app: Optional[Application] = None

_COMMANDS = [
    BotCommand("home", "Klippo command center"),
    BotCommand("status", "Status + inline controls"),
    BotCommand("monitor", "Live activity snapshot"),
    BotCommand("discover", "Discovery control center"),
    BotCommand("candidates", "Ranked candidate queue"),
    BotCommand("new", "Submit a YouTube URL manually"),
    BotCommand("jobs", "Clip Generator jobs"),
    BotCommand("clips", "Generated clip gallery"),
    BotCommand("publishing", "Publishing lifecycle"),
    BotCommand("notifications", "Notification preferences"),
    BotCommand("cancel", "Cancel the current multi-step action"),
    BotCommand("queue", "Candidate queue"),
    BotCommand("schedule", "Upcoming posts"),
    BotCommand("settings", "View settings"),
    BotCommand("health", "Operational health"),
    BotCommand("errors", "Recent errors"),
    BotCommand("pause", "Pause Autopilot"),
    BotCommand("resume", "Resume Autopilot"),
    BotCommand("help", "What Klippo can do, grouped by task"),
]


def _with_rate_limiter(builder):
    """Attach PTB's built-in AIORateLimiter — guards against button-spam and
    double-tap bursts hitting Telegram's API limits (spec section 37).

    Requires the `rate-limiter` extra (`aiolimiter`); its absence must not
    stop the bot from starting, just leave it unrate-limited.
    """
    try:
        from telegram.ext import AIORateLimiter
        return builder.rate_limiter(AIORateLimiter())
    except RuntimeError:
        log.warning(
            "AIORateLimiter unavailable (install python-telegram-bot[rate-limiter]) — "
            "starting without built-in rate limiting.")
        return builder


async def start_bot() -> None:
    """Build and start the Telegram bot. Called from app.py's FastAPI lifespan.

    A broken or unconfigured bot must never take FastAPI down with it — every
    failure here is caught by the caller (app.py), logged once, and the
    backend keeps serving.
    """
    global _app

    if not TOKEN:
        log.info("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled.")
        return
    if _app is not None:
        log.warning("start_bot() called while a bot instance is already running — ignoring.")
        return
    if not auth.is_configured():
        log.warning(
            "TELEGRAM_ADMIN_USER_IDS is not set — the bot will start but grant "
            "no access to anyone until it is configured.")

    persistence.get_store()  # fail fast if the DB path isn't writable
    notifications.prime_cursor_if_empty()

    application = _with_rate_limiter(Application.builder().token(TOKEN)).build()

    application.add_handler(CommandHandler(["start", "home"], home.show))
    application.add_handler(CommandHandler("help", help_handler.show))
    application.add_handler(CommandHandler("status", status.cmd_status))
    application.add_handler(CommandHandler("monitor", status.cmd_monitor))
    application.add_handler(CommandHandler("queue", status.cmd_queue))
    application.add_handler(CommandHandler("schedule", status.cmd_schedule))
    application.add_handler(CommandHandler("settings", settings.show))
    application.add_handler(CommandHandler("health", health.show))
    application.add_handler(CommandHandler("errors", errors_handler.show))
    application.add_handler(CommandHandler("discover", discovery.show))
    application.add_handler(CommandHandler("candidates", candidates.show_list))
    application.add_handler(CommandHandler("new", new_video.start))
    application.add_handler(CommandHandler("cancel", _cancel_any))
    application.add_handler(CommandHandler("jobs", jobs.show_list))
    application.add_handler(CommandHandler("clips", clips.show_list))
    application.add_handler(CommandHandler("publishing", publishing.show_list))
    application.add_handler(CommandHandler("notifications", notify_ui.show))
    application.add_handler(CommandHandler("process", admin.process_next))
    application.add_handler(CommandHandler("pause", admin.pause))
    application.add_handler(CommandHandler("resume", admin.resume))
    application.add_handler(CommandHandler("enable", admin.enable))
    application.add_handler(CommandHandler("disable", admin.disable))
    application.add_handler(CommandHandler("stop", admin.stop_warn))
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, new_video.handle_upload))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _dispatch_text))
    application.add_handler(CallbackQueryHandler(callbacks.router.dispatch))
    application.add_error_handler(delivery.on_error)

    application.job_queue.run_repeating(
        notifications.poll, interval=notifications.POLL_INTERVAL_SECONDS, first=15)
    application.job_queue.run_repeating(
        notifications.digest, interval=notifications.DIGEST_CHECK_INTERVAL_SECONDS, first=30)

    await application.initialize()
    await application.bot.set_my_commands(_COMMANDS)
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    _app = application
    log.info("Telegram bot started (admin configured: %s).", auth.is_configured())


async def stop_bot() -> None:
    """Graceful, deterministic shutdown. Called from app.py's lifespan cleanup."""
    global _app
    if _app is None:
        return
    application = _app
    _app = None  # clear first: a concurrent start_bot() must never see a half-torn-down app
    try:
        if application.updater is not None and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()
    except Exception as exc:
        log.warning("Bot shutdown error: %s", exc)
    finally:
        persistence.reset_store()
