"""Opportunity scoring: which discovered videos are the best Shorts material.

Sorting by raw view count picks whatever a mega-channel uploaded this week,
every week, and a hard age cutoff throws away genuinely excellent evergreen
sources for no better reason than a clock. Neither answers the question that
actually matters: *which available source gives us the best chance of a Short
people watch, like, share, follow and come back for?*

That question needs different evidence depending on how old the source is. A
video published two hours ago has no "proven demand" yet — the only honest
signal is how fast it is moving right now. A video published three years ago
has no meaningful "momentum" — total views divided by three years is a
lifetime average, not a pulse, and treating it as one is the single biggest
analytical error a naive ranking makes (it makes an 18M-view evergreen video
score *worse* than a video half a day old with 40K views, which is backwards).
So candidates are grouped into age cohorts, several signals are computed
per-cohort rather than globally, and the weight each signal carries shifts by
cohort: momentum matters most for fresh videos and fades toward neutral for
old ones, while proven-demand and evergreen-strength matter most for old
videos and stay modest for fresh ones that haven't had time to prove anything
yet.

No LLM is required for any of this — every signal here is a number with a
known meaning, reproducible and explainable. An optional, cached, shortlist-
only semantic pass (see the ``semantic_scores`` parameter) can refine the
result for the handful of candidates that make it to the final ranking, but
the system produces a complete, defensible ranking without it.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .config import DEFAULT_PENALTIES, DEFAULT_WEIGHTS
from .youtube_client import VideoRecord

_WORD_RE = re.compile(r"[a-z0-9']+")

# --- age cohorts ----------------------------------------------------------

ULTRA_FRESH = "ULTRA_FRESH"
FRESH = "FRESH"
RISING = "RISING"
RECENT = "RECENT"
ESTABLISHED = "ESTABLISHED"
EVERGREEN = "EVERGREEN"
ARCHIVE = "ARCHIVE"

# Upper bound in hours for each cohort, checked in order. A video older than
# every bound (or with an unknown publish date) falls into ARCHIVE.
_AGE_COHORT_BOUNDS: List[Tuple[str, float]] = [
    (ULTRA_FRESH, 6.0),
    (FRESH, 24.0),
    (RISING, 24.0 * 7),
    (RECENT, 24.0 * 30),
    (ESTABLISHED, 24.0 * 365),
    (EVERGREEN, 24.0 * 365 * 5),
]

# Cohorts where "views since publish, divided by hours since publish" is a
# meaningful pulse rather than a lifetime average.
_MOMENTUM_COHORTS = frozenset({ULTRA_FRESH, FRESH, RISING, RECENT})

# Per-cohort multipliers applied to the *base* ranking weight for a handful of
# signals whose relevance genuinely changes with age. Everything not listed
# keeps a multiplier of 1.0. This is what lets a three-year-old evergreen
# winner outrank a mediocre three-hour-old upload, and a genuinely exploding
# three-hour-old upload outrank a mediocre evergreen video: age itself never
# decides, but which evidence counts most does.
_COHORT_WEIGHT_MULTIPLIERS: Dict[str, Dict[str, float]] = {
    ULTRA_FRESH: {"trend_momentum": 1.3, "proven_demand": 0.35, "evergreen_strength": 0.25},
    FRESH: {"trend_momentum": 1.3, "proven_demand": 0.45, "evergreen_strength": 0.30},
    RISING: {"trend_momentum": 1.2, "proven_demand": 0.60, "evergreen_strength": 0.40},
    RECENT: {"trend_momentum": 1.0, "proven_demand": 0.80, "evergreen_strength": 0.60},
    ESTABLISHED: {"trend_momentum": 0.6, "proven_demand": 1.10, "evergreen_strength": 1.00},
    EVERGREEN: {"trend_momentum": 0.3, "proven_demand": 1.30, "evergreen_strength": 1.30},
    ARCHIVE: {"trend_momentum": 0.15, "proven_demand": 1.30, "evergreen_strength": 1.30},
}

# Bayesian smoothing prior for engagement rate: a video needs roughly this
# many views before its own numbers outweigh the prior. Below that, the
# estimate is pulled toward PRIOR_ENGAGEMENT_RATE — a 1,000-view video with a
# suspiciously perfect ratio cannot out-rank a proven million-view crowd.
PRIOR_VIEWS = 5000.0
PRIOR_ENGAGEMENT_RATE = 0.02

_EVERGREEN_KEYWORDS = (
    "explained", "explains", "tutorial", "how to", "history of", "psychology",
    "science of", "finance", "money", "investing", "interview", "podcast",
    "story", "true story", "life lessons", "self improvement", "self-improvement",
    "philosophy", "motivation", "facts about", "the truth about", "debate",
    "documentary", "biography", "lecture", "masterclass", "guide to",
)

_SHORTS_HOOK_KEYWORDS = (
    "how to", "why", "secret", "mistake", "truth", "vs", "reacts", "reaction",
    "interview", "debate", "story", "top", "best", "worst", "never", "always",
    "before and after", "what happened",
)


def age_cohort(age_hours: float) -> str:
    """Classify a video's age into a cohort. Not a quality ranking."""
    if age_hours is None or age_hours == float("inf") or age_hours < 0:
        return ARCHIVE
    for cohort, bound in _AGE_COHORT_BOUNDS:
        if age_hours <= bound:
            return cohort
    return ARCHIVE


