"""Diversity: one channel or one lane must not own the whole candidate pool.

Per-signal penalties are unit-tested in test_autopilot_opportunity.py
(channel_repeat, near_duplicate_title). This file covers the parts that only
show up once discovery and selection work together: lane rotation actually
diversifies what one run fetches, and the exploration rate in
``discovery.pick_next_source`` measurably pulls picks away from the single
top-ranked candidate over many trials.
"""
import random
from datetime import datetime, timedelta, timezone

import pytest

from automation import discovery
from automation.config import POLICY_CREATIVE_COMMONS, normalise
from automation.db import AutopilotDB
from automation.models import SourceState
from autopilot_fakes import FakeYouTubeClient, make_record, run_async

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    database = AutopilotDB(":memory:").connect()
    yield database
    database.close()


def _config(**overrides):
    base = {"rights": {"policy": POLICY_CREATIVE_COMMONS},
           "discovery": {"lanes": ["TRENDING_NOW"], "region_code": "US"}}
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value
    return normalise(base)


class TestExplorationRate:
    def test_a_zero_exploration_rate_always_picks_the_top_candidate(self, db):
        records = [make_record(f"vid{i:08d}", view_count=1000 * (i + 1), now=NOW)
                  for i in range(10)]
        client = FakeYouTubeClient(records)
        run_async(discovery.run_discovery(db, _config(), client, run_id="r1", now=NOW))
        for source in db.list_sources(states=[SourceState.ELIGIBLE], limit=20):
            # Spread scores so there is a clear top candidate.
            db.execute("UPDATE discovered_source SET score = ? WHERE id = ?",
                      (source.view_count / 100.0, source.id))
        picks = set()
        for _ in range(20):
            picked, _tier = discovery.pick_next_source(
                db, _config(discovery={"exploration_rate": 0.0}), now=NOW,
                rng=random.Random(1))
            picks.add(picked.youtube_video_id)
        assert picks == {"vid00000009"}  # the highest view_count, always picked

    def test_a_high_exploration_rate_visits_more_than_one_candidate(self, db):
        records = [make_record(f"vid{i:08d}", view_count=1000 * (i + 1), now=NOW)
                  for i in range(10)]
        client = FakeYouTubeClient(records)
        run_async(discovery.run_discovery(db, _config(), client, run_id="r1", now=NOW))
        for source in db.list_sources(states=[SourceState.ELIGIBLE], limit=20):
            db.execute("UPDATE discovered_source SET score = ? WHERE id = ?",
                      (source.view_count / 100.0, source.id))
        picks = set()
        for seed in range(50):
            picked, _tier = discovery.pick_next_source(
                db, _config(discovery={"exploration_rate": 0.9}), now=NOW,
                rng=random.Random(seed))
            picks.add(picked.youtube_video_id)
        assert len(picks) > 1

    def test_exploration_never_returns_a_rights_blocked_candidate(self, db):
        blocked = make_record("blocked", license="youtube", view_count=999_999_999, now=NOW)
        allowed = make_record("allowed", view_count=100, now=NOW)
        client = FakeYouTubeClient([blocked, allowed])
        run_async(discovery.run_discovery(db, _config(), client, run_id="r1", now=NOW))
        for _ in range(20):
            picked, _tier = discovery.pick_next_source(
                db, _config(discovery={"exploration_rate": 1.0}), now=NOW,
                rng=random.Random())
            assert picked is None or picked.youtube_video_id == "allowed"


class TestLaneDiversityWithinARun:
    def test_multiple_enabled_lanes_are_both_queried(self, db):
        # FakeYouTubeClient returns the same scripted record set from every
        # endpoint, so a real overlap check on stored `discovery_lane` would
        # only be testing the fake's simplicity (whichever lane runs first
        # claims every video id via in-memory dedup) — what this test can
        # actually pin is that discovery *asks* more than one lane, which is
        # the diversification mechanism (see automation.discovery.lanes_for_run).
        records = [make_record(f"vid{i:08d}", view_count=1000 * (i + 1), now=NOW)
                  for i in range(6)]
        client = FakeYouTubeClient(records)
        config = _config(discovery={
            "lanes": ["TRENDING_NOW", "NICHE_MOMENTUM"], "topics": ["cooking"],
            "lanes_per_run": 2})
        run_async(discovery.run_discovery(db, config, client, run_id="r1", now=NOW))
        assert client.popular_calls >= 1
        assert client.search_calls >= 1

    def test_the_first_lane_to_claim_a_video_id_tags_its_source(self, db):
        # Whichever lane actually stores a candidate first is the lane
        # recorded — proves the tagging mechanism itself works end to end.
        records = [make_record("vid00000001", view_count=1000, now=NOW)]
        client = FakeYouTubeClient(records)
        config = _config(discovery={"lanes": ["TRENDING_NOW"]})
        run_async(discovery.run_discovery(db, config, client, run_id="r1", now=NOW))
        source = db.get_source_by_video_id("vid00000001")
        assert source.discovery_lane == "TRENDING_NOW"
