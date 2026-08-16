"""/status, /monitor, /queue, /schedule — read-only Autopilot views."""
from __future__ import annotations

from typing import Any, Dict

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, errors, navigation, render
from ..callbacks import Callback, router

_STATUS_EMOJI = {
    "OFF": "⚫", "IDLE": "🟡", "RUNNING": "🟢",
    "PAUSED": "⏸", "PAUSED_ERROR": "🔴",
}
_PUBLISH_EMOJI = {
    "PENDING": "🕐", "IN_FLIGHT": "📤", "SUBMITTED": "📅",
    "PUBLISHING": "📡", "PUBLISHED": "✅", "FAILED": "❌",
    "UNCERTAIN": "⚠️", "PARTIAL_FAILED": "⚠️", "CANCELED": "🚫",
}


async def _status() -> Dict[str, Any]:
    from automation.service import get_service
    return get_service().status()


def _msg_status(status: Dict[str, Any]) -> str:
    s = status.get("status", "OFF")
    tz = status.get("timezone", "UTC")
    today = status.get("today") or {}
    src = status.get("current_source")
    q = status.get("youtube_quota") or {}
    store = status.get("storage") or {}

    lines = [
        f"{_STATUS_EMOJI.get(s, '❓')} " + render.bold(f"Autopilot — {s.lower()}"),
        "",
        f"📌 {render.esc(status.get('stage', '—'))}",
    ]
    if src:
        lines += [
            "", render.bold("Now processing:"),
            f"🎬 {render.esc(src.get('title', '')[:60])}",
            f"   {render.esc(src.get('channel', '—'))} · {render.duration(src.get('duration_seconds'))} · "
            f"{render.count(src.get('views'))} views",
            f"   ⏱ Started {render.ago(src.get('selected_at'))}",
        ]
    if status.get("paused_reason"):
        lines += ["", f"⚠️ {render.italic(status['paused_reason'][:200])}"]

    lines += [
        "", render.bold("Today:"),
        f"  Sources: {today.get('sources_selected', 0)} / {today.get('max_sources', '?')}",
        f"  Posts: {today.get('posts_scheduled', 0)} / {today.get('max_posts', '?')} "
        f"({today.get('posts_published', 0)} published)",
    ]
    if status.get("next_publish_at"):
        lines.append(f"  Next post: {render.esc(render.fmt_local(status['next_publish_at'], tz))}")
    if status.get("next_discovery_at"):
        lines.append(f"  Next discovery: {render.esc(render.fmt_local(status['next_discovery_at'], tz))}")

    gu, gb = q.get("general_units_used", 0), q.get("general_budget", 10000)
    su, sb = q.get("search_calls_used", 0), q.get("search_budget", 100)
    lines += ["", render.bold("YouTube quota:") + f" {gu}/{gb} units · {su}/{sb} searches"]

    if store.get("available"):
        lines.append(f"{render.bold('Disk:')} {store.get('free_gb')} GB free of {store.get('total_gb')} GB")
    if status.get("last_tick_at"):
        lines.append(f"\n{render.italic('Last tick: ' + render.ago(status['last_tick_at']))}")

    return "\n".join(lines)


