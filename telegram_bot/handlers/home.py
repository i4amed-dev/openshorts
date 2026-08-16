"""/start, /home — the Klippo command center dashboard."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, errors, keyboards, render
from ..callbacks import Callback, router

_STATUS_EMOJI = {
    "OFF": "⚫", "IDLE": "🟡", "RUNNING": "🟢",
    "PAUSED": "⏸", "PAUSED_ERROR": "🔴",
}


def _dashboard_text(status: dict) -> str:
    s = status.get("status", "OFF")
    tz = status.get("timezone", "UTC")
    today = status.get("today") or {}
    src = status.get("current_source")
    errors_today = len([e for e in (status.get("recent_errors") or [])])

    lines = [
        "🤖 " + render.bold("Klippo"),
        "",
        f"{_STATUS_EMOJI.get(s, '❓')} Autopilot {render.esc(s.lower())}",
    ]
    if src:
        lines.append(f"⚙️ Processing: “{render.esc(src.get('title', '')[:60])}”")
    lines += [
        f"📊 {len(status.get('queue') or [])} candidates ready",
        f"🎬 {today.get('clips_generated', 0)} clips generated today",
        f"📅 {today.get('posts_scheduled', 0)} posts scheduled",
        f"✅ {today.get('posts_published', 0)} published",
        f"⚠️ {errors_today} recent error{'s' if errors_today != 1 else ''}",
    ]

    next_lines = []
    if status.get("next_discovery_at"):
        next_lines.append(f"🔍 Discovery {render.esc(render.fmt_local(status['next_discovery_at'], tz))}")
    if status.get("next_publish_at"):
        next_lines.append(f"📤 Publish {render.esc(render.fmt_local(status['next_publish_at'], tz))}")
    if next_lines:
        lines += ["", render.bold("Next:")] + next_lines

    if status.get("paused_reason"):
        lines += ["", f"⚠️ {render.italic(status['paused_reason'][:200])}"]

    return "\n".join(lines)


async def _get_status() -> dict:
    from automation.service import get_service
    return get_service().status()


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="home:show"):
        return
    status = await _get_status()
    text = _dashboard_text(status)
    kb = keyboards.home_grid(autopilot_enabled=bool(status.get("enabled")),
                              paused=status.get("status") in ("PAUSED", "PAUSED_ERROR"))
    await errors.deliver(update, context, text, kb)


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if cb.action == "show":
        await show(update, context)


router.register("home", _on_callback)
