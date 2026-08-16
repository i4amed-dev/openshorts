"""/publishing — the real publish lifecycle, one honest state at a time.

Never shows "Published" for anything short of a vendor confirmation, and
every action here is a thin call into `AutopilotService` — retry/cancel/
resolve/reconcile logic all already lives there (and in
`automation/publishing.py` / `publishing_service.py`); this module only
renders and dispatches.
"""
from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, navigation, render
from .. import errors as delivery
from ..callbacks import Callback, router

PAGE_SIZE = 6

_STATE_EMOJI = {
    "PENDING": "🕐", "IN_FLIGHT": "📤", "SUBMITTED": "📅", "PUBLISHING": "📡",
    "PUBLISHED": "✅", "FAILED": "❌", "UNCERTAIN": "⚠️", "PARTIAL_FAILED": "⚠️",
    "CANCELED": "🚫",
}


async def _service():
    from automation.service import get_service
    return get_service()


def _row(view: dict) -> str:
    emoji = _STATE_EMOJI.get(view["state"], "❓")
    title = render.esc((view["title"] or f"clip {view['clip_index'] + 1}")[:45])
    when = render.esc(view.get("scheduled_local") or "—")
    plats = render.esc(" · ".join(view.get("platforms") or []))
    return f"{emoji} {title}\n   🕐 {when} · {plats}"


async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="publishing:list"):
        return
    from automation.service import _publish_view
    service = await _service()
    config = service.get_settings()
    attempts = service.db.list_publish_attempts(limit=100)
    views = [_publish_view(a, config) for a in attempts]
    views.sort(key=lambda v: v.get("scheduled_for_utc") or "9999")

    if not views:
        text = "📅 " + render.bold("Publishing") + "\n\n" + render.italic("Nothing scheduled yet.")
        kb = navigation.kb(navigation.nav_row(refresh=callbacks.build("publishing", "list", 0)))
        await delivery.deliver(update, context, text, kb)
        return

    items, page, total_pages = navigation.paginate(views, page, PAGE_SIZE)
    start = page * PAGE_SIZE
    lines = [f"📅 {render.bold('Publishing')} — {start + 1}–{start + len(items)} of {len(views)}"]
    buttons = []
    for i, view in enumerate(items, start + 1):
        lines.append(f"\n{i}. {_row(view)}")
        buttons.append([InlineKeyboardButton(
            f"{i}. {(view['title'] or 'clip')[:30]}",
            callback_data=callbacks.build("publishing", "show", view["id"]))])

    rows = buttons + [navigation.pagination_row("publishing", "list", page, total_pages)]
    rows.append(navigation.nav_row(refresh=callbacks.build("publishing", "list", page)))
    await delivery.deliver(update, context, "\n".join(lines), navigation.kb(*rows))


def _platform_lines(view: dict) -> list[str]:
    results = view.get("vendor_results")
    if results:
        lines = []
        for r in results:
            icon = "✅" if r.get("success") else "❌"
            lines.append(f"{icon} {render.esc(r.get('platform', '?'))}  {render.esc(r.get('status', ''))}")
        return lines
    return [f"⏳ {render.esc(p)}" for p in (view.get("platforms") or [])]


def _detail_text(view: dict) -> str:
    emoji = _STATE_EMOJI.get(view["state"], "❓")
    lines = [
        f"{emoji} " + render.bold(view["state"]), "",
        render.esc(view["title"] or f"clip {view['clip_index'] + 1}"), "",
    ]
    lines += _platform_lines(view)
    lines += ["", f"{render.bold('Scheduled:')} {render.esc(view.get('scheduled_local') or '—')}"]
    if view.get("retry_count"):
        lines.append(f"{render.bold('Retries:')} {view['retry_count']}")
    if view.get("error"):
        lines.append(f"\n⚠️ {render.esc(view['error'][:300])}")
    return "\n".join(lines)


