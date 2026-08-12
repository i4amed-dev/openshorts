"""Autopilot domain model: states, transitions and row dataclasses.

The orchestrator is a state machine and every transition is written to SQLite
before the side effect it authorises. That ordering is what makes a restart
safe: on boot we can tell exactly which stage each source reached without
re-reading logs or re-running a completed stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class SourceState:
    """Lifecycle of one discovered YouTube video."""

    DISCOVERED = "DISCOVERED"          # persisted from the YouTube API, unscored
    FILTERED = "FILTERED"              # failed a hard eligibility filter
    ELIGIBLE = "ELIGIBLE"              # scored, passes every filter, waiting its turn
    SELECTED = "SELECTED"              # chosen for this cycle, about to be submitted
    PROCESS_QUEUED = "PROCESS_QUEUED"  # handed to the Klippo clip queue (job_id set)
    PROCESSING = "PROCESSING"          # clip generator reports it running
    PROCESS_READY = "PROCESS_READY"    # clips exist on disk
    PROCESS_FAILED = "PROCESS_FAILED"  # job failed; may retry within max_process_attempts
    CLIPS_SCHEDULED = "CLIPS_SCHEDULED"  # every selected clip has a publish attempt
    DONE = "DONE"                      # nothing left to do for this source
    SKIPPED = "SKIPPED"                # deliberately not used (quality, rights, duplicate…)
    FAILED = "FAILED"                  # gave up after the configured attempts

    TERMINAL = frozenset({FILTERED, DONE, SKIPPED, FAILED})
    ACTIVE_PROCESSING = frozenset({SELECTED, PROCESS_QUEUED, PROCESSING})
    ALL = frozenset({
        DISCOVERED, FILTERED, ELIGIBLE, SELECTED, PROCESS_QUEUED, PROCESSING,
        PROCESS_READY, PROCESS_FAILED, CLIPS_SCHEDULED, DONE, SKIPPED, FAILED,
    })


# Allowed transitions. Anything not listed is a bug, and `assert_transition`
# turns it into a loud error instead of a silently corrupted pipeline.
SOURCE_TRANSITIONS: Dict[str, frozenset] = {
    SourceState.DISCOVERED: frozenset({SourceState.ELIGIBLE, SourceState.FILTERED,
                                       SourceState.SKIPPED}),
    SourceState.ELIGIBLE: frozenset({SourceState.SELECTED, SourceState.FILTERED,
                                     SourceState.SKIPPED}),
    SourceState.SELECTED: frozenset({SourceState.PROCESS_QUEUED, SourceState.SKIPPED,
                                     SourceState.PROCESS_FAILED, SourceState.ELIGIBLE}),
    SourceState.PROCESS_QUEUED: frozenset({SourceState.PROCESSING, SourceState.PROCESS_READY,
                                           SourceState.PROCESS_FAILED, SourceState.SKIPPED}),
    SourceState.PROCESSING: frozenset({SourceState.PROCESS_READY, SourceState.PROCESS_FAILED,
                                       SourceState.SKIPPED}),
    SourceState.PROCESS_READY: frozenset({SourceState.CLIPS_SCHEDULED, SourceState.DONE,
                                          SourceState.SKIPPED, SourceState.FAILED}),
    SourceState.PROCESS_FAILED: frozenset({SourceState.ELIGIBLE, SourceState.FAILED,
                                           SourceState.SKIPPED}),
    SourceState.CLIPS_SCHEDULED: frozenset({SourceState.DONE, SourceState.FAILED}),
    SourceState.DONE: frozenset(),
    SourceState.FILTERED: frozenset({SourceState.ELIGIBLE}),  # re-evaluated after a config change
    SourceState.SKIPPED: frozenset(),
    # FAILED is terminal for the engine — nothing automatic leaves it. The one
    # way out is an operator pressing Retry in the dashboard, which is a
    # deliberate human decision to spend another pipeline run on this source.
    SourceState.FAILED: frozenset({SourceState.ELIGIBLE}),
}


class TransitionError(RuntimeError):
    """An illegal state transition was attempted."""


def assert_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in SourceState.ALL:
        raise TransitionError(f"Unknown source state {target!r}")
    allowed = SOURCE_TRANSITIONS.get(current)
    if allowed is None:
        raise TransitionError(f"Unknown source state {current!r}")
    if target not in allowed:
        raise TransitionError(f"Illegal source transition {current} → {target}")


class ClipState:
    PENDING = "PENDING"        # generated, not yet given a slot
    SCHEDULED = "SCHEDULED"    # a publish attempt owns it; vendor may hold it
    PUBLISHED = "PUBLISHED"    # vendor CONFIRMED it went live (not merely accepted)
    PARTIAL = "PARTIAL"        # live on some platforms, failed on others
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"        # not selected (below the top-N cut, wrong duration…)
    ALL = frozenset({PENDING, SCHEDULED, PUBLISHED, PARTIAL, FAILED, SKIPPED})
    # Nothing more will happen to the clip without an operator.
    SETTLED = frozenset({PUBLISHED, PARTIAL, FAILED, SKIPPED})


class PublishState:
    """Lifecycle of one publish attempt.

    The distinction that matters: **SUBMITTED is not PUBLISHED**. Upload-Post
    returning 2xx means it accepted the job. For a post scheduled three days out
    nothing exists on any platform yet, and for an async upload the platforms
    are still being worked through. Only a vendor status check saying so moves
    an attempt to PUBLISHED.
    """
    PENDING = "PENDING"        # row reserved, request not sent
    IN_FLIGHT = "IN_FLIGHT"    # request handed to the vendor, outcome unknown
    SUBMITTED = "SUBMITTED"    # vendor accepted; scheduled or queued, NOT live
    PUBLISHING = "PUBLISHING"  # vendor is actively working the platforms
    PUBLISHED = "PUBLISHED"    # vendor confirmed every platform succeeded
    PARTIAL_FAILED = "PARTIAL_FAILED"  # some platforms live, others failed
    FAILED = "FAILED"          # vendor rejected / every platform failed
    UNCERTAIN = "UNCERTAIN"    # outcome unknown — never auto-retried
    CANCELED = "CANCELED"
    ALL = frozenset({PENDING, IN_FLIGHT, SUBMITTED, PUBLISHING, PUBLISHED,
                     PARTIAL_FAILED, FAILED, UNCERTAIN, CANCELED})
    # States that still reserve a publishing slot / keep the clip file alive.
    # PUBLISHING and PARTIAL_FAILED are included: the vendor may still be
    # working, and the operator needs the evidence to resolve a partial.
    HOLDS_SLOT = frozenset({PENDING, IN_FLIGHT, SUBMITTED, PUBLISHING,
                            PUBLISHED, PARTIAL_FAILED, UNCERTAIN})
    # Nothing further will happen without an operator.
    TERMINAL = frozenset({PUBLISHED, FAILED, CANCELED})
    # Worth asking the vendor about on the next tick.
    NEEDS_RECONCILIATION = frozenset({SUBMITTED, PUBLISHING, UNCERTAIN})


# Legal publish transitions. Anything else is a bug rather than a state.
PUBLISH_TRANSITIONS = {
    PublishState.PENDING: frozenset({PublishState.IN_FLIGHT, PublishState.CANCELED,
                                     PublishState.FAILED}),
    PublishState.IN_FLIGHT: frozenset({PublishState.SUBMITTED, PublishState.PUBLISHING,
                                       PublishState.PUBLISHED, PublishState.FAILED,
                                       PublishState.PARTIAL_FAILED,
                                       PublishState.UNCERTAIN, PublishState.PENDING}),
    PublishState.SUBMITTED: frozenset({PublishState.PUBLISHING, PublishState.PUBLISHED,
                                       PublishState.FAILED, PublishState.PARTIAL_FAILED,
                                       PublishState.CANCELED, PublishState.UNCERTAIN}),
    PublishState.PUBLISHING: frozenset({PublishState.PUBLISHED, PublishState.FAILED,
                                        PublishState.PARTIAL_FAILED,
                                        PublishState.UNCERTAIN}),
    # An uncertain attempt is resolved by a status lookup or by a human, in
    # either direction — including back to PENDING once we know it never landed.
    PublishState.UNCERTAIN: frozenset({PublishState.SUBMITTED, PublishState.PUBLISHING,
                                       PublishState.PUBLISHED, PublishState.FAILED,
                                       PublishState.PARTIAL_FAILED, PublishState.PENDING,
                                       PublishState.CANCELED}),
    # A partial failure needs a human: they may accept it (PUBLISHED) or give up.
    PublishState.PARTIAL_FAILED: frozenset({PublishState.PUBLISHED, PublishState.FAILED,
                                            PublishState.CANCELED}),
    PublishState.FAILED: frozenset({PublishState.PENDING, PublishState.CANCELED}),
    PublishState.PUBLISHED: frozenset(),
    PublishState.CANCELED: frozenset(),
}


def assert_publish_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in PublishState.ALL:
        raise TransitionError(f"Unknown publish state {target!r}")
    allowed = PUBLISH_TRANSITIONS.get(current)
    if allowed is None:
        raise TransitionError(f"Unknown publish state {current!r}")
    if target not in allowed:
        raise TransitionError(f"Illegal publish transition {current} → {target}")


class RunStatus:
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EngineStatus:
    """What the operator sees at the top of the Autopilot page."""
    OFF = "OFF"
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"              # user asked it to stop after the current job
    PAUSED_ERROR = "PAUSED_ERROR"  # circuit breaker tripped


# Machine-readable reasons a candidate was rejected. The dashboard renders these
# verbatim, so they double as the operator's explanation of *why*.
class Reason:
    DUPLICATE = "duplicate_source"
    TOO_SHORT = "duration_below_minimum"
    TOO_LONG = "duration_above_maximum"
    TOO_OLD = "older_than_age_window"
    LOW_VIEWS = "views_below_minimum"
    LOW_VELOCITY = "view_velocity_below_minimum"
    LOW_ENGAGEMENT = "engagement_below_minimum"
    LIVE = "live_broadcast"
    UPCOMING = "upcoming_broadcast"
    UNAVAILABLE = "not_public_or_unavailable"
    AGE_RESTRICTED = "age_restricted"
    MADE_FOR_KIDS = "made_for_kids"
    NO_CAPTIONS = "captions_required"
    LOW_DEFINITION = "definition_below_minimum"
    CHANNEL_DENIED = "channel_denylisted"
    CHANNEL_NOT_ALLOWED = "channel_not_allowlisted"
    CHANNEL_COOLDOWN = "channel_cooldown"
    RIGHTS_POLICY = "rights_policy"
    KEYWORD_EXCLUDED = "keyword_excluded"
    KEYWORD_MISSING = "keyword_required"
    QUALITY_GATE = "source_quality_below_minimum"
    NO_CLIPS = "no_clips_generated"
    DAILY_SOURCE_LIMIT = "daily_source_limit_reached"


@dataclass
class DiscoveredSource:
    """One row of ``discovered_source`` (see automation/db.py for the DDL)."""
    id: Optional[int] = None
    youtube_video_id: str = ""
    url: str = ""
    channel_id: str = ""
    channel_title: str = ""
    title: str = ""
    description: str = ""
    published_at: Optional[str] = None      # UTC ISO-8601
    duration_seconds: int = 0
    category_id: str = ""
    view_count: int = 0
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    license: str = ""                       # youtube | creativeCommon
    definition: str = ""                    # sd | hd
    caption_available: bool = False
    live_state: str = "none"                # none | live | upcoming
    made_for_kids: bool = False
    age_restricted: bool = False
    privacy_status: str = "public"
    embeddable: bool = True
    discovery_source: str = ""              # most_popular | niche_search:<topic>
    discovered_at: Optional[str] = None
    chart_rank: Optional[int] = None
    run_id: Optional[str] = None
    score: float = 0.0
    score_breakdown: Dict[str, Any] = field(default_factory=dict)
    eligible: bool = False
    rejection_reason: Optional[str] = None
    state: str = SourceState.DISCOVERED
    state_changed_at: Optional[str] = None
    selected_at: Optional[str] = None
    job_id: Optional[str] = None
    attempts: int = 0
    last_error: Optional[str] = None
    rights_policy: Optional[str] = None
    next_retry_at: Optional[str] = None

    @property
    def watch_url(self) -> str:
        return self.url or f"https://www.youtube.com/watch?v={self.youtube_video_id}"


@dataclass
class GeneratedClip:
    id: Optional[int] = None
    source_id: int = 0
    job_id: str = ""
    clip_index: int = 0
    filename: str = ""
    title: str = ""
    description: str = ""
    start_seconds: float = 0.0
    end_seconds: float = 0.0
    rank: int = 0
    state: str = ClipState.PENDING
    skip_reason: Optional[str] = None
    created_at: Optional[str] = None

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end_seconds) - float(self.start_seconds))


@dataclass
class PublishAttempt:
    id: Optional[int] = None
    clip_id: int = 0
    source_id: int = 0
    job_id: str = ""
    clip_index: int = 0
    idempotency_key: str = ""
    platforms: List[str] = field(default_factory=list)
    scheduled_for_utc: Optional[str] = None
    timezone: str = "UTC"
    state: str = PublishState.PENDING
    vendor_response: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    retry_count: int = 0
    submitted_at: Optional[str] = None
    created_at: Optional[str] = None
    title: str = ""
    description: str = ""
    # --- vendor reconciliation (schema v2) ---
    vendor_request_id: Optional[str] = None   # ours, sent with the request
    vendor_job_id: Optional[str] = None       # theirs, for scheduled posts
    vendor_status: Optional[str] = None       # last top-level status seen
    vendor_results: Optional[List[Dict[str, Any]]] = None  # per-platform outcomes
    last_status_check_at: Optional[str] = None
    next_status_check_at: Optional[str] = None
    finalized_at: Optional[str] = None

    @property
    def tracking_id(self) -> Optional[str]:
        """Whichever identifier the vendor status endpoint accepts for this attempt."""
        return self.vendor_job_id or self.vendor_request_id

    @property
    def is_vendor_scheduled(self) -> bool:
        """True once Upload-Post holds this as a future scheduled job."""
        return bool(self.vendor_job_id)
