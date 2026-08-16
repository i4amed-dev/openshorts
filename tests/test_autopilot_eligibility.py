"""Filtering: which trending videos are actually usable, and permitted.

"Popular" and "clippable" are not the same set. A 4-hour livestream VOD, a
45-second Short and a region-locked upload are all popular. And popularity is
certainly not a licence — the rights policy is checked before anything else so
its rejection is the one the operator sees.
"""
from datetime import datetime, timedelta, timezone

import pytest

from automation.config import (
    POLICY_CC_OR_ALLOWLISTED, POLICY_CREATIVE_COMMONS, POLICY_OWNED_OR_ALLOWLISTED,
    normalise,
)
from automation.eligibility import check_eligibility, check_rights, evaluate
from automation.models import Reason
from autopilot_fakes import base_config, make_record

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
ALLOWED_CHANNEL = "UC" + "a" * 22


class TestRightsPolicy:
    def test_creative_commons_policy_accepts_a_cc_video(self):
        record = make_record("vid00000001", license="creativeCommon")
        assert check_rights(record, {"policy": POLICY_CREATIVE_COMMONS}) == (True, None)

    def test_creative_commons_policy_rejects_a_standard_licence(self):
        record = make_record("vid00000001", license="youtube")
        ok, reason = check_rights(record, {"policy": POLICY_CREATIVE_COMMONS})
        assert (ok, reason) == (False, Reason.RIGHTS_POLICY)

    def test_allowlist_policy_ignores_the_licence(self):
        record = make_record("vid00000001", license="youtube", channel_id=ALLOWED_CHANNEL)
        ok, _ = check_rights(record, {"policy": POLICY_OWNED_OR_ALLOWLISTED,
                                      "allowlisted_channel_ids": [ALLOWED_CHANNEL]})
        assert ok

    def test_allowlist_policy_rejects_an_unknown_channel(self):
        record = make_record("vid00000001", license="creativeCommon",
                             channel_id="UCotherotherotherotherx")
        ok, reason = check_rights(record, {"policy": POLICY_OWNED_OR_ALLOWLISTED,
                                           "allowlisted_channel_ids": [ALLOWED_CHANNEL]})
        assert (ok, reason) == (False, Reason.CHANNEL_NOT_ALLOWED)

    def test_combined_policy_accepts_either(self):
        rights = {"policy": POLICY_CC_OR_ALLOWLISTED,
                  "allowlisted_channel_ids": [ALLOWED_CHANNEL]}
        cc = make_record("vid00000001", license="creativeCommon", channel_id="UCzzz")
        owned = make_record("vid00000002", license="youtube", channel_id=ALLOWED_CHANNEL)
        neither = make_record("vid00000003", license="youtube", channel_id="UCzzz")
        assert check_rights(cc, rights)[0]
        assert check_rights(owned, rights)[0]
        assert not check_rights(neither, rights)[0]

    def test_an_unrecognised_policy_denies(self):
        # Fail closed: an unknown policy string must never mean "allow".
        record = make_record("vid00000001", license="creativeCommon")
        ok, _ = check_rights(record, {"policy": "SOMETHING_ELSE"})
        assert not ok

    def test_rights_are_checked_before_usefulness(self):
        # A too-short, wrong-licence video reports the rights problem, because
        # that is the decision the operator actually has to make.
        config = base_config()
        record = make_record("vid00000001", license="youtube", duration_seconds=5,
                             now=NOW)
        ok, reason = evaluate(record, config, now=NOW)
        assert (ok, reason) == (False, Reason.RIGHTS_POLICY)


class TestAvailabilityFilters:
    @pytest.mark.parametrize("field,value,expected", [
        ("live_state", "live", Reason.LIVE),
        ("live_state", "upcoming", Reason.UPCOMING),
        ("privacy_status", "private", Reason.UNAVAILABLE),
        ("upload_status", "rejected", Reason.UNAVAILABLE),
        ("made_for_kids", True, Reason.MADE_FOR_KIDS),
        ("embeddable", False, Reason.AGE_RESTRICTED),
    ])
    def test_unusable_sources_are_rejected_with_their_own_reason(self, field, value,
                                                                 expected):
        record = make_record("vid00000001", now=NOW, **{field: value})
        ok, reason = check_eligibility(record, base_config(), now=NOW)
        assert (ok, reason) == (False, expected)