def _minmax(values: Sequence[float]) -> Callable[[float], float]:
    """Scale into 0..1 across a set of values.

    A degenerate set (every value identical, or empty) maps to 0.5 — a lone
    candidate is neither the best nor the worst of anything, and pinning it to
    an extreme would distort the weighted sum.
    """
    if not values:
        return lambda _v: 0.5
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return lambda _v: 0.5
    span = hi - lo
    return lambda v: max(0.0, min(1.0, (v - lo) / span))


def _cohort_normalisers(items: Sequence[Tuple[str, float]]) -> Dict[str, Callable[[float], float]]:
    """One ``_minmax`` per cohort, fit only on that cohort's own values.

    Comparing a 5-hour-old video's velocity against other 5-hour-old videos is
    meaningful; comparing it against a 5-year-old video's lifetime-average
    velocity is not (see the module docstring). Each cohort gets its own
    normaliser so "fast for its age" is judged against genuine peers.
    """
    grouped: Dict[str, List[float]] = {}
    for cohort, value in items:
        grouped.setdefault(cohort, []).append(value)
    return {cohort: _minmax(values) for cohort, values in grouped.items()}


def _tokens(text: str) -> set:
    return set(_WORD_RE.findall((text or "").lower()))


def _keyword_hits(haystack: str, keywords: Sequence[str]) -> int:
    return sum(1 for kw in keywords if kw in haystack)


def relevance_score(record: VideoRecord, topics: Sequence[str]) -> float:
    """Fraction of configured topics the title/description actually matches.

    Phrase topics ("f1 highlights") must appear as a phrase; single words match
    on token identity so "cooking" doesn't fire on "precooked".
    """
    if not topics:
        return 0.0
    haystack = f"{record.title}\n{record.description}".lower()
    words = _tokens(haystack)
    hits = 0
    for topic in topics:
        topic = (topic or "").strip().lower()
        if not topic:
            continue
        if " " in topic:
            if topic in haystack:
                hits += 1
        elif topic in words:
            hits += 1
    return hits / len(topics)


def duration_fit(record: VideoRecord, clips_config: Dict[str, Any]) -> float:
    """How comfortably this source can yield the configured number of clips.

    A 6-minute video asked for 3×60s clips is tight; anything past ~8× the clip
    budget is a long-tail VOD where the good moments are sparse. Peak in the
    middle, taper both ways.
    """
    wanted = max(1, int(clips_config.get("max_clips_per_source", 3)))
    clip_len = float(clips_config.get("max_clip_seconds", 60.0))
    ideal_low = wanted * clip_len * 2.0
    ideal_high = wanted * clip_len * 8.0
    duration = float(record.duration_seconds or 0)
    if duration <= 0:
        return 0.0
    if ideal_low <= duration <= ideal_high:
        return 1.0
    if duration < ideal_low:
        return max(0.0, duration / ideal_low)
    # Long sources still work, they just cost more compute per usable second.
    return max(0.0, min(1.0, ideal_high / duration))


