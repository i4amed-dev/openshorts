"""Opportunity scoring: age cohorts, smoothing, and the "why did it pick
that one" behaviour that formerly lived in test_autopilot_ranking.py.

These tests exist because the previous ranking formula had a specific,
diagnosed failure mode: it fed ``total_views / age_hours`` into eligibility
*and* ranking identically for a video three hours old and one three years
old, which is a lifetime average for the old video, not a pulse — and it let
that number, plus a handful of independent hard gates, make almost every
candidate invisible. Every test class here pins one piece of the fix.
"""
import math
from datetime import datetime, timedelta, timezone

from automation.opportunity import (
    ARCHIVE, ESTABLISHED, EVERGREEN, FRESH, RECENT, RISING, ULTRA_FRESH,
    age_cohort, bayesian_engagement_rate, channel_outperformance, evergreen_strength,
    score_candidates,
)
from autopilot_fakes import base_config, make_record

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _score_map(results):
    return {record.video_id: score for record, score, _ in results}


def _breakdown_map(results):
    return {record.video_id: breakdown for record, _score, breakdown in results}


class TestAgeCohorts:
    def test_bands_match_the_spec(self):
        assert age_cohort(0) == ULTRA_FRESH
        assert age_cohort(5.9) == ULTRA_FRESH
        assert age_cohort(6.1) == FRESH
        assert age_cohort(23.9) == FRESH
        assert age_cohort(24.1) == RISING
        assert age_cohort(24 * 6.9) == RISING
        assert age_cohort(24 * 8) == RECENT
        assert age_cohort(24 * 29) == RECENT
        assert age_cohort(24 * 31) == ESTABLISHED
        assert age_cohort(24 * 364) == ESTABLISHED
        assert age_cohort(24 * 366) == EVERGREEN
        assert age_cohort(24 * 365 * 4.9) == EVERGREEN
        assert age_cohort(24 * 365 * 5.1) == ARCHIVE

    def test_an_unknown_publish_date_is_archive_not_a_crash(self):
        assert age_cohort(float("inf")) == ARCHIVE

    def test_age_cohort_is_persisted_on_the_breakdown(self):
        config = base_config()
        record = make_record("vid00000001", published_at=NOW - timedelta(days=1000), now=NOW)
        _r, _s, breakdown = score_candidates([record], config, now=NOW)[0]
        assert breakdown["age_cohort"] == EVERGREEN


class TestEngagementSmoothing:
    def test_tiny_sample_does_not_dominate_a_proven_million_view_video(self):
        # Spec's exact scenario: 10 views / 5 likes must not outrank
        # 1,000,000 views / 70,000 likes.
        tiny = bayesian_engagement_rate(likes=5, comments=0, views=10)
        proven = bayesian_engagement_rate(likes=70_000, comments=0, views=1_000_000)
        assert proven > tiny

    def test_smoothing_pulls_a_tiny_sample_toward_the_prior(self):
        from automation.opportunity import PRIOR_ENGAGEMENT_RATE
        raw_rate = 300 / 1000  # a 4-hour-old video with 1K views, 300 likes
        smoothed = bayesian_engagement_rate(likes=300, comments=0, views=1000)
        assert smoothed < raw_rate
        assert smoothed > PRIOR_ENGAGEMENT_RATE  # still pulled up somewhat, not erased

    def test_a_large_sample_is_barely_moved_by_the_prior(self):
        raw_rate = 70_000 / 1_000_000
        smoothed = bayesian_engagement_rate(likes=70_000, comments=0, views=1_000_000)
        assert abs(smoothed - raw_rate) < 0.01


