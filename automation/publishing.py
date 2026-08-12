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
    """Send one reserved attempt to Upload-Post. Returns True on success.

    The row moves to IN_FLIGHT *before* the request leaves, so a crash mid-upload
    is recoverable as "unknown outcome" instead of looking like it never started.
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
                     "set UPLOAD_POST_API_KEY and UPLOAD_POST_USER",
                     level="error", source_id=attempt.source_id, job_id=attempt.job_id,
                     publish_attempt_id=attempt.id)
        db.set_publish_state(attempt.id, PublishState.PENDING,
                             error="Upload-Post credentials missing")
        return False

    db.set_publish_state(attempt.id, PublishState.IN_FLIGHT)
    scheduled_local = slot.astimezone(scheduler.get_zone(attempt.timezone or "UTC"))
    try:
        response = await publisher.publish(
            file_path=path,
            platforms=attempt.platforms,
            user=profile,
            api_key=api_key,
            title=attempt.title or "Viral Short",
            description=attempt.description or "",
            # Upload-Post reads a naive local datetime plus the timezone name.
            scheduled_date=scheduled_local.replace(tzinfo=None, microsecond=0).isoformat(),
            timezone=attempt.timezone or "UTC",
        )
    except PublishUncertain as exc:
        # Never auto-retried: the vendor has no idempotency key, so a retry here
        # could publish the same clip twice. A human resolves it from the UI.
        db.set_publish_state(attempt.id, PublishState.UNCERTAIN, error=str(exc))
        db.log_event("publishing",
                     "Upload-Post gave no verdict — left UNCERTAIN, not retried "
                     "(retrying could double-post)",
                     level="error", source_id=attempt.source_id, job_id=attempt.job_id,
                     publish_attempt_id=attempt.id)
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
        db.log_event("publishing", f"Publish failed: {exc}",
                     level="error" if terminal else "warn",
                     source_id=attempt.source_id, job_id=attempt.job_id,
                     publish_attempt_id=attempt.id,
                     data={"retry_count": retries, "terminal": terminal})
        return False

    db.set_publish_state(attempt.id, PublishState.SUBMITTED, vendor_response=response)
    db.set_clip_state(attempt.clip_id, ClipState.PUBLISHED)
    db.log_event("publishing",
                 f"Submitted clip {attempt.clip_index + 1} for "
                 f"{scheduler.describe_local(slot, config)}",
                 source_id=attempt.source_id, job_id=attempt.job_id,
                 publish_attempt_id=attempt.id,
                 data={"platforms": attempt.platforms, "slot_utc": iso(slot)})
    return True


def reconcile_in_flight(db: AutopilotDB) -> int:
    """Startup sweep: an IN_FLIGHT row means we died mid-request.

    We cannot know whether the vendor accepted it. Marking it UNCERTAIN (and
    never retrying automatically) is the only honest option — the alternative
    silently double-posts.
    """
    stuck = db.list_publish_attempts(states=[PublishState.IN_FLIGHT], limit=200)
    for attempt in stuck:
        db.set_publish_state(
            attempt.id, PublishState.UNCERTAIN,
            error="Backend restarted while the upload was in flight; outcome unknown")
        db.log_event("recovery",
                     "Publish attempt was in flight during a restart — marked UNCERTAIN "
                     "for manual review",
                     level="warn", source_id=attempt.source_id, job_id=attempt.job_id,
                     publish_attempt_id=attempt.id)
    return len(stuck)
