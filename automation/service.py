"""Autopilot lifecycle: the loop, the lease, and the view the dashboard reads.

The loop is a single asyncio task in the backend process. It does not need a
worker framework: a tick is a handful of SQLite statements plus, occasionally,
one API call. The expensive work already has a home — the existing Klippo job
queue — and Autopilot's job is to decide *what* to put in it, not to do it.

Two processes must never both drive the state machine (two containers, a stray
`uvicorn --reload` worker, an ops script). A DB-backed lease with an expiry
settles that: a crashed holder stops renewing and the lease is taken over one
TTL later, where a process-global boolean would have deadlocked forever.
"""
from __future__ import annotations

import asyncio
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import discovery, scheduler
from .config import ConfigError, normalise
from .db import AutopilotDB, iso, parse_iso, utcnow
from .models import ClipState, EngineStatus, PublishState, SourceState
from .orchestrator import Orchestrator
from .ports import runtime as get_runtime

TICK_SECONDS = int(os.environ.get("AUTOPILOT_TICK_SECONDS", "30"))
LEASE_TTL_SECONDS = int(os.environ.get("AUTOPILOT_LEASE_TTL", "120"))


class AutopilotService:
    """Owns the database handle, the loop task and the operator actions."""

    def __init__(self, db: Optional[AutopilotDB] = None, *, db_path: Optional[str] = None):
        self.db = db or AutopilotDB(db_path)
        self.holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._orchestrator: Optional[Orchestrator] = None
        self._has_lease = False
        self._last_error: Optional[str] = None

    # --- lifecycle ------------------------------------------------------------

    def open(self) -> "AutopilotService":
        self.db.connect()
        if self.db.load_settings() is None:
            self.db.save_settings(normalise(None))
        self._orchestrator = Orchestrator(self.db, get_runtime())
        return self

    async def start(self) -> None:
        if self._task is not None:
            return
        self.open()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="autopilot-loop")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._has_lease:
            self.db.release_lease(self.holder)
            self._has_lease = False

    @property
    def orchestrator(self) -> Orchestrator:
        if self._orchestrator is None:
            self.open()
        assert self._orchestrator is not None
        return self._orchestrator

    async def _run(self) -> None:
        # Reconciliation happens once, under the lease, before any tick — so a
        # process that loses the race never rewrites another's state.
        reconciled = False
        while not self._stop.is_set():
            try:
                self._has_lease = self.db.acquire_lease(self.holder, LEASE_TTL_SECONDS)
                if self._has_lease:
                    if not reconciled:
                        self.orchestrator.reconcile_on_start()
                        reconciled = True
                    await self.orchestrator.tick()
                    self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A tick must never kill the loop: the next one re-reads state
                # from SQLite and carries on.
                self._last_error = str(exc)
                try:
                    self.db.log_event("engine", f"Tick error: {exc}", level="error")
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    # --- settings -------------------------------------------------------------

    def get_settings(self) -> Dict[str, Any]:
        return normalise(self.db.load_settings())

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merge a partial update over the stored config, then validate.

        A partial patch is what the UI sends (one tab at a time); replacing the
        whole document from a form would silently reset every field that tab
        does not render.
        """
        current = self.get_settings()
        merged = _deep_merge(current, patch or {})
        config = normalise(merged)           # raises ConfigError on bad input
        self.db.save_settings(config)
        self.db.log_event("settings", "Configuration updated",
                          data={"enabled": config["enabled"],
                                "timezone": config["timezone"]})
        return config

    # --- operator actions -----------------------------------------------------

    def enable(self) -> Dict[str, Any]:
        config = self.update_settings({"enabled": True})
        self.db.update_engine_state(engine_status=EngineStatus.IDLE, pause_requested=0,
                                    paused_reason=None, consecutive_failures=0)
        self.db.log_event("engine", "Autopilot enabled")
        return config

    def disable(self) -> Dict[str, Any]:
        config = self.update_settings({"enabled": False})
        self.db.update_engine_state(engine_status=EngineStatus.OFF)
        self.db.log_event("engine", "Autopilot disabled")
        return config

    def pause(self) -> None:
        """Stop starting new sources; finish what is already running."""
        self.db.update_engine_state(pause_requested=1, engine_status=EngineStatus.PAUSED)
        self.db.log_event("engine", "Paused — will finish the current job and stop")

    def resume(self) -> None:
        self.db.update_engine_state(pause_requested=0, paused_reason=None,
                                    consecutive_failures=0,
                                    engine_status=EngineStatus.IDLE)
        self.db.log_event("engine", "Resumed")

    def emergency_stop(self) -> Dict[str, int]:
        """Kill switch: disable, and cancel everything not already handed to the vendor.

        SUBMITTED attempts are left alone on purpose — the post already exists on
        Upload-Post's calendar and only they can cancel it. Saying otherwise in
        the UI would be a lie.
        """
        self.update_settings({"enabled": False})
        canceled = 0
        for attempt in self.db.list_publish_attempts(states=[PublishState.PENDING], limit=500):
            self.db.set_publish_state(attempt.id, PublishState.CANCELED,
                                      error="Emergency stop")
            self.db.set_clip_state(attempt.clip_id, ClipState.SKIPPED, "emergency_stop")
            canceled += 1
        released = 0
        for state in (SourceState.SELECTED, SourceState.ELIGIBLE):
            for source in self.db.list_sources(states=[state], limit=500):
                if self.db.transition_source(source.id, SourceState.SKIPPED,
                                             expected=[state],
                                             rejection_reason="emergency_stop"):
                    released += 1
        self.db.update_engine_state(engine_status=EngineStatus.OFF, pause_requested=1,
                                    paused_reason="Emergency stop")
        self.db.log_event("engine",
                          f"EMERGENCY STOP — {canceled} scheduled post(s) canceled, "
                          f"{released} candidate(s) dropped. Already-submitted posts "
                          f"remain on the Upload-Post calendar.", level="error")
        return {"canceled_publishes": canceled, "released_sources": released}

    async def run_discovery_now(self) -> Dict[str, Any]:
        config = self.get_settings()
        ok = await self.orchestrator.run_discovery(config, force=True)
        return {"ok": ok}

    async def process_next_now(self) -> Dict[str, Any]:
        config = self.get_settings()
        if self.db.active_processing_source() is not None:
            return {"ok": False, "reason": "a source is already processing"}
        started = await self.orchestrator._start_next_source(config, now=utcnow())
        return {"ok": bool(started)}

    def skip_source(self, source_id: int) -> bool:
        source = self.db.get_source(source_id)
        if source is None or source.state in SourceState.TERMINAL:
            return False
        moved = self.db.transition_source(source_id, SourceState.SKIPPED,
                                          expected=[source.state],
                                          rejection_reason="skipped_by_operator")
        if moved:
            self.db.log_event("engine", "Candidate skipped by the operator",
                              source_id=source_id,
                              youtube_video_id=source.youtube_video_id)
        return moved

    def retry_source(self, source_id: int) -> bool:
        source = self.db.get_source(source_id)
        if source is None or source.state not in (SourceState.FAILED,
                                                  SourceState.PROCESS_FAILED,
                                                  SourceState.SKIPPED):
            return False
        if source.state == SourceState.SKIPPED:
            return False  # skipped is terminal by design; discovery finds it again
        moved = self.db.transition_source(source_id, SourceState.ELIGIBLE,
                                          expected=[source.state],
                                          attempts=0, next_retry_at=None, job_id=None,
                                          last_error=None)
        if moved:
            self.db.log_event("engine", "Source re-queued by the operator",
                              source_id=source_id)
        return moved

    def retry_publish(self, attempt_id: int) -> bool:
        """Re-arm a failed publish. UNCERTAIN attempts require an explicit force.

        The distinction is the whole point of the UNCERTAIN state: the vendor may
        already hold the post, so re-sending it is a decision only a human who
        checked the calendar can make.
        """
        attempt = self.db.get_publish_attempt(attempt_id)
        if attempt is None or attempt.state != PublishState.FAILED:
            return False
        self.db.set_publish_state(attempt_id, PublishState.PENDING, error=None)
        self.db.set_clip_state(attempt.clip_id, ClipState.SCHEDULED)
        self.db.log_event("publishing", "Publish attempt re-queued by the operator",
                          publish_attempt_id=attempt_id, source_id=attempt.source_id)
        return True

    def force_retry_uncertain(self, attempt_id: int) -> bool:
        attempt = self.db.get_publish_attempt(attempt_id)
        if attempt is None or attempt.state != PublishState.UNCERTAIN:
            return False
        self.db.set_publish_state(attempt_id, PublishState.PENDING, error=None)
        self.db.log_event("publishing",
                          "Operator confirmed the uncertain post did NOT go out — re-queued",
                          level="warn", publish_attempt_id=attempt_id,
                          source_id=attempt.source_id)
        return True

    def resolve_uncertain(self, attempt_id: int) -> bool:
        """Operator confirmed the post *did* land: close it out as submitted."""
        attempt = self.db.get_publish_attempt(attempt_id)
        if attempt is None or attempt.state != PublishState.UNCERTAIN:
            return False
        self.db.set_publish_state(attempt_id, PublishState.SUBMITTED,
                                  error="Confirmed by the operator after an unknown outcome")
        self.db.set_clip_state(attempt.clip_id, ClipState.PUBLISHED)
        self.db.log_event("publishing", "Operator confirmed the post was published",
                          publish_attempt_id=attempt_id, source_id=attempt.source_id)
        return True

    # --- dashboard view -------------------------------------------------------

    def status(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Everything the Autopilot page needs, in one query round.

        Deliberately answers the questions an operator has after two days away:
        what did it find, what did it choose and why, what is running, what is
        scheduled, what failed, and what happens next.
        """
        now = now or utcnow()
        config = self.get_settings()
        state = self.db.load_engine_state()
        # The runtime this service actually drives — not the process-wide
        # registry, which a test (or a second service) may not have populated.
        runtime = self.orchestrator.runtime

        active = self.db.active_processing_source()
        day_start, day_end = scheduler.local_day_bounds(config, now=now)

        engine_status = state.get("engine_status") or EngineStatus.OFF
        if not config.get("enabled"):
            engine_status = EngineStatus.OFF

        next_publish = None
        upcoming = self.db.list_publish_attempts(
            states=[PublishState.PENDING, PublishState.SUBMITTED], limit=100)
        future = sorted(
            (a for a in upcoming
             if (parse_iso(a.scheduled_for_utc) or now) >= now),
            key=lambda a: a.scheduled_for_utc or "")
        if future:
            next_publish = future[0].scheduled_for_utc

        quota = self.db.get_quota("youtube")
        blocked = parse_iso(quota.get("exhausted_until"))

        lease = self.db.lease_holder() or {}

        return {
            "enabled": bool(config.get("enabled")),
            "status": engine_status,
            "paused_reason": state.get("paused_reason"),
            "pause_requested": bool(state.get("pause_requested")),
            "consecutive_failures": int(state.get("consecutive_failures") or 0),
            "timezone": config.get("timezone"),
            "stage": self._describe_stage(active, engine_status),
            "current_source": _source_view(active) if active else None,
            "current_job": active.job_id if active else None,
            "next_discovery_at": iso(scheduler.next_discovery_time(config, after=now)),
            "next_publish_at": next_publish,
            "last_discovery_at": state.get("last_discovery_at"),
            "last_tick_at": state.get("last_tick_at"),
            "today": {
                "sources_selected": self.db.sources_selected_since(day_start),
                "max_sources": (config.get("schedule") or {}).get("max_sources_per_day"),
                "clips_generated": self._clips_since(day_start),
                "posts_scheduled": self.db.posts_scheduled_between(day_start, day_end),
                "max_posts": (config.get("schedule") or {}).get("max_posts_per_day"),
                "posts_submitted": self._submitted_since(day_start),
            },
            "queue": [_source_view(s) for s in self.db.list_sources(
                states=[SourceState.ELIGIBLE], limit=12)],
            "recent_selected": [_source_view(s) for s in self.db.list_sources(
                states=[SourceState.PROCESS_QUEUED, SourceState.PROCESSING,
                        SourceState.PROCESS_READY, SourceState.CLIPS_SCHEDULED,
                        SourceState.DONE, SourceState.FAILED],
                limit=10, order="recent")],
            "rejected": [_source_view(s) for s in self.db.list_sources(
                states=[SourceState.FILTERED, SourceState.SKIPPED], limit=15,
                order="recent")],
            "publish_attempts": [_publish_view(a, config) for a in
                                 self.db.list_publish_attempts(limit=20)],
            "recent_errors": self.db.recent_events(limit=15, level="error"),
            "events": self.db.recent_events(limit=40),
            "runs": self.db.recent_runs(limit=8),
            "youtube_quota": {
                "units_used_today": quota.get("units_used", 0),
                "daily_budget": (config.get("limits") or {}).get("youtube_daily_quota_units"),
                "blocked_until": iso(blocked) if blocked and blocked > now else None,
                "configured": bool(os.environ.get("YOUTUBE_DATA_API_KEY")),
            },
            "credentials": self._credential_state(config),
            "runtime_ready": runtime.ready,
            "scheduler": {
                "holder": lease.get("holder"),
                "is_this_process": lease.get("holder") == self.holder,
                "expires_at": lease.get("expires_at"),
                "tick_seconds": TICK_SECONDS,
                "last_error": self._last_error,
            },
            "storage": self._storage_view(),
        }

    def _describe_stage(self, active, engine_status: str) -> str:
        if engine_status == EngineStatus.OFF:
            return "Autopilot is off"
        if engine_status == EngineStatus.PAUSED_ERROR:
            return "Paused after repeated failures"
        if active is not None:
            return {
                SourceState.SELECTED: "Preparing to submit a source",
                SourceState.PROCESS_QUEUED: "Waiting for the clip queue",
                SourceState.PROCESSING: "Generating clips",
            }.get(active.state, active.state)
        if self.db.list_sources(states=[SourceState.PROCESS_READY], limit=1):
            return "Selecting and scheduling clips"
        if self.db.list_publish_attempts(states=[PublishState.PENDING], limit=1):
            return "Submitting scheduled posts"
        return "Waiting for the next scheduled run"

    def _clips_since(self, since: datetime) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM generated_clip WHERE created_at >= ?", (iso(since),))
        return int(row["n"]) if row else 0

    def _submitted_since(self, since: datetime) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM publish_attempt WHERE submitted_at >= ?", (iso(since),))
        return int(row["n"]) if row else 0

    def _credential_state(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Whether each unattended credential is *present* — never its value."""
        runtime = self.orchestrator.runtime
        upload_key = upload_user = None
        if runtime.publisher is not None:
            try:
                upload_key, upload_user = runtime.publisher.credentials()
            except Exception:
                pass
        return {
            "gemini": bool(os.environ.get("GEMINI_API_KEY")),
            "youtube_data_api": bool(os.environ.get("YOUTUBE_DATA_API_KEY")),
            "upload_post_key": bool(upload_key),
            "upload_post_user": bool(upload_user),
        }

    def _storage_view(self) -> Dict[str, Any]:
        """Free space on the output volume, so a full disk is visible before it bites."""
        path = os.environ.get("AUTOPILOT_STATE_DIR", "output")
        try:
            usage = os.statvfs(path if os.path.isdir(path) else ".")
            free_gb = usage.f_bavail * usage.f_frsize / 1024 ** 3
            total_gb = usage.f_blocks * usage.f_frsize / 1024 ** 3
        except (OSError, AttributeError):
            return {"available": False}
        return {
            "available": True,
            "free_gb": round(free_gb, 1),
            "total_gb": round(total_gb, 1),
            "low": free_gb < 10,
            "db_path": self.db.path,
        }

    def files_in_use(self) -> List[Dict[str, str]]:
        """Clip files a pending publish still needs — retention must skip these."""
        try:
            return self.db.files_in_use()
        except Exception:
            return []


def _source_view(source) -> Dict[str, Any]:
    return {
        "id": source.id,
        "video_id": source.youtube_video_id,
        "url": source.watch_url,
        "title": source.title,
        "channel": source.channel_title,
        "channel_id": source.channel_id,
        "published_at": source.published_at,
        "duration_seconds": source.duration_seconds,
        "views": source.view_count,
        "likes": source.like_count,
        "comments": source.comment_count,
        "license": source.license,
        "definition": source.definition,
        "captions": source.caption_available,
        "discovery_source": source.discovery_source,
        "discovered_at": source.discovered_at,
        "score": source.score,
        "score_breakdown": source.score_breakdown,
        "state": source.state,
        "state_changed_at": source.state_changed_at,
        "rejection_reason": source.rejection_reason,
        "job_id": source.job_id,
        "attempts": source.attempts,
        "last_error": source.last_error,
        "rights_policy": source.rights_policy,
        "selected_at": source.selected_at,
        # When a failed source becomes selectable again. Without it the queue
        # shows a candidate sitting there with no explanation for why it is not
        # being picked, which reads as a stuck engine.
        "next_retry_at": source.next_retry_at,
    }


def _publish_view(attempt, config) -> Dict[str, Any]:
    slot = parse_iso(attempt.scheduled_for_utc)
    return {
        "id": attempt.id,
        "clip_id": attempt.clip_id,
        "source_id": attempt.source_id,
        "job_id": attempt.job_id,
        "clip_index": attempt.clip_index,
        "title": attempt.title,
        "platforms": attempt.platforms,
        "state": attempt.state,
        "scheduled_for_utc": attempt.scheduled_for_utc,
        "scheduled_local": scheduler.describe_local(slot, config) if slot else None,
        "timezone": attempt.timezone,
        "retry_count": attempt.retry_count,
        "error": attempt.error,
        "submitted_at": attempt.submitted_at,
        "created_at": attempt.created_at,
    }


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# --- module-level singleton ---------------------------------------------------

_service: Optional[AutopilotService] = None


def get_service() -> AutopilotService:
    global _service
    if _service is None:
        _service = AutopilotService().open()
    return _service


def reset_service() -> None:
    """Test hook — drops the process-wide instance."""
    global _service
    if _service is not None:
        _service.db.close()
    _service = None
