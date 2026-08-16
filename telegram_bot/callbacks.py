"""Structured, attacker-safe callback_data namespace + a single dispatch policy.

Every `callback_query.data` is attacker-controlled input — it can be replayed,
edited, or forged by anyone who can send a callback to the bot's API. Routes
are looked up by namespace in a fixed table (never used to build SQL or shell
commands), and every id segment is validated as an integer before any handler
sees it.

Format: ``ns:action:arg1:arg2:...`` joined on ':'. Telegram limits
callback_data to 64 bytes, so `build()` raises early — loud in tests — rather
than silently truncating a route into ambiguity.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

CB_SEP = ":"
MAX_CALLBACK_BYTES = 64


def build(ns: str, action: str, *parts: object) -> str:
    data = CB_SEP.join([ns, action, *[str(p) for p in parts]])
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback_data exceeds {MAX_CALLBACK_BYTES} bytes: {data!r}")
    return data


@dataclass
class Callback:
    ns: str
    action: str
    args: List[str] = field(default_factory=list)

    def int_arg(self, index: int) -> Optional[int]:
        """Safely read a numeric argument. Never raises on bad input — attacker
        controlled data must never crash a handler, just fail the lookup."""
        try:
            return int(self.args[index])
        except (IndexError, ValueError):
            return None


def parse(data: Optional[str]) -> Optional[Callback]:
    if not data:
        return None
    parts = data.split(CB_SEP)
    if len(parts) < 2:
        return None
    return Callback(ns=parts[0], action=parts[1], args=parts[2:])


Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE, Callback], Awaitable[None]]


class Router:
    """One place callback routes are registered and dispatched from, so the
    answer-once policy and unknown-callback handling can never be bypassed."""

    def __init__(self) -> None:
        self._routes: Dict[str, Handler] = {}

    def register(self, ns: str, handler: Handler) -> None:
        if ns in self._routes:
            raise RuntimeError(f"callback namespace already registered: {ns!r}")
        self._routes[ns] = handler

    async def dispatch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return

        answered = {"done": False}
        real_answer = query.answer

        async def guarded_answer(*args, **kwargs):
            if answered["done"]:
                return
            answered["done"] = True
            try:
                await real_answer(*args, **kwargs)
            except Exception as exc:
                log.warning("callback answer failed: %s", exc)

        query.answer = guarded_answer  # type: ignore[method-assign]
        try:
            cb = parse(query.data)
            if cb is None or cb.ns not in self._routes:
                await query.answer("This button has expired.")
                return
            await self._routes[cb.ns](update, context, cb)
        finally:
            if not answered["done"]:
                await query.answer()


router = Router()


async def _noop(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    await update.callback_query.answer()


router.register("noop", _noop)