def _detail_kb(view: dict) -> Any:
    attempt_id, state = view["id"], view["state"]
    rows = []
    if state == "PENDING":
        rows.append([InlineKeyboardButton("🚫 Cancel", callback_data=callbacks.build("publishing", "cancel_pending", attempt_id))])
    elif state == "FAILED":
        rows.append([InlineKeyboardButton("🔁 Retry", callback_data=callbacks.build("publishing", "retry", attempt_id))])
    elif state == "UNCERTAIN":
        rows.append([InlineKeyboardButton("🔄 Check Status", callback_data=callbacks.build("publishing", "check", attempt_id))])
        rows.append([
            InlineKeyboardButton("✅ Mark Published", callback_data=callbacks.build("publishing", "resolve", attempt_id)),
            InlineKeyboardButton("🔁 Not Published → Retry", callback_data=callbacks.build("publishing", "force_retry", attempt_id)),
        ])
    elif state == "PARTIAL_FAILED":
        rows.append([
            InlineKeyboardButton("✅ Accept as is", callback_data=callbacks.build("publishing", "resolve", attempt_id)),
            InlineKeyboardButton("🚫 Abandon", callback_data=callbacks.build("publishing", "abandon", attempt_id)),
        ])
    elif state in ("SUBMITTED", "PUBLISHING"):
        row = [InlineKeyboardButton("🔄 Refresh Status", callback_data=callbacks.build("publishing", "check", attempt_id))]
        if view.get("vendor_is_scheduled"):
            row.append(InlineKeyboardButton("🚫 Cancel Scheduled",
                                            callback_data=callbacks.build("publishing", "cancel_vendor", attempt_id)))
        rows.append(row)
    rows.append(navigation.nav_row(refresh=callbacks.build("publishing", "show", attempt_id),
                                    back=callbacks.build("publishing", "list", 0)))
    return navigation.kb(*rows)


async def show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, attempt_id: int) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="publishing:show"):
        return
    from automation.service import _publish_view
    service = await _service()
    attempt = service.db.get_publish_attempt(attempt_id)
    if attempt is None:
        await delivery.deliver(update, context, "This post no longer exists.",
                                navigation.kb(navigation.nav_row(refresh=callbacks.build("publishing", "list", 0))))
        return
    view = _publish_view(attempt, service.get_settings())
    await delivery.deliver(update, context, _detail_text(view), _detail_kb(view))


async def _act(update: Update, context: ContextTypes.DEFAULT_TYPE, attempt_id: int,
                action_name: str, coro, ok_message: str, fail_message: str) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action=f"publishing:{action_name}"):
        return
    if update.callback_query is not None:
        await update.callback_query.answer("Working…")
    from .. import persistence
    result = await coro if _is_awaitable(coro) else coro
    ok = result if isinstance(result, bool) else bool((result or {}).get("ok"))
    user, chat = update.effective_user, update.effective_chat
    persistence.get_store().log_action(
        user_id=user.id if user else None, chat_id=chat.id if chat else None,
        action=f"publishing:{action_name}", target=str(attempt_id), result="ok" if ok else "failed")
    if update.callback_query is not None:
        reason = (result or {}).get("reason") if isinstance(result, dict) else None
        await update.callback_query.answer((ok_message if ok else (reason or fail_message))[:200])
    await show_detail(update, context, attempt_id)


def _is_awaitable(x) -> bool:
    import inspect
    return inspect.isawaitable(x)


async def cancel_pending(update, context, attempt_id):
    service = await _service()
    await _act(update, context, attempt_id, "cancel_pending",
              service.cancel_pending(attempt_id), "Cancelled.", "Could not cancel.")


async def retry(update, context, attempt_id):
    service = await _service()
    await _act(update, context, attempt_id, "retry",
              service.retry_publish(attempt_id), "Re-queued.", "Could not retry.")


async def check(update, context, attempt_id):
    service = await _service()
    await _act(update, context, attempt_id, "check",
              service.check_publish_status(attempt_id), "Status checked.", "Could not check status.")


async def resolve(update, context, attempt_id):
    service = await _service()
    await _act(update, context, attempt_id, "resolve",
              service.resolve_uncertain(attempt_id), "Marked published.", "Could not resolve.")


async def force_retry(update, context, attempt_id):
    service = await _service()
    await _act(update, context, attempt_id, "force_retry",
              service.force_retry_uncertain(attempt_id), "Re-queued.", "Could not retry.")


async def abandon(update, context, attempt_id):
    service = await _service()
    await _act(update, context, attempt_id, "abandon",
              service.abandon_attempt(attempt_id), "Abandoned.", "Could not abandon.")


