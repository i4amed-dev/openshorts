"""Klippo Autopilot — Telegram Bot.

Monitoring and control for Autopilot via Telegram. Runs as an asyncio task
alongside FastAPI — no separate process or port needed.

To activate, set in .env:
    TELEGRAM_BOT_TOKEN=<your BotFather token>
    TELEGRAM_ALLOWED_CHAT_IDS=<comma-separated chat/user IDs>

Get your chat ID by messaging @userinfobot on Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
)

log = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────

TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_raw_ids: str = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
ALLOWED_IDS: set[int] = {
    int(x.strip()) for x in _raw_ids.split(",") if x.strip().lstrip("-").isdigit()
}

NOTIFY_INTERVAL = 30   # seconds between notification polls
_last_event_id: int = 0
_app: Optional[Application] = None

# ── text formatting ───────────────────────────────────────────────────────────

_MD_SPECIAL = r"\_*[]()~`>#+-=|{}.!"

def esc(text: str) -> str:
    """Escape arbitrary text for MarkdownV2."""
    for ch in _MD_SPECIAL:
        text = text.replace(ch, f"\\{ch}")
    return text


def _count(n) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _dur(s) -> str:
    if not s:
        return "—"
    s = int(s)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _ago(iso: str) -> str:
    if not iso:
        return "—"
    try:
        from datetime import datetime, timezone
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        sec = int((datetime.now(timezone.utc) - then).total_seconds())
        if sec < 60:
            return f"{sec}s ago"
        if sec < 3600:
            return f"{sec // 60}m ago"
        if sec < 86400:
            return f"{sec // 3600}h ago"
        return f"{sec // 86400}d ago"
    except Exception:
        return iso[:16]


def _fmt_tz(iso: str, tz: str) -> str:
    if not iso:
        return "—"
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo(tz)).strftime("%d %b %H:%M")
    except Exception:
        return iso[:16]


# ── message builders ──────────────────────────────────────────────────────────

_STATUS_EMOJI = {
    "OFF": "⚫", "IDLE": "🟡", "RUNNING": "🟢",
    "PAUSED": "⏸", "PAUSED_ERROR": "🔴",
}
_PUBLISH_EMOJI = {
    "PENDING": "🕐", "IN_FLIGHT": "📤", "SUBMITTED": "📅",
    "PUBLISHING": "📡", "PUBLISHED": "✅", "FAILED": "❌",
    "UNCERTAIN": "⚠️", "PARTIAL_FAILED": "⚠️", "CANCELED": "🚫",
}


def _msg_status(status: Dict[str, Any]) -> str:
    s = status.get("status", "OFF")
    tz = status.get("timezone", "UTC")
    today = status.get("today") or {}
    src = status.get("current_source")
    q = status.get("youtube_quota") or {}
    store = status.get("storage") or {}

    lines = [
        f"{_STATUS_EMOJI.get(s, '❓')} *Autopilot — {esc(s.lower())}*",
        "",
        f"📌 {esc(status.get('stage', '—'))}",
    ]

    if src:
        lines += [
            "",
            "*Now processing:*",
            f"🎬 {esc(src.get('title', '')[:60])}",
            f"   {esc(src.get('channel', '—'))} · {_dur(src.get('duration_seconds'))} · "
            f"{_count(src.get('views'))} views",
            f"   ⏱ Started {_ago(src.get('selected_at'))}",
        ]

    if status.get("paused_reason"):
        lines += ["", f"⚠️ _{esc(status['paused_reason'][:200])}_"]

    lines += [
        "",
        "*Today:*",
        f"  Sources: {today.get('sources_selected', 0)} / {today.get('max_sources', '?')}",
        f"  Posts: {today.get('posts_scheduled', 0)} / {today.get('max_posts', '?')}"
        f"  \\({today.get('posts_published', 0)} published\\)",
    ]

    if status.get("next_publish_at"):
        lines.append(f"  Next post: {esc(_fmt_tz(status['next_publish_at'], tz))}")
    if status.get("next_discovery_at"):
        lines.append(f"  Next discovery: {esc(_fmt_tz(status['next_discovery_at'], tz))}")

    gu, gb = q.get("general_units_used", 0), q.get("general_budget", 10000)
    su, sb = q.get("search_calls_used", 0), q.get("search_budget", 100)
    lines += [
        "",
        f"*YouTube quota:* {gu}/{gb} units · {su}/{sb} searches",
    ]

    if store.get("available"):
        lines.append(f"*Disk:* {store.get('free_gb')} GB free of {store.get('total_gb')} GB")

    if status.get("last_tick_at"):
        lines.append(f"\n_Last tick: {_ago(status['last_tick_at'])}_")

    return "\n".join(lines)


def _msg_monitor(status: Dict[str, Any]) -> str:
    s = status.get("status", "OFF")
    src = status.get("current_source")
    queue = status.get("queue") or []
    events = (status.get("events") or [])[:10]
    tz = status.get("timezone", "UTC")

    lines = [
        "📡 *Live Monitor*",
        "",
        f"*Status:* {_STATUS_EMOJI.get(s, '❓')} {esc(s.lower())}",
        f"*Stage:* {esc(status.get('stage', '—'))}",
    ]

    if src:
        lines += [
            "",
            "*⚙️ Running:*",
            f"  {esc(src.get('title', '')[:55])}",
            f"  {esc(src.get('state', '').replace('_', ' ').lower())} · "
            f"{_ago(src.get('selected_at'))}",
        ]

    lines += ["", "*⏭ Up next:*"]
    if queue:
        nxt = queue[0]
        lines += [
            f"  {esc(nxt.get('title', '')[:55])}",
            f"  ⭐ {nxt.get('score', 0):.1f} · {_count(nxt.get('views'))} views · "
            f"{_dur(nxt.get('duration_seconds'))}",
        ]
        if len(queue) > 1:
            lines.append(f"  _\\+{len(queue) - 1} more in queue_")
    else:
        lines.append("  _Nothing queued_")

    if events:
        lines += ["", "*📋 Recent activity:*"]
        for e in events:
            lvl = e.get("level", "info")
            dot = "🔴" if lvl == "error" else "🟡" if lvl == "warn" else "▫️"
            lines.append(
                f"  {dot} _{esc(e.get('stage', ''))}_  {esc(e.get('message', '')[:65])}"
            )

    return "\n".join(lines)


def _msg_queue(status: Dict[str, Any]) -> str:
    queue = status.get("queue") or []
    if not queue:
        return (
            "📋 *Candidate Queue*\n\n"
            "_Nothing eligible right now\\. Discovery will look again soon\\._"
        )
    lines = [f"📋 *Candidate Queue* \\({len(queue)} waiting\\)", ""]
    for i, src in enumerate(queue[:12], 1):
        score = src.get("score") or 0
        url = src.get("url", "")
        title = src.get("title", "")[:50]
        ch = src.get("channel", "—")
        lines.append(
            f"*{i}\\.* [{esc(title)}]({url})\n"
            f"   {esc(ch)} · {_dur(src.get('duration_seconds'))} · "
            f"{_count(src.get('views'))} views · ⭐ {score:.1f}"
        )
    if len(queue) > 12:
        lines.append(f"\n_\\+{len(queue) - 12} more…_")
    return "\n".join(lines)


def _msg_schedule(status: Dict[str, Any]) -> str:
    attempts = status.get("publish_attempts") or []
    tz = status.get("timezone", "UTC")
    if not attempts:
        return "📅 *Schedule*\n\n_No posts scheduled yet\\._"
    lines = [f"📅 *Schedule* \\({len(attempts)} post{'s' if len(attempts) != 1 else ''}\\)", ""]
    for a in attempts:
        emoji = _PUBLISH_EMOJI.get(a.get("state", ""), "❓")
        title = esc(a.get("title", f"clip {a.get('clip_index', 0) + 1}")[:48])
        t = _fmt_tz(a.get("scheduled_for_utc", ""), a.get("timezone") or tz)
        plats = " · ".join(a.get("platforms") or [])
        lines.append(f"{emoji} {title}\n   🕐 {esc(t)} · {esc(plats)}")
    return "\n".join(lines)


def _msg_settings(settings: Dict[str, Any]) -> str:
    disc = settings.get("discovery") or {}
    elig = settings.get("eligibility") or {}
    sched = settings.get("schedule") or {}
    rights = settings.get("rights") or {}
    pubs = settings.get("publishing") or {}
    topics = disc.get("topics") or []

    lines = [
        "⚙️ *Settings*",
        "",
        "*Discovery:*",
        f"  Strategies: {esc(', '.join(disc.get('strategies', [])))}",
        f"  Region: {esc(disc.get('region_code', 'US'))}",
        f"  Topics \\({len(topics)}\\): {esc(', '.join(topics[:4]))}{'…' if len(topics) > 4 else ''}",
        "",
        "*Eligibility:*",
        f"  Min views: {_count(elig.get('min_views', 0))}",
        f"  Min velocity: {elig.get('min_view_velocity_per_hour', 0)}/hr",
        f"  Duration: {_dur(elig.get('min_duration_seconds'))} – {_dur(elig.get('max_duration_seconds'))}",
        f"  Max age: {elig.get('max_age_hours', 168)}h",
        "",
        "*Schedule:*",
        f"  Publish times: {esc(', '.join(sched.get('publish_times', [])))}",
        f"  Discovery times: {esc(', '.join(sched.get('discovery_times', [])))}",
        f"  Max posts/day: {sched.get('max_posts_per_day')}",
        f"  Max sources/day: {sched.get('max_sources_per_day')}",
        f"  Timezone: {esc(settings.get('timezone', 'UTC'))}",
        "",
        f"*Rights policy:* {esc(rights.get('policy', '—').replace('_', ' ').title())}",
        f"*Platforms:* {esc(', '.join(pubs.get('platforms', [])))}",
    ]
    return "\n".join(lines)


def _section_detail(settings: Dict[str, Any], section: str) -> str:
    disc = settings.get("discovery") or {}
    elig = settings.get("eligibility") or {}
    sched = settings.get("schedule") or {}
    pubs = settings.get("publishing") or {}

    if section == "discovery":
        topics = disc.get("topics") or []
        return (
            "🔍 *Discovery*\n\n"
            f"Strategies: {esc(', '.join(disc.get('strategies', [])))}\n"
            f"Region: {esc(disc.get('region_code', 'US'))}\n"
            f"Language: {esc(disc.get('relevance_language', 'en'))}\n"
            f"Max candidates/run: {disc.get('max_candidates_per_run', 50)}\n\n"
            f"*Topics \\({len(topics)}\\):*\n" +
            "\n".join(f"• {esc(t)}" for t in topics)
        )
    if section == "eligibility":
        return (
            "📊 *Eligibility*\n\n"
            f"Min views: {_count(elig.get('min_views'))}\n"
            f"Min velocity: {elig.get('min_view_velocity_per_hour')}/hr\n"
            f"Min duration: {_dur(elig.get('min_duration_seconds'))}\n"
            f"Max duration: {_dur(elig.get('max_duration_seconds'))}\n"
            f"Max age: {elig.get('max_age_hours')}h\n"
            f"Min engagement: {elig.get('min_engagement_rate')}\n"
            f"Min definition: {esc(elig.get('min_definition', 'any'))}\n"
            f"Require captions: {'yes' if elig.get('require_captions') else 'no'}\n"
            f"Exclude kids: {'yes' if elig.get('exclude_made_for_kids', True) else 'no'}"
        )
    if section == "schedule":
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        days = sched.get("days_of_week", list(range(7)))
        active = ", ".join(day_names[d] for d in days)
        return (
            "🕐 *Schedule*\n\n"
            f"Publish times: {esc(', '.join(sched.get('publish_times', [])))}\n"
            f"Discovery times: {esc(', '.join(sched.get('discovery_times', [])))}\n"
            f"Days: {esc(active)}\n"
            f"Max posts/day: {sched.get('max_posts_per_day')}\n"
            f"Max sources/day: {sched.get('max_sources_per_day')}\n"
            f"Min spacing: {sched.get('min_spacing_minutes')} min\n"
            f"Catch\\-up policy: {esc(sched.get('catch_up_policy', 'next_slot'))}"
        )
    if section == "platforms":
        return (
            "📤 *Platforms*\n\n"
            f"Active: {esc(', '.join(pubs.get('platforms', [])) or 'none')}\n\n"
            f"_Edit via the dashboard to change platforms\\._"
        )
    return "⚙️ _Unknown section\\._"


# ── keyboards ─────────────────────────────────────────────────────────────────

def _kb_status(status: Dict[str, Any]) -> InlineKeyboardMarkup:
    s = status.get("status", "OFF")
    enabled = status.get("enabled", False)
    paused = status.get("pause_requested") or s in ("PAUSED", "PAUSED_ERROR")
    rows = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="do:status")],
    ]
    ctrl = []
    if not enabled:
        ctrl.append(InlineKeyboardButton("▶️ Enable", callback_data="do:enable"))
    else:
        if paused or s == "PAUSED_ERROR":
            ctrl.append(InlineKeyboardButton("▶️ Resume", callback_data="do:resume"))
        else:
            ctrl.append(InlineKeyboardButton("⏸ Pause", callback_data="do:pause"))
        ctrl.append(InlineKeyboardButton("🔍 Discover", callback_data="do:discover"))
    if ctrl:
        rows.append(ctrl)
    rows.append([
        InlineKeyboardButton("📋 Queue", callback_data="do:queue"),
        InlineKeyboardButton("📅 Schedule", callback_data="do:schedule"),
        InlineKeyboardButton("⚙️ Settings", callback_data="do:settings"),
    ])
    return InlineKeyboardMarkup(rows)


def _kb_back_status() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data="do:monitor"),
        InlineKeyboardButton("◀️ Status", callback_data="do:status"),
    ]])


def _kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Discovery", callback_data="section:discovery"),
            InlineKeyboardButton("📊 Eligibility", callback_data="section:eligibility"),
        ],
        [
            InlineKeyboardButton("🕐 Schedule", callback_data="section:schedule"),
            InlineKeyboardButton("📤 Platforms", callback_data="section:platforms"),
        ],
        [InlineKeyboardButton("◀️ Back", callback_data="do:status")],
    ])


def _kb_confirm(action: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Yes, {label}", callback_data=f"confirm:{action}"),
        InlineKeyboardButton("❌ Cancel", callback_data="do:cancel"),
    ]])


# ── auth ──────────────────────────────────────────────────────────────────────

def _allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    cid = update.effective_chat.id if update.effective_chat else None
    uid = update.effective_user.id if update.effective_user else None
    return cid in ALLOWED_IDS or uid in ALLOWED_IDS


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not _allowed(update):
        if update.message:
            await update.message.reply_text("⛔ Not authorized.")
        elif update.callback_query:
            await update.callback_query.answer("⛔ Not authorized.", show_alert=True)
        return False
    return True


# ── shared send/edit helper ───────────────────────────────────────────────────

async def _reply(update: Update, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Send a new message or edit the current one (for callbacks)."""
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)
        except Exception:
            pass
        await update.callback_query.answer()
    elif update.message:
        await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)


