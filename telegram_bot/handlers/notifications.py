"""/notifications — per-chat preferences: mode, categories, quiet hours,
daily digest. Persisted in `telegram_bot.persistence`, survives a restart.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, navigation, persistence, render
from .. import errors as delivery
from ..callbacks import Callback, router

_MODE_LABELS = {
    "critical_only": "Critical only", "important": "Important",
    "everything": "Everything", "muted": "Muted",
}
_CATEGORY_LABELS = {
    "critical_errors": "Critical errors", "source_selected": "Source selected",
    "processing_started": "Processing started", "clips_ready": "Clips ready",
    "publishing": "Publishing", "discovery_summary": "Discovery summary",
    "debug_recovery": "Debug/recovery", "daily_summary": "Daily summary",
}
PROMPT_KEY = "notify_prompt"


def _text(prefs: dict) -> str:
    lines = [
        "🔔 " + render.bold("Notifications"), "",
        f"Mode: {render.esc(_MODE_LABELS.get(prefs['notify_mode'], prefs['notify_mode']))}",
        "",
        render.bold("Categories:"),
    ]
    for key, label in _CATEGORY_LABELS.items():
        lines.append(f"{'☑' if prefs['categories'].get(key, True) else '☐'} {label}")
    lines += ["", render.bold("Quiet hours:")]
    if prefs.get("quiet_hours_start") and prefs.get("quiet_hours_end"):
        lines.append(f"{prefs['quiet_hours_start']}–{prefs['quiet_hours_end']}")
    else:
        lines.append(render.italic("Not set"))
    lines += ["", render.bold("Daily digest:"),
              ("✅ " if prefs["daily_digest_enabled"] else "❌ ") +
              f"at {render.esc(prefs['daily_digest_time'])}"]
    return "\n".join(lines)


def _kb(prefs: dict):
    rows = [[InlineKeyboardButton(("✅ " if m == prefs["notify_mode"] else "") + label,
                                  callback_data=callbacks.build("notify", "mode", m))]
            for m, label in _MODE_LABELS.items()]
    rows.append([InlineKeyboardButton(
        f"{'☑' if prefs['categories'].get(k, True) else '☐'} {label}",
        callback_data=callbacks.build("notify", "cat", k))
        for k, label in list(_CATEGORY_LABELS.items())[:2]])
    for i in range(2, len(_CATEGORY_LABELS), 2):
        pair = list(_CATEGORY_LABELS.items())[i:i + 2]
        rows.append([InlineKeyboardButton(
            f"{'☑' if prefs['categories'].get(k, True) else '☐'} {label}",
            callback_data=callbacks.build("notify", "cat", k)) for k, label in pair])
    rows.append([InlineKeyboardButton("🌙 Set quiet hours", callback_data=callbacks.build("notify", "quiet"))])
    rows.append([InlineKeyboardButton(
        "☀️ Disable digest" if prefs["daily_digest_enabled"] else "☀️ Enable digest",
        callback_data=callbacks.build("notify", "digest_toggle"))])
    rows.append(navigation.nav_row(refresh=callbacks.build("notify", "show")))
    return navigation.kb(*rows)


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="notify:show"):
        return
    chat = update.effective_chat
    prefs = persistence.get_store().get_preferences(chat.id)
    await delivery.deliver(update, context, _text(prefs), _kb(prefs))


async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="notify:mode"):
        return
    chat = update.effective_chat
    persistence.get_store().update_preferences(chat.id, {"notify_mode": mode})
    if update.callback_query is not None:
        await update.callback_query.answer()
    await show(update, context)


async def toggle_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="notify:cat"):
        return
    chat = update.effective_chat
    store = persistence.get_store()
    current = store.get_preferences(chat.id)["categories"].get(category, True)
    store.update_preferences(chat.id, {"categories": {category: not current}})
    if update.callback_query is not None:
        await update.callback_query.answer()
    await show(update, context)


async def toggle_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="notify:digest_toggle"):
        return
    chat = update.effective_chat
    store = persistence.get_store()
    current = store.get_preferences(chat.id)["daily_digest_enabled"]
    store.update_preferences(chat.id, {"daily_digest_enabled": not current})
    if update.callback_query is not None:
        await update.callback_query.answer()
    await show(update, context)


async def quiet_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="notify:quiet"):
        return
    context.user_data[PROMPT_KEY] = True
    kb = navigation.kb([InlineKeyboardButton(
        "❌ Cancel", callback_data=callbacks.build("notify", "quiet_cancel"))])
    await delivery.deliver(update, context,
        "Send quiet hours as HH:MM-HH:MM (24h, e.g. 23:00-08:00), or /cancel.", kb)


async def quiet_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(PROMPT_KEY, None)
    if update.callback_query is not None:
        await update.callback_query.answer("Cancelled.")
    await show(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get(PROMPT_KEY):
        return False
    if not await auth.guard(update, auth.Role.VIEWER, action="notify:quiet_set"):
        context.user_data.pop(PROMPT_KEY, None)
        return True
    context.user_data.pop(PROMPT_KEY, None)
    text = (update.message.text or "").strip()
    try:
        start, end = text.split("-", 1)
        start, end = start.strip(), end.strip()
        for hhmm in (start, end):
            h, m = hhmm.split(":")
            if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Use HH:MM-HH:MM, e.g. 23:00-08:00.")
        return True
    chat = update.effective_chat
    persistence.get_store().update_preferences(
        chat.id, {"quiet_hours_start": start, "quiet_hours_end": end})
    await update.message.reply_text(f"✅ Quiet hours set: {start}–{end}.")
    return True


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if cb.action == "show":
        await show(update, context)
    elif cb.action == "mode":
        await set_mode(update, context, cb.args[0] if cb.args else "important")
    elif cb.action == "cat":
        await toggle_category(update, context, cb.args[0] if cb.args else "")
    elif cb.action == "digest_toggle":
        await toggle_digest(update, context)
    elif cb.action == "quiet":
        await quiet_prompt(update, context)
    elif cb.action == "quiet_cancel":
        await quiet_cancel(update, context)
    else:
        await update.callback_query.answer("Unknown action.")


router.register("notify", _on_callback)
