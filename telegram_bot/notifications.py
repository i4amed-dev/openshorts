"""Push notifications: poll Autopilot's event log, notify on high-signal
transitions only, respect per-chat category/quiet-hours/mode preferences, and
never lose or duplicate position across a restart.

The cursor lives in `persistence.TelegramStore` (SQLite), not a module
global — a restart resumes exactly where it left off instead of either
replaying the whole history or silently skipping whatever happened while the
process was down.
"""
from __future__ import annotations

import logging
from datetime import datetime

from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from . import auth, persistence, render

log = logging.getLogger("telegram_bot.notifications")

POLL_INTERVAL_SECONDS = 30
DIGEST_CHECK_INTERVAL_SECONDS = 300

_NOTIFY_DISCOVERY_KEYWORDS = ("eligible",)

# Maps an event's (stage, level) to the notification preference category it
# belongs to. Coarse by design: Autopilot's event stages don't yet split
# "processing started" from "clips ready" as distinct stages, so both fall
# under the closest existing one — see the engineering report.
_STAGE_CATEGORY = {
    "selection": "source_selected",
    "publishing": "publishing",
    "recovery": "debug_recovery",
    "discovery": "discovery_summary",
}
_STAGE_EMOJI = {"selection": "🎯", "publishing": "📤", "recovery": "♻️", "discovery": "🔍"}


def _should_notify(stage: str, level: str, message: str) -> tuple[bool, str, str]:
    """Return (should_notify, emoji, category)."""
    if level == "error":
        return True, "🔴", "critical_errors"
    if level == "warn" and stage == "engine":
        return True, "🟡", "critical_errors"
    if level == "info" and stage in _STAGE_CATEGORY:
        if stage == "discovery" and not any(k in message for k in _NOTIFY_DISCOVERY_KEYWORDS):
            return False, "", ""
        return True, _STAGE_EMOJI[stage], _STAGE_CATEGORY[stage]
    return False, "", ""


def prime_cursor_if_empty() -> None:
    """First-ever startup only: don't replay the entire history. A later
    restart keeps whatever position was actually persisted."""
    store = persistence.get_store()
    cursor = store.get_cursor()
    if cursor["last_event_id"]:
        return
    try:
        from automation.service import get_service
        status = get_service().status()
        events = status.get("events") or []
        if events:
            store.set_cursor(last_event_id=max(e.get("id", 0) for e in events))
    except Exception as exc:
        log.info("Could not prime notification cursor (Autopilot not ready yet): %s", exc)


def _recipient_chat_ids() -> list[int]:
    store = persistence.get_store()
    chat_ids = store.known_chat_ids()
    allowed = auth.allowed_chat_ids()
    if allowed:
        chat_ids = [c for c in chat_ids if c in allowed]
    return chat_ids


def _configured_timezone() -> str:
    try:
        from automation.service import get_service
        return get_service().get_settings().get("timezone") or "UTC"
    except Exception:
        return "UTC"


def _local_hhmm(tz_name: str, *, now: datetime | None = None) -> str:
    from zoneinfo import ZoneInfo
    now = now or datetime.now(ZoneInfo(tz_name))
    if now.tzinfo is None:
        now = now.astimezone(ZoneInfo(tz_name))
    return now.astimezone(ZoneInfo(tz_name)).strftime("%H:%M")


def _in_quiet_hours(prefs: dict, hhmm: str) -> bool:
    start, end = prefs.get("quiet_hours_start"), prefs.get("quiet_hours_end")
    if not start or not end:
        return False
    if start <= end:
        return start <= hhmm < end
    return hhmm >= start or hhmm < end  # wraps past midnight


def _wants(prefs: dict, category: str) -> bool:
    """Whether this chat's preferences allow a notification in this category
    right now. Quiet hours suppress everything except critical errors —
    Autopilot's single configured timezone stands in for a per-chat one,
    since this bot is built for one operator's machine."""
    mode = prefs.get("notify_mode", "important")
    if mode == "muted":
        return False
    if mode == "critical_only" and category != "critical_errors":
        return False
    if not prefs.get("categories", {}).get(category, True):
        return False
    if category != "critical_errors":
        tz = _configured_timezone()
        if _in_quiet_hours(prefs, _local_hhmm(tz)):
            return False
    return True


async def poll(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth.is_configured():
        return
    store = persistence.get_store()
    try:
        from automation.service import get_service
        status = get_service().status()
    except Exception as exc:
        log.debug("Notification poll: Autopilot not reachable yet: %s", exc)
        return

    events = status.get("events") or []
    last_id = store.get_cursor()["last_event_id"]
    new_events = [e for e in events if (e.get("id") or 0) > last_id]
    if not new_events:
        return
    store.set_cursor(last_event_id=max(e.get("id", 0) for e in new_events))

    chat_ids = _recipient_chat_ids()
    if not chat_ids:
        return

    for event in new_events:
        stage = event.get("stage", "")
        level = event.get("level", "info")
        message = event.get("message", "")
        notify, emoji, category = _should_notify(stage, level, message)
        if not notify:
            continue
        text = f"{emoji} <b>{render.esc(stage)}</b>\n{render.esc(message[:300])}"
        for chat_id in chat_ids:
            if not _wants(store.get_preferences(chat_id), category):
                continue
            try:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            except Forbidden:
                store.mark_blocked(chat_id)
                log.info("Chat %s blocked the bot — marked and skipped.", chat_id)
            except TelegramError as exc:
                log.warning("Notification to %s failed: %s", chat_id, exc)


def _digest_text(status: dict) -> str:
    today = status.get("today") or {}
    lines = [
        "☀️ " + render.bold("Klippo Daily"), "",
        f"🎬 {today.get('sources_selected', 0)} source(s) selected",
        f"✂️ {today.get('clips_generated', 0)} clips generated",
        f"📅 {today.get('posts_scheduled', 0)} post(s) submitted",
        f"✅ {today.get('posts_published', 0)} published",
    ]
    errors = status.get("recent_errors") or []
    if errors:
        lines.append(f"⚠️ {len(errors)} recent error(s)")
    else:
        lines.append("No critical errors.")
    store = status.get("storage") or {}
    if store.get("available"):
        lines += ["", f"Disk: {store.get('free_gb')} GB free"]
    return "\n".join(lines)


async def digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs frequently (every few minutes) but sends at most once per chat per
    day, at that chat's configured local time — checked against
    `last_digest_sent_date` so a restart never double-sends."""
    if not auth.is_configured():
        return
    store = persistence.get_store()
    tz = _configured_timezone()
    try:
        from zoneinfo import ZoneInfo
        today_local = datetime.now(ZoneInfo(tz)).date().isoformat()
    except Exception:
        return
    hhmm = _local_hhmm(tz)

    try:
        from automation.service import get_service
        status = get_service().status()
    except Exception:
        return
    text = _digest_text(status)

    for chat_id in _recipient_chat_ids():
        prefs = store.get_preferences(chat_id)
        if not prefs.get("daily_digest_enabled"):
            continue
        digest_time = prefs.get("daily_digest_time") or "09:00"
        # Fire once the configured minute has passed, not only on an exact
        # match — a 5-minute poll interval could otherwise skip it entirely.
        if hhmm < digest_time:
            continue
        if prefs.get("last_digest_sent_date") == today_local:
            continue  # already sent to THIS chat today
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            store.mark_digest_sent(chat_id, today_local)
        except Forbidden:
            store.mark_blocked(chat_id)
        except TelegramError as exc:
            log.warning("Digest to %s failed: %s", chat_id, exc)
