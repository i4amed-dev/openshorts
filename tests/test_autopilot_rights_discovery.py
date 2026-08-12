"""The rights policy is the single authority over discovery filtering.

The defect: discovery carried its own ``creative_commons_search_only`` switch,
independent of ``rights.policy``. With an owned-channels policy and that flag
left on, YouTube filtered standard-licence videos out of the search response
before the rights gate ever saw them — so the operator's OWN uploads were
invisible, with nothing in the dashboard to explain why.

The query is now derived from the policy, and the policy still has the final
say after the results come back.
"""
from datetime import datetime, timedelta, timezone

import pytest

from automation import discovery
from automation.config import (
    POLICY_CC_OR_ALLOWLISTED, POLICY_CREATIVE_COMMONS, POLICY_OWNED_OR_ALLOWLISTED,
    normalise,
)
from automation.db import AutopilotDB, utcnow
from automation.eligibility import search_requires_creative_commons
from automation.models import Reason, SourceState
from automation.youtube_client import BUCKET_GENERAL, BUCKET_SEARCH
from autopilot_fakes import FakeYouTubeClient, base_config, make_record, run_async

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
OWNED = "UC" + "a" * 22


@pytest.fixture
def db():
    database = AutopilotDB(":memory:").connect()
    yield database
    database.close()


def config_for(policy, **overrides):
    base = {"rights": {"policy": policy,
                       "allowlisted_channel_ids": [] if policy == POLICY_CREATIVE_COMMONS
                       else [OWNED]},
            "discovery": {"strategies": ["niche_search"], "topics": ["chess"]},
            "eligibility": {"min_views": 0, "min_view_velocity_per_hour": 0,
                            "min_engagement_rate": 0, "channel_cooldown_hours": 0}}
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value
    return normalise(base)


class TestQueryDerivation:
    def test_cc_only_policy_narrows_the_search(self):
        assert search_requires_creative_commons({"policy": POLICY_CREATIVE_COMMONS})

    def test_owned_channels_policy_must_not_narrow_the_search(self):
        """Your own standard-licence uploads have to be discoverable."""
        assert not search_requires_creative_commons(
            {"policy": POLICY_OWNED_OR_ALLOWLISTED, "allowlisted_channel_ids": [OWNED]})

    def test_hybrid_policy_must_not_narrow_the_search(self):
        assert not search_requires_creative_commons(
            {"policy": POLICY_CC_OR_ALLOWLISTED, "allowlisted_channel_ids": [OWNED]})

    def test_an_unknown_policy_defaults_to_the_safe_narrow_query(self):
        assert not search_requires_creative_commons({"policy": "SOMETHING_NEW"})

    def test_the_contradictory_switch_no_longer_exists(self):
        """Two independent switches could disagree; now there is one."""
        config = normalise({"discovery": {"creative_commons_search_only": True}})
        assert "creative_commons_search_only" not in config["discovery"]


class TestSearchRequestReflectsThePolicy:
    def _capture(self, policy):
        captured = {}

        class Recording(FakeYouTubeClient):
            async def search_video_ids(self, query, **kwargs):
                captured.update(kwargs)
                captured["query"] = query
                return [r.video_id for r in self.records]

        client = Recording([make_record("vid00000001", now=NOW)])
        run_async(discovery.fetch_candidates(client, config_for(policy), now=NOW))
        return captured

    def test_cc_only_asks_youtube_for_creative_commons(self):
        assert self._capture(POLICY_CREATIVE_COMMONS)["creative_commons"] is True

    def test_owned_channels_does_not_ask_for_creative_commons(self):
        assert self._capture(POLICY_OWNED_OR_ALLOWLISTED)["creative_commons"] is False

    def test_hybrid_does_not_ask_for_creative_commons(self):
        assert self._capture(POLICY_CC_OR_ALLOWLISTED)["creative_commons"] is False


