"""/new — manual video submission: a YouTube URL or a direct Telegram video
upload, either way a real metadata/quality preview, an explicit rights
confirmation, then submission through the exact same `app.submit_clip_job()`
path `/api/process` uses. Never a second downloader, never a synthesized
acknowledgement — the confirmation tap IS the attestation, recorded the same
way a checkbox click would be.

Uploads are capped at 20 MB — the standard (non-local) Bot API's limit for a
bot to download a file it received, verified against current Telegram Bot
API docs. Downloaded via PTB's streaming `download_to_drive`, never read
fully into memory, to a job-id-derived filename (never the client-supplied
one) so a crafted filename can't escape the upload directory.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, navigation, render
from .. import errors as delivery
from ..callbacks import Callback, router

log = logging.getLogger("telegram_bot.new_video")

STAGE_KEY = "new_video"
_YOUTUBE_URL_RE = re.compile(r"(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)", re.IGNORECASE)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="new_video:start"):
        return
    context.user_data[STAGE_KEY] = {"stage": "await_url"}
    await update.message.reply_text(
        "➕ New Video\n\nSend me a YouTube URL, or upload a video directly "
        "(up to 20 MB), or /cancel.")


async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registered as a video/document MessageHandler. Only acts when a video
    or a video-mimetype document arrives while the sender is mid-`/new` flow
    (or unprompted — a direct upload also starts the flow); anything else is
    left untouched for other handlers."""
    tg_file_obj = update.message.video or (
        update.message.document
        if update.message.document and (update.message.document.mime_type or "").startswith("video/")
        else None)
    if tg_file_obj is None:
        return
    if not await auth.guard(update, auth.Role.ADMIN, action="new_video:upload"):
        return

    size = tg_file_obj.file_size or 0
    if size > MAX_UPLOAD_BYTES:
        await update.message.reply_text(
            f"❌ That file is {size // (1024 * 1024)} MB — Telegram bots can only receive "
            f"files up to 20 MB this way. Use /new with a YouTube URL, or upload directly "
            f"in the dashboard.")
        return

    await update.message.reply_text("📥 Downloading…")
    from app import UPLOAD_DIR
    job_id = str(uuid.uuid4())
    dest_path = os.path.join(UPLOAD_DIR, f"{job_id}_telegram_upload.mp4")
    try:
        tg_file = await tg_file_obj.get_file()
        await tg_file.download_to_drive(dest_path)
    except Exception as exc:
        log.warning("Telegram upload download failed: %s", exc)
        await update.message.reply_text(f"❌ Could not download the file: {exc}")
        return

    context.user_data[STAGE_KEY] = {"stage": "confirm", "input_path": dest_path, "job_id": job_id}
    lines = [
        "➕ " + render.bold("New Video (uploaded)"), "",
        f"{render.bold('Size:')} {size // (1024 * 1024)} MB",
        "", render.italic("I own this video or have permission to process it."),
    ]
    kb = navigation.kb(
        [InlineKeyboardButton("✅ I confirm", callback_data=callbacks.build("newvid", "confirm")),
         InlineKeyboardButton("❌ Cancel", callback_data=callbacks.build("newvid", "cancel"))])
    await update.message.reply_text("\n".join(lines), reply_markup=kb)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registered as a generic text MessageHandler. A no-op unless the sender
    is mid-flow — everyone else's plain text passes through untouched."""
    state = context.user_data.get(STAGE_KEY)
    if not state or state.get("stage") != "await_url":
        return
    if not await auth.guard(update, auth.Role.ADMIN, action="new_video:url"):
        context.user_data.pop(STAGE_KEY, None)
        return

    url = (update.message.text or "").strip()
    if not _YOUTUBE_URL_RE.search(url):
        await update.message.reply_text(
            "That doesn't look like a YouTube URL. Send a link, or /cancel.")
        return

    await update.message.reply_text("🔎 Checking this video…")
    from app import _probe_youtube_quality
    try:
        probe = await _probe_youtube_quality(url)
    except Exception:
        probe = {}

    state.update(stage="confirm", url=url, probe=probe)

    lines = ["➕ " + render.bold("New Video"), ""]
    if probe.get("title"):
        lines.append(f"{render.bold('Title:')} {render.esc(probe['title'][:80])}")
    if probe.get("channel"):
        lines.append(f"{render.bold('Channel:')} {render.esc(probe['channel'])}")
    if probe.get("duration"):
        lines.append(f"{render.bold('Duration:')} {render.duration(probe['duration'])}")
    if probe.get("max_height"):
        lines.append(f"{render.bold('Available quality:')} up to {probe['max_height']}p")
    if not any(probe.get(k) for k in ("title", "channel", "duration", "max_height")):
        lines.append(render.italic("Could not preview this video — it may still process fine."))
    lines += ["", render.italic("I own this video or have permission to process it.")]

    kb = navigation.kb(
        [InlineKeyboardButton("✅ I confirm", callback_data=callbacks.build("newvid", "confirm")),
         InlineKeyboardButton("❌ Cancel", callback_data=callbacks.build("newvid", "cancel"))])
    await delivery.deliver(update, context, "\n".join(lines), kb)


