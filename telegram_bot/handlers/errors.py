"""/errors — recent error events, each with a real deep link to the source,
job, or publish attempt it happened on (never a generic status dump)."""
from __future__ import annotations

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, navigation, render
from .. import errors as delivery
from ..callbacks import Callback, router

PAGE_SIZE = 8


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="errors:list"):
        return
    from automation.service import get_service
    events = get_service().db.recent_events(limit=100, level="error")

    if not events:
        text = "✅ " + render.bold("No recent errors.")
        kb = navigation.kb(navigation.nav_row(refresh=callbacks.build("errors", "list", 0)))
        await delivery.deliver(update, context, text, kb)
        return

    items, page, total_pages = navigation.paginate(events, page, PAGE_SIZE)
    start = page * PAGE_SIZE
    lines = [f"🚨 {render.bold('Recent Errors')} — {start + 1}–{start + len(items)} of {len(events)}"]
    rows = []
    for i, e in enumerate(items, start + 1):
        lines.append(f"\n{i}. {render.italic(render.ago(e.get('ts')))}  {render.esc(e.get('message', '')[:110])}")
        link_row = []
        if e.get("publish_attempt_id"):
            link_row.append(InlineKeyboardButton(
                f"{i}. Publish", callback_data=callbacks.build("publishing", "show", e["publish_attempt_id"])))
        elif e.get("job_id") and e.get("source_id"):
            link_row.append(InlineKeyboardButton(
                f"{i}. Job", callback_data=callbacks.build("jobs", "show", e["source_id"])))
        elif e.get("source_id"):
            link_row.append(InlineKeyboardButton(
                f"{i}. Source", callback_data=callbacks.build("candidates", "show", e["source_id"])))
        if link_row:
            rows.append(link_row)

    rows.append(navigation.pagination_row("errors", "list", page, total_pages))
    rows.append(navigation.nav_row(refresh=callbacks.build("errors", "list", page)))
    await delivery.deliver(update, context, "\n".join(lines), navigation.kb(*rows))


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if cb.action == "list":
        await show(update, context, cb.int_arg(0) or 0)
        return
    await update.callback_query.answer("Unknown action.")


router.register("errors", _on_callback)
