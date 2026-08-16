"""/health — operational health, safe to show a phone screen.

Reuses `app.health_detail()` directly as a Python call (FastAPI route handlers
are plain async functions) rather than an HTTP loopback to localhost, and
rather than re-implementing the checks or reloading any models.
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, errors, navigation, render
from ..callbacks import Callback, router


def _fmt(payload: dict) -> str:
    backend = payload.get("backend") or {}
    autopilot = payload.get("autopilot") or {}
    overall = "✅" if payload.get("status") == "ok" else "⚠️"

    lines = [f"🩺 {render.bold('Klippo Health')}  {overall}", ""]
    lines.append(render.bold("Backend"))
    lines.append(f"  Active jobs: {backend.get('active_jobs', 0)}/{backend.get('max_concurrent_jobs', '?')}")
    lines.append(f"  Queue depth: {backend.get('queue_depth', 0)}")

    if autopilot.get("enabled"):
        lines += ["", render.bold("Autopilot")]
        if autopilot.get("database") == "error":
            lines.append(f"  ❌ {render.esc(autopilot.get('error', 'unknown error')[:200])}")
        else:
            lines.append(f"  Engine: {render.esc(autopilot.get('engine', '—'))}")
            lines.append(f"  Database: ✅")
            lines.append(f"  Last tick: {render.ago(autopilot.get('last_tick_at'))}")
            lines.append(f"  Pending publishes: {autopilot.get('pending_publishes', 0)}")
            lines.append(f"  Uncertain publishes: {autopilot.get('uncertain_publishes', 0)}")
            creds = autopilot.get("credentials") or {}
            if creds:
                lines += ["", render.bold("Credentials")]
                for name, ok in creds.items():
                    lines.append(f"  {'✅' if ok else '❌'} {render.esc(name)}")
            store = autopilot.get("storage") or {}
            if store.get("available"):
                lines += ["", f"{render.bold('Disk:')} {store.get('free_gb')} GB free"]
    else:
        lines += ["", render.italic("Autopilot is disabled.")]

    lines += ["", f"{render.bold('Telegram:')} ✅ connected"]
    return "\n".join(lines)


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="health:show"):
        return
    from app import health_detail
    payload = await health_detail()
    kb = navigation.kb(navigation.nav_row(refresh=callbacks.build("health", "show")))
    await errors.deliver(update, context, _fmt(payload), kb)


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if cb.action == "show":
        await show(update, context)
        return
    await update.callback_query.answer("Unknown action.")


router.register("health", _on_callback)
