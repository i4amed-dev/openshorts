"""/candidates — the ranked queue, candidate detail with its score breakdown,
and processing a *specific* candidate out of score order.

Everything here reads through `service.db` (the same repository
`GET /sources/{id}` already uses) and mutates only through `service.*` methods
— never raw SQL from a handler.
"""
from __future__ import annotations

from typing import Any, List

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, navigation, render
from .. import errors as delivery
from ..callbacks import Callback, router

PAGE_SIZE = 5


async def _service():
    from automation.service import get_service
    return get_service()


def _rejection_breakdown(service) -> List[str]:
    from automation.models import SourceState
    filtered = service.db.list_sources(states=[SourceState.FILTERED], limit=500)
    skipped = service.db.list_sources(states=[SourceState.SKIPPED], limit=500)
    tally: dict[str, int] = {}
    for s in filtered + skipped:
        reason = (s.rejection_reason or "unknown").replace("_", " ").title()
        tally[reason] = tally.get(reason, 0) + 1
    return [f"• {count} {reason}" for reason, count in sorted(tally.items(), key=lambda kv: -kv[1])]


def _row(source) -> str:
    return (f"⭐ {source.score:.0f} · {render.esc(source.title[:55])}\n"
            f"   {render.esc(source.channel_title)} · {render.duration(source.duration_seconds)} · "
            f"{render.count(source.view_count)} views")


async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="candidates:list"):
        return
    from automation.models import SourceState
    service = await _service()
    all_eligible = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=200)

    if not all_eligible:
        breakdown = _rejection_breakdown(service)
        lines = ["🎯 " + render.bold("Candidates"), "", "No candidate is ready yet."]
        if breakdown:
            total = sum(int(b.split()[1]) for b in breakdown)
            lines += ["", f"{total} discovered candidate{'s' if total != 1 else ''} were not eligible:"]
            lines += breakdown[:8]
        kb = navigation.kb(
            [InlineKeyboardButton("🔍 Run Discovery", callback_data=callbacks.build("discovery", "show"))],
            navigation.nav_row(refresh=callbacks.build("candidates", "list", 0)))
        await delivery.deliver(update, context, "\n".join(lines), kb)
        return

    items, page, total_pages = navigation.paginate(all_eligible, page, PAGE_SIZE)
    start = page * PAGE_SIZE
    lines = [f"🎯 {render.bold('Candidates')} — {start + 1}–{start + len(items)} of {len(all_eligible)}", ""]
    buttons = []
    for i, source in enumerate(items, start + 1):
        lines.append(f"{i}. {_row(source)}")
        buttons.append([InlineKeyboardButton(f"{i}. {source.title[:30]}",
                                              callback_data=callbacks.build("candidates", "show", source.id))])

    rows = buttons + [navigation.pagination_row("candidates", "list", page, total_pages)]
    rows.append(navigation.nav_row(refresh=callbacks.build("candidates", "list", page)))
    await delivery.deliver(update, context, "\n".join(lines), navigation.kb(*rows))


def _score_bar(components: dict, key: str) -> int:
    return round(float(components.get(key, 0) or 0) * 100)


def _detail_text(source) -> str:
    breakdown = source.score_breakdown or {}
    components = breakdown.get("components") or {}
    age = render.ago(source.published_at)

    lines = [
        f"🎯 Opportunity: {render.bold(f'{source.score:.0f}/100')}",
    ]
    if source.discovery_lane:
        lines.append(f"Lane: {render.esc(source.discovery_lane.replace('_', ' ').title())}")
    lines.append(f"Age: {age}")
    lines += [
        "",
        f"Views: {render.count(source.view_count)}",
        f"Likes: {render.count(source.like_count)}",
        f"Comments: {render.count(source.comment_count)}",
    ]
    if components:
        lines += [
            "",
            f"Trend momentum      {_score_bar(components, 'trend_momentum')}",
            f"Engagement          {_score_bar(components, 'engagement_quality')}",
            f"Proven demand       {_score_bar(components, 'proven_demand')}",
            f"Evergreen           {_score_bar(components, 'evergreen_strength')}",
            f"Shorts suitability  {_score_bar(components, 'shorts_suitability')}",
        ]

    lines += ["", render.bold("Rights:")]
    if source.state == "FILTERED":
        reason = (source.rejection_reason or "not eligible").replace("_", " ")
        lines.append(f"🔒 Blocked — {render.esc(reason)}")
    else:
        lines.append("✅ Eligible")

    lines += ["", f"{render.bold('Title:')} {render.esc(source.title)}"]
    lines.append(f"{render.bold('Channel:')} {render.esc(source.channel_title)}")
    return "\n".join(lines)


