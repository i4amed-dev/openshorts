"""Callback data is attacker-controlled: build()/parse() must round-trip
safely, stay under Telegram's 64-byte limit, and the router must answer every
callback exactly once — including when a handler double-answers, forgets to
answer, or raises.
"""
from __future__ import annotations

import pytest

from autopilot_fakes import run_async
from telegram_bot.callbacks import Callback, Router, build, parse


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class FakeUpdate:
    def __init__(self, data):
        self.callback_query = FakeQuery(data)


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

    def test_handler_double_answer_still_hits_telegram_once(self):
        router = Router()

        async def handler(update, context, cb):
            await update.callback_query.answer("first")
            await update.callback_query.answer("second")

        router.register("dbl", handler)
        update = FakeUpdate(build("dbl", "go"))
        run_async(router.dispatch(update, context=None))
        assert len(update.callback_query.answers) == 1
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
