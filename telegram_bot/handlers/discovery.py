"""/discover — discovery control center: current settings, the last run's real
results, and a Run Now action. No dry-run button: `automation/discovery.py`
has no dry-run mode to call, so the UI doesn't promise one.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, navigation, render
from .. import errors as delivery
from ..callbacks import Callback, router


async def _service():
    from automation.service import get_service
    return get_service()


def _last_discovery_run(service) -> Optional[Dict[str, Any]]:
    for run in service.db.recent_runs(limit=8):
        if run.get("kind") == "discovery":
            return run
    return None


def _msg(settings: Dict[str, Any], last_run: Optional[Dict[str, Any]]) -> str:
    disc = settings.get("discovery") or {}
    topics = disc.get("topics") or []
    lines = [
        "🔍 " + render.bold("Discovery"), "",
        f"Strategies: {render.esc(', '.join(disc.get('strategies', [])))}",
        f"Region: {render.esc(disc.get('region_code', 'US'))}",
        f"Topics ({len(topics)}): {render.esc(', '.join(topics[:6]))}"
        f"{'…' if len(topics) > 6 else ''}",
    ]

    if last_run is None:
        lines += ["", render.italic("No discovery run yet.")]
        return "\n".join(lines)

    stats = last_run.get("stats") or {}
    status = last_run.get("status", "?")
    if status == "RUNNING":
        lines += ["", "⏳ " + render.italic("A discovery run is in progress…")]
        return "\n".join(lines)

    lines += ["", render.bold("Last run:") + f"  {render.ago(last_run.get('finished_at') or last_run.get('started_at'))}"]
    if stats.get("quota_exhausted"):
        lines.append(f"⚠️ Skipped — {render.esc(stats.get('bucket', 'a'))} YouTube quota is exhausted.")
        return "\n".join(lines)
    if status == "FAILED":
        lines.append(f"❌ {render.esc((last_run.get('error') or 'Discovery failed')[:200])}")
        return "\n".join(lines)

    lines += [
        f"Fetched: {stats.get('candidates', 0)}",
        f"Stored (new): {stats.get('stored', 0)}",
        f"Already known: {stats.get('duplicates', 0)}",
        f"Rights + technically eligible: {stats.get('eligible', 0)}",
        f"Rejected: {stats.get('rejected', 0)}",
    ]
    if stats.get("best_opportunity"):
        lines.append(f"Best opportunity score: {stats.get('best_opportunity')}")
    return "\n".join(lines)


def _kb() -> Any:
    return navigation.kb(
        [InlineKeyboardButton("▶️ Run Now", callback_data=callbacks.build("discovery", "run"))],
        [InlineKeyboardButton("🎯 Candidates", callback_data=callbacks.build("candidates", "list", 0)),
         InlineKeyboardButton("⚙️ Settings", callback_data=callbacks.build("settings", "section", "discovery"))],
        navigation.nav_row(refresh=callbacks.build("discovery", "show")),
    )


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="discovery:show"):
        return
    service = await _service()
    settings = service.get_settings()
    last_run = _last_discovery_run(service)
    await delivery.deliver(update, context, _msg(settings, last_run), _kb())


async def run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="discovery:run"):
        return
    if update.callback_query is not None:
        await update.callback_query.answer("Discovery started…")
    service = await _service()
    from .. import persistence
    user = update.effective_user
    chat = update.effective_chat
    try:
        result = await service.run_discovery_now()
        persistence.get_store().log_action(
            user_id=user.id if user else None, chat_id=chat.id if chat else None,
            action="discovery:run", detail=str(result))
    except Exception as exc:
        persistence.get_store().log_action(
            user_id=user.id if user else None, chat_id=chat.id if chat else None,
            action="discovery:run", result="error", detail=str(exc)[:200])
    await show(update, context)


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if cb.action == "show":
        await show(update, context)
        return
    if cb.action == "run":
        await run(update, context)
        return
    await update.callback_query.answer("Unknown action.")


router.register("discovery", _on_callback)