class TestEvergreenStrength:
    def test_a_fresh_video_gets_no_age_credit_yet(self):
        score = evergreen_strength(
            make_record("v", title="How the economy actually works", now=NOW),
            age_hours=2.0, engagement_quality=0.5)
        aged = evergreen_strength(
            make_record("v", title="How the economy actually works", now=NOW),
            age_hours=24 * 400, engagement_quality=0.5)
        assert aged > score

    def test_timeless_topic_keywords_lift_the_score(self):
        timeless = make_record("v", title="The psychology explained: why we procrastinate",
                               now=NOW)
        generic = make_record("v", title="My Tuesday vlog", now=NOW)
        assert (evergreen_strength(timeless, age_hours=24 * 60, engagement_quality=0.3)
                > evergreen_strength(generic, age_hours=24 * 60, engagement_quality=0.3))


class TestChannelOutperformance:
    def test_missing_baseline_is_neutral_not_penalised(self):
        assert channel_outperformance(500_000, None) == 0.5

    def test_far_above_baseline_scores_high(self):
        assert channel_outperformance(900_000, 40_000) > channel_outperformance(2_000_000,
                                                                                3_000_000)

    def test_at_baseline_is_modest(self):
        at_baseline = channel_outperformance(40_000, 40_000)
        far_above = channel_outperformance(2_000_000, 40_000)
        assert far_above > at_baseline


class TestSyntheticCandidates:
    """The exact five-candidate scenario used to validate the redesign.

    A: 2h old, 80K views, 9K likes, 1K comments — a breakout candidate.
    B: 3y old, 18M views, 1.2M likes, 90K comments — an evergreen winner.
    C: 2d old, 400K views, 5K likes, 100 comments — reach without engagement.
    D: 5y old, 5K views, 20 likes — genuinely weak, no proven demand.
    E: 4h old, 1K views, 300 likes, 80 comments — tiny sample, should not
       game the system on its raw ratio.
    """

    def _candidates(self):
        a = make_record("candidateA", title="Why this AI mistake keeps happening",
                        published_at=NOW - timedelta(hours=2), view_count=80_000,
                        like_count=9_000, comment_count=1_000, now=NOW)
        b = make_record("candidateB", title="The psychology of habit explained: full story",
                        published_at=NOW - timedelta(days=365 * 3), view_count=18_000_000,
                        like_count=1_200_000, comment_count=90_000, now=NOW)
        c = make_record("candidateC", title="Big news update today",
                        published_at=NOW - timedelta(days=2), view_count=400_000,
                        like_count=5_000, comment_count=100, now=NOW)
        d = make_record("candidateD", title="Random Tuesday clip",
                        published_at=NOW - timedelta(days=365 * 5 + 10), view_count=5_000,
                        like_count=20, comment_count=0, now=NOW)
        e = make_record("candidateE", title="Wow amazing", published_at=NOW - timedelta(hours=4),
                        view_count=1_000, like_count=300, comment_count=80, now=NOW)
        return a, b, c, d, e

    def test_evergreen_winner_and_breakout_both_beat_the_weak_archive_video(self):
        a, b, c, d, e = self._candidates()
        config = base_config()
        scores = _score_map(score_candidates([a, b, c, d, e], config, now=NOW))
        assert scores["candidateB"] > scores["candidateD"]
        assert scores["candidateA"] > scores["candidateD"]

    def test_tiny_sample_candidate_does_not_top_the_ranking(self):
        a, b, c, d, e = self._candidates()
        config = base_config()
        scores = _score_map(score_candidates([a, b, c, d, e], config, now=NOW))
        assert scores["candidateE"] < scores["candidateA"]
        assert scores["candidateE"] < scores["candidateB"]

    def test_reach_without_engagement_does_not_automatically_win(self):
        a, b, c, d, e = self._candidates()
        config = base_config()
        scores = _score_map(score_candidates([a, b, c, d, e], config, now=NOW))
        assert scores["candidateB"] > scores["candidateC"]


