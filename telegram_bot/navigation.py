"""Reusable navigation building blocks: a consistent Refresh/Back/Home row and
generic pagination, so every screen behaves the same way instead of each
handler reinventing button layout.
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import callbacks


def nav_row(*, refresh: str, back: str | None = None,
            home: bool = True) -> List[InlineKeyboardButton]:
    """A standard bottom row: Refresh (always), Back (optional), Home (usual)."""
    row = [InlineKeyboardButton("🔄 Refresh", callback_data=refresh)]
    if back:
        row.append(InlineKeyboardButton("◀️ Back", callback_data=back))
    if home:
        row.append(InlineKeyboardButton("🏠 Home", callback_data=callbacks.build("home", "show")))
    return row


def paginate(items: Sequence, page: int, page_size: int) -> Tuple[List, int, int]:
    """Slice `items` for `page` (0-indexed). Returns (page_items, page, total_pages).
    Out-of-range pages clamp instead of raising — a stale/forged page number in
    callback_data must never crash the handler."""
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return list(items[start:start + page_size]), page, total_pages


def pagination_row(ns: str, list_action: str, page: int, total_pages: int,
                    *extra_args: object) -> List[InlineKeyboardButton]:
    """[◀] [page/total] [▶] — idempotent: re-tapping the same edge is a no-op
    because `paginate()` clamps out-of-range pages rather than erroring."""
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(
            "◀", callback_data=callbacks.build(ns, list_action, page - 1, *extra_args)))
    row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop:noop"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton(
            "▶", callback_data=callbacks.build(ns, list_action, page + 1, *extra_args)))
    return row


def kb(*rows: Sequence[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])
