"""/jobs — the Clip Generator jobs Autopilot has submitted, keyed by the
candidate `source_id` (compact enough for callback_data; the underlying
`job_id` UUID is shown, never used as a button key).

No fake percentages: the real pipeline only exposes stages, so stages are
what's shown — never an invented completion bar.
"""
from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, navigation, render
from .. import errors as delivery
from ..callbacks import Callback, router

PAGE_SIZE = 6

_RECENT_STATES = ("PROCESS_QUEUED", "PROCESSING", "PROCESS_READY", "CLIPS_SCHEDULED",
                   "DONE", "PROCESS_FAILED", "FAILED")

_STAGE_LABEL = {
    "PROCESS_QUEUED": "🕐 Waiting for the Clip Generator",
    "PROCESSING": "⚙️ Generating clips",
    "PROCESS_READY": "✅ Clips ready",
    "CLIPS_SCHEDULED": "📅 Clips scheduled",
    "DONE": "✅ Done",
    "PROCESS_FAILED": "❌ Failed (may retry)",
    "FAILED": "❌ Failed",
}


async def _service():
    from automation.service import get_service
    return get_service()


def _row(source) -> str:
    stage = _STAGE_LABEL.get(source.state, source.state)
    return f"{stage}\n   {render.esc(source.title[:55])}"


async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="jobs:list"):
        return
    service = await _service()
    jobs = service.db.list_sources(states=list(_RECENT_STATES), limit=200, order="recent")

    if not jobs:
        text = "🎬 " + render.bold("Jobs") + "\n\n" + render.italic("No jobs yet.")
        kb = navigation.kb(navigation.nav_row(refresh=callbacks.build("jobs", "list", 0)))
        await delivery.deliver(update, context, text, kb)
        return

    items, page, total_pages = navigation.paginate(jobs, page, PAGE_SIZE)
    start = page * PAGE_SIZE
    lines = [f"🎬 {render.bold('Jobs')} — {start + 1}–{start + len(items)} of {len(jobs)}"]
    buttons = []
    for i, source in enumerate(items, start + 1):
        lines.append(f"\n{i}. {_row(source)}")
        buttons.append([InlineKeyboardButton(f"{i}. {source.title[:30]}",
                                              callback_data=callbacks.build("jobs", "show", source.id))])

    rows = buttons + [navigation.pagination_row("jobs", "list", page, total_pages)]
    rows.append(navigation.nav_row(refresh=callbacks.build("jobs", "list", page)))
    await delivery.deliver(update, context, "\n".join(lines), navigation.kb(*rows))


def _detail_text(source, clips, attempts) -> str:
    stage = _STAGE_LABEL.get(source.state, source.state)
    lines = [
        "🎬 " + render.bold(stage), "",
        render.esc(source.title),
        f"{render.bold('Job:')} {render.code(source.job_id or '—')}",
        f"{render.bold('Elapsed:')} {render.ago(source.selected_at)}",
    ]
    if source.last_error:
        lines += ["", f"❌ {render.esc(source.last_error[:300])}"]
    if attempts:
        lines += ["", render.bold(f"Attempts: {len(attempts)}")]
    if clips:
        lines += ["", render.bold(f"Clips ({len(clips)}):")]
        for c in sorted(clips, key=lambda c: c.clip_index)[:10]:
            icon = {"PUBLISHED": "✅", "SCHEDULED": "📅", "FAILED": "❌",
                    "SKIPPED": "⏭"}.get(c.state, "▫️")
            title = c.title or f"Clip {c.clip_index + 1}"
            lines.append(f"  {icon} {render.esc(title[:45])}")
    return "\n".join(lines)


async def show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, source_id: int) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="jobs:show"):
        return
    service = await _service()
    source = service.db.get_source(source_id)
    if source is None:
        await delivery.deliver(update, context, "This job no longer exists.",
                                navigation.kb(navigation.nav_row(refresh=callbacks.build("jobs", "list", 0))))
        return
    clips = service.db.list_clips(source_id=source_id)
    attempts = service.db.list_processing_attempts(source_id)

    rows: list[Any] = []
    if clips:
        rows.append([InlineKeyboardButton("✂️ View Clips",
                                           callback_data=callbacks.build("clips", "list", 0, source_id))])
    if source.state in ("PROCESS_FAILED", "FAILED"):
        rows.append([InlineKeyboardButton("🔁 Retry",
                                           callback_data=callbacks.build("jobs", "retry", source_id))])
    rows.append([InlineKeyboardButton("🎯 View Candidate",
                                       callback_data=callbacks.build("candidates", "show", source_id))])
    rows.append(navigation.nav_row(refresh=callbacks.build("jobs", "show", source_id),
                                    back=callbacks.build("jobs", "list", 0)))
    await delivery.deliver(update, context, _detail_text(source, clips, attempts), navigation.kb(*rows))


async def retry(update: Update, context: ContextTypes.DEFAULT_TYPE, source_id: int) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="jobs:retry"):
        return
    service = await _service()
    from .. import persistence
    ok = service.retry_source(source_id)
    user, chat = update.effective_user, update.effective_chat
    persistence.get_store().log_action(
        user_id=user.id if user else None, chat_id=chat.id if chat else None,
        action="jobs:retry", target=str(source_id), result="ok" if ok else "failed")
    if update.callback_query is not None:
        await update.callback_query.answer(
            "Re-queued — it's back in the candidate pool." if ok else "Could not retry this job.")
    await show_detail(update, context, source_id)


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if cb.action == "list":
        await show_list(update, context, cb.int_arg(0) or 0)
        return
    source_id = cb.int_arg(0)
    if source_id is None:
        await update.callback_query.answer("This job no longer exists.")
        return
    if cb.action == "show":
        await show_detail(update, context, source_id)
    elif cb.action == "retry":
        await retry(update, context, source_id)
    else:
        await update.callback_query.answer("Unknown action.")


router.register("jobs", _on_callback)
