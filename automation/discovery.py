"""Discovery: find candidate YouTube sources across several opportunity
lanes, score them, judge them, persist the verdict.

One pass does four things in order — fetch, deduplicate, score, judge — and
writes every outcome, including the rejections. The rejected rows are the
point: without them the dashboard can only say "nothing was selected", and an
operator tuning filters is guessing.

The previous version of this module ran one search-or-chart strategy and fed
the result through hard eligibility gates (age, views, velocity, engagement,
definition) that rejected almost everything, on top of a rights gate that
already rejected almost everything else. Both problems are fixed here:
eligibility (see automation.eligibility) is now technical validity and
rights only; performance ranks instead of rejecting (see
automation.opportunity); and instead of one discovery strategy there are six
independent lanes, each targeting a different kind of opportunity —
currently trending, early breakout, niche momentum, proven evergreen demand,
underexposed high engagement, and channels that have already produced a good
candidate. See automation/config.py's LANES for the full list.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import channel_context, eligibility, opportunity, ports
from .config import (
    LANE_CHANNEL_WINNERS, LANE_EARLY_BREAKOUT, LANE_EVERGREEN_WINNERS, LANE_NICHE_MOMENTUM,
    LANE_TRENDING_NOW, LANE_UNDEREXPOSED, LANES, POLICY_CREATIVE_COMMONS,
    SEARCH_LANES_REQUIRING_TOPICS,
)
from .db import AutopilotDB, iso, parse_iso, utcnow
from .query_expansion import expand_topic
from .models import DiscoveredSource, Reason, SourceState
from .youtube_client import (
    BUCKET_GENERAL, BUCKET_SEARCH, QuotaExhausted, VideoRecord, YouTubeClient, YouTubeError,
)

# Selection tiers, most to least strict. Only the OPPORTUNITY floor relaxes
# between tiers — technical validity and rights are identical at every tier
# and never relaxed (see pick_next_source).
TIER_STRICT = "STRICT"
TIER_NORMAL = "NORMAL"
TIER_EXPLORATION = "EXPLORATION"

# search.list queries budgeted per lane per run — bounded independently of
# how many topics/variants are configured, so an operator adding topics can
# never accidentally multiply one run's quota spend unboundedly.
_MAX_QUERIES_PER_LANE = 6
_MAX_TOPICS_PER_LANE = 5
_MAX_CHANNELS_PER_RUN = 5

# EARLY_BREAKOUT rotates through these windows by run, so a single run does
# not pay for every horizon at once but coverage still broadens over time.
_BREAKOUT_WINDOWS_HOURS = (6, 24, 24 * 3, 24 * 7)

# discovery.discovery_mode -> lanes that win the rotation more often. Not
# used by BALANCED (every enabled lane rotates evenly) or unrecognised modes.
_MODE_LANE_PRIORITY: Dict[str, frozenset] = {
    "TREND_HEAVY": frozenset({LANE_EARLY_BREAKOUT, LANE_NICHE_MOMENTUM}),
    "EVERGREEN_HEAVY": frozenset({LANE_EVERGREEN_WINNERS, LANE_UNDEREXPOSED}),
    "NICHE_FOCUSED": frozenset({LANE_NICHE_MOMENTUM, LANE_UNDEREXPOSED, LANE_CHANNEL_WINNERS}),
}
# EXPERIMENTAL leans on exploration instead of lane priority — see
# pick_next_source's use of this additive bump.
_EXPERIMENTAL_EXPLORATION_BONUS = 0.20


class DiscoveryResult(dict):
    """Stats for the run row and the dashboard."""


def _lane_of(discovery_source: str) -> str:
    lane = (discovery_source or "").split(":", 1)[0]
    return lane if lane in LANES else LANE_TRENDING_NOW


def _stable_run_index(run_id: str, modulus: int = 997) -> int:
    """Deterministic per-run rotation offset, derived from the run id alone.

    No sequential counter to maintain: the run id is already unique, so
    hashing it gives a stable, evenly-distributed index without new state.
    """
    if not run_id:
        return 0
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulus


def lanes_for_run(config: Dict[str, Any], *, run_index: int = 0) -> List[str]:
    """Which lanes actually execute this run.

    TRENDING_NOW is cheap (general bucket, ~free) and always runs when
    enabled. The search-based lanes are rotated — ``discovery.lanes_per_run``
    of them per run — so quota is spent breadth-first across runs rather
    than exhaustively on every enabled lane every time.

    This never overrides which lanes an operator explicitly enabled — under
    the owned/allowlisted-channels rights policy a broad keyword search can
    still be exactly what an operator wants (results outside the allowlist
    are simply rejected afterward by the rights gate, same as any other
    policy). "Smart rights-aware discovery" here means CHANNEL_WINNERS is
    available to prioritise the operator's own catalogue and TRENDING_NOW's
    budget share shrinks where it structurally cannot pass CC-only rights
    (see fetch_candidates) — not silently disabling lanes the operator asked
    for.
    """
    discovery = config.get("discovery") or {}
    enabled = [l for l in (discovery.get("lanes") or [LANE_TRENDING_NOW]) if l in LANES]

    always_on = [LANE_TRENDING_NOW] if LANE_TRENDING_NOW in enabled else []
    rotating = [l for l in enabled if l != LANE_TRENDING_NOW]
    if not rotating:
        return always_on
    lanes_per_run = max(1, int(discovery.get("lanes_per_run") or 3))
    # The discovery-mode preset tilts *how often a lane wins the rotation*,
    # not which lanes are allowed to run at all — a low-risk way to implement
    # "Trend-heavy" / "Evergreen-heavy" / "Niche-focused" without a second
    # configuration surface duplicating `lanes`. Priority lanes get extra
    # entries in the rotation pool, so across many runs they appear in
    # `picked` more often, without ever fully excluding the others.
    priority = _MODE_LANE_PRIORITY.get(discovery.get("discovery_mode") or "", frozenset())
    weighted = [lane for lane in rotating for _ in range(2 if lane in priority else 1)]
    offset = run_index % len(weighted)
    rotated = weighted[offset:] + weighted[:offset]
    picked: List[str] = []
    for lane in rotated:
        if lane not in picked:
            picked.append(lane)
        if len(picked) >= lanes_per_run:
            break
    return always_on + picked


def _lane_search_params(lane: str, now: datetime,
                        run_index: int) -> Tuple[Optional[datetime], str]:
    """(published_after, order) for one search-based lane.

    Each lane asks YouTube a genuinely different question: EARLY_BREAKOUT
    wants "brand new, sorted by upload time" (a viewCount sort would bury a
    3-hour-old breakout under a 3-week-old establishd hit); NICHE_MOMENTUM
    wants "biggest in the niche recently"; EVERGREEN_WINNERS wants "biggest
    ever, no age limit at all"; UNDEREXPOSED wants "best-liked", which is
    what YouTube's own ``rating`` order approximates independent of reach.
    """
    if lane == LANE_EARLY_BREAKOUT:
        hours = _BREAKOUT_WINDOWS_HOURS[run_index % len(_BREAKOUT_WINDOWS_HOURS)]
        return now - timedelta(hours=hours), "date"
    if lane == LANE_NICHE_MOMENTUM:
        return now - timedelta(days=30), "viewCount"
    if lane == LANE_EVERGREEN_WINNERS:
        return None, "viewCount"
    if lane == LANE_UNDEREXPOSED:
        return now - timedelta(days=90), "rating"
    return None, "viewCount"


def _queries_for_lane(topics: List[str], variants_per_topic: int,
                      run_index: int) -> List[Tuple[str, str]]:
    """Bounded ``(topic, query)`` pairs for one lane's search calls this run."""
    out: List[Tuple[str, str]] = []
    for topic in topics[:_MAX_TOPICS_PER_LANE]:
        for variant in expand_topic(topic, variants_per_run=variants_per_topic,
                                    run_index=run_index):
            out.append((topic, variant))
            if len(out) >= _MAX_QUERIES_PER_LANE:
                return out
    return out


