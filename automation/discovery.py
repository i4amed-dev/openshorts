"""Discovery: find candidate YouTube sources, judge them, persist the verdict.

One pass does four things in order — fetch, deduplicate, filter, score — and
writes every outcome, including the rejections. The rejected rows are the point:
without them the dashboard can only say "nothing was selected", and an operator
tuning filters is guessing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import eligibility, ranking
from .db import AutopilotDB, iso, utcnow
from .models import DiscoveredSource, Reason, SourceState
from .youtube_client import QuotaExhausted, VideoRecord, YouTubeClient, YouTubeError


class DiscoveryResult(dict):
    """Stats for the run row and the dashboard."""


async def fetch_candidates(client: YouTubeClient, config: Dict[str, Any], *,
                           now: Optional[datetime] = None) -> List[VideoRecord]:
    """Run the configured strategies and return de-duplicated candidates.

    Strategy A (``most_popular``) is 1 quota unit per 50 videos and tells us what
    a region is watching right now. Strategy B (``niche_search``) is 100 units
    per topic, so it runs once per topic per discovery run and never per tick.
    """
    now = now or utcnow()
    discovery = config.get("discovery") or {}
    strategies = discovery.get("strategies") or ["most_popular"]
    region = discovery.get("region_code") or "US"
    language = discovery.get("relevance_language") or "en"
    budget = int(discovery.get("max_candidates_per_run") or 50)
    max_age_hours = int((config.get("eligibility") or {}).get("max_age_hours") or 168)

    collected: Dict[str, VideoRecord] = {}

    if "most_popular" in strategies:
        categories = discovery.get("category_ids") or [""]
        per_category = max(5, budget // max(1, len(categories)))
        for category_id in categories[:5]:
            if len(collected) >= budget:
                break
            records = await client.most_popular(
                region_code=region, category_id=category_id,
                max_results=min(50, per_category))
            for record in records:
                collected.setdefault(record.video_id, record)

    if "niche_search" in strategies:
        topics = discovery.get("topics") or []
        published_after = now - timedelta(hours=max_age_hours)
        cc_only = bool(discovery.get("creative_commons_search_only", True))
        # search.list returns ids only, so each topic costs 100 units plus one
        # cheap videos.list batch to hydrate statistics/contentDetails/status.
        for topic in topics[:8]:
            if len(collected) >= budget:
                break
            ids = await client.search_video_ids(
                topic, region_code=region, relevance_language=language,
                published_after=published_after, max_results=25,
                creative_commons=cc_only)
            fresh = [vid for vid in ids if vid not in collected]
            if not fresh:
                continue
            for record in await client.hydrate(fresh,
                                               discovery_source=f"niche_search:{topic[:60]}"):
                collected.setdefault(record.video_id, record)

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
        discovery_source=record.discovery_source or "most_popular",
        chart_rank=record.chart_rank,
        run_id=run_id,
    )


def evaluate_and_store(db: AutopilotDB, config: Dict[str, Any],
                       records: List[VideoRecord], *, run_id: str,
                       now: Optional[datetime] = None) -> DiscoveryResult:
    """Persist candidates with their score, eligibility and rejection reason.

    A video already in the database is never re-evaluated into a fresh pipeline
    run — that is the primary dedup guard, and it is enforced by the UNIQUE
    constraint on ``youtube_video_id`` rather than by this check alone.
    """
    now = now or utcnow()
    known = db.known_video_ids([r.video_id for r in records])

    fresh = [r for r in records if r.video_id not in known]
    duplicates = len(records) - len(fresh)

    channel_counts = {r.channel_id: db.channel_use_count(r.channel_id) for r in fresh
                      if r.channel_id}
    scored = ranking.score_candidates(fresh, config, now=now,
                                      channel_use_counts=channel_counts,
                                      previously_seen=known)

    eligible_count = 0
    stored = 0
    reasons: Dict[str, int] = {}

    for record, score, breakdown in scored:
        source = _record_to_source(record, run_id)
        source.score = score
        source.score_breakdown = breakdown
        source_id, is_new = db.upsert_source(source)
        if not is_new:
            duplicates += 1
            continue
        stored += 1

        channel_last = db.channel_last_selected(record.channel_id) if record.channel_id else None
        ok, reason = eligibility.evaluate(record, config, now=now,
                                          channel_last_used=channel_last)
        db.set_source_score(source_id, score, breakdown, ok, reason)
        target = SourceState.ELIGIBLE if ok else SourceState.FILTERED
        db.transition_source(source_id, target, expected=[SourceState.DISCOVERED],
                             rejection_reason=reason, eligible=int(ok))
        if ok:
            eligible_count += 1
        else:
            reasons[reason or "unknown"] = reasons.get(reason or "unknown", 0) + 1
            db.log_event("discovery", f"Rejected: {reason}", level="debug", run_id=run_id,
                         source_id=source_id, youtube_video_id=record.video_id,
                         data={"reason": reason, "title": record.title, "score": score})

    return DiscoveryResult(
        candidates=len(records), stored=stored, duplicates=duplicates,
        eligible=eligible_count, rejected=stored - eligible_count,
        rejection_reasons=reasons,
    )


async def run_discovery(db: AutopilotDB, config: Dict[str, Any], client: YouTubeClient, *,
                        run_id: str, now: Optional[datetime] = None) -> DiscoveryResult:
    """One complete discovery pass. Never raises for quota — parks instead."""
    now = now or utcnow()
    db.start_run(run_id, "discovery")
    try:
        records = await fetch_candidates(client, config, now=now)
    except QuotaExhausted as exc:
        from .youtube_client import quota_reset_time
        reset = quota_reset_time(now)
        db.mark_quota_exhausted(reset, str(exc))
        db.log_event("discovery", "YouTube quota exhausted — discovery parked until reset",
                     level="warn", run_id=run_id,
                     data={"reason": exc.reason, "until": iso(reset)})
        db.finish_run(run_id, "FAILED", {"quota_exhausted": True}, str(exc))
        return DiscoveryResult(candidates=0, stored=0, duplicates=0, eligible=0,
                               rejected=0, quota_exhausted=True)
    except YouTubeError as exc:
        db.log_event("discovery", f"YouTube API error: {exc}", level="error", run_id=run_id)
        db.finish_run(run_id, "FAILED", {}, str(exc))
        raise

    result = evaluate_and_store(db, config, records, run_id=run_id, now=now)
    db.log_event(
        "discovery",
        f"Discovered {result['candidates']} candidates — "
        f"{result['eligible']} eligible, {result['rejected']} rejected, "
        f"{result['duplicates']} already known",
        run_id=run_id, data=dict(result))
    db.finish_run(run_id, "COMPLETED", dict(result))
    db.update_engine_state(last_discovery_at=iso(now))
    return result


def pick_next_source(db: AutopilotDB, config: Dict[str, Any], *,
                     now: Optional[datetime] = None) -> Tuple[Optional[DiscoveredSource],
                                                              Optional[str]]:
    """Highest-scoring eligible source that still passes the live filters.

    Re-checking at selection time is not redundant: a candidate scored at 03:00
    may since have had its channel used, or aged out of the window. Returns
    ``(source, None)`` or ``(None, reason)``.
    """
    now = now or utcnow()
    retry_ready = []
    for source in db.list_sources(states=[SourceState.ELIGIBLE], limit=100):
        if source.next_retry_at:
            from .db import parse_iso
            due = parse_iso(source.next_retry_at)
            if due and due > now:
                continue
        retry_ready.append(source)

    cooldown_hours = int((config.get("eligibility") or {}).get("channel_cooldown_hours") or 0)
    max_age_hours = float((config.get("eligibility") or {}).get("max_age_hours") or 10 ** 9)
    denylist = set((config.get("discovery") or {}).get("channel_denylist") or [])

    for source in retry_ready:
        if source.channel_id in denylist:
            db.transition_source(source.id, SourceState.SKIPPED,
                                 expected=[SourceState.ELIGIBLE],
                                 rejection_reason=Reason.CHANNEL_DENIED)
            continue
        if cooldown_hours and source.channel_id:
            last = db.channel_last_selected(source.channel_id)
            if last and last > now - timedelta(hours=cooldown_hours):
                continue  # not skipped: it becomes selectable once the cooldown ends
        from .db import parse_iso
        published = parse_iso(source.published_at)
        if published and (now - published).total_seconds() / 3600.0 > max_age_hours:
            db.transition_source(source.id, SourceState.SKIPPED,
                                 expected=[SourceState.ELIGIBLE],
                                 rejection_reason=Reason.TOO_OLD)
            continue
        return source, None

    return None, "no_eligible_source"


def quota_blocked_until(db: AutopilotDB, now: Optional[datetime] = None) -> Optional[datetime]:
    from .db import parse_iso
    now = now or utcnow()
    quota = db.get_quota("youtube")
    until = parse_iso(quota.get("exhausted_until"))
    if until and until > now:
        return until
    if until:
        db.clear_quota_block("youtube")
    return None