async def cancel_vendor(update, context, attempt_id):
    service = await _service()
    await _act(update, context, attempt_id, "cancel_vendor",
              service.cancel_scheduled_attempt(attempt_id), "Cancelled with Upload-Post.",
              "Could not cancel with Upload-Post.")


_ACTIONS = {
    "cancel_pending": cancel_pending, "retry": retry, "check": check, "resolve": resolve,
    "force_retry": force_retry, "abandon": abandon, "cancel_vendor": cancel_vendor,
}


# --- schedule-a-clip conversation -------------------------------------------------
#
# Platforms → date → time → confirm. State lives in context.user_data, the same
# lightweight pattern handlers/new_video.py uses — short-lived enough that full
# ConversationHandler persistence would be overkill. Custom date/time entry is a
# known simplification (see the engineering report); Today/Tomorrow plus the
# configured publish slots cover the common case.

SCHEDULE_KEY = "schedule_clip"


def _sched_kb(rows) -> Any:
    rows = rows + [[InlineKeyboardButton("❌ Cancel", callback_data=callbacks.build("publishing", "sched_cancel"))]]
    return navigation.kb(*rows)


async def new(update: Update, context: ContextTypes.DEFAULT_TYPE, clip_id: int) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="publishing:new"):
        return
    service = await _service()
    clip = service.db.get_clip(clip_id)
    if clip is None:
        await update.callback_query.answer("This clip no longer exists.")
        return
    if clip.state != "PENDING":
        await update.callback_query.answer(
            f"This clip is {clip.state.lower()}, not pending — nothing to schedule.", show_alert=True)
        return
    platforms = list((service.get_settings().get("publishing") or {}).get("platforms") or [])
    if not platforms:
        await update.callback_query.answer("No platforms are configured for publishing.", show_alert=True)
        return

    context.user_data[SCHEDULE_KEY] = {"clip_id": clip_id, "platforms": set(platforms), "all": platforms}
    await _render_platform_step(update, context)


async def _render_platform_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data[SCHEDULE_KEY]
    text = "📅 " + render.bold("Schedule") + "\n\nPlatforms:\n" + "\n".join(
        f"{'☑' if p in state['platforms'] else '☐'} {render.esc(p)}" for p in state["all"])
    rows = [[InlineKeyboardButton(f"{'☑' if p in state['platforms'] else '☐'} {p}",
                                  callback_data=callbacks.build("publishing", "sched_toggle", p))]
            for p in state["all"]]
    rows.append([InlineKeyboardButton("Next →", callback_data=callbacks.build("publishing", "sched_dates"))])
    await delivery.deliver(update, context, text, _sched_kb(rows))


async def sched_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    state = context.user_data.get(SCHEDULE_KEY)
    if not state:
        await update.callback_query.answer("This request has expired.", show_alert=True)
        return
    platform = cb.args[0] if cb.args else ""
    if platform in state["platforms"]:
        state["platforms"].discard(platform)
    else:
        state["platforms"].add(platform)
    await _render_platform_step(update, context)


async def sched_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get(SCHEDULE_KEY)
    if not state:
        await update.callback_query.answer("This request has expired.", show_alert=True)
        return
    if not state["platforms"]:
        await update.callback_query.answer("Select at least one platform.", show_alert=True)
        return
    text = "📅 " + render.bold("Schedule") + "\n\nDate:"
    rows = [[
        InlineKeyboardButton("Today", callback_data=callbacks.build("publishing", "sched_date", "today")),
        InlineKeyboardButton("Tomorrow", callback_data=callbacks.build("publishing", "sched_date", "tomorrow")),
    ]]
    await delivery.deliver(update, context, text, _sched_kb(rows))


def _resolve_day(token: str, tz):
    from datetime import datetime, timedelta
    today = datetime.now(tz).date()
    return today + timedelta(days=1) if token == "tomorrow" else today


