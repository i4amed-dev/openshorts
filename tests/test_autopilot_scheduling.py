"""Slot maths: timezones, DST, daily caps, spacing and catch-up.

This is where an unattended system quietly goes wrong. A naive local datetime
shifts the whole content calendar by an hour twice a year; a missing spacing
check dumps three posts in ten minutes after a restart. Both are invisible until
someone looks at the actual feed, so they are pinned here instead.
"""
from datetime import datetime, timedelta, timezone

import pytest

from automation.scheduler import (
    allocate_publish_slots, apply_catch_up, discovery_is_due, get_zone,
    local_day_bounds, local_slot_to_utc, next_discovery_time, upcoming_slots,
)
from autopilot_fakes import base_config

MADRID = "Europe/Madrid"
NEW_YORK = "America/New_York"


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class TestLocalToUtc:
    def test_winter_offset(self):
        # Madrid is UTC+1 in January.
        assert local_slot_to_utc(datetime(2026, 1, 15).date(), "11:30",
                                 get_zone(MADRID)) == utc(2026, 1, 15, 10, 30)

    def test_summer_offset(self):
        # ...and UTC+2 in July. The same wall-clock slot is a different instant.
        assert local_slot_to_utc(datetime(2026, 7, 15).date(), "11:30",
                                 get_zone(MADRID)) == utc(2026, 7, 15, 9, 30)

    def test_the_stored_instant_is_always_utc(self):
        slot = local_slot_to_utc(datetime(2026, 7, 15).date(), "11:30", get_zone(NEW_YORK))
        assert slot.tzinfo == timezone.utc


class TestDaylightSaving:
    def test_spring_forward_never_yields_two_posts_at_one_instant(self):
        """The nonexistent-hour trap.

        On 29-mar-2026 Madrid jumps 02:00 → 03:00. Slots at 02:30 and 03:30 can
        normalise onto the same real instant, which would double-book one moment.
        """
        config = base_config(timezone=MADRID,
                             schedule={"publish_times": ["01:30", "02:30", "03:30"]})
        slots = upcoming_slots(config, config["schedule"]["publish_times"],
                               after=utc(2026, 3, 28, 12, 0), limit=12)
        assert len(slots) == len(set(slots))

    def test_fall_back_repeated_hour_fires_once(self):
        """25-oct-2026: Madrid repeats 02:00→03:00. A 02:30 slot must fire once."""
        config = base_config(timezone=MADRID, schedule={"publish_times": ["02:30"]})
        slots = upcoming_slots(config, ["02:30"], after=utc(2026, 10, 24, 12, 0),
                               limit=5, horizon_days=3)
        on_the_day = [s for s in slots if s.astimezone(get_zone(MADRID)).date()
                      == datetime(2026, 10, 25).date()]
        assert len(on_the_day) == 1

    def test_a_slot_keeps_its_local_wall_clock_time_across_a_transition(self):
        config = base_config(timezone=MADRID, schedule={"publish_times": ["11:30"]})
        slots = upcoming_slots(config, ["11:30"], after=utc(2026, 3, 27, 0, 0),
                               limit=5, horizon_days=5)
        zone = get_zone(MADRID)
        assert all(s.astimezone(zone).strftime("%H:%M") == "11:30" for s in slots)


class TestUpcomingSlots:
    def test_slots_are_strictly_in_the_future(self):
        # A slot exactly "now" belongs to the tick that already ran; re-emitting
        # it is how a restart double-fires a schedule.
        config = base_config(timezone="UTC", schedule={"publish_times": ["12:00"]})
        now = utc(2026, 8, 12, 12, 0)
        assert all(s > now for s in upcoming_slots(config, ["12:00"], after=now, limit=3))

    def test_disabled_days_are_skipped(self):
        # Weekdays only: 0=Mon … 4=Fri.
        config = base_config(timezone="UTC",
                             schedule={"publish_times": ["12:00"],
                                       "days_of_week": [0, 1, 2, 3, 4]})
        slots = upcoming_slots(config, ["12:00"], after=utc(2026, 8, 14, 13, 0), limit=3)
        # 14-aug-2026 is a Friday, so the next slots are Mon/Tue/Wed.
        assert [s.weekday() for s in slots] == [0, 1, 2]

    def test_slots_come_back_in_chronological_order(self):
        config = base_config(timezone="UTC",
                             schedule={"publish_times": ["21:00", "09:00", "15:00"]})
        slots = upcoming_slots(config, config["schedule"]["publish_times"],
                               after=utc(2026, 8, 12, 0, 0), limit=6)
        assert slots == sorted(slots)

    def test_the_horizon_bounds_the_walk(self):
        config = base_config(timezone="UTC", schedule={"publish_times": ["12:00"]})
        slots = upcoming_slots(config, ["12:00"], after=utc(2026, 8, 12, 0, 0),
                               limit=100, horizon_days=3)
        assert len(slots) <= 4


