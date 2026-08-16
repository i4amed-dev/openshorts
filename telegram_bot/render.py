"""One rendering system for the whole bot.

HTML parse mode instead of MarkdownV2: MarkdownV2 requires escaping ~20
punctuation characters and gets fragile fast once titles/channel names/vendor
error strings are involved. HTML needs exactly one escape function
(`html.escape`) applied at the boundary where dynamic text enters a template,
and Telegram's HTML subset is small and predictable.

`safe_text()` is the only place callers should reach for — it always returns
something Telegram can render, falling back to a stripped-tag plain-text
version if the built string still fails to parse (caller sends with
`plain=True` in that case).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Optional

MAX_MESSAGE_LENGTH = 4096


def esc(value) -> str:
    """Escape dynamic text for embedding in an HTML-parse-mode message."""
    if value is None:
        return "—"
    return html.escape(str(value), quote=False)


def link(text: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(text)}</a>'


def bold(text: str) -> str:
    return f"<b>{esc(text)}</b>"


def italic(text: str) -> str:
    return f"<i>{esc(text)}</i>"


def code(text: str) -> str:
    return f"<code>{esc(text)}</code>"


_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(text: str) -> str:
    """Plain-text fallback: drop markup, unescape entities back to literal text."""
    return html.unescape(_TAG_RE.sub("", text))


@dataclass
class Rendered:
    text: str
    parse_mode: Optional[str]  # "HTML" or None (plain)


def truncate(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rsplit("\n", 1)[0] + "\n\n… (truncated)"


def count(n) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def duration(seconds) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ago(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return "—"
    try:
        from datetime import datetime, timezone
        then = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        sec = int((datetime.now(timezone.utc) - then).total_seconds())
        if sec < 0:
            sec = 0
        if sec < 60:
            return f"{sec}s ago"
        if sec < 3600:
            return f"{sec // 60}m ago"
        if sec < 86400:
            return f"{sec // 3600}h ago"
        return f"{sec // 86400}d ago"
    except (ValueError, TypeError):
        return str(iso_ts)[:16]


def fmt_local(iso_ts: Optional[str], tz: str) -> str:
    if not iso_ts:
        return "—"
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo(tz)).strftime("%d %b %H:%M")
    except Exception:
        return str(iso_ts)[:16]
