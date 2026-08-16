"""Channel-relative performance: candidate views vs. this channel's own baseline.

The scenario this module exists for (spec-equivalent to "channel A: 10M
subs, typical video 5M views, candidate 5.5M views" vs. "channel B: 80K
subs, typical video 30K views, candidate 900K views" — B should win on
outperformance even though A's raw numbers are much bigger).
"""
from datetime import datetime, timedelta, timezone

import pytest

from automation.channel_context import get_channel_baselines
from automation.db import AutopilotDB
from automation.opportunity import channel_outperformance
from automation.youtube_client import ChannelRecord
from autopilot_fakes import FakeYouTubeClient, run_async

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    database = AutopilotDB(":memory:").connect()
    yield database
    database.close()


class TestBaselineFetchAndCache:
    def test_baselines_are_computed_from_channel_totals(self, db):
        client = FakeYouTubeClient(channel_records=[
            ChannelRecord(channel_id="UCbig", view_count=50_000_000, video_count=10),
            ChannelRecord(channel_id="UCsmall", view_count=2_400_000, video_count=80),
        ])
        baselines = run_async(get_channel_baselines(db, client, ["UCbig", "UCsmall"], now=NOW))
        assert baselines["UCbig"] == pytest.approx(5_000_000.0)
        assert baselines["UCsmall"] == pytest.approx(30_000.0)

    def test_a_second_call_within_the_ttl_does_not_refetch(self, db):
        client = FakeYouTubeClient(channel_records=[
            ChannelRecord(channel_id="UCbig", view_count=50_000_000, video_count=10),
        ])
        run_async(get_channel_baselines(db, client, ["UCbig"], now=NOW))
        run_async(get_channel_baselines(db, client, ["UCbig"], now=NOW + timedelta(hours=1)))
        assert client.channels_calls == 1

    def test_a_stale_cache_entry_is_refreshed(self, db):
        client = FakeYouTubeClient(channel_records=[
            ChannelRecord(channel_id="UCbig", view_count=50_000_000, video_count=10),
        ])
        run_async(get_channel_baselines(db, client, ["UCbig"], now=NOW))
        run_async(get_channel_baselines(db, client, ["UCbig"], now=NOW + timedelta(days=30)))
        assert client.channels_calls == 2

    def test_a_channel_with_no_data_is_simply_absent(self, db):
        client = FakeYouTubeClient(channel_records=[])
        baselines = run_async(get_channel_baselines(db, client, ["UCunknown"], now=NOW))
        assert "UCunknown" not in baselines

    def test_an_unconfigured_client_does_not_crash_discovery(self, db):
        client = FakeYouTubeClient(channel_records=[])
        client.configured = False
        baselines = run_async(get_channel_baselines(db, client, ["UCabc"], now=NOW))
        assert baselines == {}


class TestOutperformanceRewardsTheSmallChannelBreakout:
    def test_a_channel_beating_its_own_average_by_a_lot_scores_higher(self):
        # Channel A: 10M subs, typical 5M views, candidate 5.5M views (modest lift).
        # Channel B: 80K subs, typical 30K views, candidate 900K views (30x lift).
        channel_a_outperformance = channel_outperformance(5_500_000, 5_000_000)
        channel_b_outperformance = channel_outperformance(900_000, 30_000)
        assert channel_b_outperformance > channel_a_outperformance
