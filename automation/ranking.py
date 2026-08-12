"""Deterministic viral ranking for discovered sources.

Sorting by raw view count picks whatever a mega-channel uploaded this week, every
week. What actually predicts a clippable source is *rate of change* — views per
hour while the video is young, plus how much of that audience is reacting — so
each signal is normalised **within the candidate set** and combined with
configurable weights.

Normalising inside the set matters more than the exact formula: absolute views
span six orders of magnitude, so an un-normalised sum is just "views" wearing a
disguise. Every component here lands in 0..1 before it is weighted.

No LLM is involved. These are numbers with known meanings; a language model
would make the result non-reproducible and unexplainable for no gain. Gemini
stays where semantics genuinely help — picking the moments inside the video,
which the existing pipeline already does.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .config import DEFAULT_PENALTIES, DEFAULT_WEIGHTS
from .youtube_client import VideoRecord

_WORD_RE = re.compile(r"[a-z0-9']+")


def _minmax(values: Sequence[float]) -> Callable[[float], float]:
    """Scale into 0..1 across the candidate set.

    A degenerate set (every value identical) maps to 0.5 rather than 0 or 1 — a
    single candidate is neither the best nor the worst of anything, and pinning
    it to an extreme would distort the weighted sum.
    """
    if not values:
        return lambda _v: 0.0
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return lambda _v: 0.5
    span = hi - lo
    return lambda v: max(0.0, min(1.0, (v - lo) / span))


def _tokens(text: str) -> set:
    return set(_WORD_RE.findall((text or "").lower()))


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


def score_candidates(
    records: Sequence[VideoRecord],
    config: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    channel_use_counts: Optional[Dict[str, int]] = None,
    previously_seen: Optional[set] = None,
) -> List[Tuple[VideoRecord, float, Dict[str, Any]]]:
    """Score every candidate; returns ``(record, score, breakdown)`` best first.

    The breakdown is persisted alongside the score so the dashboard can explain
    the choice component by component instead of showing an opaque number.
    """
    if not records:
        return []
    now = now or datetime.now(timezone.utc)
    channel_use_counts = channel_use_counts or {}
    previously_seen = previously_seen or set()

    ranking = config.get("ranking") or {}
    weights = {**DEFAULT_WEIGHTS, **(ranking.get("weights") or {})}
    penalties = {**DEFAULT_PENALTIES, **(ranking.get("penalties") or {})}
    topics = (config.get("discovery") or {}).get("topics") or []
    clips_config = config.get("clips") or {}
    max_age = float((config.get("eligibility") or {}).get("max_age_hours") or 168)

    # Raw signals. Views and comments are log-compressed *before* normalisation:
    # the distribution is heavy-tailed, and min-max on raw counts would give the
    # single biggest video ~1.0 and squash everything else near 0.
    velocities = [r.views_per_hour(now) for r in records]
    log_views = [math.log10(1 + max(0, r.view_count)) for r in records]
    engagements = [r.engagement_rate() for r in records]
    log_comments = [math.log10(1 + max(0, r.comment_count or 0)) for r in records]

    norm_velocity = _minmax(velocities)
    norm_views = _minmax(log_views)
    norm_engagement = _minmax(engagements)
    norm_comments = _minmax(log_comments)

    ranked_positions = [r.chart_rank for r in records if r.chart_rank]
    worst_rank = max(ranked_positions) if ranked_positions else 1

    results: List[Tuple[VideoRecord, float, Dict[str, Any]]] = []
    for record in records:
        age_hours = record.age_hours(now)
        recency = 0.0 if age_hours == float("inf") else max(
            0.0, 1.0 - (age_hours / max(1.0, max_age)))

        chart = 0.0
        if record.chart_rank:
            chart = max(0.0, 1.0 - ((record.chart_rank - 1) / max(1, worst_rank)))

        components = {
            "velocity": norm_velocity(record.views_per_hour(now)),
            "views": norm_views(math.log10(1 + max(0, record.view_count))),
            "engagement": norm_engagement(record.engagement_rate()),
            "comments": norm_comments(math.log10(1 + max(0, record.comment_count or 0))),
            "recency": recency,
            "chart_rank": chart,
            "relevance": relevance_score(record, topics),
            "duration_fit": duration_fit(record, clips_config),
        }

        positive = sum(components[key] * float(weights.get(key, 0.0)) for key in components)

        # Penalties keep one prolific channel from owning the whole queue and
        # de-prioritise candidates we have already looked at and passed over.
        repeats = int(channel_use_counts.get(record.channel_id, 0))
        repeat_penalty = min(3, repeats) * float(penalties.get("channel_repeat", 0.0))
        seen_penalty = (float(penalties.get("previously_seen", 0.0))
                        if record.video_id in previously_seen else 0.0)
        penalty_total = repeat_penalty + seen_penalty

        total_weight = sum(float(weights.get(key, 0.0)) for key in components) or 1.0
        # Report on a 0..100 scale so the dashboard number means something on its
        # own, independent of how the operator tuned the weights.
        raw = (positive / total_weight) - (penalty_total / total_weight)
        score = round(max(0.0, min(1.0, raw)) * 100.0, 2)

        breakdown = {
            "components": {k: round(v, 4) for k, v in components.items()},
            "weights": {k: round(float(weights.get(k, 0.0)), 4) for k in components},
            "contributions": {
                k: round(components[k] * float(weights.get(k, 0.0)) / total_weight * 100.0, 2)
                for k in components
            },
            "penalties": {
                "channel_repeat": round(repeat_penalty / total_weight * 100.0, 2),
                "previously_seen": round(seen_penalty / total_weight * 100.0, 2),
            },
            "signals": {
                "views": record.view_count,
                "likes": record.like_count,
                "comments": record.comment_count,
                "views_per_hour": round(record.views_per_hour(now), 1),
                "engagement_rate": round(record.engagement_rate(), 5),
                "age_hours": None if age_hours == float("inf") else round(age_hours, 1),
                "duration_seconds": record.duration_seconds,
                "chart_rank": record.chart_rank,
            },
            "score": score,
        }
        results.append((record, score, breakdown))

    # Deterministic ordering: score desc, then video id, so an identical
    # candidate set always produces an identical ranking (tests rely on this,
    # and so does "why did it pick that one" reproducibility).
    results.sort(key=lambda item: (-item[1], item[0].video_id))
    return results