class TestShapeFilters:
    """Duration/captions are the only "shape" checks left as hard gates —
    they are genuine processability, not a preference. Age, definition,
    views, velocity, engagement and channel cooldown moved to
    ``automation.opportunity`` as scoring inputs (see
    ``tests/test_autopilot_opportunity.py``): none of them says a video is
    *unusable*, only that it might not be a great source, and treating them
    as hard gates was what emptied the candidate pool. See eligibility.py's
    module docstring.
    """

    def test_a_short_is_too_short(self):
        record = make_record("vid00000001", duration_seconds=45, now=NOW)
        assert check_eligibility(record, base_config(), now=NOW) == (False, Reason.TOO_SHORT)

    def test_a_marathon_stream_is_too_long(self):
        record = make_record("vid00000001", duration_seconds=6 * 3600, now=NOW)
        assert check_eligibility(record, base_config(), now=NOW) == (False, Reason.TOO_LONG)

    def test_an_old_video_is_not_rejected_for_being_old(self):
        record = make_record("vid00000001", published_at=NOW - timedelta(days=30), now=NOW)
        assert check_eligibility(record, base_config(), now=NOW) == (True, None)

    def test_a_five_year_old_video_is_not_rejected_for_being_old(self):
        record = make_record("vid00000001", published_at=NOW - timedelta(days=365 * 5 + 30),
                             now=NOW)
        assert check_eligibility(record, base_config(), now=NOW) == (True, None)

    def test_sd_is_not_rejected_even_when_hd_is_the_preference(self):
        record = make_record("vid00000001", definition="sd", now=NOW)
        assert check_eligibility(record, base_config(), now=NOW) == (True, None)

    def test_captions_can_be_required(self):
        config = base_config(eligibility={"require_captions": True})
        record = make_record("vid00000001", caption=False, now=NOW)
        assert check_eligibility(record, config, now=NOW) == (False, Reason.NO_CAPTIONS)


class TestTractionIsNoLongerAGate:
    """Views, velocity and engagement rank candidates now; they never make
    the candidate pool empty. See test_autopilot_opportunity.py.
    """

    def test_a_low_view_count_is_not_rejected(self):
        config = base_config(eligibility={"min_views": 50_000})
        record = make_record("vid00000001", view_count=1_000, now=NOW)
        assert check_eligibility(record, config, now=NOW) == (True, None)

    def test_a_slow_burner_is_not_rejected(self):
        # 5M views accumulated over 300 days is a lifetime average, not
        # current momentum — but it is not disqualifying either.
        config = base_config(eligibility={"min_view_velocity_per_hour": 1000})
        record = make_record("vid00000001", view_count=5_000_000,
                             published_at=NOW - timedelta(days=300), now=NOW)
        assert record.views_per_hour(NOW) < 1000
        assert check_eligibility(record, config, now=NOW) == (True, None)

    def test_low_engagement_is_not_rejected(self):
        config = base_config(eligibility={"min_engagement_rate": 0.05})
        record = make_record("vid00000001", view_count=1_000_000, like_count=10,
                             comment_count=1, now=NOW)
        assert check_eligibility(record, config, now=NOW) == (True, None)


class TestChannelPolicy:
    def test_denylisted_channel_is_rejected(self):
        record = make_record("vid00000001", channel_id="UCbad", now=NOW)
        config = base_config(discovery={"channel_denylist": ["UCbad"]})
        assert check_eligibility(record, config, now=NOW) == (False, Reason.CHANNEL_DENIED)

    def test_allowlist_excludes_everything_else(self):
        record = make_record("vid00000001", channel_id="UCsomeone", now=NOW)
        config = base_config(discovery={"channel_allowlist": ["UCfriend"]})
        assert check_eligibility(record, config, now=NOW) == (
            False, Reason.CHANNEL_NOT_ALLOWED)

    def test_a_recently_used_channel_is_not_hard_rejected(self):
        # Cooldown is a soft ranking penalty and a selection-time deferral
        # now (see opportunity.py's channel_recent penalty and
        # discovery.pick_next_source), not a hard eligibility gate — an
        # operator relaxing the cooldown must not have to wait for the
        # candidate to be re-discovered from scratch.
        config = base_config(eligibility={"channel_cooldown_hours": 168})
        record = make_record("vid00000001", now=NOW)
        recent = NOW - timedelta(hours=10)
        assert check_eligibility(record, config, now=NOW, channel_last_used=recent) == (
            True, None)


class TestKeywordFilters:
    def test_excluded_keyword_rejects(self):
        config = base_config(eligibility={"keywords_none": ["giveaway"]})
        record = make_record("vid00000001", title="Huge GIVEAWAY stream", now=NOW)
        assert check_eligibility(record, config, now=NOW) == (False, Reason.KEYWORD_EXCLUDED)

    def test_required_keyword_must_appear(self):
        config = base_config(eligibility={"keywords_any": ["chess"]})
        record = make_record("vid00000001", title="Cooking pasta",
                             description="no chess here at all... wait", now=NOW)
        # It IS in the description, so it passes — the match is over both fields.
        assert check_eligibility(record, config, now=NOW) == (True, None)

    def test_required_keyword_missing_rejects(self):
        config = base_config(eligibility={"keywords_any": ["chess"]})
        record = make_record("vid00000001", title="Cooking pasta",
                             description="tomatoes and basil", now=NOW)
        assert check_eligibility(record, config, now=NOW) == (False, Reason.KEYWORD_MISSING)


class TestDeduplication:
    def test_an_already_known_video_is_a_duplicate(self):
        record = make_record("vid00000001", now=NOW)
        assert check_eligibility(record, base_config(), now=NOW, already_known=True) == (
            False, Reason.DUPLICATE)


def test_a_good_candidate_passes_everything():
    record = make_record("vid00000001", now=NOW)
    assert evaluate(record, base_config(), now=NOW) == (True, None)