def _detail_kb(source) -> Any:
    rows = []
    if source.state == "ELIGIBLE":
        rows.append([
            InlineKeyboardButton("▶️ Process", callback_data=callbacks.build("candidates", "process", source.id)),
            InlineKeyboardButton("⏭ Skip", callback_data=callbacks.build("candidates", "skip", source.id)),
        ])
    rows.append([InlineKeyboardButton("📺 Open YouTube", url=source.watch_url)])
    rows.append(navigation.nav_row(
        refresh=callbacks.build("candidates", "show", source.id),
        back=callbacks.build("candidates", "list", 0)))
    return navigation.kb(*rows)


async def show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, source_id: int) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="candidates:show"):
        return
    service = await _service()
    source = service.db.get_source(source_id)
    if source is None:
        await delivery.deliver(update, context, "This candidate no longer exists.",
                                navigation.kb(navigation.nav_row(refresh=callbacks.build("candidates", "list", 0))))
        return
    await delivery.deliver(update, context, _detail_text(source), _detail_kb(source))


async def process(update: Update, context: ContextTypes.DEFAULT_TYPE, source_id: int) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="candidates:process"):
        return
    if update.callback_query is not None:
        await update.callback_query.answer("Submitting…")
    service = await _service()
    from .. import persistence
    result = await service.process_source(source_id)
    user, chat = update.effective_user, update.effective_chat
    persistence.get_store().log_action(
        user_id=user.id if user else None, chat_id=chat.id if chat else None,
        action="candidates:process", target=str(source_id),
        result="ok" if result.get("ok") else "failed", detail=result.get("reason"))

    if result.get("ok"):
        text = "✅ " + render.bold("Submitted.") + " Use Jobs to track it."
        kb = navigation.kb(
            [InlineKeyboardButton("🎬 Jobs", callback_data=callbacks.build("jobs", "list", 0))],
            navigation.nav_row(refresh=callbacks.build("candidates", "list", 0)))
    else:
        text = f"ℹ️ {render.esc(result.get('reason', 'Could not start this candidate.'))}"
        kb = navigation.kb(navigation.nav_row(
            refresh=callbacks.build("candidates", "show", source_id),
            back=callbacks.build("candidates", "list", 0)))
    await delivery.deliver(update, context, text, kb)


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE, source_id: int) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="candidates:skip"):
        return
    service = await _service()
    from .. import persistence
    ok = service.skip_source(source_id)
    user, chat = update.effective_user, update.effective_chat
    persistence.get_store().log_action(
        user_id=user.id if user else None, chat_id=chat.id if chat else None,
        action="candidates:skip", target=str(source_id), result="ok" if ok else "failed")
    if update.callback_query is not None:
        await update.callback_query.answer("Skipped" if ok else "Could not skip this candidate.")
    await show_list(update, context, 0)


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if cb.action == "list":
        await show_list(update, context, cb.int_arg(0) or 0)
        return
    source_id = cb.int_arg(0)
    if source_id is None:
        await update.callback_query.answer("This candidate no longer exists.")
        return
    if cb.action == "show":
        await show_detail(update, context, source_id)
    elif cb.action == "process":
        await process(update, context, source_id)
    elif cb.action == "skip":
        await skip(update, context, source_id)
    else:
        await update.callback_query.answer("Unknown action.")


router.register("candidates", _on_callback)