def _shorts_hook_heuristic(record: VideoRecord) -> float:
    """Deterministic proxy for "does this look like it contains a standalone
    moment" — question hooks, list/ranking formats, conflict/reaction framing.
    Refined by an optional semantic pass for the final shortlist only.
    """
    haystack = f"{record.title}\n{record.description}".lower()
    hits = _keyword_hits(haystack, _SHORTS_HOOK_KEYWORDS)
    has_digit = any(ch.isdigit() for ch in record.title)
    has_question = "?" in record.title
    score = min(1.0, hits / 3.0) * 0.7 + (0.15 if has_digit else 0.0) + (0.15 if has_question else 0.0)
    return max(0.0, min(1.0, score))


def bayesian_engagement_rate(likes: int, comments: int, views: int, *,
                             prior_views: float = PRIOR_VIEWS,
                             prior_rate: float = PRIOR_ENGAGEMENT_RATE) -> float:
    """Smoothed (likes+comments)/views, resistant to tiny-sample gaming.

    A 1,000-view video with a suspicious 38% engagement rate is mostly noise;
    a 10,000,000-view video with a 5% rate has actually demonstrated it. The
    prior pulls small samples toward a typical rate and lets large samples
    speak almost entirely for themselves.
    """
    views = max(0, int(views or 0))
    positive = max(0, int(likes or 0)) + max(0, int(comments or 0))
    return (positive + prior_views * prior_rate) / (views + prior_views)


def evergreen_strength(record: VideoRecord, age_hours: float,
                       engagement_quality: float) -> float:
    """Heuristic: timeless topic + age to have proven it + sustained reaction.

    A video can *look* evergreen-shaped from its title before it has any age
    to prove it (a fresh "how the economy actually works" upload); the age
    factor is what keeps a freshly-uploaded video from claiming full evergreen
    credit it hasn't earned yet — the cohort weight multiplier does the rest.
    """
    haystack = f"{record.title}\n{record.description}".lower()
    topic_hit = min(1.0, _keyword_hits(haystack, _EVERGREEN_KEYWORDS) / 2.0)
    age_factor = 0.0
    if age_hours and age_hours != float("inf") and age_hours > 24 * 30:
        age_factor = min(1.0, (age_hours - 24 * 30) / (24 * 365))
    return max(0.0, min(1.0, 0.4 * topic_hit + 0.3 * age_factor + 0.3 * engagement_quality))


def channel_outperformance(view_count: int, baseline: Optional[float]) -> float:
    """Candidate views vs. this channel's own typical reach, log-compressed.

    ``baseline`` is a per-channel average (see automation.channel_context);
    ``None`` means no data was available and the caller should treat this as
    unknown rather than bad — a missing signal is not a low one.
    """
    if not baseline or baseline <= 0:
        return 0.5
    ratio = max(0.0, view_count) / baseline
    return math.log10(1.0 + max(0.0, ratio)) / math.log10(1.0 + 50.0)  # 50x baseline ≈ 1.0


