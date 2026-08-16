"""/help — contextual, not a wall of commands: grouped by what you're trying
to do, plus the handful of questions a new operator actually asks.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, navigation, render
from .. import errors as delivery
from ..callbacks import Callback, router

_SECTIONS = {
    "monitor": (
        "📡 Monitor", [
            ("/home", "The command center — current activity, next actions, quick nav"),
            ("/status", "Full status with inline pause/resume/discover controls"),
            ("/monitor", "Live snapshot: what's running, what's next, recent activity"),
            ("/candidates", "Browse the ranked queue, see why each one scored the way it did"),
            ("/jobs", "Autopilot-submitted Clip Generator jobs and their real stage"),
            ("/health", "Backend + Autopilot + Telegram health, safe to screenshot"),
            ("/errors", "Recent errors, each linked to the source/job/post it happened on"),
        ]),
    "create": (
        "➕ Create", [
            ("/new", "Submit a YouTube URL manually — probes it, then asks you to confirm rights"),
            ("/candidates → Process", "Process a specific candidate right now, out of score order"),
        ]),
    "publish": (
        "📤 Publish", [
            ("/clips", "Browse generated clips, preview them, or publish one"),
            ("/publishing", "Every scheduled/live post and its real per-platform state"),
        ]),
    "settings": (
        "⚙️ Settings", [
            ("/settings", "Discovery, rights, schedule, platforms — all guided, no raw JSON"),
            ("/notifications", "What this chat gets pushed, quiet hours, the daily digest"),
        ]),
    "admin": (
        "🛠 Admin", [
            ("/pause · /resume", "Soft stop/start — pause finishes the current job, starts none new"),
            ("Admin → Enable/Disable", "Turn Autopilot on (runs a real preflight first) or off"),
            ("Admin → Emergency Stop", "Two-step confirm; cancels what it safely can and reports exactly what happened"),
        ]),
}

_FAQ = [
    ("What does Autopilot do?",
     "Discovers YouTube videos, ranks them, submits the best one to the same Clip "
     "Generator manual mode uses, then schedules and publishes the resulting clips "
     "through Upload-Post — unattended."),
    ("Why can't I process this candidate?",
     "Either something else is already processing (Klippo runs one heavy job at a "
     "time), today's source limit is reached, or it failed the quality gate. The "
     "candidate detail screen shows the real reason."),
    ("What does UNCERTAIN mean?",
     "Upload-Post's response was lost or timed out, so Klippo genuinely doesn't know "
     "if it posted. It is never auto-retried — Check Status asks the vendor, or you "
     "confirm what actually happened."),
    ("Why is YouTube Data API needed?",
     "For discovery — finding and scoring candidate videos. Upload-Post is a separate "
     "key, for the social publishing step. Neither substitutes for the other."),
]


def _text() -> str:
    lines = ["❓ " + render.bold("Help"), ""]
    for _key, (title, items) in _SECTIONS.items():
        lines.append(render.bold(title))
        for cmd, desc in items:
            lines.append(f"  {render.code(cmd)} — {render.esc(desc)}")
        lines.append("")
    lines.append(render.bold("Common questions:"))
    for q, a in _FAQ:
        lines.append(f"\n{render.bold('Q: ' + q)}\n{render.esc(a)}")
    return "\n".join(lines)


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="help:show"):
        return
    kb = navigation.kb([InlineKeyboardButton("🏠 Home", callback_data=callbacks.build("home", "show"))])
    await delivery.deliver(update, context, _text(), kb)


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if cb.action == "show":
        await show(update, context)
        return
    await update.callback_query.answer("Unknown action.")


router.register("help", _on_callback)