def _channel_winner_seeds(config: Dict[str, Any], db: Optional[AutopilotDB]) -> List[str]:
    """Channels worth specifically re-checking: allowlisted, or previously strong."""
    rights = config.get("rights") or {}
    discovery = config.get("discovery") or {}
    seeds: List[str] = list(rights.get("allowlisted_channel_ids") or [])
    for channel_id in discovery.get("channel_allowlist") or []:
        if channel_id not in seeds:
            seeds.append(channel_id)
    if db is not None:
        for channel_id in db.top_channels(limit=_MAX_CHANNELS_PER_RUN):
            if channel_id not in seeds:
                seeds.append(channel_id)
    return seeds[:_MAX_CHANNELS_PER_RUN]


async def fetch_candidates(client: YouTubeClient, config: Dict[str, Any], *,
                           now: Optional[datetime] = None,
                           allow_search: bool = True,
                           allow_general: bool = True,
                           run_id: str = "",
                           db: Optional[AutopilotDB] = None) -> List[VideoRecord]:
    """Run this cycle's discovery lanes and return de-duplicated candidates.

    Each returned record's ``discovery_source`` is tagged ``LANE:detail`` —
    see ``_lane_of`` for how the canonical lane is recovered from it.
    """
    now = now or utcnow()
    discovery = config.get("discovery") or {}
    region = discovery.get("region_code") or "US"
    language = discovery.get("relevance_language") or "en"
    budget = int(discovery.get("max_candidates_per_run") or 50)
    variants_per_topic = int(discovery.get("query_variants_per_topic") or 2)
    topics = discovery.get("topics") or []
    rights = config.get("rights") or {}
    policy = rights.get("policy") or POLICY_CREATIVE_COMMONS
    # Derived from the rights policy — never a second, contradictable switch.
    cc_only = eligibility.search_requires_creative_commons(rights)
    run_index = _stable_run_index(run_id)

    lanes = lanes_for_run(config, run_index=run_index)
    collected: Dict[str, VideoRecord] = {}

    def _absorb(records: List[VideoRecord], tag: str) -> None:
        for record in records:
            if record.video_id in collected:
                continue
            if not record.discovery_source:
                record.discovery_source = tag
            collected[record.video_id] = record

    # --- TRENDING_NOW: the chart, 1 unit per page ----------------------------
    if LANE_TRENDING_NOW in lanes and allow_general:
        categories = discovery.get("category_ids") or [""]
        # This lane cannot be filtered to Creative Commons at all (videoLicense
        # is a search.list-only parameter) — under a CC-only policy almost
        # everything it returns will fail the rights gate, so it gets a
        # reduced share of the budget rather than none: still useful for the
        # dashboard's rights-vs-performance diagnostic, never the point of
        # the run under that policy.
        share = 0.25 if policy == POLICY_CREATIVE_COMMONS else 1.0
        lane_budget = max(5, int(budget * share))
        per_category = max(5, lane_budget // max(1, len(categories)))
        for category_id in categories[:5]:
            if len(collected) >= budget:
                break
            records = await client.most_popular(
                region_code=region, category_id=category_id,
                max_results=min(50, per_category))
            _absorb(records, f"{LANE_TRENDING_NOW}:{category_id}")

    # --- search-based lanes ---------------------------------------------------
    if allow_search:
        search_lanes = [l for l in lanes if l != LANE_TRENDING_NOW]
        for lane in search_lanes:
            if len(collected) >= budget:
                break
            if lane == LANE_CHANNEL_WINNERS:
                for channel_id in _channel_winner_seeds(config, db):
                    if len(collected) >= budget:
                        break
                    ids = await client.search_video_ids(
                        "", region_code=region, relevance_language=language,
                        max_results=10, creative_commons=cc_only, order="viewCount",
                        channel_id=channel_id)
                    fresh = [vid for vid in ids if vid not in collected]
                    if not fresh:
                        continue
                    _absorb(await client.hydrate(fresh, discovery_source=f"{lane}:{channel_id}"),
                           f"{lane}:{channel_id}")
                continue

            if lane not in SEARCH_LANES_REQUIRING_TOPICS or not topics:
                continue
            published_after, order = _lane_search_params(lane, now, run_index)
            for topic, query in _queries_for_lane(topics, variants_per_topic, run_index):
                if len(collected) >= budget:
                    break
                ids = await client.search_video_ids(
                    query, region_code=region, relevance_language=language,
                    published_after=published_after, max_results=25,
                    creative_commons=cc_only, order=order)
                fresh = [vid for vid in ids if vid not in collected]
                if not fresh:
                    continue
                _absorb(await client.hydrate(fresh, discovery_source=f"{lane}:{topic[:50]}"),
                       f"{lane}:{topic[:50]}")

    return list(collected.values())[:budget]


def _record_to_source(record: VideoRecord, run_id: str) -> DiscoveredSource:
    return DiscoveredSource(
        youtube_video_id=record.video_id,
        url=record.url,
        channel_id=record.channel_id,
        channel_title=record.channel_title,
        title=record.title,
        description=record.description,
        published_at=iso(record.published_at),
        duration_seconds=record.duration_seconds,
        category_id=record.category_id,
        view_count=record.view_count,
        like_count=record.like_count,
        comment_count=record.comment_count,
        license=record.license,
        definition=record.definition,
        caption_available=record.caption,
        live_state=record.live_state,
        made_for_kids=record.made_for_kids,
        privacy_status=record.privacy_status,
        embeddable=record.embeddable,
        discovery_source=record.discovery_source or LANE_TRENDING_NOW,
        chart_rank=record.chart_rank,
        run_id=run_id,
    )


async def _score(db: AutopilotDB, config: Dict[str, Any], client: Optional[YouTubeClient],
                 records: List[VideoRecord], *, now: datetime, channel_counts: Dict[str, int],
                 known: set) -> List[Tuple[VideoRecord, float, Dict[str, Any]]]:
    """Fetch channel baselines (best-effort) and score one batch of candidates."""
    channel_baselines: Dict[str, float] = {}
    if client is not None and client.configured:
        channel_ids = {r.channel_id for r in records if r.channel_id}
        if channel_ids:
            try:
                channel_baselines = await channel_context.get_channel_baselines(
                    db, client, channel_ids, now=now)
            except (QuotaExhausted, YouTubeError):
                pass  # channel context is an enhancement, never a blocker
    channel_last_used = {}
    for record in records:
        if record.channel_id and record.channel_id not in channel_last_used:
            last = db.channel_last_selected(record.channel_id)
            if last:
                channel_last_used[record.channel_id] = last
    discovery_lanes = {r.video_id: _lane_of(r.discovery_source) for r in records}
    return opportunity.score_candidates(
        records, config, now=now, channel_use_counts=channel_counts, previously_seen=known,
        channel_last_used=channel_last_used, channel_baselines=channel_baselines,
        discovery_lanes=discovery_lanes)


async def evaluate_and_store(db: AutopilotDB, config: Dict[str, Any],
                             records: List[VideoRecord], *, run_id: str,
                             client: Optional[YouTubeClient] = None,
                             now: Optional[datetime] = None) -> DiscoveryResult:
    """Persist candidates with their opportunity score and technical/rights verdict.

    Videos already in the database in a non-FILTERED state are skipped — the
    UNIQUE constraint on ``youtube_video_id`` is the primary dedup guard.
    FILTERED videos are re-evaluated: if the config was relaxed since they were
    first seen, they can graduate to ELIGIBLE without being re-inserted.
    """
    now = now or utcnow()
    known = db.known_video_ids([r.video_id for r in records])
    filterable = db.filtered_video_ids([r.video_id for r in records if r.video_id in known])

    fresh = [r for r in records if r.video_id not in known]
    re_eval = [r for r in records if r.video_id in filterable]
    duplicates = len(records) - len(fresh) - len(re_eval)

    channel_counts = {r.channel_id: db.channel_use_count(r.channel_id)
                      for r in fresh + re_eval if r.channel_id}

    eligible_count = 0
    stored = 0
    reasons: Dict[str, int] = {}
    lane_counts: Dict[str, int] = {}
    cohort_counts: Dict[str, int] = {}
    scores: List[float] = []

    for record, score, breakdown in await _score(db, config, client, fresh, now=now,
                                                  channel_counts=channel_counts, known=known):
        source = _record_to_source(record, run_id)
        source.score = score
        source.score_breakdown = breakdown
        source.discovery_lane = breakdown.get("discovery_lane")
        source.age_cohort = breakdown.get("age_cohort")
        source_id, is_new = db.upsert_source(source)
        if not is_new:
            duplicates += 1
            continue
        stored += 1
        lane_counts[source.discovery_lane or "unknown"] = (
            lane_counts.get(source.discovery_lane or "unknown", 0) + 1)
        cohort_counts[source.age_cohort or "unknown"] = (
            cohort_counts.get(source.age_cohort or "unknown", 0) + 1)
        scores.append(score)

        rights_ok, rights_reason = eligibility.check_rights(record, config.get("rights") or {})
        technical_ok, technical_reason = eligibility.check_eligibility(record, config, now=now)
        ok = rights_ok and technical_ok
        reason = rights_reason if not rights_ok else technical_reason
        db.set_source_score(source_id, score, breakdown, ok, reason,
                            discovery_lane=source.discovery_lane, age_cohort=source.age_cohort,
                            technical_eligible=technical_ok, policy_eligible=rights_ok)
        target = SourceState.ELIGIBLE if ok else SourceState.FILTERED
        db.transition_source(source_id, target, expected=[SourceState.DISCOVERED],
                             rejection_reason=reason, eligible=int(ok))
        if ok:
            eligible_count += 1
        else:
            reasons[reason or "unknown"] = reasons.get(reason or "unknown", 0) + 1
            db.log_event("discovery", f"Rejected: {reason}", level="debug", run_id=run_id,
                         source_id=source_id, youtube_video_id=record.video_id,
                         data={"reason": reason, "title": record.title, "score": score,
                               "lane": source.discovery_lane})

    for record, score, breakdown in await _score(db, config, client, re_eval, now=now,
                                                  channel_counts=channel_counts, known=known):
        source = db.get_source_by_video_id(record.video_id)
        if source is None:
            duplicates += 1
            continue
        lane = breakdown.get("discovery_lane")
        cohort = breakdown.get("age_cohort")
        rights_ok, rights_reason = eligibility.check_rights(record, config.get("rights") or {})
        technical_ok, technical_reason = eligibility.check_eligibility(record, config, now=now)
        ok = rights_ok and technical_ok
        reason = rights_reason if not rights_ok else technical_reason
        db.set_source_score(source.id, score, breakdown, ok, reason,
                            discovery_lane=lane, age_cohort=cohort,
                            technical_eligible=technical_ok, policy_eligible=rights_ok)
        if ok:
            db.transition_source(source.id, SourceState.ELIGIBLE,
                                 expected=[SourceState.FILTERED],
                                 rejection_reason=None, eligible=1)
            eligible_count += 1
        else:
            duplicates += 1
            reasons[reason or "unknown"] = reasons.get(reason or "unknown", 0) + 1

    return DiscoveryResult(
        candidates=len(records), stored=stored, duplicates=duplicates,
        eligible=eligible_count, rejected=stored - eligible_count,
        rejection_reasons=reasons, lane_counts=lane_counts, age_distribution=cohort_counts,
        average_opportunity=round(sum(scores) / len(scores), 1) if scores else 0.0,
        best_opportunity=round(max(scores), 1) if scores else 0.0,
    )


async def run_discovery(db: AutopilotDB, config: Dict[str, Any], client: YouTubeClient, *,
                        run_id: str, now: Optional[datetime] = None) -> DiscoveryResult:
    """One complete discovery pass. Never raises for quota — parks instead."""
    now = now or utcnow()
    db.start_run(run_id, "discovery")

    # Buckets are independent, so an exhausted search allocation must not stop
    # chart discovery — that pool may still have thousands of units.
    allow_search = db.quota_blocked(now, bucket=BUCKET_SEARCH) is None
    allow_general = db.quota_blocked(now, bucket=BUCKET_GENERAL) is None

    run_index = _stable_run_index(run_id)
    lanes = lanes_for_run(config, run_index=run_index)
    runnable = ((LANE_TRENDING_NOW in lanes and allow_general)
                or (any(l != LANE_TRENDING_NOW for l in lanes) and allow_search))
    if not runnable:
        blocked = "search" if any(l != LANE_TRENDING_NOW for l in lanes) and not allow_search \
            else "general"
        db.log_event("discovery",
                     f"Skipped — the {blocked} YouTube quota bucket is exhausted and no "
                     f"enabled lane can run without it",
                     level="warn", run_id=run_id)
        db.finish_run(run_id, "FAILED", {"quota_exhausted": True, "bucket": blocked},
                      "No discovery lane can run within the available quota")
        return DiscoveryResult(candidates=0, stored=0, duplicates=0, eligible=0,
                               rejected=0, quota_exhausted=True, bucket=blocked)

    try:
        records = await fetch_candidates(client, config, now=now,
                                         allow_search=allow_search,
                                         allow_general=allow_general,
                                         run_id=run_id, db=db)
    except QuotaExhausted as exc:
        from .youtube_client import quota_reset_time
        reset = quota_reset_time(now)
        bucket = getattr(exc, "bucket", BUCKET_GENERAL)
        db.mark_quota_exhausted(reset, str(exc), bucket=bucket)
        db.log_event("discovery",
                     f"YouTube {bucket} quota exhausted — that strategy is parked "
                     f"until the reset; the other bucket keeps working",
                     level="warn", run_id=run_id,
                     data={"reason": exc.reason, "bucket": bucket, "until": iso(reset)})
        db.finish_run(run_id, "FAILED", {"quota_exhausted": True, "bucket": bucket}, str(exc))
        return DiscoveryResult(candidates=0, stored=0, duplicates=0, eligible=0,
                               rejected=0, quota_exhausted=True, bucket=bucket)
    except YouTubeError as exc:
        db.log_event("discovery", f"YouTube API error: {exc}", level="error", run_id=run_id)
        db.finish_run(run_id, "FAILED", {}, str(exc))
        raise

    result = await evaluate_and_store(db, config, records, run_id=run_id, client=client, now=now)
    result["lanes_run"] = lanes
    try:
        result["semantic_evaluated"] = await refine_with_semantics(db, config)
    except Exception as exc:  # never let an optional refinement fail discovery
        db.log_event("discovery", f"Semantic refinement skipped: {exc}", level="warn",
                     run_id=run_id)
    db.log_event(
        "discovery",
        f"Discovered {result['candidates']} candidates — "
        f"{result['eligible']} eligible, {result['rejected']} rejected, "
        f"{result['duplicates']} already known",
        run_id=run_id, data=dict(result))
    db.finish_run(run_id, "COMPLETED", dict(result))
    db.update_engine_state(last_discovery_at=iso(now))
    return result


async def refine_with_semantics(db: AutopilotDB, config: Dict[str, Any]) -> int:
    """Optional Gemini shortlist pass: refine the top ELIGIBLE candidates only.

    A no-op whenever no SemanticEvaluatorPort is registered (e.g. in CI) or
    the shortlist size is configured to 0 — semantic scoring is a refinement
    on top of a complete, defensible ranking, never a requirement for one.
    Cached forever per (video id, model version), so an evergreen source that
    resurfaces across runs is never re-scored. Returns how many candidates
    were newly evaluated (for observability/tests).
    """
    evaluator = ports.runtime().semantic_evaluator
    shortlist_size = int((config.get("discovery") or {}).get("semantic_shortlist_size") or 0)
    if evaluator is None or shortlist_size <= 0:
        return 0

    candidates = db.list_sources(states=[SourceState.ELIGIBLE], limit=shortlist_size)
    if not candidates:
        return 0

    cached: Dict[str, Dict[str, Any]] = {}
    to_evaluate = []
    for source in candidates:
        result = db.get_semantic_evaluation(source.youtube_video_id, evaluator.model_version)
        if result is not None:
            cached[source.youtube_video_id] = result
        else:
            to_evaluate.append(source)

    evaluated = 0
    if to_evaluate:
        payload = [{"video_id": s.youtube_video_id, "title": s.title,
                    "description": (s.description or "")[:500]} for s in to_evaluate]
        try:
            results = await evaluator.evaluate(payload)
        except Exception:
            results = {}
        for source in to_evaluate:
            result = results.get(source.youtube_video_id)
            if not isinstance(result, dict):
                continue
            db.save_semantic_evaluation(source.youtube_video_id, evaluator.model_version, result)
            cached[source.youtube_video_id] = result
            evaluated += 1

    weights = ((config.get("ranking") or {}).get("weights") or {})
    semantic_weight = float(weights.get("semantic", 0.0))
    for source in candidates:
        result = cached.get(source.youtube_video_id)
        if not result:
            continue
        overall = max(0.0, min(1.0, float(result.get("overall_score", 0.0))))
        breakdown = dict(source.score_breakdown or {})
        components = dict(breakdown.get("components", {}))
        components["semantic"] = round(overall, 4)
        breakdown["components"] = components
        # A bounded nudge, not a re-normalised re-score: the semantic pass
        # refines an already-complete ranking, it does not replace it.
        new_score = round(min(100.0, source.score + semantic_weight * overall * 50.0), 2)
        breakdown["score"] = new_score
        db.set_source_score(source.id, new_score, breakdown, True, None)
    return evaluated


def _tier_floor(config: Dict[str, Any], tier: str) -> float:
    selection = config.get("selection") or {}
    return float(selection.get({
        TIER_STRICT: "strict_floor", TIER_NORMAL: "normal_floor",
        TIER_EXPLORATION: "minimum_floor",
    }[tier], 0.0))


def pick_next_source(db: AutopilotDB, config: Dict[str, Any], *,
                     now: Optional[datetime] = None,
                     rng=None) -> Tuple[Optional[DiscoveredSource], Optional[str]]:
    """Best available source, via three adaptive opportunity-score tiers.

    STRICT wants a high-confidence pick; if nothing clears that bar, NORMAL
    relaxes the floor; if still nothing, EXPLORATION takes anything above a
    true minimum. What never relaxes across tiers is technical validity or
    rights — those were already enforced before a source ever reached the
    ELIGIBLE state, and this function does not re-derive them. A small
    fraction of picks (``discovery.exploration_rate``) deliberately choose
    from outside the top of the queue even when STRICT would have succeeded,
    so Autopilot does not get stuck exploiting one channel or lane forever.

    Does not itself change any source's state — that is the caller's job
    (see orchestrator._submit_source), so a candidate picked here but not
    actually submitted (daily cap, no clip generator, a racing tick) is never
    left stranded mid-transition.

    Returns ``(source, tier)`` on success or ``(None, reason)`` on failure —
    ``tier`` is one of STRICT/NORMAL/EXPLORATION/EXPLORATION_PICK, worth
    persisting alongside the SELECTED transition for the dashboard's "why did
    it pick this" explanation.
    """
    import random as _random
    now = now or utcnow()
    rng = rng or _random.Random()

    candidates = []
    for source in db.list_sources(states=[SourceState.ELIGIBLE], limit=200):
        if source.next_retry_at:
            due = parse_iso(source.next_retry_at)
            if due and due > now:
                continue
        candidates.append(source)

    cooldown_hours = int((config.get("eligibility") or {}).get("channel_cooldown_hours") or 0)
    denylist = set((config.get("discovery") or {}).get("channel_denylist") or [])

    live: List[DiscoveredSource] = []
    for source in candidates:
        if source.channel_id in denylist:
            db.transition_source(source.id, SourceState.SKIPPED,
                                 expected=[SourceState.ELIGIBLE],
                                 rejection_reason=Reason.CHANNEL_DENIED)
            continue
        if cooldown_hours and source.channel_id:
            last = db.channel_last_selected(source.channel_id)
            if last and last > now - timedelta(hours=cooldown_hours):
                continue  # not skipped: selectable again once the cooldown ends
        live.append(source)

    if not live:
        return None, Reason.NO_ELIGIBLE_SOURCE

    live.sort(key=lambda s: (-s.score, s.id))

    discovery_cfg = config.get("discovery") or {}
    exploration_rate = float(discovery_cfg.get("exploration_rate") or 0.0)
    if discovery_cfg.get("discovery_mode") == "EXPERIMENTAL":
        exploration_rate = min(1.0, exploration_rate + _EXPERIMENTAL_EXPLORATION_BONUS)
    if len(live) > 1 and exploration_rate > 0 and rng.random() < exploration_rate:
        # Explore outside the top pick: a lower-ranked-but-still-eligible
        # candidate, weighted toward the front so exploration still favours
        # reasonable options over the worst of the queue.
        pool = live[1:] or live
        weights = [1.0 / (i + 1) for i in range(len(pool))]
        chosen = rng.choices(pool, weights=weights, k=1)[0]
        return chosen, "EXPLORATION_PICK"

    for tier in (TIER_STRICT, TIER_NORMAL, TIER_EXPLORATION):
        floor = _tier_floor(config, tier)
        for source in live:
            if source.score >= floor:
                return source, tier

    return None, Reason.LOW_OPPORTUNITY


def explain_empty_selection(db: AutopilotDB, config: Dict[str, Any]) -> Dict[str, Any]:
    """Structured "why did nothing get picked" diagnostic for the dashboard.

    Distinguishes a rights bottleneck (policy blocked almost everything) from
    a genuine opportunity shortfall (nothing discovered was good enough) —
    conflating the two is exactly what made the old system undiagnosable.
    """
    recent = db.list_sources(states=[SourceState.DISCOVERED, SourceState.ELIGIBLE,
                                     SourceState.FILTERED, SourceState.SKIPPED], limit=500)
    if not recent:
        return {"bottleneck": "no_candidates", "message": "No candidates discovered yet."}

    total = len(recent)
    policy_blocked = sum(1 for s in recent if not s.policy_eligible)
    technically_invalid = sum(1 for s in recent if s.policy_eligible and not s.technical_eligible)
    opportunity_scores = [s.score for s in recent if s.policy_eligible and s.technical_eligible]
    eligible_now = [s for s in recent if s.state == SourceState.ELIGIBLE]

    if not opportunity_scores:
        if policy_blocked / total > 0.5:
            pct = round(policy_blocked / total * 100)
            return {
                "bottleneck": "rights_policy",
                "message": f"The rights policy blocked {pct}% of recently discovered "
                           f"candidates. Performance filters were not the issue.",
                "policy_blocked": policy_blocked, "total": total,
            }
        return {
            "bottleneck": "technical_validity",
            "message": f"{technically_invalid} of {total} recent candidates were technically "
                       f"unusable (unavailable, wrong shape, or duplicates).",
            "technically_invalid": technically_invalid, "total": total,
        }

    best = max(opportunity_scores)
    minimum_floor = _tier_floor(config, TIER_EXPLORATION)
    if best < minimum_floor:
        return {
            "bottleneck": "low_opportunity",
            "message": f"Highest opportunity score among rights-eligible candidates is "
                       f"{best:.0f}; the exploration floor is {minimum_floor:.0f}. This is a "
                       f"quiet discovery cycle, not a broken one.",
            "rights_eligible": len(opportunity_scores), "best_opportunity": best,
            "minimum_floor": minimum_floor,
        }
    return {
        "bottleneck": "already_selected_or_processing",
        "message": f"{len(eligible_now)} candidates are currently ELIGIBLE and waiting their "
                   f"turn; one heavy job runs at a time.",
        "eligible_now": len(eligible_now),
    }


def quota_blocked_until(db: AutopilotDB, now: Optional[datetime] = None,
                        bucket: str = BUCKET_GENERAL) -> Optional[datetime]:
    """When one YouTube quota bucket frees up, or None if it is usable now."""
    return db.quota_blocked(now or utcnow(), bucket=bucket)
