"""Authorization. Fails closed: with no admin configured, nobody gets anything.

Roles are computed live from environment configuration on every request rather
than cached in the database, so a change to the env (or a restart) takes effect
immediately with no stale-role drift.

    TELEGRAM_ADMIN_USER_IDS   — full control (settings, processing, publishing,
                                 Emergency Stop)
    TELEGRAM_VIEWER_USER_IDS  — read-only (status, candidates, health, errors)
    TELEGRAM_ALLOWED_CHAT_IDS — destination policy only: which chats the bot will
                                 operate in / push notifications to. NOT a
                                 privilege grant — an allowed group does not make
                                 every member an admin.

Chat IDs and user IDs are always kept in separate sets; a control decision is
made on `effective_user.id`, never on `effective_chat.id`.
"""
from __future__ import annotations

import os
from enum import Enum
from typing import Optional, Set

from telegram import Update


class Role(str, Enum):
    ADMIN = "ADMIN"
    VIEWER = "VIEWER"


def _parse_ids(raw: str) -> Set[int]:
    return {int(x.strip()) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}


def _admin_ids() -> Set[int]:
    return _parse_ids(os.environ.get("TELEGRAM_ADMIN_USER_IDS", ""))


def _viewer_ids() -> Set[int]:
    return _parse_ids(os.environ.get("TELEGRAM_VIEWER_USER_IDS", ""))


def allowed_chat_ids() -> Set[int]:
    """Chats the bot will reply in / notify. Empty = every chat that reaches it
    (still gated on user role for anything but the "not configured" message)."""
    return _parse_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))


def is_configured() -> bool:
    """The fail-closed gate: at least one admin must be explicitly configured."""
    return bool(_admin_ids())


def role_for(user_id: Optional[int]) -> Optional[Role]:
    """Return the caller's role, or None if they have none — including the case
    where the bot has no admin configured at all, which grants no one anything."""
    if not is_configured() or user_id is None:
        return None
    if user_id in _admin_ids():
        return Role.ADMIN
    if user_id in _viewer_ids():
        return Role.VIEWER
    return None


def chat_allowed(chat_id: Optional[int]) -> bool:
    allowed = allowed_chat_ids()
    if not allowed:
        return True
    return chat_id in allowed


def role_for_update(update: Update) -> Optional[Role]:
    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id if user else None
    chat_id = chat.id if chat else None
    if not chat_allowed(chat_id):
        return None
    return role_for(user_id)


def has_role(update: Update, minimum: Role) -> bool:
    role = role_for_update(update)
    if role is None:
        return False
    if minimum == Role.VIEWER:
        return True  # ADMIN or VIEWER both satisfy a VIEWER requirement
    return role == Role.ADMIN


NOT_CONFIGURED_MESSAGE = (
    "🔒 This bot is not configured for access.\n\n"
    "Set TELEGRAM_ADMIN_USER_IDS in the server's .env with at least one Telegram "
    "user id, then restart Klippo."
)
NOT_AUTHORIZED_MESSAGE = "⛔ You're not authorized to use this bot."
ADMIN_REQUIRED_MESSAGE = "⛔ This action requires an admin."


def denial_message(update: Update, minimum: Role) -> str:
    if not is_configured():
        return NOT_CONFIGURED_MESSAGE
    role = role_for_update(update)
    if role is None:
        return NOT_AUTHORIZED_MESSAGE
    return ADMIN_REQUIRED_MESSAGE


async def guard(update: Update, minimum: Role, *, action: str) -> bool:
    """Call at the top of every command/callback handler.

    On success, registers the chat for notifications (an authorized touch is
    exactly the signal that this chat belongs to someone who should hear from
    Klippo) and returns True. On failure, replies with the precise denial
    reason, records it to the audit log, and returns False — callers must
    return immediately without doing any further work or leaking any data.
    """
    from . import persistence

    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id if user else None
    chat_id = chat.id if chat else None

    if has_role(update, minimum):
        if chat_id is not None:
            persistence.get_store().register_chat(chat_id, user_id,
                                                    user.username if user else None)
        return True

    persistence.get_store().log_action(
        user_id=user_id, chat_id=chat_id, action=action, result="denied")
    text = denial_message(update, minimum)
    if update.callback_query is not None:
        await update.callback_query.answer(text, show_alert=True)
    elif update.message is not None:
        await update.message.reply_text(text)
    return False
