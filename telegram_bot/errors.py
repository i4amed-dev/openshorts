"""Centralized error handling: PTB exception classification, message delivery
with a guaranteed-safe fallback chain, and the Application-level error handler.

Nothing in this bot should ever silently `except Exception: pass`. Delivery
failures fall back in a fixed order (edit → fresh send → plain text) and are
always logged; anything that still fails reaches `on_error`, which logs the
full traceback server-side and shows the user one safe, non-leaking message.
"""
from __future__ import annotations

import logging
import traceback
from typing import Optional

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ContextTypes

from . import render

log = logging.getLogger("telegram_bot")

_NOT_MODIFIED = "message is not modified"


async def deliver(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                   kb: Optional[InlineKeyboardMarkup] = None, *, edit: bool = True) -> None:
    """Send or edit a message, with every reliability fallback the spec asks for:

    1. Prefer editing the triggering callback's message (keeps the screen in place).
    2. "message is not modified" from Telegram is not a failure — ignore it.
    3. Any other edit failure (deleted message, too old, never editable) falls
       back to sending a fresh message rather than leaving the user thinking
       the button is broken.
    4. If HTML parsing itself is rejected, retry once as plain text and log the
       renderer bug — the user still gets a response.
    """
    text = render.truncate(text)
    query = update.callback_query if edit else None
    chat = update.effective_chat

    if query is not None:
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        except BadRequest as exc:
            if _NOT_MODIFIED in str(exc).lower():
                return
            if "can't parse entities" in str(exc).lower():
                log.warning("HTML render rejected, falling back to plain text: %s", exc)
                try:
                    await query.edit_message_text(render.strip_tags(text), reply_markup=kb)
                    return
                except BadRequest:
                    pass  # fall through to fresh-send below
            log.info("Could not edit message (%s) — sending a fresh one instead", exc)
        except TelegramError as exc:
            log.warning("Edit failed (%s) — sending a fresh message instead", exc)

    if chat is None:
        return
    try:
        await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except BadRequest as exc:
        if "can't parse entities" in str(exc).lower():
            log.warning("HTML render rejected on fresh send, falling back to plain text: %s", exc)
            await context.bot.send_message(chat.id, render.strip_tags(text), reply_markup=kb)
        else:
            raise


def classify(exc: BaseException) -> str:
    if isinstance(exc, RetryAfter):
        return "rate_limited"
    if isinstance(exc, Forbidden):
        return "forbidden"
    if isinstance(exc, TimedOut):
        return "timed_out"
    if isinstance(exc, NetworkError):
        return "network_error"
    if isinstance(exc, BadRequest):
        return "bad_request"
    if isinstance(exc, TelegramError):
        return "telegram_error"
    return "internal_error"


_SAFE_MESSAGES = {
    "rate_limited": "⏳ Telegram is rate-limiting the bot right now — please try again shortly.",
    "forbidden": "⛔ The bot can't reach this chat anymore.",
    "timed_out": "⏳ Telegram timed out — please try again.",
    "network_error": "📡 Network hiccup talking to Telegram — please try again.",
    "bad_request": "❌ Couldn't complete that request.",
    "telegram_error": "❌ Something went wrong talking to Telegram.",
    "internal_error": "❌ Something went wrong. It's been logged.",
}


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registered via `Application.add_error_handler`. The one place an
    unhandled exception from any handler ends up — never allowed to crash the
    bot or vanish silently."""
    exc = context.error
    category = classify(exc) if exc else "internal_error"
    log.error("Unhandled Telegram error (%s): %s\n%s", category, exc,
               "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
               if exc else "")

    if isinstance(exc, Forbidden) and isinstance(update, Update) and update.effective_chat:
        from . import persistence
        persistence.get_store().mark_blocked(update.effective_chat.id)
        return  # nothing we can send back to a chat that blocked us

    if not isinstance(update, Update):
        return
    text = _SAFE_MESSAGES.get(category, _SAFE_MESSAGES["internal_error"])
    try:
        if update.callback_query is not None:
            try:
                await update.callback_query.answer(text[:200], show_alert=True)
            except TelegramError:
                pass
        elif update.effective_chat is not None:
            await context.bot.send_message(update.effective_chat.id, text)
    except TelegramError as send_exc:
        log.warning("Could not deliver error message to user: %s", send_exc)