# ── command handlers ──────────────────────────────────────────────────────────

async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    await update.message.reply_text(
        "🤖 *Klippo Autopilot Bot*\n\n"
        "Monitor and control Autopilot from Telegram\\.\n\n"
        "*Monitoring*\n"
        "/status — status \\+ inline controls\n"
        "/monitor — live activity snapshot\n"
        "/queue — candidate queue\n"
        "/schedule — upcoming posts\n"
        "/errors — recent errors\n\n"
        "*Control*\n"
        "/discover — run discovery now\n"
        "/process — process next candidate\n"
        "/pause · /resume\n"
        "/enable · /disable\n"
        "/stop — emergency stop\n\n"
        "*Settings*\n"
        "/settings — view all settings\n\n"
        "/help — this message",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    from automation.service import get_service
    status = get_service().status()
    await _reply(update, _msg_status(status), _kb_status(status))


async def _cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    from automation.service import get_service
    status = get_service().status()
    await _reply(update, _msg_monitor(status), _kb_back_status())


async def _cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    from automation.service import get_service
    status = get_service().status()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data="do:queue"),
        InlineKeyboardButton("◀️ Status", callback_data="do:status"),
    ]])
    await _reply(update, _msg_queue(status), kb)


async def _cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    from automation.service import get_service
    status = get_service().status()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data="do:schedule"),
        InlineKeyboardButton("◀️ Status", callback_data="do:status"),
    ]])
    await _reply(update, _msg_schedule(status), kb)


