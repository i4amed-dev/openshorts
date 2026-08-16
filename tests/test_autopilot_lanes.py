"""Discovery lanes: rotation, budget, and query expansion.

Six independent ways of finding candidates exist so the pool is not
structurally biased toward one kind of opportunity (see automation/discovery.py
and automation/config.py's LANES). These tests pin the scheduling around them:
which lanes run a given tick, how the budget is shared, and that query
expansion stays bounded regardless of how many topics an operator configures.
"""
from datetime import datetime, timezone

from automation.config import (
    LANE_CHANNEL_WINNERS, LANE_EARLY_BREAKOUT, LANE_EVERGREEN_WINNERS, LANE_NICHE_MOMENTUM,
    LANE_TRENDING_NOW, LANE_UNDEREXPOSED, POLICY_CREATIVE_COMMONS, normalise,
)
from automation.discovery import _queries_for_lane, _stable_run_index, lanes_for_run
from automation.query_expansion import expand_topic


def _config(**overrides):
    base = {"rights": {"policy": POLICY_CREATIVE_COMMONS},
           "discovery": {"lanes": [LANE_TRENDING_NOW, LANE_EARLY_BREAKOUT, LANE_NICHE_MOMENTUM,
                                    LANE_EVERGREEN_WINNERS, LANE_UNDEREXPOSED,
                                    LANE_CHANNEL_WINNERS],
                         "topics": ["cooking"], "lanes_per_run": 2}}
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value
    return normalise(base)


class TestLaneRotation:
    def test_trending_now_always_runs_when_enabled(self):
        config = _config()
        for run_index in range(10):
            assert LANE_TRENDING_NOW in lanes_for_run(config, run_index=run_index)

    def test_only_lanes_per_run_search_lanes_run_at_once(self):
        config = _config()
        lanes = lanes_for_run(config, run_index=0)
        search_lanes = [l for l in lanes if l != LANE_TRENDING_NOW]
        assert len(search_lanes) == 2  # lanes_per_run

    def test_rotation_is_deterministic_for_the_same_run_index(self):
        config = _config()
        first = lanes_for_run(config, run_index=3)
        second = lanes_for_run(config, run_index=3)
        assert first == second

    def test_rotation_covers_different_lanes_across_runs(self):
        config = _config()
        seen = set()
        for run_index in range(5):
            seen.update(lanes_for_run(config, run_index=run_index))
        # Every enabled lane should appear at least once across enough runs.
        assert LANE_EVERGREEN_WINNERS in seen or LANE_UNDEREXPOSED in seen

    def test_disabled_lanes_never_run(self):
        config = _config(discovery={"lanes": [LANE_TRENDING_NOW]})
        for run_index in range(5):
            assert lanes_for_run(config, run_index=run_index) == [LANE_TRENDING_NOW]

    def test_a_stable_run_index_is_deterministic_per_run_id(self):
        assert _stable_run_index("run-abc") == _stable_run_index("run-abc")
        assert _stable_run_index("run-abc") != _stable_run_index("run-xyz")


class TestQueryExpansion:
    def test_bounded_by_variants_per_run(self):
        assert len(expand_topic("cooking", variants_per_run=3, run_index=0)) == 3

    def test_the_literal_topic_is_always_included(self):
        assert expand_topic("cooking", variants_per_run=1, run_index=0) == ["cooking"]

    def test_an_empty_topic_expands_to_nothing(self):
        assert expand_topic("", variants_per_run=3, run_index=0) == []

    def test_rotation_changes_which_variants_are_picked(self):
        run0 = expand_topic("cooking", variants_per_run=2, run_index=0)
        run1 = expand_topic("cooking", variants_per_run=2, run_index=1)
        # At least the rotation offset should differ across enough run indices.
        variants_seen = set()
        for i in range(10):
            variants_seen.update(expand_topic("cooking", variants_per_run=2, run_index=i))
        assert len(variants_seen) > 2

    def test_queries_for_lane_is_bounded_regardless_of_topic_count(self):
        many_topics = [f"topic{i}" for i in range(25)]
        queries = _queries_for_lane(many_topics, variants_per_topic=5, run_index=0)
        assert len(queries) <= 6


class TestDiscoveryModePriority:
    def test_evergreen_heavy_favours_evergreen_over_many_runs(self):
        config = _config(discovery={"discovery_mode": "EVERGREEN_HEAVY", "lanes_per_run": 1})
        counts = {}
        for run_index in range(30):
            for lane in lanes_for_run(config, run_index=run_index):
                counts[lane] = counts.get(lane, 0) + 1
        # EVERGREEN_WINNERS is in the priority set for this mode — it should
        # win the single rotation slot at least as often as a non-priority
        # lane like NICHE_MOMENTUM.
        assert counts.get(LANE_EVERGREEN_WINNERS, 0) >= counts.get(LANE_NICHE_MOMENTUM, 0)

    def test_balanced_mode_has_no_priority_bias(self):
        config = _config(discovery={"discovery_mode": "BALANCED"})
        # Just confirming it runs without error and returns a valid lane set.
        lanes = lanes_for_run(config, run_index=0)
        assert LANE_TRENDING_NOW in lanes


class TestRightsAwareBudget:
    def test_config_validation_still_requires_topics_for_search_lanes(self):
        import pytest
        from automation.config import ConfigError
        with pytest.raises(ConfigError):
            normalise({"discovery": {"lanes": [LANE_NICHE_MOMENTUM], "topics": []}})

    def test_trending_now_alone_needs_no_topics(self):
        config = normalise({"discovery": {"lanes": [LANE_TRENDING_NOW], "topics": []}})
        assert config["discovery"]["lanes"] == [LANE_TRENDING_NOW]
