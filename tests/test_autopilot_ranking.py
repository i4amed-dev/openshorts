"""Ranking: the "why did it choose that one" tests.

The whole reason this module is deterministic code and not an LLM call is that
its output has to be reproducible and explainable. These tests pin the two
properties that make it so: identical input gives identical order, and no single
raw count can dominate the score.
"""
from datetime import datetime, timedelta, timezone

from automation.ranking import duration_fit, relevance_score, score_candidates
from autopilot_fakes import base_config, make_record

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _score_map(results):
    return {record.video_id: score for record, score, _ in results}


class TestNormalisation:
    def test_a_single_giant_view_count_does_not_win_on_its_own(self):
        """The failure mode this module exists to prevent.

        An old video with 50M lifetime views and no current momentum must lose
        to a fresh one climbing fast. Un-normalised summation gets this wrong
        every time, because 50,000,000 dwarfs every other term.
        """
        config = base_config()
        stale_giant = make_record(
            "vid00000001", view_count=50_000_000, like_count=100_000,
            comment_count=5_000, published_at=NOW - timedelta(days=150), now=NOW)
        fresh_climber = make_record(
            "vid00000002", view_count=400_000, like_count=40_000,
            comment_count=6_000, published_at=NOW - timedelta(hours=8), now=NOW)

        scores = _score_map(score_candidates([stale_giant, fresh_climber], config, now=NOW))
        assert scores["vid00000002"] > scores["vid00000001"]

    def test_every_component_lands_between_zero_and_one(self):
        config = base_config()
        records = [make_record(f"vid0000000{i}", view_count=10 ** (i + 2), now=NOW)
                   for i in range(1, 6)]
        for _record, _score, breakdown in score_candidates(records, config, now=NOW):
            for name, value in breakdown["components"].items():
                assert 0.0 <= value <= 1.0, f"{name} escaped 0..1: {value}"

    def test_a_lone_candidate_scores_mid_range_not_perfect(self):
        # With one candidate there is no "best in set"; pinning it to 100 would
        # make a weak sole option look like a great pick.
        config = base_config()
        results = score_candidates([make_record("vid00000001", now=NOW)], config, now=NOW)
        _record, score, _ = results[0]
        assert 0 < score < 100

    def test_score_is_reported_on_a_zero_to_hundred_scale(self):
        config = base_config()
        records = [make_record(f"vid0000000{i}", view_count=1000 * i, now=NOW)
                   for i in range(1, 5)]
        for _r, score, _b in score_candidates(records, config, now=NOW):
            assert 0.0 <= score <= 100.0


class TestDeterminism:
    def test_the_same_input_gives_the_same_order(self):
        config = base_config()
        records = [make_record(f"vid0000000{i}", view_count=1000 * i, now=NOW)
                   for i in range(1, 8)]
        first = [r.video_id for r, _, _ in score_candidates(records, config, now=NOW)]
        second = [r.video_id for r, _, _ in score_candidates(list(reversed(records)),
                                                             config, now=NOW)]
        assert first == second

    def test_ties_break_on_video_id_not_input_order(self):
        config = base_config()
        twins = [make_record("vid00000002", now=NOW), make_record("vid00000001", now=NOW)]
        order = [r.video_id for r, _, _ in score_candidates(twins, config, now=NOW)]
        assert order == ["vid00000001", "vid00000002"]