def _cleanup_upload(state: dict) -> None:
    path = state.get("input_path")
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("Could not clean up unused upload %s: %s", path, exc)


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="new_video:confirm"):
        return
    state = context.user_data.get(STAGE_KEY)
    if not state or state.get("stage") != "confirm":
        await update.callback_query.answer("This request has expired — send /new again.", show_alert=True)
        return
    context.user_data.pop(STAGE_KEY, None)
    url = state.get("url")
    input_path = state.get("input_path")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        _cleanup_upload(state)
        await delivery.deliver(update, context,
            "❌ GEMINI_API_KEY is not configured on the server — the Telegram/Autopilot "
            "path cannot read a browser-stored key. Set it in .env.")
        return

    if update.callback_query is not None:
        await update.callback_query.answer("Submitting…")

    from app import OUTPUT_DIR, submit_clip_job
    job_id = state.get("job_id") or str(uuid.uuid4())
    job_output_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_output_dir, exist_ok=True)

    user = update.effective_user
    attestation = {
        "acknowledged": True,
        "ip": f"telegram:{user.id if user else 'unknown'}",
        "user_agent": f"klippo-telegram/{(user.username or user.id) if user else 'unknown'}",
        "timestamp": time.time(),
        "source": "url" if url else "file",
    }
    try:
        await asyncio.to_thread(
            submit_clip_job, job_id=job_id, job_output_dir=job_output_dir, api_key=api_key,
            url=url, input_path=input_path, attestation=attestation, priority=2)
    except Exception as exc:
        _cleanup_upload(state)
        await delivery.deliver(update, context, f"❌ Could not submit: {render.esc(str(exc)[:300])}")
        return

    from .. import persistence
    chat = update.effective_chat
    persistence.get_store().log_action(
        user_id=user.id if user else None, chat_id=chat.id if chat else None,
        action="new_video:submit", target=job_id)

    text = ("✅ " + render.bold("Job submitted.") + f"\n\n{render.code(job_id)}\n\n"
            + render.italic("Manually-submitted jobs aren't tracked in /jobs yet (that view "
                             "currently only follows Autopilot-selected sources) — check the "
                             "dashboard for progress."))
    kb = navigation.kb([InlineKeyboardButton("🏠 Home", callback_data=callbacks.build("home", "show"))])
    await delivery.deliver(update, context, text, kb)


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    state = context.user_data.pop(STAGE_KEY, None)
    if state:
        _cleanup_upload(state)
    if update.callback_query is not None:
        await update.callback_query.answer("Cancelled.")
    await delivery.deliver(update, context, "❌ Cancelled — nothing was submitted.")


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if cb.action == "confirm":
        await confirm(update, context, cb)
    elif cb.action == "cancel":
        await cancel_flow(update, context, cb)
    else:
        await update.callback_query.answer("Unknown action.")


router.register("newvid", _on_callback)