async def sched_date(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    state = context.user_data.get(SCHEDULE_KEY)
    if not state:
        await update.callback_query.answer("This request has expired.", show_alert=True)
        return
    state["day_token"] = cb.args[0] if cb.args else "today"

    service = await _service()
    config = service.get_settings()
    times = list((config.get("schedule") or {}).get("publish_times") or [])
    if not times:
        await update.callback_query.answer("No publish time slots are configured.", show_alert=True)
        return
    text = "📅 " + render.bold("Schedule") + "\n\nTime:"
    rows = [[InlineKeyboardButton(t, callback_data=callbacks.build("publishing", "sched_time", t))]
            for t in times]
    await delivery.deliver(update, context, text, _sched_kb(rows))


async def sched_time(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    state = context.user_data.get(SCHEDULE_KEY)
    if not state:
        await update.callback_query.answer("This request has expired.", show_alert=True)
        return
    state["hhmm"] = cb.args[0] if cb.args else None

    service = await _service()
    config = service.get_settings()
    tz_name = config.get("timezone") or "UTC"
    text = (
        "📅 " + render.bold("Confirm") + "\n\n"
        f"Platforms: {render.esc(', '.join(sorted(state['platforms'])))}\n"
        f"When: {render.esc(state['day_token'])} at {render.esc(state['hhmm'])}\n"
        f"Timezone: {render.esc(tz_name)}"
    )
    rows = [[InlineKeyboardButton("✅ Schedule", callback_data=callbacks.build("publishing", "sched_confirm"))]]
    await delivery.deliver(update, context, text, _sched_kb(rows))


async def sched_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.pop(SCHEDULE_KEY, None)
    if not state or not state.get("hhmm"):
        await update.callback_query.answer("This request has expired.", show_alert=True)
        return
    if update.callback_query is not None:
        await update.callback_query.answer("Scheduling…")

    service = await _service()
    from automation import scheduler
    tz = scheduler.get_zone(service.get_settings().get("timezone") or "UTC")
    day = _resolve_day(state["day_token"], tz)
    result = service.schedule_clip(state["clip_id"], sorted(state["platforms"]), day=day, hhmm=state["hhmm"])

    from .. import persistence
    user, chat = update.effective_user, update.effective_chat
    persistence.get_store().log_action(
        user_id=user.id if user else None, chat_id=chat.id if chat else None,
        action="publishing:schedule", target=str(state["clip_id"]),
        result="ok" if result.get("ok") else "failed", detail=result.get("reason"))

    if result.get("ok"):
        text = "✅ " + render.bold("Scheduled.")
        kb = navigation.kb([InlineKeyboardButton(
            "📅 View", callback_data=callbacks.build("publishing", "show", result["attempt_id"]))],
            navigation.nav_row(refresh=callbacks.build("publishing", "list", 0)))
    else:
        text = f"❌ {render.esc(result.get('reason', 'Could not schedule.'))}"
        kb = navigation.kb(navigation.nav_row(refresh=callbacks.build("publishing", "list", 0)))
    await delivery.deliver(update, context, text, kb)


async def sched_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(SCHEDULE_KEY, None)
    if update.callback_query is not None:
        await update.callback_query.answer("Cancelled.")
    await delivery.deliver(update, context, "❌ Cancelled — nothing was scheduled.")


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if cb.action == "list":
        await show_list(update, context, cb.int_arg(0) or 0)
        return
    if cb.action == "new":
        clip_id = cb.int_arg(0)
        if clip_id is None:
            await update.callback_query.answer("This clip no longer exists.")
            return
        await new(update, context, clip_id)
        return
    if cb.action == "sched_toggle":
        await sched_toggle(update, context, cb)
        return
    if cb.action == "sched_dates":
        await sched_dates(update, context)
        return
    if cb.action == "sched_date":
        await sched_date(update, context, cb)
        return
    if cb.action == "sched_time":
        await sched_time(update, context, cb)
        return
    if cb.action == "sched_confirm":
        await sched_confirm(update, context)
        return
    if cb.action == "sched_cancel":
        await sched_cancel(update, context)
        return
    attempt_id = cb.int_arg(0)
    if attempt_id is None:
        await update.callback_query.answer("This post no longer exists.")
        return
    if cb.action == "show":
        await show_detail(update, context, attempt_id)
        return
    handler = _ACTIONS.get(cb.action)
    if handler is None:
        await update.callback_query.answer("Unknown action.")
        return
    await handler(update, context, attempt_id)


router.register("publishing", _on_callback)