async def _cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    from automation.service import get_service
    settings = get_service().get_settings()
    await _reply(update, _msg_settings(settings), _kb_settings())


async def _cmd_errors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    from automation.service import get_service
    status = get_service().status()
    errors = status.get("recent_errors") or []
    if not errors:
        await _reply(update, "✅ *No recent errors\\.*")
        return
    lines = [f"🚨 *Recent Errors* \\({len(errors)}\\)", ""]
    for e in errors[:15]:
        lines.append(f"• _{_ago(e.get('ts'))}_  {esc(e.get('message', '')[:120])}")
    await _reply(update, "\n".join(lines))


async def _cmd_discover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if update.callback_query:
        await update.callback_query.answer("Running discovery…")
    if update.message:
        await update.message.reply_text("🔍 Running discovery…")
    try:
        from automation.service import get_service
        await get_service().run_discovery_now()
        text = "✅ Discovery complete\\. Use /queue to see candidates\\."
    except Exception as e:
        text = f"❌ Discovery failed: {esc(str(e)[:200])}"
    if update.effective_chat:
        await context.bot.send_message(
            update.effective_chat.id, text, parse_mode=ParseMode.MARKDOWN_V2)


async def _cmd_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if update.message:
        await update.message.reply_text("⚡ Submitting next candidate…")
    try:
        from automation.service import get_service
        result = await get_service().process_next_now()
        if result.get("ok"):
            text = "✅ Job submitted\\. Use /monitor to track it\\."
        else:
            text = f"ℹ️ {esc(str(result.get('reason', 'Nothing to process')))}"
    except Exception as e:
        text = f"❌ {esc(str(e)[:200])}"
    if update.effective_chat:
        await context.bot.send_message(
            update.effective_chat.id, text, parse_mode=ParseMode.MARKDOWN_V2)


