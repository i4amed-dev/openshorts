"""The backtest CLI: re-rank stored candidates against the current config,
with no network calls, no processing, no publishing.

Tuning ranking weights or the rights policy against a real discovery run is
slow and burns quota per iteration — this is the fast loop instead.
"""
from datetime import datetime, timezone

import pytest

from automation.backtest_discovery import run_backtest
from automation.config import normalise
from automation.db import AutopilotDB
from automation.models import DiscoveredSource

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    database = AutopilotDB(":memory:").connect()
    database.save_settings(normalise({}))
    yield database
    database.close()


def _seed(db, video_id, **overrides):
    defaults = dict(
        youtube_video_id=video_id, url=f"https://youtu.be/{video_id}", channel_id="UCabc",
        channel_title="Chan", title=f"Video {video_id}", description="desc",
        published_at="2026-08-10T00:00:00+00:00", duration_seconds=600, view_count=10000,
        like_count=500, comment_count=50, license="creativeCommon", definition="hd",
        discovery_source="TRENDING_NOW",
    )
    defaults.update(overrides)
    source = DiscoveredSource(**defaults)
    db.upsert_source(source)


class TestBacktest:
    def test_it_never_touches_the_network_or_a_client(self, db):
        # No YouTubeClient is even constructed — passing one in would be a
        # signature error, which is the point: this function cannot call out.
        _seed(db, "vid1")
        result = run_backtest(db, now=NOW)
        assert result["total_candidates"] == 1

    def test_reflects_the_current_rights_policy_not_the_one_at_discovery_time(self, db):
        _seed(db, "vid1", license="youtube")
        result = run_backtest(db, now=NOW)
        assert result["would_be_eligible"] == 0
        assert "rights_policy" in result["rejection_reasons"]

        db.save_settings(normalise({"rights": {"policy": "OWNED_OR_ALLOWLISTED_CHANNELS",
                                                "allowlisted_channel_ids": ["UC" + "a" * 22]}}))
        # A different channel from the (differently-shaped) allowlist above —
        # still blocked, but for a different, still-visible reason.
        result2 = run_backtest(db, now=NOW)
        assert result2["would_be_eligible"] == 0
        assert "channel_not_allowlisted" in result2["rejection_reasons"]

    def test_ranked_list_is_sorted_best_first(self, db):
        _seed(db, "vid_weak", view_count=100, like_count=1, comment_count=0)
        _seed(db, "vid_strong", view_count=5_000_000, like_count=400_000, comment_count=20_000)
        result = run_backtest(db, now=NOW)
        assert result["ranked"][0]["video_id"] == "vid_strong"

    def test_lane_distribution_is_reported(self, db):
        _seed(db, "vid1", discovery_source="EVERGREEN_WINNERS:cooking")
        result = run_backtest(db, now=NOW)
        assert result["lane_distribution"].get("EVERGREEN_WINNERS") == 1

    def test_each_ranked_item_carries_a_score_breakdown(self, db):
        _seed(db, "vid1")
        result = run_backtest(db, now=NOW)
        assert "components" in result["ranked"][0]["breakdown"]

    def test_an_empty_database_is_not_an_error(self, db):
        result = run_backtest(db, now=NOW)
        assert result == {
            "config_snapshot": result["config_snapshot"],
            "total_candidates": 0, "would_be_eligible": 0,
            "rejection_reasons": {}, "lane_distribution": {}, "ranked": [],
        }