class TestAllocation:
    def test_allocates_the_configured_slots_in_order(self):
        config = base_config(timezone="UTC",
                             schedule={"publish_times": ["11:30", "16:30", "21:00"],
                                       "min_spacing_minutes": 60})
        slots = allocate_publish_slots(config, count=3, now=utc(2026, 8, 12, 8, 0),
                                       taken=[])
        assert slots == [utc(2026, 8, 12, 11, 30), utc(2026, 8, 12, 16, 30),
                         utc(2026, 8, 12, 21, 0)]

    def test_daily_cap_pushes_the_overflow_to_tomorrow(self):
        config = base_config(timezone="UTC",
                             schedule={"publish_times": ["11:30", "16:30", "21:00"],
                                       "max_posts_per_day": 2, "min_spacing_minutes": 60})
        slots = allocate_publish_slots(config, count=4, now=utc(2026, 8, 12, 8, 0),
                                       taken=[])
        days = [s.date() for s in slots]
        assert days.count(datetime(2026, 8, 12).date()) == 2
        assert days.count(datetime(2026, 8, 13).date()) == 2

    def test_a_taken_slot_is_never_reused(self):
        config = base_config(timezone="UTC",
                             schedule={"publish_times": ["11:30", "16:30", "21:00"],
                                       "min_spacing_minutes": 0})
        taken = [utc(2026, 8, 12, 16, 30)]
        slots = allocate_publish_slots(config, count=2, now=utc(2026, 8, 12, 8, 0),
                                       taken=taken)
        assert utc(2026, 8, 12, 16, 30) not in slots

    def test_minimum_spacing_is_honoured_against_existing_posts(self):
        config = base_config(timezone="UTC",
                             schedule={"publish_times": ["11:00", "11:30", "16:30"],
                                       "min_spacing_minutes": 120})
        taken = [utc(2026, 8, 12, 11, 0)]
        slots = allocate_publish_slots(config, count=2, now=utc(2026, 8, 12, 8, 0),
                                       taken=taken)
        # 11:30 is only 30 minutes after an existing post — skipped.
        assert utc(2026, 8, 12, 11, 30) not in slots
        assert slots[0] == utc(2026, 8, 12, 16, 30)

    def test_returns_fewer_slots_rather_than_inventing_one(self):
        config = base_config(timezone="UTC",
                             schedule={"publish_times": ["12:00"], "max_posts_per_day": 1,
                                       "horizon_days": 2, "min_spacing_minutes": 0})
        slots = allocate_publish_slots(config, count=10, now=utc(2026, 8, 12, 13, 0),
                                       taken=[])
        assert 0 < len(slots) <= 3

    def test_daily_cap_is_counted_in_the_operators_timezone(self):
        """A UTC day boundary would miscount an evening schedule.

        New York 21:00 is 01:00 UTC the *next* day. Counting "posts today" in UTC
        would let a 2/day cap schedule four in one local evening.
        """
        config = base_config(timezone=NEW_YORK,
                             schedule={"publish_times": ["20:00", "21:00", "22:00"],
                                       "max_posts_per_day": 2, "min_spacing_minutes": 0})
        slots = allocate_publish_slots(config, count=3, now=utc(2026, 8, 12, 12, 0),
                                       taken=[])
        zone = get_zone(NEW_YORK)
        local_days = [s.astimezone(zone).date() for s in slots]
        assert local_days.count(datetime(2026, 8, 12).date()) == 2

    def test_the_earliest_floor_is_respected(self):
        config = base_config(timezone="UTC",
                             schedule={"publish_times": ["11:30", "16:30", "21:00"],
                                       "min_spacing_minutes": 0})
        slots = allocate_publish_slots(config, count=1, now=utc(2026, 8, 12, 8, 0),
                                       taken=[], earliest=utc(2026, 8, 12, 17, 0))
        assert slots[0] == utc(2026, 8, 12, 21, 0)