async def _cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    from automation.service import get_service
    get_service().pause()
    if update.callback_query:
        await update.callback_query.answer("Paused")
    await _reply(update, "⏸ Autopilot pausing after the current job\\.")


async def _cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    from automation.service import get_service
    get_service().resume()
    if update.callback_query:
        await update.callback_query.answer("Resumed")
    await _reply(update, "▶️ Autopilot resumed\\.")


async def _cmd_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if update.callback_query:
        await update.callback_query.answer()
    try:
        from automation.service import get_service
        await get_service().enable()
        text = "✅ Autopilot enabled\\."
    except Exception as e:
        text = f"❌ Could not enable: {esc(str(e)[:200])}"
    await _reply(update, text)


async def _cmd_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    from automation.service import get_service
    get_service().disable()
    await _reply(update, "⏹ Autopilot disabled\\.")


async def _cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    await _reply(
        update,
        "⚠️ *Emergency Stop*\n\n"
        "This will disable Autopilot, drop the queue, and cancel all scheduled posts "
        "that have not been published yet\\.\n\n"
        "Posts already live stay live\\. Are you sure?",
        _kb_confirm("emergency_stop", "Emergency Stop"),
    )


# ── callback router ───────────────────────────────────────────────────────────

async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _allowed(update):
        await query.answer("⛔ Not authorized.", show_alert=True)
        return

    data = query.data or ""

    if data == "do:status":
        await _cmd_status(update, context)

    elif data == "do:monitor":
        await _cmd_monitor(update, context)

    elif data == "do:queue":
        await _cmd_queue(update, context)

    elif data == "do:schedule":
        await _cmd_schedule(update, context)

    elif data == "do:settings":
        await _cmd_settings(update, context)

    elif data == "do:pause":
        await _cmd_pause(update, context)

    elif data == "do:resume":
        await _cmd_resume(update, context)

    elif data == "do:enable":
        await _cmd_enable(update, context)

    elif data == "do:discover":
        await _cmd_discover(update, context)

    elif data == "do:cancel":
        await query.answer("Cancelled")
        try:
            await query.edit_message_text("❌ Cancelled\\.", parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            pass

    elif data.startswith("section:"):
        section = data.split(":", 1)[1]
        await query.answer()
        from automation.service import get_service
        settings = get_service().get_settings()
        text = _section_detail(settings, section)
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data="do:settings"),
                ]]),
            )
        except Exception:
            pass

    elif data == "confirm:emergency_stop":
        await query.answer("Stopping…")
        try:
            from automation.service import get_service
            await get_service().emergency_stop()
            text = "🛑 Emergency stop executed\\."
        except Exception as e:
            text = f"❌ {esc(str(e)[:200])}"
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            pass

    else:
        await query.answer("Unknown action.")