def score_candidates(
    records: Sequence[VideoRecord],
    config: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    channel_use_counts: Optional[Dict[str, int]] = None,
    previously_seen: Optional[set] = None,
    channel_last_used: Optional[Dict[str, datetime]] = None,
    channel_baselines: Optional[Dict[str, float]] = None,
    semantic_scores: Optional[Dict[str, Dict[str, float]]] = None,
    discovery_lanes: Optional[Dict[str, str]] = None,
) -> List[Tuple[VideoRecord, float, Dict[str, Any]]]:
    """Score every candidate; returns ``(record, score, breakdown)`` best first.

    The breakdown is persisted alongside the score so the dashboard can explain
    the choice component by component instead of showing an opaque number, and
    so an operator can see *why* a candidate scored the way it did.
    """
    if not records:
        return []
    now = now or datetime.now(timezone.utc)
    channel_use_counts = channel_use_counts or {}
    previously_seen = previously_seen or set()
    channel_last_used = channel_last_used or {}
    channel_baselines = channel_baselines or {}
    semantic_scores = semantic_scores or {}
    discovery_lanes = discovery_lanes or {}

    ranking = config.get("ranking") or {}
    weights = {**DEFAULT_WEIGHTS, **(ranking.get("weights") or {})}
    penalties = {**DEFAULT_PENALTIES, **(ranking.get("penalties") or {})}
    topics = (config.get("discovery") or {}).get("topics") or []
    clips_config = config.get("clips") or {}
    cooldown_hours = float((config.get("eligibility") or {}).get("channel_cooldown_hours") or 0)

    ages = {r.video_id: r.age_hours(now) for r in records}
    cohorts = {r.video_id: age_cohort(ages[r.video_id]) for r in records}

    # Momentum is only computed from raw velocity for cohorts where "views
    # since publish / hours since publish" is a pulse rather than a lifetime
    # average; everything else is neutral (0.5) before normalisation, and its
    # weight is separately faded via the cohort multiplier.
    momentum_inputs = [
        (cohorts[r.video_id], r.views_per_hour(now))
        for r in records if cohorts[r.video_id] in _MOMENTUM_COHORTS
    ]
    momentum_norms = _cohort_normalisers(momentum_inputs)

    log_views = [math.log10(1 + max(0, r.view_count)) for r in records]
    log_comments = [math.log10(1 + max(0, r.comment_count or 0)) for r in records]
    norm_views = _minmax(log_views)
    norm_comments = _minmax(log_comments)

    ranked_positions = [r.chart_rank for r in records if r.chart_rank]
    worst_rank = max(ranked_positions) if ranked_positions else 1

    title_tokens = {r.video_id: _tokens(r.title) for r in records}

    def _max_title_similarity(record: VideoRecord) -> float:
        """Highest Jaccard similarity against any other candidate this run.

        Two near-identical titles ("5 productivity hacks" / "5 Productivity
        Hacks!") are very likely the same underlying moment reposted or
        re-uploaded — a diversity concern, not a duplicate-id concern (that is
        already handled by the DB's unique constraint on video id).
        """
        mine = title_tokens[record.video_id]
        if not mine:
            return 0.0
        best = 0.0
        for other in records:
            if other.video_id == record.video_id:
                continue
            theirs = title_tokens[other.video_id]
            if not theirs:
                continue
            union = mine | theirs
            if not union:
                continue
            similarity = len(mine & theirs) / len(union)
            best = max(best, similarity)
        return best

    results: List[Tuple[VideoRecord, float, Dict[str, Any]]] = []
    for record in records:
        age_hours = ages[record.video_id]
        cohort = cohorts[record.video_id]
        mult = _COHORT_WEIGHT_MULTIPLIERS.get(cohort, {})

        if cohort in _MOMENTUM_COHORTS:
            trend_momentum = momentum_norms[cohort](record.views_per_hour(now))
        else:
            trend_momentum = 0.5  # neutral: not a meaningful signal at this age
        if record.chart_rank:
            chart_component = max(0.0, 1.0 - ((record.chart_rank - 1) / max(1, worst_rank)))
            trend_momentum = max(0.0, min(1.0, 0.7 * trend_momentum + 0.3 * chart_component))

        engagement_quality = bayesian_engagement_rate(
            record.like_count or 0, record.comment_count or 0, record.view_count)
        # bayesian_engagement_rate already lands close to 0..1 for realistic
        # inputs (prior_rate is small), but clamp defensively.
        engagement_quality = max(0.0, min(1.0, engagement_quality))

        proven_demand = max(0.0, min(1.0,
            0.5 * norm_views(math.log10(1 + max(0, record.view_count)))
            + 0.3 * engagement_quality
            + 0.2 * norm_comments(math.log10(1 + max(0, record.comment_count or 0)))))

        outperformance = channel_outperformance(record.view_count,
                                                channel_baselines.get(record.channel_id))
        content_relevance = relevance_score(record, topics)
        shape_fit = duration_fit(record, clips_config)
        hook_heuristic = _shorts_hook_heuristic(record)
        semantic = semantic_scores.get(record.video_id) or {}
        semantic_clip = semantic.get("clipability_score")
        if semantic_clip is not None:
            shorts_suitability = max(0.0, min(1.0,
                0.55 * shape_fit + 0.25 * hook_heuristic + 0.20 * float(semantic_clip)))
        else:
            shorts_suitability = max(0.0, min(1.0, 0.7 * shape_fit + 0.3 * hook_heuristic))

        evergreen = evergreen_strength(record, age_hours, engagement_quality)
        conversion_proxy = max(0.0, min(1.0,
            0.30 * engagement_quality + 0.25 * content_relevance
            + 0.25 * outperformance + 0.20 * evergreen))

        components: Dict[str, float] = {
            "trend_momentum": trend_momentum,
            "engagement_quality": engagement_quality,
            "proven_demand": proven_demand,
            "channel_outperformance": outperformance,
            "content_relevance": content_relevance,
            "shorts_suitability": shorts_suitability,
            "evergreen_strength": evergreen,
            "conversion_proxy": conversion_proxy,
        }
        semantic_overall = semantic.get("overall_score")
        if semantic_overall is not None:
            components["semantic"] = max(0.0, min(1.0, float(semantic_overall)))

        effective_weights = {
            key: float(weights.get(key, 0.0)) * mult.get(key, 1.0) for key in components
        }
        total_weight = sum(effective_weights.values()) or 1.0
        positive = sum(components[k] * effective_weights[k] for k in components)

        # --- penalties -------------------------------------------------------
        repeats = int(channel_use_counts.get(record.channel_id, 0))
        repeat_penalty = min(3, repeats) * float(penalties.get("channel_repeat", 0.0))
        seen_penalty = (float(penalties.get("previously_seen", 0.0))
                        if record.video_id in previously_seen else 0.0)
        recent_penalty = 0.0
        last_used = channel_last_used.get(record.channel_id)
        if cooldown_hours > 0 and last_used is not None:
            elapsed = max(0.0, (now - last_used).total_seconds() / 3600.0)
            if elapsed < cooldown_hours:
                recent_penalty = (1.0 - elapsed / cooldown_hours) * float(
                    penalties.get("channel_recent", 0.0))
        similarity = _max_title_similarity(record) if len(records) > 1 else 0.0
        duplicate_penalty = (similarity * float(penalties.get("near_duplicate_title", 0.0))
                             if similarity > 0.6 else 0.0)
        penalty_total = repeat_penalty + seen_penalty + recent_penalty + duplicate_penalty

        raw = (positive / total_weight) - (penalty_total / total_weight)
        score = round(max(0.0, min(1.0, raw)) * 100.0, 2)

        breakdown = {
            "components": {k: round(v, 4) for k, v in components.items()},
            "weights": {k: round(effective_weights[k], 4) for k in components},
            "contributions": {
                k: round(components[k] * effective_weights[k] / total_weight * 100.0, 2)
                for k in components
            },
            "penalties": {
                "channel_repeat": round(repeat_penalty / total_weight * 100.0, 2),
                "previously_seen": round(seen_penalty / total_weight * 100.0, 2),
                "channel_recent": round(recent_penalty / total_weight * 100.0, 2),
                "near_duplicate_title": round(duplicate_penalty / total_weight * 100.0, 2),
            },
            "signals": {
                "views": record.view_count,
                "likes": record.like_count,
                "comments": record.comment_count,
                "views_per_hour": round(record.views_per_hour(now), 1),
                "early_lifetime_velocity": (
                    round(record.views_per_hour(now), 1) if cohort in _MOMENTUM_COHORTS else None),
                "engagement_rate_raw": round(record.engagement_rate(), 5),
                "age_hours": None if age_hours == float("inf") else round(age_hours, 1),
                "duration_seconds": record.duration_seconds,
                "chart_rank": record.chart_rank,
                "channel_baseline": channel_baselines.get(record.channel_id),
            },
            "age_cohort": cohort,
            "discovery_lane": discovery_lanes.get(record.video_id, record.discovery_source),
            "score": score,
        }
        results.append((record, score, breakdown))

    # Deterministic ordering: score desc, then video id, so an identical
    # candidate set always produces an identical ranking (tests rely on this,
    # and so does "why did it pick that one" reproducibility).
    results.sort(key=lambda item: (-item[1], item[0].video_id))
    return results