class TestSignals:
    def test_engagement_lifts_an_otherwise_equal_video(self):
        config = base_config()
        dull = make_record("vid00000001", view_count=100_000, like_count=100,
                           comment_count=5, now=NOW)
        lively = make_record("vid00000002", view_count=100_000, like_count=20_000,
                             comment_count=3_000, now=NOW)
        scores = _score_map(score_candidates([dull, lively], config, now=NOW))
        assert scores["vid00000002"] > scores["vid00000001"]

    def test_chart_position_counts_when_everything_else_matches(self):
        config = base_config()
        top = make_record("vid00000001", chart_rank=1, now=NOW)
        bottom = make_record("vid00000002", chart_rank=50, now=NOW)
        scores = _score_map(score_candidates([top, bottom], config, now=NOW))
        assert scores["vid00000001"] > scores["vid00000002"]

    def test_channel_repetition_is_penalised(self):
        config = base_config()
        a = make_record("vid00000001", channel_id="UCoveruser", now=NOW)
        b = make_record("vid00000002", channel_id="UCfresh", now=NOW)
        results = score_candidates([a, b], config, now=NOW,
                                   channel_use_counts={"UCoveruser": 3})
        scores = _score_map(results)
        assert scores["vid00000002"] > scores["vid00000001"]

    def test_the_penalty_is_capped_so_a_channel_is_never_permanently_banned(self):
        config = base_config()
        record = make_record("vid00000001", channel_id="UCa", now=NOW)
        heavy = score_candidates([record], config, now=NOW,
                                 channel_use_counts={"UCa": 99})[0][1]
        capped = score_candidates([record], config, now=NOW,
                                  channel_use_counts={"UCa": 3})[0][1]
        assert heavy == capped

    def test_weights_are_configurable(self):
        record_a = make_record("vid00000001", view_count=10_000_000,
                               like_count=1, comment_count=1, now=NOW)
        record_b = make_record("vid00000002", view_count=1_000,
                               like_count=500, comment_count=400, now=NOW)
        views_only = base_config(ranking={"weights": {
            "velocity": 0, "views": 1, "engagement": 0, "comments": 0,
            "recency": 0, "chart_rank": 0, "relevance": 0, "duration_fit": 0}})
        engagement_only = base_config(ranking={"weights": {
            "velocity": 0, "views": 0, "engagement": 1, "comments": 0,
            "recency": 0, "chart_rank": 0, "relevance": 0, "duration_fit": 0}})
        assert _score_map(score_candidates([record_a, record_b], views_only,
                                           now=NOW))["vid00000001"] > 50
        assert _score_map(score_candidates([record_a, record_b], engagement_only,
                                           now=NOW))["vid00000002"] > 50


class TestRelevance:
    def test_a_phrase_topic_needs_the_phrase(self):
        record = make_record("vid00000001", title="Formula 1 season review",
                             description="racing")
        assert relevance_score(record, ["f1 highlights"]) == 0.0

    def test_a_word_topic_matches_on_whole_words(self):
        record = make_record("vid00000001", title="Cooking with fire",
                             description="a recipe")
        assert relevance_score(record, ["cooking"]) == 1.0
        # "cook" must not fire on "cooking" — that is how unrelated niches leak in.
        assert relevance_score(record, ["cook"]) == 0.0

    def test_partial_topic_coverage_is_proportional(self):
        record = make_record("vid00000001", title="Chess and poker night",
                             description="")
        assert relevance_score(record, ["chess", "poker", "bridge"]) == 2 / 3

    def test_no_topics_configured_means_no_relevance_signal(self):
        record = make_record("vid00000001")
        assert relevance_score(record, []) == 0.0


class TestDurationFit:
    def test_the_ideal_length_scores_full_marks(self):
        clips = {"max_clips_per_source": 3, "max_clip_seconds": 60}
        # 3 clips x 60s x 2..8 → 360s..1440s is the comfortable band.
        assert duration_fit(make_record("v", duration_seconds=900), clips) == 1.0

    def test_a_source_barely_longer_than_the_clips_scores_low(self):
        clips = {"max_clips_per_source": 3, "max_clip_seconds": 60}
        assert duration_fit(make_record("v", duration_seconds=200), clips) < 0.6

    def test_a_very_long_source_tapers_off(self):
        clips = {"max_clips_per_source": 3, "max_clip_seconds": 60}
        assert duration_fit(make_record("v", duration_seconds=6 * 3600), clips) < 0.1

    def test_zero_duration_is_no_fit(self):
        assert duration_fit(make_record("v", duration_seconds=0), {}) == 0.0


def test_breakdown_explains_the_score():
    """The dashboard renders this; it must contain the evidence, not just a number."""
    config = base_config()
    record = make_record("vid00000001", now=NOW)
    _r, score, breakdown = score_candidates([record], config, now=NOW)[0]
    assert breakdown["score"] == score
    assert set(breakdown) >= {"components", "weights", "contributions", "penalties",
                              "signals"}
    assert breakdown["signals"]["views"] == record.view_count
    assert breakdown["signals"]["views_per_hour"] > 0


def test_an_empty_candidate_set_is_not_an_error():
    assert score_candidates([], base_config(), now=NOW) == []