class TestDiscoverySchedule:
    def test_due_when_a_slot_passed_since_the_last_run(self):
        config = base_config(timezone="UTC", schedule={"discovery_times": ["03:00"]})
        assert discovery_is_due(config, now=utc(2026, 8, 12, 4, 0),
                                last_discovery_at=utc(2026, 8, 11, 3, 0))

    def test_not_due_twice_for_the_same_slot(self):
        config = base_config(timezone="UTC", schedule={"discovery_times": ["03:00"]})
        assert not discovery_is_due(config, now=utc(2026, 8, 12, 4, 0),
                                    last_discovery_at=utc(2026, 8, 12, 3, 1))

    def test_enabling_at_lunchtime_does_not_fire_this_mornings_slot(self):
        # First-ever run: only a slot in the last couple of hours counts, so
        # switching Autopilot on at 14:00 does not immediately kick off the
        # 03:00 job.
        config = base_config(timezone="UTC", schedule={"discovery_times": ["03:00"]})
        assert not discovery_is_due(config, now=utc(2026, 8, 12, 14, 0),
                                    last_discovery_at=None)

    def test_one_missed_day_produces_one_catch_up_not_a_backlog(self):
        config = base_config(timezone="UTC", schedule={"discovery_times": ["03:00"]})
        # Down for five days: still just "due once".
        assert discovery_is_due(config, now=utc(2026, 8, 12, 10, 0),
                                last_discovery_at=utc(2026, 8, 7, 3, 0))

    def test_next_discovery_time_is_the_upcoming_slot(self):
        config = base_config(timezone=MADRID, schedule={"discovery_times": ["03:00"]})
        assert next_discovery_time(config, after=utc(2026, 8, 12, 12, 0)) == utc(
            2026, 8, 13, 1, 0)  # 03:00 Madrid in summer = 01:00 UTC


class TestCatchUp:
    def test_a_future_slot_is_left_alone(self):
        config = base_config(timezone="UTC")
        future = utc(2026, 8, 12, 21, 0)
        assert apply_catch_up(config, missed_slot=future, now=utc(2026, 8, 12, 12, 0),
                              taken=[]) == future

    def test_next_slot_policy_moves_it_forward(self):
        config = base_config(timezone="UTC",
                             schedule={"publish_times": ["11:30", "16:30", "21:00"],
                                       "catch_up_policy": "next_slot",
                                       "min_spacing_minutes": 0})
        new_slot = apply_catch_up(config, missed_slot=utc(2026, 8, 11, 11, 30),
                                  now=utc(2026, 8, 12, 12, 0), taken=[])
        assert new_slot == utc(2026, 8, 12, 16, 30)

    def test_skip_policy_drops_it(self):
        config = base_config(timezone="UTC", schedule={"catch_up_policy": "skip"})
        assert apply_catch_up(config, missed_slot=utc(2026, 8, 11, 11, 30),
                              now=utc(2026, 8, 12, 12, 0), taken=[]) is None

    def test_immediate_policy_still_spaces_the_burst(self):
        """The anti-burst guarantee.

        Three posts missed while the Mac slept must not all fire at once, even
        under the most eager catch-up policy.
        """
        config = base_config(timezone="UTC",
                             schedule={"catch_up_policy": "immediate",
                                       "min_spacing_minutes": 90})
        now = utc(2026, 8, 12, 12, 0)
        taken = []
        chosen = []
        for missed in (utc(2026, 8, 11, 11, 30), utc(2026, 8, 11, 16, 30),
                       utc(2026, 8, 11, 21, 0)):
            slot = apply_catch_up(config, missed_slot=missed, now=now, taken=taken)
            chosen.append(slot)
            taken.append(slot)
        gaps = [(b - a).total_seconds() / 60 for a, b in zip(chosen, chosen[1:])]
        assert all(gap >= 90 for gap in gaps)


class TestLocalDayBounds:
    def test_bounds_track_the_configured_zone(self):
        config = base_config(timezone=NEW_YORK)
        start, end = local_day_bounds(config, now=utc(2026, 8, 12, 12, 0))
        # 12-aug-2026 in New York (EDT, UTC-4) starts at 04:00 UTC.
        assert start == utc(2026, 8, 12, 4, 0)
        assert end - start == timedelta(days=1)

    def test_late_evening_utc_is_still_the_same_local_day(self):
        config = base_config(timezone=NEW_YORK)
        start, _end = local_day_bounds(config, now=utc(2026, 8, 13, 2, 0))
        assert start == utc(2026, 8, 12, 4, 0)