def _msg_monitor(status: Dict[str, Any]) -> str:
    s = status.get("status", "OFF")
    src = status.get("current_source")
    queue = status.get("queue") or []
    events = (status.get("events") or [])[:10]

    lines = [
        "📡 " + render.bold("Live Monitor"), "",
        f"{render.bold('Status:')} {_STATUS_EMOJI.get(s, '❓')} {render.esc(s.lower())}",
        f"{render.bold('Stage:')} {render.esc(status.get('stage', '—'))}",
    ]
    if src:
        lines += [
            "", "⚙️ " + render.bold("Running:"),
            f"  {render.esc(src.get('title', '')[:55])}",
            f"  {render.esc(src.get('state', '').replace('_', ' ').lower())} · "
            f"{render.ago(src.get('selected_at'))}",
        ]
    lines += ["", "⏭ " + render.bold("Up next:")]
    if queue:
        nxt = queue[0]
        lines += [
            f"  {render.esc(nxt.get('title', '')[:55])}",
            f"  ⭐ {nxt.get('score', 0):.1f} · {render.count(nxt.get('views'))} views · "
            f"{render.duration(nxt.get('duration_seconds'))}",
        ]
        if len(queue) > 1:
            lines.append(render.italic(f"  +{len(queue) - 1} more in queue"))
    else:
        lines.append(render.italic("  Nothing queued"))
    if events:
        lines += ["", "📋 " + render.bold("Recent activity:")]
        for e in events:
            lvl = e.get("level", "info")
            dot = "🔴" if lvl == "error" else "🟡" if lvl == "warn" else "▫️"
            lines.append(f"  {dot} {render.italic(e.get('stage', ''))} "
                          f"{render.esc(e.get('message', '')[:65])}")
    return "\n".join(lines)


def _msg_queue(status: Dict[str, Any]) -> str:
    queue = status.get("queue") or []
    if not queue:
        return "📋 " + render.bold("Candidate Queue") + "\n\n" + \
            render.italic("Nothing eligible right now. Discovery will look again soon.")
    lines = [f"📋 {render.bold('Candidate Queue')} ({len(queue)} waiting)", ""]
    for i, src in enumerate(queue[:12], 1):
        score = src.get("score") or 0
        title = src.get("title", "")[:50]
        ch = src.get("channel", "—")
        lines.append(
            f"{i}. {render.link(title, src.get('url', '') or '#')}\n"
            f"   {render.esc(ch)} · {render.duration(src.get('duration_seconds'))} · "
            f"{render.count(src.get('views'))} views · ⭐ {score:.1f}")
    if len(queue) > 12:
        lines.append(render.italic(f"\n+{len(queue) - 12} more…"))
    return "\n".join(lines)


def _msg_schedule(status: Dict[str, Any]) -> str:
    attempts = status.get("publish_attempts") or []
    tz = status.get("timezone", "UTC")
    if not attempts:
        return "📅 " + render.bold("Schedule") + "\n\n" + render.italic("No posts scheduled yet.")
    lines = [f"📅 {render.bold('Schedule')} ({len(attempts)} post{'s' if len(attempts) != 1 else ''})", ""]
    for a in attempts:
        emoji = _PUBLISH_EMOJI.get(a.get("state", ""), "❓")
        title = render.esc(a.get("title") or f"clip {a.get('clip_index', 0) + 1}")[:48]
        t = render.fmt_local(a.get("scheduled_for_utc", ""), a.get("timezone") or tz)
        plats = " · ".join(a.get("platforms") or [])
        lines.append(f"{emoji} {title}\n   🕐 {render.esc(t)} · {render.esc(plats)}")
    return "\n".join(lines)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="status:status"):
        return
    status = await _status()
    kb = navigation.kb(navigation.nav_row(refresh=callbacks.build("status", "status")))
    await errors.deliver(update, context, _msg_status(status), kb)


async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="status:monitor"):
        return
    status = await _status()
    kb = navigation.kb(navigation.nav_row(refresh=callbacks.build("status", "monitor")))
    await errors.deliver(update, context, _msg_monitor(status), kb)


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="status:queue"):
        return
    status = await _status()
    kb = navigation.kb(navigation.nav_row(refresh=callbacks.build("status", "queue")))
    await errors.deliver(update, context, _msg_queue(status), kb)


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="status:schedule"):
        return
    status = await _status()
    kb = navigation.kb(navigation.nav_row(refresh=callbacks.build("status", "schedule")))
    await errors.deliver(update, context, _msg_schedule(status), kb)


_ACTIONS = {"status": cmd_status, "monitor": cmd_monitor, "queue": cmd_queue, "schedule": cmd_schedule}


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    handler = _ACTIONS.get(cb.action)
    if handler is None:
        await update.callback_query.answer("Unknown action.")
        return
    await handler(update, context)


router.register("status", _on_callback)
