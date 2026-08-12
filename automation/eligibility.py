"""Which discovered videos Autopilot is allowed to process.

Two separate gates, deliberately not merged:

* **Eligibility filters** — is this a *useful* Clip Generator input? Trending is
  not the same as clippable: a 4-hour livestream VOD, a 45-second Short and a
  region-blocked upload are all "popular".
* **Rights policy** — is Autopilot *permitted* to process it? Manual mode asks a
  human to attest ownership per request. Unattended mode cannot, so it carries a
  persistent policy instead, and the policy that authorised each run is stored
  with the source. Nothing here ever synthesises the manual attestation.

Every rejection returns a machine-readable reason from :class:`Reason`, which is
what the dashboard renders — so "why was this skipped" is always answerable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from .config import (
    POLICY_CC_OR_ALLOWLISTED, POLICY_CREATIVE_COMMONS, POLICY_OWNED_OR_ALLOWLISTED,
)
from .models import Reason
from .youtube_client import VideoRecord

CREATIVE_COMMONS = "creativecommon"


def check_rights(record: VideoRecord, rights: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Apply the configured Source Rights Policy. Returns (allowed, reason).

    The safe default for automatic third-party discovery is Creative Commons:
    a CC-BY upload is licensed for reuse, while "it was trending" is not a
    licence to republish anything.
    """
    policy = str(rights.get("policy") or POLICY_CREATIVE_COMMONS)
    allowlist = set(rights.get("allowlisted_channel_ids") or [])
    is_cc = str(record.license or "").lower() == CREATIVE_COMMONS
    is_allowlisted = bool(record.channel_id) and record.channel_id in allowlist

    if policy == POLICY_CREATIVE_COMMONS:
        return (True, None) if is_cc else (False, Reason.RIGHTS_POLICY)
    if policy == POLICY_OWNED_OR_ALLOWLISTED:
        return (True, None) if is_allowlisted else (False, Reason.CHANNEL_NOT_ALLOWED)
    if policy == POLICY_CC_OR_ALLOWLISTED:
        return (True, None) if (is_cc or is_allowlisted) else (False, Reason.RIGHTS_POLICY)
    # Unknown policy: refuse rather than fall through to "allow".
    return False, Reason.RIGHTS_POLICY


def check_eligibility(record: VideoRecord, config: Dict[str, Any], *,
                      now: Optional[datetime] = None,
                      channel_last_used: Optional[datetime] = None,
                      already_known: bool = False) -> Tuple[bool, Optional[str]]:
    """Return ``(eligible, rejection_reason)`` for one candidate.

    Ordered cheapest-and-most-decisive first, so the stored reason is the one an
    operator would name themselves ("it's a livestream", not "engagement 0.4%").
    """
    now = now or datetime.now(timezone.utc)
    rules = config.get("eligibility") or {}
    discovery = config.get("discovery") or {}

    if already_known:
        return False, Reason.DUPLICATE

    # --- availability -------------------------------------------------------
    if record.live_state == "live":
        return False, Reason.LIVE
    if record.live_state == "upcoming":
        return False, Reason.UPCOMING
    if record.privacy_status != "public" or record.upload_status != "processed":
        return False, Reason.UNAVAILABLE
    if rules.get("exclude_made_for_kids", True) and record.made_for_kids:
        return False, Reason.MADE_FOR_KIDS
    # The Data API exposes age restriction through contentRating, which is not in
    # the parts we request; `embeddable=False` is the reliable proxy we do get,
    # and an unembeddable video is usually restricted or licence-locked anyway.
    if rules.get("exclude_age_restricted", True) and not record.embeddable:
        return False, Reason.AGE_RESTRICTED

    # --- channel policy -----------------------------------------------------
    denylist = set(discovery.get("channel_denylist") or [])
    if record.channel_id and record.channel_id in denylist:
        return False, Reason.CHANNEL_DENIED
    allowlist = set(discovery.get("channel_allowlist") or [])
    if allowlist and record.channel_id not in allowlist:
        return False, Reason.CHANNEL_NOT_ALLOWED

    cooldown_hours = int(rules.get("channel_cooldown_hours") or 0)
    if cooldown_hours and channel_last_used is not None:
        if channel_last_used > now - timedelta(hours=cooldown_hours):
            return False, Reason.CHANNEL_COOLDOWN

    # --- shape --------------------------------------------------------------
    duration = int(record.duration_seconds or 0)
    if duration < int(rules.get("min_duration_seconds", 0)):
        return False, Reason.TOO_SHORT
    if duration > int(rules.get("max_duration_seconds", 10 ** 9)):
        return False, Reason.TOO_LONG

    age_hours = record.age_hours(now)
    if age_hours > float(rules.get("max_age_hours", 10 ** 9)):
        return False, Reason.TOO_OLD

    definition_floor = str(rules.get("min_definition") or "any").lower()
    if definition_floor == "hd" and record.definition != "hd":
        return False, Reason.LOW_DEFINITION

    if rules.get("require_captions") and not record.caption:
        return False, Reason.NO_CAPTIONS

    # --- traction -----------------------------------------------------------
    if record.view_count < int(rules.get("min_views", 0)):
        return False, Reason.LOW_VIEWS
    if record.views_per_hour(now) < float(rules.get("min_view_velocity_per_hour", 0)):
        return False, Reason.LOW_VELOCITY
    if record.engagement_rate() < float(rules.get("min_engagement_rate", 0)):
        return False, Reason.LOW_ENGAGEMENT

    # --- topic ---------------------------------------------------------------
    haystack = f"{record.title}\n{record.description}".lower()
    excluded = [kw for kw in (rules.get("keywords_none") or []) if kw in haystack]
    if excluded:
        return False, Reason.KEYWORD_EXCLUDED
    required = rules.get("keywords_any") or []
    if required and not any(kw in haystack for kw in required):
        return False, Reason.KEYWORD_MISSING

    return True, None


def evaluate(record: VideoRecord, config: Dict[str, Any], *,
             now: Optional[datetime] = None,
             channel_last_used: Optional[datetime] = None,
             already_known: bool = False) -> Tuple[bool, Optional[str]]:
    """Rights first, then usefulness.

    Rights lead because a rejection there is a policy decision the operator must
    see plainly, not something buried behind "view velocity too low".
    """
    allowed, reason = check_rights(record, config.get("rights") or {})
    if not allowed:
        return False, reason
    return check_eligibility(record, config, now=now, channel_last_used=channel_last_used,
                             already_known=already_known)
