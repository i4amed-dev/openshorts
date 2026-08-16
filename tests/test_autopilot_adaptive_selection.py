"""Adaptive selection: STRICT → NORMAL → EXPLORATION, never an empty pool.

The failure this module exists to prevent: a discovery cycle finds real,
technically-valid, rights-clear candidates, but a single static threshold
rejects all of them and Autopilot ends the cycle with nothing selected. The
fix is three passes over the same ELIGIBLE queue with a relaxing opportunity
floor — never a relaxing rights or technical-validity check.
"""
from datetime import datetime, timedelta, timezone

import pytest

from automation import discovery
from automation.config import POLICY_CREATIVE_COMMONS, normalise
from automation.db import AutopilotDB
from automation.models import Reason, SourceState
from autopilot_fakes import FakeYouTubeClient, make_record, run_async

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    database = AutopilotDB(":memory:").connect()
    yield database
    database.close()


def _config(**overrides):
    base = {
        "rights": {"policy": POLICY_CREATIVE_COMMONS},
        "discovery": {"lanes": ["TRENDING_NOW"], "region_code": "US"},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value
    return normalise(base)


def _discover(db, config, records):
    client = FakeYouTubeClient(records)
    return run_async(discovery.run_discovery(db, config, client, run_id="run-1", now=NOW))


class TestZeroCandidateReproduction:
    """Spec's exact reproduction: a realistic mixed pool must not empty out."""

    def test_a_realistic_mixed_pool_yields_eligible_candidates(self, db):
        records = []
        for i in range(20):
            age_days = [0.1, 2, 10, 40, 400, 1200][i % 6]
            records.append(make_record(
                f"vid{i:08d}", published_at=NOW - timedelta(days=age_days),
                view_count=1000 * (i + 1), like_count=20 * (i + 1),
                comment_count=5 * (i + 1), now=NOW))
        result = _discover(db, _config(), records)
        assert result["eligible"] > 0

    def test_the_performance_system_does_not_hard_reject_a_whole_technically_valid_batch(self, db):
        # Every one of these is technically valid and CC-licensed (the fake's
        # default), so all 20 should end up ELIGIBLE — the old hard gates
        # (age/views/velocity/engagement/definition) are the exact thing that
        # used to zero this out.
        records = [make_record(f"vid{i:08d}", view_count=100 * (i + 1),
                               published_at=NOW - timedelta(days=(i + 1) * 20), now=NOW)
                  for i in range(20)]
        result = _discover(db, _config(), records)
        assert result["eligible"] == 20


class TestAdaptiveTiers:
    def test_strict_tier_picks_a_high_scoring_source(self, db):
        strong = make_record("strong", view_count=5_000_000, like_count=400_000,
                             comment_count=30_000, now=NOW)
        _discover(db, _config(), [strong])
        source = db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        db.execute("UPDATE discovered_source SET score = 90 WHERE id = ?", (source.id,))
        picked, tier = discovery.pick_next_source(
            db, _config(discovery={"exploration_rate": 0.0}), now=NOW)
        assert picked is not None
        assert tier == discovery.TIER_STRICT

    def test_normal_tier_fires_when_nothing_clears_strict(self, db):
        weak = make_record("weak", view_count=500, like_count=2, comment_count=0, now=NOW)
        _discover(db, _config(), [weak])
        source = db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        db.execute("UPDATE discovered_source SET score = 50 WHERE id = ?", (source.id,))
        picked, tier = discovery.pick_next_source(
            db, _config(discovery={"exploration_rate": 0.0}), now=NOW)
        assert picked is not None
        assert tier == discovery.TIER_NORMAL

    def test_exploration_tier_fires_as_a_last_resort_above_the_true_floor(self, db):
        weak = make_record("weak", view_count=10, like_count=0, comment_count=0, now=NOW)
        _discover(db, _config(), [weak])
        source = db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        db.execute("UPDATE discovered_source SET score = 25 WHERE id = ?", (source.id,))
        picked, tier = discovery.pick_next_source(
            db, _config(discovery={"exploration_rate": 0.0}), now=NOW)
        assert picked is not None
        assert tier == discovery.TIER_EXPLORATION

    def test_below_the_true_floor_nothing_is_selected(self, db):
        weak = make_record("weak", view_count=10, like_count=0, comment_count=0, now=NOW)
        _discover(db, _config(), [weak])
        source = db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        db.execute("UPDATE discovered_source SET score = 5 WHERE id = ?", (source.id,))
        picked, reason = discovery.pick_next_source(
            db, _config(discovery={"exploration_rate": 0.0}), now=NOW)
        assert picked is None
        assert reason == Reason.LOW_OPPORTUNITY

    def test_the_highest_scoring_live_candidate_wins_within_a_tier(self, db):
        low = make_record("low", view_count=1000, now=NOW)
        high = make_record("high", view_count=1000, now=NOW)
        _discover(db, _config(), [low, high])
        rows = {s.youtube_video_id: s for s in db.list_sources(states=[SourceState.ELIGIBLE],
                                                                limit=10)}
        db.execute("UPDATE discovered_source SET score = 80 WHERE id = ?", (rows["low"].id,))
        db.execute("UPDATE discovered_source SET score = 95 WHERE id = ?", (rows["high"].id,))
        picked, tier = discovery.pick_next_source(
            db, _config(discovery={"exploration_rate": 0.0}), now=NOW)
        assert picked.youtube_video_id == "high"
        assert tier == discovery.TIER_STRICT


class TestRightsAndTechnicalValidityNeverRelax:
    def test_a_rights_blocked_candidate_is_never_selected_at_any_tier(self, db):
        blocked = make_record("blocked", license="youtube", view_count=50_000_000,
                              like_count=4_000_000, comment_count=200_000, now=NOW)
        _discover(db, _config(), [blocked])
        # Confirm it never reached ELIGIBLE regardless of how good its numbers are.
        source = db.get_source_by_video_id("blocked")
        assert source.state == SourceState.FILTERED
        assert source.rejection_reason == Reason.RIGHTS_POLICY
        assert source.policy_eligible is False
        picked, _ = discovery.pick_next_source(db, _config(), now=NOW)
        assert picked is None

    def test_a_technically_invalid_candidate_is_never_selected_at_any_tier(self, db):
        live = make_record("live_now", live_state="live", view_count=10_000_000, now=NOW)
        _discover(db, _config(), [live])
        source = db.get_source_by_video_id("live_now")
        assert source.state == SourceState.FILTERED
        assert source.technical_eligible is False
        picked, _ = discovery.pick_next_source(db, _config(), now=NOW)
        assert picked is None

    def test_opportunity_score_is_preserved_even_when_rights_blocked(self, db):
        # Spec's exact requirement: Opportunity 96 + rights blocked must not
        # collapse into a 0 — the two are independent facts.
        blocked = make_record("blocked", license="youtube", view_count=50_000_000,
                              like_count=4_000_000, comment_count=200_000, now=NOW)
        _discover(db, _config(), [blocked])
        source = db.get_source_by_video_id("blocked")
        assert source.score > 0
        assert source.policy_eligible is False


class TestWhyNothingWasSelected:
    def test_a_rights_bottleneck_is_named_explicitly(self, db):
        records = [make_record(f"vid{i:08d}", license="youtube", now=NOW) for i in range(10)]
        _discover(db, _config(), records)
        diagnostic = discovery.explain_empty_selection(db, _config())
        assert diagnostic["bottleneck"] == "rights_policy"

    def test_a_low_opportunity_bottleneck_is_named_explicitly(self, db):
        records = [make_record(f"vid{i:08d}", view_count=10, like_count=0, comment_count=0,
                               now=NOW) for i in range(5)]
        _discover(db, _config(), records)
        for source in db.list_sources(states=[SourceState.ELIGIBLE], limit=10):
            db.execute("UPDATE discovered_source SET score = 3 WHERE id = ?", (source.id,))
        diagnostic = discovery.explain_empty_selection(db, _config())
        assert diagnostic["bottleneck"] == "low_opportunity"

    def test_no_candidates_at_all_is_named_explicitly(self, db):
        diagnostic = discovery.explain_empty_selection(db, _config())
        assert diagnostic["bottleneck"] == "no_candidates"
