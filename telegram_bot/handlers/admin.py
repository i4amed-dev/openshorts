"""/pause /resume /enable /disable /discover /process /stop — engine controls.

Emergency Stop is deliberately the hardest thing to trigger by accident: it
lives behind the Admin screen (never next to ordinary navigation), needs two
taps with an explicit warning in between, and always renders the *real*
outcome `service.emergency_stop()` reports — never a static "done" string.
"""
from __future__ import annotations

from typing import Any, Dict

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, errors, navigation, render
from ..callbacks import Callback, router


async def _service():
    from automation.service import get_service
    return get_service()


def _admin_kb(*, enabled: bool, paused: bool) -> Any:
    rows = []
    if not enabled:
        rows.append([InlineKeyboardButton("▶️ Enable", callback_data=callbacks.build("admin", "enable"))])
    else:
        rows.append([
            InlineKeyboardButton("▶️ Resume" if paused else "⏸ Pause",
                                  callback_data=callbacks.build("admin", "resume" if paused else "pause")),
            InlineKeyboardButton("⏹ Disable", callback_data=callbacks.build("admin", "disable")),
        ])
    rows.append([
        InlineKeyboardButton("🔍 Discovery", callback_data=callbacks.build("discovery", "show")),
        InlineKeyboardButton("⚡ Process next", callback_data=callbacks.build("admin", "process")),
    ])
    rows.append([InlineKeyboardButton("🚨 Emergency Stop", callback_data=callbacks.build("admin", "stop_warn"))])
    rows.append(navigation.nav_row(refresh=callbacks.build("admin", "show")))
    return navigation.kb(*rows)


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="admin:show"):
        return
    service = await _service()
    status = service.status()
    text = "🛠 " + render.bold("Admin") + "\n\n" + render.italic(
        "Engine controls and Emergency Stop. Actions here affect the live system.")
    await errors.deliver(update, context, text, _admin_kb(
        enabled=bool(status.get("enabled")),
        paused=status.get("status") in ("PAUSED", "PAUSED_ERROR")))


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="admin:pause"):
        return
    service = await _service()
    service.pause()
    _log(update, "admin:pause")
    await show(update, context)


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="admin:resume"):
        return
    service = await _service()
    service.resume()
    _log(update, "admin:resume")
    await show(update, context)


async def disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="admin:disable"):
        return
    service = await _service()
    service.disable()
    _log(update, "admin:disable")
    await show(update, context)


def _preflight_text(report: Dict[str, Any]) -> str:
    lines = ["❌ " + render.bold("Autopilot cannot start"), ""]
    for c in report.get("checks", []):
        mark = "✅" if c["ok"] else "❌"
        lines.append(f"{mark} {render.esc(c['message'])}")
    return "\n".join(lines)


async def enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="admin:enable"):
        return
    service = await _service()
    from automation.service import PreflightError
    try:
        await service.enable()
        _log(update, "admin:enable")
        text = "✅ " + render.bold("Autopilot enabled.")
    except PreflightError as exc:
        _log(update, "admin:enable", result="failed", detail="preflight failed")
        text = _preflight_text(exc.report)
    except Exception as exc:
        _log(update, "admin:enable", result="error", detail=str(exc)[:200])
        text = "❌ " + render.esc(str(exc)[:300])
    kb = navigation.kb([InlineKeyboardButton("⚙️ Settings", callback_data=callbacks.build("settings", "show"))],
                        navigation.nav_row(refresh=callbacks.build("admin", "show"), back=callbacks.build("admin", "show")))
    await errors.deliver(update, context, text, kb)


async def process_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="admin:process"):
        return
    if update.callback_query is not None:
        await update.callback_query.answer("Submitting…")
    service = await _service()
    try:
        result = await service.process_next_now()
        _log(update, "admin:process", detail=str(result))
        text = "✅ Job submitted. Use Jobs to track it." if result.get("ok") else \
            f"ℹ️ {render.esc(str(result.get('reason', 'Nothing to process')))}"
    except Exception as exc:
        _log(update, "admin:process", result="error", detail=str(exc)[:200])
        text = "❌ " + render.esc(str(exc)[:200])
    kb = navigation.kb(
        [InlineKeyboardButton("🎬 Jobs", callback_data=callbacks.build("jobs", "list", 0))],
        navigation.nav_row(refresh=callbacks.build("admin", "show"), back=callbacks.build("admin", "show")))
    await errors.deliver(update, context, text, kb)


async def stop_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="admin:stop_warn"):
        return
    text = (
        "⚠️ " + render.bold("Emergency Stop") + "\n\n"
        "This will:\n"
        "• stop new discovery\n"
        "• stop new source submissions\n"
        "• cancel local pending publishes\n"
        "• attempt to cancel eligible Upload-Post scheduled jobs\n\n"
        + render.italic("Already published content cannot be undone.")
    )
    kb = navigation.kb(
        [InlineKeyboardButton("Continue →", callback_data=callbacks.build("admin", "stop_confirm"))],
        [InlineKeyboardButton("❌ Cancel", callback_data=callbacks.build("admin", "show"))])
    await errors.deliver(update, context, text, kb)


async def stop_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="admin:stop_confirm"):
        return
    text = "🛑 " + render.bold("Are you absolutely sure?") + "\n\nThis cannot be undone."
    kb = navigation.kb(
        [InlineKeyboardButton("🛑 CONFIRM STOP", callback_data=callbacks.build("admin", "stop_go"))],
        [InlineKeyboardButton("❌ Cancel", callback_data=callbacks.build("admin", "show"))])
    await errors.deliver(update, context, text, kb)


async def stop_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="admin:stop_go"):
        return
    if update.callback_query is not None:
        await update.callback_query.answer("Stopping…")
    service = await _service()
    try:
        report = await service.emergency_stop()
        _log(update, "admin:stop_go", detail=str(report))
        lines = ["🛑 " + render.bold("Emergency Stop executed"), ""]
        lines.append(f"Local jobs canceled: {report.get('canceled_local', 0)}")
        lines.append(f"Upload-Post schedules canceled: {report.get('canceled_vendor', 0)}")
        lines.append(f"Already published: {report.get('already_published', 0)}")
        lines.append(f"Cancel failed: {report.get('vendor_errors', 0)}")
        lines.append(f"Unknown (could not confirm): {report.get('vendor_not_found', 0)}")
        lines.append(f"Candidates dropped: {report.get('released_sources', 0)}")
        text = "\n".join(lines)
    except Exception as exc:
        _log(update, "admin:stop_go", result="error", detail=str(exc)[:200])
        text = "❌ Emergency Stop hit an error: " + render.esc(str(exc)[:300]) + \
            "\n\nCheck /health and /errors — the engine may be partially stopped."
    kb = navigation.kb(navigation.nav_row(refresh=callbacks.build("admin", "show")))
    await errors.deliver(update, context, text, kb)


def _log(update: Update, action: str, *, result: str = "ok", detail: str | None = None) -> None:
    from .. import persistence
    user = update.effective_user
    chat = update.effective_chat
    persistence.get_store().log_action(
        user_id=user.id if user else None, chat_id=chat.id if chat else None,
        action=action, result=result, detail=detail)


_ACTIONS = {
    "show": show, "pause": pause, "resume": resume, "enable": enable, "disable": disable,
    "process": process_next,
    "stop_warn": stop_warn, "stop_confirm": stop_confirm, "stop_go": stop_go,
}


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    handler = _ACTIONS.get(cb.action)
    if handler is None:
        await update.callback_query.answer("Unknown action.")
        return
    await handler(update, context)


router.register("admin", _on_callback)
