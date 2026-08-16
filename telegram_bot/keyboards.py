"""Keyboards shared across multiple screens. Screen-specific keyboards live next
to the handler that renders them; only truly cross-cutting ones (confirmation,
the home grid) live here.
"""
from __future__ import annotations

import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import callbacks


def dashboard_url() -> str | None:
    """The configured dashboard URL, or None if unset/clearly unreachable
    from a phone (e.g. localhost) — never show a button that can't work."""
    url = (os.environ.get("TELEGRAM_DASHBOARD_URL") or "").strip()
    if not url:
        return None
    if "localhost" in url or "127.0.0.1" in url:
        return None
    return url


def confirm(ns: str, action: str, label: str, *args: object) -> InlineKeyboardMarkup:
    """A single yes/no confirmation. For destructive multi-step flows (Emergency
    Stop) handlers build their own two-stage version instead of this."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Yes, {label}", callback_data=callbacks.build(ns, action, *args)),
        InlineKeyboardButton("❌ Cancel", callback_data=callbacks.build(ns, "cancel", *args)),
    ]])


def home_grid(*, autopilot_enabled: bool, paused: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📡 Monitor", callback_data=callbacks.build("status", "monitor")),
            InlineKeyboardButton("🔍 Discover", callback_data=callbacks.build("discovery", "show")),
        ],
        [
            InlineKeyboardButton("🎯 Candidates", callback_data=callbacks.build("candidates", "list", 0)),
            InlineKeyboardButton("🎬 Jobs", callback_data=callbacks.build("jobs", "list", 0)),
        ],
        [
            InlineKeyboardButton("✂️ Clips", callback_data=callbacks.build("clips", "list", 0)),
            InlineKeyboardButton("📅 Publishing", callback_data=callbacks.build("publishing", "list", 0)),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data=callbacks.build("settings", "show")),
            InlineKeyboardButton("🩺 Health", callback_data=callbacks.build("health", "show")),
        ],
        [
            InlineKeyboardButton("🔔 Notifications", callback_data=callbacks.build("notify", "show")),
            InlineKeyboardButton("🚨 Errors", callback_data=callbacks.build("errors", "list", 0)),
        ],
    ]
    if autopilot_enabled:
        rows.append([
            InlineKeyboardButton(
                "▶️ Resume" if paused else "⏸ Pause",
                callback_data=callbacks.build("admin", "resume" if paused else "pause")),
            InlineKeyboardButton("🛠 Admin", callback_data=callbacks.build("admin", "show")),
        ])
    else:
        rows.append([
            InlineKeyboardButton("▶️ Enable", callback_data=callbacks.build("admin", "enable")),
            InlineKeyboardButton("🛠 Admin", callback_data=callbacks.build("admin", "show")),
        ])
    url = dashboard_url()
    if url:
        rows.append([InlineKeyboardButton("🌐 Open Dashboard", url=url)])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data=callbacks.build("home", "show"))])
    return InlineKeyboardMarkup(rows)
