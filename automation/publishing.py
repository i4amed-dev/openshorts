"""Turning finished clips into scheduled posts.

Clip selection reuses the ordering the existing Gemini pass already produced —
``metadata['shorts']`` comes back ranked by predicted performance, so index 0 is
the model's best pick. Running a second AI ranking pass over the same clips
would cost another Gemini call per source to re-derive an ordering we were
handed for free.

Publishing goes through :mod:`publishing_service`, the same code the manual
button calls. Autopilot submits with a future ``scheduled_date`` (exactly as the
Schedule Week modal does) so the vendor holds the post: once submitted, a Mac
that sleeps through the slot cannot make the post miss its time.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import scheduler
from .db import AutopilotDB, iso, parse_iso, utcnow
from .models import (
    ClipState, DiscoveredSource, GeneratedClip, PublishAttempt, PublishState, Reason,
)

# Never hand the vendor a slot that is about to pass while the upload is still
# in flight — it would either post immediately or reject the date.
MIN_LEAD = timedelta(minutes=5)

# How long after a scheduled slot to first ask the vendor what happened. The
# vendor needs a moment to actually run the job; asking at the exact second
# just returns "pending".
SETTLE_DELAY_SECONDS = 120


def vendor_request_id(attempt: PublishAttempt) -> str:
    """Our own tracking id for the vendor, derived from the attempt row.

    Deliberately distinct from :func:`idempotency_key`, which is Klippo's
    internal "this clip may be published once" constraint. This one is the
    vendor's handle for the same attempt, and the two must not be conflated:
    the internal key guards our database, this one asks Upload-Post a question.
    """
    return f"klippo-{attempt.id}-{attempt.idempotency_key[:16]}"


def idempotency_key(job_id: str, clip_index: int, platforms: List[str]) -> str:
    """Stable identity for "this clip, on these platforms".

    Deliberately excludes the slot: re-scheduling a not-yet-submitted post must
    reuse the same row rather than mint a second one, and the key is the DB-level
    guarantee that one clip is never published twice to the same platform set.
    """
    payload = f"v1|{job_id}|{int(clip_index)}|{','.join(sorted(platforms))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def select_clips(db: AutopilotDB, source: DiscoveredSource, clips: List[Dict[str, Any]],
                 config: Dict[str, Any]) -> Tuple[List[GeneratedClip], List[GeneratedClip]]:
    """Persist every generated clip and mark which ones will be posted.

    Returns ``(chosen, skipped)``. Duration bounds are applied before the top-N
    cut so a 12-second fragment never occupies one of the day's few slots.
    """
    clip_config = config.get("clips") or {}
    max_clips = int(clip_config.get("max_clips_per_source", 3))
    min_seconds = float(clip_config.get("min_clip_seconds", 15.0))
    max_seconds = float(clip_config.get("max_clip_seconds", 60.0))
    take_all = str(clip_config.get("selection", "top_n")) == "all"

    chosen: List[GeneratedClip] = []
    skipped: List[GeneratedClip] = []

    for index, raw in enumerate(clips):
        filename = _filename_from(raw)
        start = float(raw.get("start") or 0.0)
        end = float(raw.get("end") or 0.0)
        record = GeneratedClip(
            source_id=source.id or 0,
            job_id=source.job_id or "",
            clip_index=index,
            filename=filename,
            title=(raw.get("video_title_for_youtube_short") or raw.get("title") or "")[:200],
            description=(raw.get("video_description_for_instagram")
                         or raw.get("video_description_for_tiktok") or "")[:2000],
            start_seconds=start,
            end_seconds=end,
            rank=index,
        )
        duration = record.duration
        reason: Optional[str] = None
        if not filename:
            reason = "clip_file_missing"
        elif duration and duration < min_seconds:
            reason = "clip_shorter_than_minimum"
        elif duration and duration > max_seconds:
            reason = "clip_longer_than_maximum"
        elif not take_all and len(chosen) >= max_clips:
            reason = "beyond_max_clips_per_source"

        record.state = ClipState.PENDING if reason is None else ClipState.SKIPPED
        record.skip_reason = reason
        record.id = db.upsert_clip(record)
        # upsert_clip preserves an existing row's state, so re-reconciling a job
        # cannot demote a clip that already has a publish attempt.
        stored = db.get_clip(record.id) or record
        if stored.state == ClipState.SKIPPED:
            skipped.append(stored)
        else:
            chosen.append(stored)

    return chosen, skipped


def _filename_from(raw: Dict[str, Any]) -> str:
    url = raw.get("video_url") or ""
    return url.rsplit("/", 1)[-1] if url else ""


def schedule_clips(db: AutopilotDB, source: DiscoveredSource, clips: List[GeneratedClip],
                   config: Dict[str, Any], *, now: Optional[datetime] = None) -> List[int]:
    """Give each pending clip a publishing slot. Returns the new attempt ids.

    Slots come from :func:`scheduler.allocate_publish_slots`, which already
    honours the daily cap, the minimum spacing and every slot another attempt
    holds. The DB's partial unique indexes are the final word: if two ticks race,
    the loser's insert fails and it simply does nothing.
    """
    now = now or utcnow()
    platforms = list((config.get("publishing") or {}).get("platforms") or [])
    if not platforms or not clips:
        return []

    pending = [clip for clip in clips if clip.state == ClipState.PENDING]
    if not pending:
        return []

    slots = scheduler.allocate_publish_slots(
        config, count=len(pending), now=now, taken=db.taken_slots(now - timedelta(days=1)),
        earliest=now + MIN_LEAD)

    created: List[int] = []
    for clip, slot in zip(pending, slots):
        attempt = PublishAttempt(
            clip_id=clip.id or 0,
            source_id=source.id or 0,
            job_id=clip.job_id,
            clip_index=clip.clip_index,
            idempotency_key=idempotency_key(clip.job_id, clip.clip_index, platforms),
            platforms=platforms,
            scheduled_for_utc=iso(slot),
            timezone=config.get("timezone") or "UTC",
            title=clip.title or "Viral Short",
            description=clip.description or "",
        )
        attempt_id = db.reserve_publish_attempt(attempt)
        if attempt_id is None:
            db.log_event("publishing", "Slot already claimed — clip left pending",
                         level="debug", source_id=source.id, job_id=clip.job_id,
                         data={"clip_index": clip.clip_index, "slot": iso(slot)})
            continue
        db.set_clip_state(clip.id, ClipState.SCHEDULED)
        created.append(attempt_id)
        db.log_event("publishing",
                     f"Clip {clip.clip_index + 1} scheduled for {iso(slot)}",
                     source_id=source.id, job_id=clip.job_id, publish_attempt_id=attempt_id,
                     data={"slot_utc": iso(slot),
                           "slot_local": scheduler.describe_local(slot, config),
                           "platforms": platforms})

    if len(created) < len(pending):
        db.log_event("publishing",
                     f"{len(pending) - len(created)} clip(s) still waiting for a free slot",
                     level="warn", source_id=source.id, job_id=source.job_id)
    return created


def reslot_if_missed(db: AutopilotDB, attempt: PublishAttempt, config: Dict[str, Any], *,
                     now: datetime) -> Optional[datetime]:
    """Apply the catch-up policy to an attempt whose slot passed while we were down.

    Returns the slot to use, or None when the policy says to drop it.
    """
    slot = parse_iso(attempt.scheduled_for_utc)
    if slot is None:
        return None
    if slot > now + MIN_LEAD:
        return slot

    new_slot = scheduler.apply_catch_up(
        config, missed_slot=slot, now=now + MIN_LEAD,
        taken=db.taken_slots(now - timedelta(days=1)))

    if new_slot is None:
        db.set_publish_state(attempt.id, PublishState.CANCELED,
                             error="Missed its slot; catch-up policy is 'skip'")
        db.set_clip_state(attempt.clip_id, ClipState.SKIPPED, "missed_slot_skipped")
        db.log_event("publishing", "Missed slot dropped by catch-up policy", level="warn",
                     source_id=attempt.source_id, job_id=attempt.job_id,
                     publish_attempt_id=attempt.id)
        return None

    db.execute("UPDATE publish_attempt SET scheduled_for_utc = ? WHERE id = ?",
               (iso(new_slot), attempt.id))
    db.log_event("publishing",
                 f"Missed slot {iso(slot)} moved to {iso(new_slot)} (catch-up)",
                 level="warn", source_id=attempt.source_id, job_id=attempt.job_id,
                 publish_attempt_id=attempt.id,
                 data={"missed": iso(slot), "rescheduled": iso(new_slot)})
    return new_slot


async def dispatch_attempt(db: AutopilotDB, attempt: PublishAttempt, config: Dict[str, Any],
                           *, publisher, clip_path_resolver, now: datetime) -> bool:
    """Send one reserved attempt to Upload-Post.

    Returns True when the vendor ACCEPTED the job — which is not the same as
    published. The attempt lands in SUBMITTED and only a later status check can
    move it to PUBLISHED.

    Ordering matters: our ``request_id`` is persisted and the row moves to
    IN_FLIGHT *before* the request leaves. A crash mid-upload is then both
    detectable (IN_FLIGHT) and resolvable (we know what to ask the vendor about).
    """
    from publishing_service import PublishError, PublishUncertain

    clip = db.get_clip(attempt.clip_id)
    if clip is None:
        db.set_publish_state(attempt.id, PublishState.FAILED, error="Clip record vanished")
        return False

    slot = reslot_if_missed(db, attempt, config, now=now)
    if slot is None:
        return False

    path = clip_path_resolver(attempt.job_id, clip.filename)
    if not path:
        limit = int((config.get("limits") or {}).get("max_publish_attempts", 3))
        state = PublishState.FAILED if attempt.retry_count + 1 >= limit else PublishState.PENDING
        db.set_publish_state(attempt.id, state, increment_retry=True,
                             error=f"Clip file not found on disk: {clip.filename}")
        db.log_event("publishing", f"Clip file missing: {clip.filename}", level="error",
                     source_id=attempt.source_id, job_id=attempt.job_id,
                     publish_attempt_id=attempt.id)
        return False

    api_key, profile = publisher.credentials()
    if not api_key or not profile:
        db.log_event("publishing",
                     "Upload-Post credentials are not configured server-side — "
                     "set UPLOAD_POST_API_KEY and choose a profile",
                     level="error", source_id=attempt.source_id, job_id=attempt.job_id,
                     publish_attempt_id=attempt.id)
        db.set_publish_state(attempt.id, PublishState.PENDING,
                             error="Upload-Post credentials missing")
        return False

    # Stable, deterministic, and written down BEFORE the network call — that is
    # what turns "did it post?" into a question the vendor can answer.
    request_id = attempt.vendor_request_id or vendor_request_id(attempt)
    db.record_vendor_ids(attempt.id, request_id=request_id, job_id=None)
    db.set_publish_state(attempt.id, PublishState.IN_FLIGHT,
                         expected=[PublishState.PENDING])

    scheduled_local = slot.astimezone(scheduler.get_zone(attempt.timezone or "UTC"))
    try:
        result = await publisher.publish(
            file_path=path,
            platforms=attempt.platforms,
            user=profile,
            api_key=api_key,
            title=attempt.title or "Viral Short",
            description=attempt.description or "",
            # Upload-Post reads a naive local datetime plus the timezone name.
            scheduled_date=scheduled_local.replace(tzinfo=None, microsecond=0).isoformat(),
            timezone=attempt.timezone or "UTC",
            request_id=request_id,
        )
    except PublishUncertain as exc:
        # Never auto-retried: a blind re-POST could publish the same clip twice.
        # It is not a dead end though — we hold the request_id, so the next tick
        # asks the vendor what happened (see reconcile_attempt).
        db.set_publish_state(
            attempt.id, PublishState.UNCERTAIN, error=str(exc),
            vendor_request_id=request_id,
            next_status_check_at=iso(now + timedelta(seconds=30)))
        db.log_event("publishing",
                     "Upload-Post gave no verdict — marked UNCERTAIN. Not retried "
                     "(that could double-post); the outcome will be looked up instead.",
                     level="warn", source_id=attempt.source_id, job_id=attempt.job_id,
                     publish_attempt_id=attempt.id,
                     data={"request_id": request_id})
        return False
    except PublishError as exc:
        limit = int((config.get("limits") or {}).get("max_publish_attempts", 3))
        retries = attempt.retry_count + 1
        terminal = retries >= limit or not getattr(exc, "retryable", False)
        db.set_publish_state(attempt.id,
                             PublishState.FAILED if terminal else PublishState.PENDING,
                             increment_retry=True, error=str(exc))
        if terminal:
            db.set_clip_state(attempt.clip_id, ClipState.FAILED, "publish_failed")
        db.log_event("publishing", f"Publish rejected: {exc}",
                     level="error" if terminal else "warn",
                     source_id=attempt.source_id, job_id=attempt.job_id,
                     publish_attempt_id=attempt.id,
                     data={"retry_count": retries, "terminal": terminal})
        return False

    # Accepted. The clip stays SCHEDULED — the vendor is holding it, nothing is
    # live yet, and claiming otherwise is the bug this whole pass exists to fix.
    db.set_publish_state(
        attempt.id, PublishState.SUBMITTED,
        vendor_response=_sanitize(result.get("response") if isinstance(result, dict) else None),
        vendor_request_id=result.get("request_id") if isinstance(result, dict) else None,
        vendor_job_id=result.get("job_id") if isinstance(result, dict) else None,
        next_status_check_at=iso(_first_check_time(slot, now)),
        expected=[PublishState.IN_FLIGHT])
    db.set_clip_state(attempt.clip_id, ClipState.SCHEDULED)
    db.log_event("publishing",
                 f"Upload-Post accepted clip {attempt.clip_index + 1} for "
                 f"{scheduler.describe_local(slot, config)} — not published yet",
                 source_id=attempt.source_id, job_id=attempt.job_id,
                 publish_attempt_id=attempt.id,
                 data={"platforms": attempt.platforms, "slot_utc": iso(slot),
                       "job_id": (result or {}).get("job_id"),
                       "request_id": (result or {}).get("request_id")})
    return True


def _sanitize(payload):
    from publishing_service import sanitize_vendor_payload
    return sanitize_vendor_payload(payload) if payload else None


def _first_check_time(slot: Optional[datetime], now: datetime) -> datetime:
    """When to first ask the vendor about a freshly accepted job.

    A post scheduled for Thursday should not be polled every ten seconds until
    Thursday — the vendor's cadence guidance is about an upload in progress, not
    a calendar entry. So we wait until just after the slot, and poll an
    immediate (unscheduled) upload promptly.
    """
    if slot is None or slot <= now:
        return now + timedelta(seconds=SETTLE_DELAY_SECONDS)
    return slot + timedelta(seconds=SETTLE_DELAY_SECONDS)


async def reconcile_attempt(db: AutopilotDB, attempt: PublishAttempt, *,
                            api_key: str, now: datetime) -> Optional[str]:
    """Ask Upload-Post what actually happened to one attempt.

    This is the only path to PUBLISHED. Returns the new state, or None when
    nothing changed.
    """
    from publishing_service import PublishError, get_status, poll_interval_seconds

    tracking_id = attempt.vendor_job_id or attempt.vendor_request_id
    if not tracking_id:
        return None

    try:
        status = await get_status(api_key,
                                  request_id=attempt.vendor_request_id,
                                  job_id=attempt.vendor_job_id)
    except PublishError as exc:
        # A status check failing tells us nothing about the post. Back off and
        # try later rather than inventing an outcome.
        db.set_publish_state(attempt.id, attempt.state, mark_checked=True,
                             next_status_check_at=iso(now + timedelta(minutes=10)))
        db.log_event("publishing", f"Status check failed: {exc}", level="warn",
                     source_id=attempt.source_id, publish_attempt_id=attempt.id)
        return None

    results = [{"platform": r.platform, "status": r.status, "success": r.success,
                "message": r.message, "timestamp": r.timestamp} for r in status.results]

    # --- not found -----------------------------------------------------------
    if status.not_found:
        if attempt.state == PublishState.UNCERTAIN:
            # The ambiguous request never reached the vendor: nothing was posted,
            # so it is safe to send it again.
            db.set_publish_state(
                attempt.id, PublishState.PENDING,
                error="Upload-Post has no record of this request — safe to resend",
                vendor_status=status.status, mark_checked=True, next_status_check_at=None)
            db.log_event("publishing",
                         "Uncertain attempt resolved: the vendor never received it, "
                         "so it has been re-queued",
                         source_id=attempt.source_id, publish_attempt_id=attempt.id)
            return PublishState.PENDING
        # A previously-accepted job that the vendor no longer knows about is not
        # something we can guess at.
        db.set_publish_state(attempt.id, PublishState.UNCERTAIN,
                             error="Upload-Post no longer has this job",
                             vendor_status=status.status, mark_checked=True,
                             next_status_check_at=None)
        return PublishState.UNCERTAIN

    # An UNCERTAIN attempt the vendor DOES know about was received after all.
    base_state = attempt.state
    if base_state == PublishState.UNCERTAIN:
        db.log_event("publishing",
                     f"Uncertain attempt resolved: Upload-Post has it ({status.status})",
                     source_id=attempt.source_id, publish_attempt_id=attempt.id)

    # --- mixed outcome -------------------------------------------------------
    # Checked before the terminal statuses: `completed` means all succeeded and
    # `failed` means all failed, so a mix can only be read off the results.
    if status.is_partial_failure:
        db.set_publish_state(
            attempt.id, PublishState.PARTIAL_FAILED,
            vendor_status=status.status, vendor_results=results,
            vendor_response=status.raw, mark_checked=True, next_status_check_at=None,
            error=(f"Published to {', '.join(status.succeeded_platforms)}; "
                   f"failed on {', '.join(status.failed_platforms)}"))
        db.set_clip_state(attempt.clip_id, ClipState.PARTIAL, "partial_platform_failure")
        db.log_event("publishing",
                     f"Partial publish — live on {', '.join(status.succeeded_platforms)}, "
                     f"failed on {', '.join(status.failed_platforms)}. Not retried: "
                     f"resending would duplicate the successful platforms.",
                     level="error", source_id=attempt.source_id,
                     publish_attempt_id=attempt.id, data={"results": results})
        return PublishState.PARTIAL_FAILED

    # --- terminal ------------------------------------------------------------
    if status.status == "completed":
        db.set_publish_state(attempt.id, PublishState.PUBLISHED,
                             vendor_status=status.status, vendor_results=results,
                             vendor_response=status.raw, mark_checked=True,
                             next_status_check_at=None)
        db.set_clip_state(attempt.clip_id, ClipState.PUBLISHED)
        db.log_event("publishing",
                     f"Published to {', '.join(status.succeeded_platforms) or 'all platforms'}",
                     source_id=attempt.source_id, publish_attempt_id=attempt.id,
                     data={"results": results})
        return PublishState.PUBLISHED

    if status.status == "failed":
        db.set_publish_state(attempt.id, PublishState.FAILED,
                             vendor_status=status.status, vendor_results=results,
                             vendor_response=status.raw, mark_checked=True,
                             next_status_check_at=None,
                             error=status.message or "Upload-Post reported failure")
        db.set_clip_state(attempt.clip_id, ClipState.FAILED, "vendor_failed")
        db.log_event("publishing", f"Upload-Post reported failure: {status.message}",
                     level="error", source_id=attempt.source_id,
                     publish_attempt_id=attempt.id, data={"results": results})
        return PublishState.FAILED

    # --- still working -------------------------------------------------------
    interval = poll_interval_seconds(status.status) or 60
    target = (PublishState.PUBLISHING
              if status.status in ("processing", "in_progress")
              else PublishState.SUBMITTED)
    # A scheduled job that has not executed yet stays 'pending' at the vendor;
    # there is nothing to watch closely until its slot arrives.
    if status.status == "pending" and attempt.scheduled_for_utc:
        slot = parse_iso(attempt.scheduled_for_utc)
        if slot and slot > now:
            interval = max(interval, int((slot - now).total_seconds()) + SETTLE_DELAY_SECONDS)
        target = PublishState.SUBMITTED

    changed = target != base_state
    db.set_publish_state(attempt.id, target, vendor_status=status.status,
                         vendor_results=results or None, mark_checked=True,
                         next_status_check_at=iso(now + timedelta(seconds=interval)))
    if changed and target == PublishState.PUBLISHING:
        db.log_event("publishing", "Upload-Post is publishing this clip now",
                     source_id=attempt.source_id, publish_attempt_id=attempt.id)
    return target if changed else None


def reconcile_in_flight(db: AutopilotDB, *, now: Optional[datetime] = None) -> int:
    """Startup sweep: an IN_FLIGHT row means we died mid-request.

    We cannot know whether the vendor accepted it, so it becomes UNCERTAIN — but
    it is scheduled for a status check rather than left for a human, because we
    persisted our request_id before sending and can simply ask.
    """
    now = now or utcnow()
    stuck = db.list_publish_attempts(states=[PublishState.IN_FLIGHT], limit=200)
    for attempt in stuck:
        db.set_publish_state(
            attempt.id, PublishState.UNCERTAIN,
            error="Backend restarted while the upload was in flight; outcome unknown",
            next_status_check_at=iso(now))
        db.log_event("recovery",
                     "Publish attempt was in flight during a restart — marked UNCERTAIN. "
                     + ("Its outcome will be looked up at the vendor."
                        if attempt.vendor_request_id else
                        "No request id was recorded, so it needs manual review."),
                     level="warn", source_id=attempt.source_id, job_id=attempt.job_id,
                     publish_attempt_id=attempt.id)
    return len(stuck)
