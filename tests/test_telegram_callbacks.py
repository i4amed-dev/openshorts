"""Callback data is attacker-controlled: build()/parse() must round-trip
safely, stay under Telegram's 64-byte limit, and the router must guarantee
the client's spinner clears — including when a handler double-answers,
forgets to answer, or raises — without ever mutating a real PTB object
(which blocks arbitrary attribute assignment; a production incident happened
because an earlier version relied on that working).
"""
from __future__ import annotations

import pytest

from autopilot_fakes import run_async
from telegram_bot.callbacks import Callback, Router, build, parse


class FakeQuery:
    """Mimics the one real-PTB behavior that matters here: `CallbackQuery` is
    a `TelegramObject`, and `TelegramObject.__setattr__` hard-blocks
    assigning any attribute after construction — `query.answer = X` raises
    `AttributeError` on the real class. A production incident happened
    because an earlier version of the router relied on exactly that
    assignment working; this fake exists so that regression can never again
    pass silently against a permissive plain-object double.
    """

    def __init__(self, data, *, answer_raises: Exception | None = None):
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "answers", [])
        object.__setattr__(self, "_answer_raises", answer_raises)

    def __setattr__(self, name, value):
        raise AttributeError(f"Attribute `{name}` of class `CallbackQuery` can't be set!")

    async def answer(self, *args, **kwargs):
        if self._answer_raises is not None:
            raise self._answer_raises
        self.answers.append((args, kwargs))


class FakeUpdate:
    def __init__(self, data, *, query: "FakeQuery | None" = None):
        self.callback_query = query or FakeQuery(data)


class TestBuildParse:
    def test_round_trip(self):
        data = build("candidates", "show", 42)
        cb = parse(data)
        assert cb == Callback(ns="candidates", action="show", args=["42"])

    def test_int_arg_parses_valid_int(self):
        cb = parse(build("clips", "preview", 7, 0))
        assert cb.int_arg(0) == 7
        assert cb.int_arg(1) == 0

    def test_int_arg_never_raises_on_forged_data(self):
        cb = Callback(ns="candidates", action="show", args=["not-a-number"])
        assert cb.int_arg(0) is None
        assert cb.int_arg(99) is None  # out of range

    def test_too_long_raises_at_build_time(self):
        with pytest.raises(ValueError):
            build("candidates", "show", "x" * 100)

    def test_parse_rejects_malformed_data(self):
        assert parse(None) is None
        assert parse("") is None
        assert parse("onepart") is None

    def test_parse_tolerates_arbitrary_forged_strings(self):
        # Must never raise, no matter what an attacker sends as callback_data.
        for junk in ("' OR 1=1 --:x", "../../etc/passwd:x", ":::::", "ns:action:" + "y" * 500):
            parse(junk)  # only requirement: no exception


class TestRouterDispatch:
    def test_unknown_namespace_answers_once_with_a_safe_message(self):
        router = Router()
        update = FakeUpdate(build("ghost", "show"))
        run_async(router.dispatch(update, context=None))
        assert len(update.callback_query.answers) == 1

    def test_handler_double_answer_is_tolerated_not_prevented(self):
        """Real `CallbackQuery` objects block attribute assignment (see
        FakeQuery's docstring), so the router can't intercept a handler's own
        `.answer()` calls — it can only guarantee ONE MORE trailing answer on
        top of whatever the handler already did, and never crash either way."""
        router = Router()

        async def handler(update, context, cb):
            await update.callback_query.answer("first")
            await update.callback_query.answer("second")

        router.register("dbl", handler)
        update = FakeUpdate(build("dbl", "go"))
        run_async(router.dispatch(update, context=None))  # must not raise
        assert len(update.callback_query.answers) == 3  # 2 from the handler + 1 trailing
        assert update.callback_query.answers[0][0] == ("first",)

    def test_handler_that_forgets_to_answer_still_gets_answered(self):
        router = Router()

        async def handler(update, context, cb):
            return  # never calls query.answer()

        router.register("quiet", handler)
        update = FakeUpdate(build("quiet", "go"))
        run_async(router.dispatch(update, context=None))
        assert len(update.callback_query.answers) == 1

    def test_handler_exception_still_answers_once_and_propagates(self):
        router = Router()

        async def handler(update, context, cb):
            raise RuntimeError("boom")

        router.register("boom", handler)
        update = FakeUpdate(build("boom", "go"))
        with pytest.raises(RuntimeError):
            run_async(router.dispatch(update, context=None))
        assert len(update.callback_query.answers) == 1

    def test_double_registration_of_the_same_namespace_is_a_loud_bug(self):
        router = Router()

        async def handler(update, context, cb):
            pass

        router.register("dup", handler)
        with pytest.raises(RuntimeError):
            router.register("dup", handler)

    def test_a_rejected_trailing_answer_never_crashes_the_dispatch(self):
        """Telegram can reject an answer (expired query, already answered) —
        `TelegramError` from the router's own trailing call must be swallowed,
        not surfaced as an unhandled exception in the update handler."""
        from telegram.error import BadRequest

        router = Router()

        async def handler(update, context, cb):
            pass  # never answers itself — the trailing call must fire and fail safely

        router.register("flaky", handler)
        data = build("flaky", "go")
        query = FakeQuery(data, answer_raises=BadRequest("Query is too old and response timeout expired"))
        update = FakeUpdate(data, query=query)
        run_async(router.dispatch(update, context=None))  # must not raise

    def test_real_setattr_restriction_is_faithfully_modeled(self):
        """The regression this whole class of test exists to prevent: a real
        `CallbackQuery.answer = X` raises `AttributeError`. If this test ever
        fails, FakeQuery has drifted from real PTB behavior and every other
        test in this file may be passing for the wrong reason."""
        query = FakeQuery("ns:action")
        with pytest.raises(AttributeError):
            query.answer = lambda *a, **kw: None
