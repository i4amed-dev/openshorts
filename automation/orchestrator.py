"""The Autopilot state machine.

One tick does a bounded amount of work and returns. Nothing here blocks waiting
for a video: a 20-minute source would otherwise freeze discovery, publishing and
every operator action behind it. Progress is made by advancing persisted state,
so the loop is resumable at any point — killing the process between any two
stages loses at most the tick in progress.

The invariant that matters on a single Mac: **at most one heavy pipeline at a
time**, enforced here from Autopilot's own database rather than trusting
``MAX_CONCURRENT_JOBS``. If someone raises that env var, manual jobs may run
concurrently, but Autopilot still refuses to start a second source of its own.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import discovery, publishing, scheduler
from .config import normalise
from .db import AutopilotDB, iso, parse_iso, utcnow
from .models import (
    ClipState, DiscoveredSource, EngineStatus, PublishState, Reason, SourceState,
)
from .ports import JobSnapshot, Runtime
from .youtube_client import YouTubeClient


class Orchestrator:
    """Drives one Autopilot instance. Owns no timers — see automation.service."""

    def __init__(self, db: AutopilotDB, runtime: Runtime,
                 client_factory=None):
        self.db = db
        self.runtime = runtime
        self._client_factory = client_factory or (lambda: YouTubeClient(
            on_units=lambda units, bucket: self.db.add_quota_units(units, bucket=bucket)))

    # --- config ---------------------------------------------------------------

    def config(self) -> Dict[str, Any]:
        return normalise(self.db.load_settings())

    # --- startup reconciliation ----------------------------------------------

    def reconcile_on_start(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Bring persisted state back in line with reality after a restart.

        Runs before the first tick and logs everything it changes. It never
        repeats a completed stage: each repair is a compare-and-set against the
        state the row is actually in.
        """
        report = {"jobs_recovered": 0, "jobs_lost": 0, "in_flight_publishes": 0,
                  "orphan_selections": 0}
        cg = self.runtime.clip_generator

        report["in_flight_publishes"] = publishing.reconcile_in_flight(
            self.db, now=now or utcnow())

        # A source stuck in SELECTED never got a job id — nothing was submitted,
        # so it is safe (and correct) to put it back in the eligible pool.
        for source in self.db.list_sources(states=[SourceState.SELECTED], limit=50):
            if not source.job_id:
                self.db.transition_source(source.id, SourceState.ELIGIBLE,
                                          expected=[SourceState.SELECTED])
                report["orphan_selections"] += 1
                self.db.log_event("recovery",
                                  "Source was selected but never submitted — returned to the "
                                  "eligible queue", level="warn", source_id=source.id,
                                  youtube_video_id=source.youtube_video_id)

        # Sources bound to a job: reattach to whatever the clip queue knows.
        for state in (SourceState.PROCESS_QUEUED, SourceState.PROCESSING):
            for source in self.db.list_sources(states=[state], limit=50):
                if not source.job_id or cg is None:
                    continue
                snapshot = cg.get_job(source.job_id)
                if snapshot.status == "missing":
                    # app.py's own recovery could not find the job or its output.
                    self._fail_processing(source, "Job disappeared during a restart")
                    report["jobs_lost"] += 1
                else:
                    report["jobs_recovered"] += 1
                    self.db.log_event(
                        "recovery",
                        f"Reattached to job {source.job_id} (status: {snapshot.status})",
                        source_id=source.id, job_id=source.job_id,
                        youtube_video_id=source.youtube_video_id)

        # Clips exist but scheduling never happened (crash between the two).
        for source in self.db.list_sources(states=[SourceState.PROCESS_READY], limit=50):
            self.db.log_event("recovery",
                              "Clips were ready but not scheduled — scheduling resumes",
                              source_id=source.id, job_id=source.job_id)

        self.db.log_event("recovery", "Startup reconciliation complete", data=report)
        return report

    # --- the tick -------------------------------------------------------------

    async def tick(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or utcnow()
        config = self.config()
        actions: List[str] = []
        self.db.update_engine_state(last_tick_at=iso(now))

        state = self.db.load_engine_state()
        if not config.get("enabled"):
            self.db.update_engine_state(engine_status=EngineStatus.OFF)
            return {"status": EngineStatus.OFF, "actions": actions}

        if state.get("engine_status") == EngineStatus.PAUSED_ERROR:
            # The circuit breaker is open. Only an explicit resume closes it —
            # auto-resuming is how an unattended system burns credits all night.
            return {"status": EngineStatus.PAUSED_ERROR, "actions": actions,
                    "reason": state.get("paused_reason")}

        # Reconciliation and publishing keep running while paused: pausing means
        # "start no new source", not "abandon work already in flight".
        active = self.db.active_processing_source()
        if active is not None:
            changed = await self._reconcile_active(active, config, now=now)
            if changed:
                actions.append(f"job:{changed}")

        actions.extend(await self._schedule_ready_sources(config, now=now))
        # Ask the vendor about open submissions BEFORE sending anything new:
        # this is the only path to PUBLISHED, and it also resolves an UNCERTAIN
        # attempt before a later stage could act on stale information.
        actions.extend(await self._reconcile_vendor(config, now=now))
        actions.extend(await self._dispatch_publishes(config, now=now))

        paused = bool(state.get("pause_requested"))
        if not paused:
            if scheduler.discovery_is_due(
                    config, now=now,
                    last_discovery_at=parse_iso(state.get("last_discovery_at"))):
                if await self.run_discovery(config, now=now):
                    actions.append("discovery")

            if self.db.active_processing_source() is None:
                if await self._start_next_source(config, now=now):
                    actions.append("submit")

        status = self._current_status(paused=paused)
        self.db.update_engine_state(engine_status=status)
        return {"status": status, "actions": actions}

    def _current_status(self, *, paused: bool) -> str:
        if paused:
            return EngineStatus.PAUSED
        if self.db.active_processing_source() is not None:
            return EngineStatus.RUNNING
        pending = self.db.list_publish_attempts(states=[PublishState.PENDING], limit=1)
        return EngineStatus.RUNNING if pending else EngineStatus.IDLE

    # --- discovery ------------------------------------------------------------

    async def run_discovery(self, config: Optional[Dict[str, Any]] = None, *,
                            now: Optional[datetime] = None,
                            force: bool = False) -> bool:
        config = config or self.config()
        now = now or utcnow()

        # Only skip when BOTH buckets are parked: search exhaustion alone still
        # leaves chart-based discovery perfectly usable.
        from .youtube_client import BUCKET_GENERAL, BUCKET_SEARCH
        if not force:
            search_blocked = self.db.quota_blocked(now, bucket=BUCKET_SEARCH)
            general_blocked = self.db.quota_blocked(now, bucket=BUCKET_GENERAL)
            if search_blocked and general_blocked:
                return False

        client = self._client_factory()
        if not client.configured:
            self.db.log_event("discovery",
                              "YOUTUBE_DATA_API_KEY is not set — discovery cannot run",
                              level="error")
            self._record_failure("YouTube Data API key missing", config)
            return False

        run_id = str(uuid.uuid4())
        try:
            async with client:
                result = await discovery.run_discovery(self.db, config, client,
                                                       run_id=run_id, now=now)
        except Exception as exc:  # network/API failure — bounded by the breaker
            self._record_failure(f"Discovery failed: {exc}", config)
            return False

        if result.get("quota_exhausted"):
            return False
        self._record_success()
        return True

    # --- source selection + submission ---------------------------------------

    async def _start_next_source(self, config: Dict[str, Any], *, now: datetime) -> bool:
        cg = self.runtime.clip_generator
        if cg is None:
            return False

        # Daily source cap, counted in the operator's local day.
        day_start, day_end = scheduler.local_day_bounds(config, now=now)
        used_today = self.db.sources_selected_since(day_start)
        max_sources = int((config.get("schedule") or {}).get("max_sources_per_day", 1))
        if used_today >= max_sources:
            return False

        # Belt and braces on top of the DB check: never start a second heavy
        # pipeline, even if a manual job is already occupying the machine.
        try:
            if cg.active_heavy_jobs() > 0:
                return False
        except Exception:
            pass

        source, _reason = discovery.pick_next_source(self.db, config, now=now)
        if source is None:
            return False

        if not self.db.transition_source(source.id, SourceState.SELECTED,
                                         expected=[SourceState.ELIGIBLE]):
            return False  # another tick took it

        # Quality gate. Autopilot must never enter the interactive confirmation
        # flow the manual UI uses, so a low-quality source is skipped with a
        # reason and the next candidate gets its turn.
        min_height = int((config.get("processing") or {}).get("min_source_height", 0))
        if min_height > 0:
            try:
                probe = await cg.probe_quality(source.watch_url)
            except Exception:
                probe = {}
            max_height = int(probe.get("max_height") or 0)
            if 0 < max_height < min_height:
                self.db.transition_source(source.id, SourceState.SKIPPED,
                                          expected=[SourceState.SELECTED],
                                          rejection_reason=Reason.QUALITY_GATE)
                self.db.log_event("selection",
                                  f"Skipped — only {max_height}p available "
                                  f"(minimum {min_height}p)",
                                  level="warn", source_id=source.id,
                                  youtube_video_id=source.youtube_video_id)
                return False

        rights_policy = str((config.get("rights") or {}).get("policy"))
        attempt_no = source.attempts + 1
        try:
            job_id = await cg.submit_url(
                url=source.watch_url,
                output_format=(config.get("processing") or {}).get("output_format", "vertical"),
                origin="autopilot",
                rights_policy=rights_policy,
                label=source.youtube_video_id,
            )
        except Exception as exc:
            self.db.transition_source(source.id, SourceState.PROCESS_FAILED,
                                      expected=[SourceState.SELECTED],
                                      attempts=attempt_no, last_error=str(exc)[:500])
            self.db.log_event("selection", f"Could not submit source: {exc}", level="error",
                              source_id=source.id, youtube_video_id=source.youtube_video_id)
            self._record_failure(f"Submission failed: {exc}", config)
            self._apply_retry_policy(source.id, attempt_no, config, now=now)
            return False

        if not self.db.claim_source_for_processing(source.id, job_id, rights_policy):
            # Another tick bound a job first. Ours is a duplicate — say so
            # loudly rather than leaving an untracked pipeline running.
            self.db.log_event("selection",
                              f"Duplicate submission detected; job {job_id} is not tracked",
                              level="error", source_id=source.id, job_id=job_id)
            return False

        self.db.execute("UPDATE discovered_source SET attempts = ? WHERE id = ?",
                        (attempt_no, source.id))
        self.db.record_processing_attempt(source.id, job_id, attempt_no)
        self.db.log_event("selection",
                          f"Selected “{source.title}” (score {source.score}) → job {job_id}",
                          source_id=source.id, youtube_video_id=source.youtube_video_id,
                          job_id=job_id,
                          data={"score": source.score, "breakdown": source.score_breakdown,
                                "rights_policy": rights_policy,
                                "channel": source.channel_title})
        return True

    # --- processing reconciliation -------------------------------------------

    async def _reconcile_active(self, source: DiscoveredSource, config: Dict[str, Any], *,
                                now: datetime) -> Optional[str]:
        cg = self.runtime.clip_generator
        if cg is None or not source.job_id:
            return None
        snapshot: JobSnapshot = cg.get_job(source.job_id)

        if snapshot.status == "processing" and source.state != SourceState.PROCESSING:
            self.db.transition_source(source.id, SourceState.PROCESSING,
                                      expected=[SourceState.PROCESS_QUEUED])
            return "processing"

        if snapshot.status == "completed":
            clips = snapshot.clips or []
            self.db.finish_processing_attempt(source.job_id, "COMPLETED")
            if not clips:
                self._fail_processing(source, "The pipeline produced no clips",
                                      reason=Reason.NO_CLIPS)
                return "no_clips"
            moved = self.db.transition_source(
                source.id, SourceState.PROCESS_READY,
                expected=[SourceState.PROCESS_QUEUED, SourceState.PROCESSING])
            if moved:
                self.db.log_event("processing", f"Job finished with {len(clips)} clip(s)",
                                  source_id=source.id, job_id=source.job_id)
                self._record_success()
            return "ready"

        if snapshot.status in ("failed", "missing"):
            self._fail_processing(source, snapshot.error or "Processing failed")
            self._apply_retry_policy(source.id, source.attempts, config, now=now)
            self._record_failure(f"Processing failed: {snapshot.error}", config)
            return "failed"

        return None

    def _fail_processing(self, source: DiscoveredSource, error: str,
                         reason: Optional[str] = None) -> None:
        if source.job_id:
            self.db.finish_processing_attempt(source.job_id, "FAILED", error)
        self.db.transition_source(
            source.id, SourceState.PROCESS_FAILED,
            expected=[SourceState.SELECTED, SourceState.PROCESS_QUEUED,
                      SourceState.PROCESSING],
            last_error=error[:500], rejection_reason=reason)
        self.db.log_event("processing", error, level="error", source_id=source.id,
                          job_id=source.job_id, youtube_video_id=source.youtube_video_id)

    def _apply_retry_policy(self, source_id: int, attempts: int, config: Dict[str, Any], *,
                            now: datetime) -> None:
        """Bounded retries with exponential backoff, then give up on this source."""
        limit = int((config.get("limits") or {}).get("max_process_attempts", 2))
        source = self.db.get_source(source_id)
        if source is None:
            return
        if attempts >= limit:
            self.db.transition_source(source_id, SourceState.FAILED,
                                      expected=[SourceState.PROCESS_FAILED],
                                      last_error=source.last_error)
            self.db.log_event("processing",
                              f"Giving up after {attempts} attempt(s)", level="error",
                              source_id=source_id, youtube_video_id=source.youtube_video_id)
            return
        backoff = timedelta(minutes=min(120, 15 * (2 ** max(0, attempts - 1))))
        self.db.transition_source(source_id, SourceState.ELIGIBLE,
                                  expected=[SourceState.PROCESS_FAILED],
                                  next_retry_at=iso(now + backoff), job_id=None)
        self.db.log_event("processing",
                          f"Retry {attempts + 1}/{limit} scheduled for {iso(now + backoff)}",
                          level="warn", source_id=source_id)

    # --- clip selection + scheduling -----------------------------------------

    async def _schedule_ready_sources(self, config: Dict[str, Any], *,
                                      now: datetime) -> List[str]:
        cg = self.runtime.clip_generator
        actions: List[str] = []
        if cg is None:
            return actions
        for source in self.db.list_sources(states=[SourceState.PROCESS_READY], limit=10):
            if not source.job_id:
                continue
            snapshot = cg.get_job(source.job_id)
            clips = snapshot.clips or []
            if not clips:
                self.db.transition_source(source.id, SourceState.FAILED,
                                          expected=[SourceState.PROCESS_READY],
                                          rejection_reason=Reason.NO_CLIPS)
                continue
            chosen, skipped = publishing.select_clips(self.db, source, clips, config)
            if not chosen:
                self.db.transition_source(source.id, SourceState.DONE,
                                          expected=[SourceState.PROCESS_READY],
                                          rejection_reason="no_clip_met_the_duration_rules")
                self.db.log_event("publishing",
                                  f"No clip met the duration rules "
                                  f"({len(skipped)} skipped)", level="warn",
                                  source_id=source.id, job_id=source.job_id)
                continue
            created = publishing.schedule_clips(self.db, source, chosen, config, now=now)
            still_pending = [c for c in self.db.list_clips(source_id=source.id,
                                                           states=[ClipState.PENDING])]
            if not still_pending:
                self.db.transition_source(source.id, SourceState.CLIPS_SCHEDULED,
                                          expected=[SourceState.PROCESS_READY])
                actions.append(f"scheduled:{len(created)}")
        return actions

    # --- publishing -----------------------------------------------------------

    async def _dispatch_publishes(self, config: Dict[str, Any], *,
                                  now: datetime) -> List[str]:
        publisher = self.runtime.publisher
        cg = self.runtime.clip_generator
        actions: List[str] = []
        if publisher is None or cg is None:
            return actions

        pending = self.db.list_publish_attempts(states=[PublishState.PENDING], limit=10)
        for attempt in pending:
            ok = await publishing.dispatch_attempt(
                self.db, attempt, config, publisher=publisher,
                clip_path_resolver=cg.clip_path, now=now)
            if ok:
                actions.append(f"published:{attempt.id}")
                self._record_success()
            else:
                refreshed = self.db.get_publish_attempt(attempt.id)
                if refreshed and refreshed.state in (PublishState.FAILED,
                                                     PublishState.UNCERTAIN):
                    self._record_failure("Publish failed", config)

        # A source is done only once every clip has genuinely settled. A clip
        # sitting in SCHEDULED is one the vendor is still holding — calling the
        # source complete there is what made "published" a lie in v1.
        for source in self.db.list_sources(states=[SourceState.CLIPS_SCHEDULED], limit=20):
            clips = self.db.list_clips(source_id=source.id)
            if clips and all(c.state in ClipState.SETTLED for c in clips):
                self.db.transition_source(source.id, SourceState.DONE,
                                          expected=[SourceState.CLIPS_SCHEDULED])
                published = sum(1 for c in clips if c.state == ClipState.PUBLISHED)
                self.db.log_event(
                    "publishing",
                    f"Source complete — {published}/{len(clips)} clip(s) confirmed published",
                    source_id=source.id, job_id=source.job_id)
        return actions

    async def _reconcile_vendor(self, config: Dict[str, Any], *,
                                now: datetime) -> List[str]:
        """Poll Upload-Post for attempts whose outcome is still open.

        Cadence comes from the persisted ``next_status_check_at``, so a restart
        resumes the vendor's recommended intervals instead of re-polling
        everything at once.
        """
        publisher = self.runtime.publisher
        actions: List[str] = []
        if publisher is None:
            return actions
        api_key, _profile = publisher.credentials()
        if not api_key:
            return actions

        from . import publishing
        due = self.db.attempts_due_for_status_check(now, limit=8)
        for attempt in due:
            try:
                new_state = await publishing.reconcile_attempt(
                    self.db, attempt, api_key=api_key, now=now)
            except Exception as exc:
                self.db.log_event("publishing", f"Reconciliation error: {exc}",
                                  level="warn", source_id=attempt.source_id,
                                  publish_attempt_id=attempt.id)
                continue
            if new_state:
                actions.append(f"reconciled:{attempt.id}:{new_state}")
        return actions

    # --- circuit breaker ------------------------------------------------------

    def _record_success(self) -> None:
        state = self.db.load_engine_state()
        if int(state.get("consecutive_failures") or 0):
            self.db.update_engine_state(consecutive_failures=0)

    def _record_failure(self, message: str, config: Dict[str, Any]) -> None:
        """Count consecutive failures and trip the breaker at the configured limit.

        Unattended software that retries forever is how an API bill arrives. The
        breaker latches: only an explicit Resume from the dashboard clears it.
        """
        state = self.db.load_engine_state()
        failures = int(state.get("consecutive_failures") or 0) + 1
        limit = int((config.get("limits") or {}).get("consecutive_failure_limit", 5))
        if failures >= limit:
            self.db.update_engine_state(
                consecutive_failures=failures,
                engine_status=EngineStatus.PAUSED_ERROR,
                paused_reason=f"{failures} consecutive failures. Last: {message[:300]}")
            self.db.log_event("engine",
                              f"Circuit breaker tripped after {failures} consecutive "
                              f"failures — Autopilot paused", level="error",
                              data={"last_error": message[:500]})
        else:
            self.db.update_engine_state(consecutive_failures=failures)