# ── push notifications ────────────────────────────────────────────────────────

# Events that warrant a Telegram push notification.
_NOTIFY_STAGES = {"selection", "publishing", "recovery", "engine"}
_NOTIFY_DISCOVERY_KEYWORDS = ("eligible",)


def _should_notify(stage: str, level: str, message: str) -> tuple[bool, str]:
    """Return (should_notify, emoji)."""
    if level == "error":
        return True, "🔴"
    if level == "warn" and stage == "engine":
        return True, "🟡"
    if level == "info":
        if stage == "selection":
            return True, "🎯"
        if stage == "publishing":
            return True, "📤"
        if stage == "recovery":
            return True, "♻️"
        if stage == "discovery" and any(k in message for k in _NOTIFY_DISCOVERY_KEYWORDS):
            return True, "🔍"
    return False, ""


async def _notify_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll for new events and push notifications to all allowed chat IDs."""
    global _last_event_id
    if not ALLOWED_IDS:
        return
    try:
        from automation.service import get_service
        status = get_service().status()
        events = status.get("events") or []

        new_events = [e for e in events if (e.get("id") or 0) > _last_event_id]
        if not new_events:
            return

        _last_event_id = max(e.get("id", 0) for e in new_events)

        for event in new_events:
            stage = event.get("stage", "")
            level = event.get("level", "info")
            message = event.get("message", "")

            notify, emoji = _should_notify(stage, level, message)
            if not notify:
                continue

            text = f"{emoji} *{esc(stage)}*\n{esc(message[:300])}"
            for chat_id in ALLOWED_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                except Exception as exc:
                    log.warning("Notification to %s failed: %s", chat_id, exc)

    except Exception as exc:
        log.warning("Notification job error: %s", exc)


# ── lifecycle ─────────────────────────────────────────────────────────────────

async def start_bot() -> None:
    """Build and start the Telegram bot. Called from app.py lifespan."""
    global _app, _last_event_id

    if not TOKEN:
        log.info("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled.")
        return

    # Prime cursor so startup doesn't spam all historical events.
    try:
        from automation.service import get_service
        status = get_service().status()
        events = status.get("events") or []
        if events:
            _last_event_id = max(e.get("id", 0) for e in events)
    except Exception:
        pass

    _app = Application.builder().token(TOKEN).build()

    _app.add_handler(CommandHandler("start", _cmd_start))
    _app.add_handler(CommandHandler("help", _cmd_start))
    _app.add_handler(CommandHandler("status", _cmd_status))
    _app.add_handler(CommandHandler("monitor", _cmd_monitor))
    _app.add_handler(CommandHandler("queue", _cmd_queue))
    _app.add_handler(CommandHandler("schedule", _cmd_schedule))
    _app.add_handler(CommandHandler("settings", _cmd_settings))
    _app.add_handler(CommandHandler("errors", _cmd_errors))
    _app.add_handler(CommandHandler("discover", _cmd_discover))
    _app.add_handler(CommandHandler("process", _cmd_process))
    _app.add_handler(CommandHandler("pause", _cmd_pause))
    _app.add_handler(CommandHandler("resume", _cmd_resume))
    _app.add_handler(CommandHandler("enable", _cmd_enable))
    _app.add_handler(CommandHandler("disable", _cmd_disable))
    _app.add_handler(CommandHandler("stop", _cmd_stop))
    _app.add_handler(CallbackQueryHandler(_on_callback))

    _app.job_queue.run_repeating(_notify_job, interval=NOTIFY_INTERVAL, first=15)

    await _app.initialize()
    await _app.bot.set_my_commands([
        BotCommand("status",   "Status + inline controls"),
        BotCommand("monitor",  "Live activity snapshot"),
        BotCommand("queue",    "Candidate queue"),
        BotCommand("schedule", "Upcoming posts"),
        BotCommand("settings", "View settings"),
        BotCommand("discover", "Run discovery now"),
        BotCommand("process",  "Process next candidate"),
        BotCommand("pause",    "Pause autopilot"),
        BotCommand("resume",   "Resume autopilot"),
        BotCommand("enable",   "Enable autopilot"),
        BotCommand("disable",  "Disable autopilot"),
        BotCommand("errors",   "Recent errors"),
        BotCommand("stop",     "Emergency stop"),
        BotCommand("help",     "All commands"),
    ])
    await _app.start()
    await _app.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram bot started.")


async def stop_bot() -> None:
    """Graceful shutdown. Called from app.py lifespan cleanup."""
    global _app
    if _app is None:
        return
    try:
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
    except Exception as exc:
        log.warning("Bot shutdown error: %s", exc)
    finally:
        _app = None
