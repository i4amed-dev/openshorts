"""/clips — the generated-clip gallery, detail, and preview.

Preview streams the file straight from disk via python-telegram-bot's file
upload (never reads the whole file into memory) and never even attempts a
send once the file is over the standard Bot API's 50 MB limit — verified
against the current Telegram Bot API docs (a local Bot API server raises this
to 2000 MB, which is a deliberately deferred option, not something this bot
silently assumes).
"""
from __future__ import annotations

import os

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, navigation, render
from .. import errors as delivery
from ..callbacks import Callback, router

PAGE_SIZE = 6
MAX_PREVIEW_BYTES = 50 * 1024 * 1024  # standard (non-local) Bot API limit

_CLIP_ICON = {"PUBLISHED": "✅", "SCHEDULED": "📅", "FAILED": "❌", "SKIPPED": "⏭",
              "PARTIAL": "⚠️", "PENDING": "▫️"}


async def _service():
    from automation.service import get_service
    return get_service()


def _recent_clips(service, limit_sources: int = 15):
    from automation.models import SourceState
    sources = service.db.list_sources(
        states=[SourceState.PROCESS_READY, SourceState.CLIPS_SCHEDULED, SourceState.DONE],
        limit=limit_sources, order="recent")
    clips = []
    title_by_source_id = {}
    for source in sources:
        title_by_source_id[source.id] = source.title
        clips.extend(service.db.list_clips(source_id=source.id))
    clips.sort(key=lambda c: c.created_at or "", reverse=True)
    return clips, title_by_source_id


def _row(clip, source_title: str) -> str:
    icon = _CLIP_ICON.get(clip.state, "▫️")
    dur = render.duration(clip.end_seconds - clip.start_seconds)
    rank = f"⭐ Rank #{clip.rank}" if clip.rank else ""
    return (f"{icon} {render.esc((clip.title or f'Clip {clip.clip_index + 1}')[:50])}\n"
            f"   {dur} · {render.esc(source_title[:30])} {rank}")


async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0,
                     source_id: int | None = None) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="clips:list"):
        return
    service = await _service()

    if source_id is not None:
        clips = service.db.list_clips(source_id=source_id)
        source = service.db.get_source(source_id)
        title_of = {source_id: source.title if source else "—"}
    else:
        clips, title_of = _recent_clips(service)

    if not clips:
        text = "✂️ " + render.bold("Clips") + "\n\n" + render.italic("No clips yet.")
        kb = navigation.kb(navigation.nav_row(refresh=callbacks.build("clips", "list", 0)))
        await delivery.deliver(update, context, text, kb)
        return

    items, page, total_pages = navigation.paginate(clips, page, PAGE_SIZE)
    start = page * PAGE_SIZE
    lines = [f"✂️ {render.bold('Clips')} — {start + 1}–{start + len(items)} of {len(clips)}"]
    buttons = []
    extra = (source_id,) if source_id is not None else ()
    for i, clip in enumerate(items, start + 1):
        lines.append(f"\n{i}. {_row(clip, title_of.get(clip.source_id, '—'))}")
        buttons.append([InlineKeyboardButton(
            f"{i}. {(clip.title or 'Clip')[:30]}",
            callback_data=callbacks.build("clips", "show", clip.id))])

    rows = buttons + [navigation.pagination_row("clips", "list", page, total_pages, *extra)]
    rows.append(navigation.nav_row(refresh=callbacks.build("clips", "list", page, *extra)))
    await delivery.deliver(update, context, "\n".join(lines), navigation.kb(*rows))


def _detail_text(clip, source) -> str:
    icon = _CLIP_ICON.get(clip.state, "▫️")
    lines = [
        "✂️ " + render.esc(clip.title or f"Clip {clip.clip_index + 1}"), "",
        f"{render.bold('Duration:')} {render.duration(clip.end_seconds - clip.start_seconds)}",
        f"{render.bold('Source:')} {render.esc(source.title if source else '—')}",
        f"{render.bold('Rank:')} {clip.rank or '—'}",
        f"{render.bold('Publish state:')} {icon} {render.esc(clip.state)}",
    ]
    if clip.skip_reason:
        lines.append(f"{render.bold('Reason:')} {render.esc(clip.skip_reason)}")
    return "\n".join(lines)


async def show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, clip_id: int) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="clips:show"):
        return
    service = await _service()
    clip = service.db.get_clip(clip_id)
    if clip is None:
        await delivery.deliver(update, context, "This clip no longer exists.",
                                navigation.kb(navigation.nav_row(refresh=callbacks.build("clips", "list", 0))))
        return
    source = service.db.get_source(clip.source_id)

    rows = [[
        InlineKeyboardButton("▶️ Preview", callback_data=callbacks.build("clips", "preview", clip_id)),
        InlineKeyboardButton("📤 Publish", callback_data=callbacks.build("publishing", "new", clip_id)),
    ]]
    rows.append(navigation.nav_row(refresh=callbacks.build("clips", "show", clip_id),
                                    back=callbacks.build("clips", "list", 0)))
    await delivery.deliver(update, context, _detail_text(clip, source), navigation.kb(*rows))


async def preview(update: Update, context: ContextTypes.DEFAULT_TYPE, clip_id: int) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="clips:preview"):
        return
    service = await _service()
    clip = service.db.get_clip(clip_id)
    if clip is None:
        if update.callback_query is not None:
            await update.callback_query.answer("This clip no longer exists.")
        return

    cg = service.orchestrator.runtime.clip_generator
    path = cg.clip_path(clip.job_id, clip.filename) if cg is not None else None
    chat = update.effective_chat

    if not path or not os.path.exists(path):
        if update.callback_query is not None:
            await update.callback_query.answer("Clip file not found on disk.", show_alert=True)
        return

    size = os.path.getsize(path)
    if size > MAX_PREVIEW_BYTES:
        if update.callback_query is not None:
            await update.callback_query.answer(
                f"Clip is {size // (1024 * 1024)} MB — too large for Telegram to send "
                f"(50 MB limit). Use the dashboard to download it.", show_alert=True)
        return

    if update.callback_query is not None:
        await update.callback_query.answer("Sending…")
    with open(path, "rb") as fh:
        await context.bot.send_video(chat.id, fh, caption=render.esc(clip.title or "")[:1024],
                                     parse_mode="HTML")


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if cb.action == "list":
        await show_list(update, context, cb.int_arg(0) or 0, cb.int_arg(1))
        return
    clip_id = cb.int_arg(0)
    if clip_id is None:
        await update.callback_query.answer("This clip no longer exists.")
        return
    if cb.action == "show":
        await show_detail(update, context, clip_id)
    elif cb.action == "preview":
        await preview(update, context, clip_id)
    else:
        await update.callback_query.answer("Unknown action.")


router.register("clips", _on_callback)
