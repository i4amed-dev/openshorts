"""Which discovered videos Autopilot is allowed to touch at all.

Three separate concepts, deliberately not merged:

* **Rights policy** — is Autopilot *permitted* to process it? Manual mode
  asks a human to attest ownership per request. Unattended mode cannot, so
  it carries a persistent policy instead, and the policy that authorised
  each run is stored with the source. Nothing here ever synthesises the
  manual attestation.
* **Technical validity** — can this even be processed, independent of how
  good a Short it might make? Duplicate, still live, unavailable, the wrong
  shape, an explicit keyword/channel block. Binary and cheap.
* **Performance / opportunity** — how *good* a candidate is this? This used
  to be a third hard-gate tier here: a minimum view count, a minimum
  velocity, a minimum engagement rate, a maximum age, an HD-only floor, a
  channel cooldown. That tier is why the candidate pool collapsed to almost
  nothing — mainstream trending video is virtually never CC-licensed, so
  nearly everything already died on the rights gate, and whatever survived
  was then frequently killed a second time by these binary floors the
  moment it was a day older or a few views under a threshold. None of that
  says whether the video would make a good Short; it only says whether
  *today's* raw numbers happen to clear an arbitrary line. Performance now
  feeds :mod:`automation.opportunity` instead — a low-performing but
  technically valid, rights-clear candidate still gets a low score and
  loses on ranking, but it is never invisible, and it is never why the
  whole pool goes empty.

Every rejection returns a machine-readable reason from :class:`Reason`, which
is what the dashboard renders — so "why was this skipped" is always
answerable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from .config import (
    POLICY_CC_OR_ALLOWLISTED, POLICY_CREATIVE_COMMONS, POLICY_OWNED_OR_ALLOWLISTED,
)
from .models import Reason
from .youtube_client import VideoRecord

CREATIVE_COMMONS = "creativecommon"


def search_requires_creative_commons(rights: Dict[str, Any]) -> bool:
    """Whether a search-based discovery lane may safely ask YouTube for CC-only
    results.

    Derived from the rights policy rather than configured separately. Two
    independent switches could contradict each other: with an owned-channels
    policy and a stale "CC only" search flag, a standard-licence video from the
    operator's OWN channel would be filtered out by the API before the rights
    gate ever saw it — invisible, and impossible to debug from the dashboard.

    Only CREATIVE_COMMONS_ONLY narrows the query. The allowlist policies must
    see standard-licence videos, because for those channels the licence is not
    what grants permission; the rights gate still has the final say either way.
    """
    policy = str((rights or {}).get("policy") or POLICY_CREATIVE_COMMONS)
    return policy == POLICY_CREATIVE_COMMONS


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
    """Return ``(technically_valid, rejection_reason)`` for one candidate.

    ``now`` and ``channel_last_used`` are accepted for call-site compatibility
    (some historical callers pass them) but no longer influence the result —
    age and channel recency are opportunity/scoring signals now, not gates.

    Ordered cheapest-and-most-decisive first, so the stored reason is the one
    an operator would name themselves ("it's a livestream", not "the title
    contains a blocked word").
    """
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

    # --- channel policy (operator instructions, not performance) ------------
    denylist = set(discovery.get("channel_denylist") or [])
    if record.channel_id and record.channel_id in denylist:
        return False, Reason.CHANNEL_DENIED
    allowlist = set(discovery.get("channel_allowlist") or [])
    if allowlist and record.channel_id not in allowlist:
        return False, Reason.CHANNEL_NOT_ALLOWED

    # --- shape: genuine processability, not preference -----------------------
    duration = int(record.duration_seconds or 0)
    if duration < int(rules.get("min_duration_seconds", 0)):
        return False, Reason.TOO_SHORT
    if duration > int(rules.get("max_duration_seconds", 10 ** 9)):
        return False, Reason.TOO_LONG

    if rules.get("require_captions") and not record.caption:
        return False, Reason.NO_CAPTIONS

    # --- topic (explicit operator instruction, not a performance signal) ----
    haystack = f"{record.title}\n{record.description}".lower()
    excluded = [kw for kw in (rules.get("keywords_none") or []) if kw in haystack]
    if excluded:
        return False, Reason.KEYWORD_EXCLUDED
    required = rules.get("keywords_any") or []
    if required and not any(kw in haystack for kw in required):
        return False, Reason.KEYWORD_MISSING

    # Age, views, velocity, engagement, definition and channel cooldown are
    # deliberately absent from this function — see the module docstring. They
    # shape automation.opportunity's score instead of gating here.
    return True, None


def evaluate(record: VideoRecord, config: Dict[str, Any], *,
             now: Optional[datetime] = None,
             channel_last_used: Optional[datetime] = None,
             already_known: bool = False) -> Tuple[bool, Optional[str]]:
    """Rights first, then technical validity.

    Rights lead because a rejection there is a policy decision the operator
    must see plainly, not something buried behind a duration check.
    """
    allowed, reason = check_rights(record, config.get("rights") or {})
    if not allowed:
        return False, reason
    return check_eligibility(record, config, now=now, channel_last_used=channel_last_used,
                             already_known=already_known)