class TestAgeFairness:
    def test_a_strong_evergreen_video_can_outrank_a_weak_fresh_upload(self):
        strong_evergreen = make_record(
            "old", title="The true story explained: full documentary interview",
            published_at=NOW - timedelta(days=365 * 3), view_count=20_000_000,
            like_count=1_500_000, comment_count=120_000, now=NOW)
        weak_fresh = make_record(
            "new", published_at=NOW - timedelta(hours=3), view_count=500,
            like_count=2, comment_count=0, now=NOW)
        config = base_config()
        scores = _score_map(score_candidates([strong_evergreen, weak_fresh], config, now=NOW))
        assert scores["old"] > scores["new"]

    def test_a_genuine_breakout_can_outrank_a_mediocre_evergreen_video(self):
        exploding_fresh = make_record(
            "new", published_at=NOW - timedelta(hours=3), view_count=250_000,
            like_count=30_000, comment_count=4_000, now=NOW)
        mediocre_evergreen = make_record(
            "old", published_at=NOW - timedelta(days=365 * 2), view_count=40_000,
            like_count=600, comment_count=20, now=NOW)
        config = base_config()
        scores = _score_map(score_candidates([exploding_fresh, mediocre_evergreen],
                                              config, now=NOW))
        assert scores["new"] > scores["old"]

    def test_lifetime_average_velocity_is_not_computed_for_old_cohorts(self):
        # An 18M-view, 3-year-old video must not be penalised for a "low"
        # views/age_hours figure — that number is a lifetime average, not a
        # momentum reading, and this module must not treat it as one.
        old = make_record("old", published_at=NOW - timedelta(days=365 * 3),
                          view_count=18_000_000, like_count=1_200_000,
                          comment_count=90_000, now=NOW)
        config = base_config()
        _r, _s, breakdown = score_candidates([old], config, now=NOW)[0]
        assert breakdown["signals"]["early_lifetime_velocity"] is None
        # trend_momentum must not be near-zero just because lifetime-average
        # velocity is unremarkable for a video this old.
        assert breakdown["components"]["trend_momentum"] >= 0.4


class TestChartRank:
    def test_chart_position_still_counts_via_trend_momentum(self):
        config = base_config()
        top = make_record("vid00000001", chart_rank=1, now=NOW)
        bottom = make_record("vid00000002", chart_rank=50, now=NOW)
        scores = _score_map(score_candidates([top, bottom], config, now=NOW))
        assert scores["vid00000001"] > scores["vid00000002"]


class TestWeightsAreConfigurable:
    def test_proven_demand_only_favours_the_bigger_proven_video(self):
        record_a = make_record("vid00000001", view_count=10_000_000,
                               like_count=1, comment_count=1, now=NOW)
        record_b = make_record("vid00000002", view_count=1_000,
                               like_count=500, comment_count=400, now=NOW)
        proven_only = base_config(ranking={"weights": {
            "trend_momentum": 0, "engagement_quality": 0, "proven_demand": 1,
            "channel_outperformance": 0, "content_relevance": 0, "shorts_suitability": 0,
            "evergreen_strength": 0, "conversion_proxy": 0}})
        scores = _score_map(score_candidates([record_a, record_b], proven_only, now=NOW))
        assert scores["vid00000001"] > scores["vid00000002"]

    def test_engagement_only_favours_the_more_engaging_video(self):
        record_a = make_record("vid00000001", view_count=10_000_000,
                               like_count=1, comment_count=1, now=NOW)
        record_b = make_record("vid00000002", view_count=1_000,
                               like_count=500, comment_count=400, now=NOW)
        engagement_only = base_config(ranking={"weights": {
            "trend_momentum": 0, "engagement_quality": 1, "proven_demand": 0,
            "channel_outperformance": 0, "content_relevance": 0, "shorts_suitability": 0,
            "evergreen_strength": 0, "conversion_proxy": 0}})
        scores = _score_map(score_candidates([record_a, record_b], engagement_only, now=NOW))
        assert scores["vid00000002"] > scores["vid00000001"]


class TestConversionProxyIsLabelledAsAProxy:
    def test_conversion_proxy_is_a_bounded_score_not_a_subscriber_count(self):
        config = base_config()
        record = make_record("vid00000001", now=NOW)
        _r, _s, breakdown = score_candidates([record], config, now=NOW)[0]
        assert 0.0 <= breakdown["components"]["conversion_proxy"] <= 1.0