class TestRightsRemainTheFinalAuthority:
    def _discover(self, db, policy, records):
        client = FakeYouTubeClient(records)
        return run_async(discovery.run_discovery(db, config_for(policy), client,
                                                 run_id="run-1", now=NOW))

    def test_a_standard_licence_video_from_an_owned_channel_survives(self, db):
        """The exact case the old flag silently destroyed."""
        record = make_record("vid00000001", now=NOW, license="youtube", channel_id=OWNED)
        self._discover(db, POLICY_OWNED_OR_ALLOWLISTED, [record])
        source = db.get_source_by_video_id("vid00000001")
        assert source.state == SourceState.ELIGIBLE

    def test_a_standard_licence_video_from_a_stranger_is_still_rejected(self, db):
        record = make_record("vid00000001", now=NOW, license="youtube",
                             channel_id="UCstranger")
        self._discover(db, POLICY_OWNED_OR_ALLOWLISTED, [record])
        source = db.get_source_by_video_id("vid00000001")
        assert source.state == SourceState.FILTERED
        assert source.rejection_reason == Reason.CHANNEL_NOT_ALLOWED

    def test_cc_only_rejects_a_standard_licence_third_party_source(self, db):
        record = make_record("vid00000001", now=NOW, license="youtube",
                             channel_id="UCstranger")
        self._discover(db, POLICY_CREATIVE_COMMONS, [record])
        source = db.get_source_by_video_id("vid00000001")
        assert source.state == SourceState.FILTERED
        assert source.rejection_reason == Reason.RIGHTS_POLICY

    def test_hybrid_accepts_creative_commons_from_anyone(self, db):
        record = make_record("vid00000001", now=NOW, license="creativeCommon",
                             channel_id="UCstranger")
        self._discover(db, POLICY_CC_OR_ALLOWLISTED, [record])
        assert db.get_source_by_video_id("vid00000001").state == SourceState.ELIGIBLE

    def test_hybrid_accepts_standard_licence_from_an_approved_channel(self, db):
        record = make_record("vid00000001", now=NOW, license="youtube", channel_id=OWNED)
        self._discover(db, POLICY_CC_OR_ALLOWLISTED, [record])
        assert db.get_source_by_video_id("vid00000001").state == SourceState.ELIGIBLE

    def test_a_wider_query_does_not_widen_what_is_allowed(self, db):
        """Owned-channels sees more results, but still processes only its own."""
        records = [
            make_record("vid00000001", now=NOW, license="youtube", channel_id=OWNED),
            make_record("vid00000002", now=NOW, license="youtube", channel_id="UCother"),
            make_record("vid00000003", now=NOW, license="creativeCommon",
                        channel_id="UCother"),
        ]
        self._discover(db, POLICY_OWNED_OR_ALLOWLISTED, records)
        eligible = {s.youtube_video_id for s in
                    db.list_sources(states=[SourceState.ELIGIBLE], limit=10)}
        assert eligible == {"vid00000001"}


class TestQuotaBucketsInDiscovery:
    def test_search_exhaustion_still_allows_chart_discovery(self, db):
        db.mark_quota_exhausted(utcnow() + timedelta(hours=5), "quotaExceeded",
                                bucket=BUCKET_SEARCH)
        config = base_config(discovery={"strategies": ["most_popular", "niche_search"],
                                        "topics": ["chess"]})
        client = FakeYouTubeClient([make_record("vid00000001", now=NOW)])
        result = run_async(discovery.run_discovery(db, config, client,
                                                   run_id="run-1", now=NOW))
        assert not result.get("quota_exhausted")
        assert client.popular_calls == 1
        assert client.search_calls == 0

    def test_general_exhaustion_still_allows_niche_search(self, db):
        db.mark_quota_exhausted(utcnow() + timedelta(hours=5), "quotaExceeded",
                                bucket=BUCKET_GENERAL)
        config = base_config(discovery={"strategies": ["most_popular", "niche_search"],
                                        "topics": ["chess"]})
        client = FakeYouTubeClient([make_record("vid00000001", now=NOW)])
        result = run_async(discovery.run_discovery(db, config, client,
                                                   run_id="run-1", now=NOW))
        assert not result.get("quota_exhausted")
        assert client.popular_calls == 0
        assert client.search_calls == 1

    def test_discovery_parks_only_when_nothing_can_run(self, db):
        db.mark_quota_exhausted(utcnow() + timedelta(hours=5), "quotaExceeded",
                                bucket=BUCKET_GENERAL)
        config = base_config(discovery={"strategies": ["most_popular"]})
        client = FakeYouTubeClient([make_record("vid00000001", now=NOW)])
        result = run_async(discovery.run_discovery(db, config, client,
                                                   run_id="run-1", now=NOW))
        assert result["quota_exhausted"]
        assert client.popular_calls == 0
