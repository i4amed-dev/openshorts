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


class PreflightError(RuntimeError):
    """Autopilot cannot be enabled yet. Carries the individual check results."""

    def __init__(self, report: Dict[str, Any]):
        super().__init__("; ".join(report.get("errors") or ["Preflight failed"]))
        self.report = report


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
        else:
            self._migrate_loose_defaults()
        self._orchestrator = Orchestrator(self.db, get_runtime())
        return self

    def _migrate_loose_defaults(self) -> None:
        """Bump eligibility thresholds that shipped too strict (one-time migration).

        Only updates a field if it is still at the old default value — so
        intentional user customisations (lower OR higher) are left alone.
        """
        raw = self.db.load_settings() or {}
        e = raw.get("eligibility") or {}
        upd: Dict[str, Any] = {}
        if e.get("min_views") == 5000:
            upd["min_views"] = 1000
        if e.get("min_view_velocity_per_hour") == 50.0:
            upd["min_view_velocity_per_hour"] = 10.0
        if e.get("min_engagement_rate") == 0.005:
            upd["min_engagement_rate"] = 0.001
        if e.get("max_age_hours") == 168:
            upd["max_age_hours"] = 336
        if upd:
            self.update_settings({"eligibility": upd})

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

    async def preflight(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Everything that must be true before unattended execution is allowed.

        Saving settings stays permissive — you can configure Autopilot offline.
        *Enabling* it is the commitment, so the checks happen here rather than
        surfacing at 03:00 as a failed post nobody is awake to see.

        Returns ``{ok, checks:[{id, ok, message}], errors:[…]}``. Never contains
        a credential value.
        """
        config = config or self.get_settings()
        checks: List[Dict[str, Any]] = []

        def add(check_id: str, ok: bool, message: str) -> bool:
            checks.append({"id": check_id, "ok": bool(ok), "message": message})
            return ok

        add("gemini_key", bool(os.environ.get("GEMINI_API_KEY")),
            "GEMINI_API_KEY must be set in the server environment — unattended "
            "runs cannot read the browser's stored keys.")
        add("youtube_key", bool(os.environ.get("YOUTUBE_DATA_API_KEY")),
            "YOUTUBE_DATA_API_KEY must be set for source discovery.")

        publisher = self.orchestrator.runtime.publisher
        api_key = profile = None
        if publisher is not None:
            try:
                api_key, profile = publisher.credentials()
            except Exception:
                pass
        add("upload_post_key", bool(api_key),
            "UPLOAD_POST_API_KEY must be set in the server environment.")
        add("upload_post_profile", bool(profile),
            "Choose an Upload-Post profile to publish as (Autopilot setup → publishing).")

        wanted = list((config.get("publishing") or {}).get("platforms") or [])

        # The check that actually saves a scheduled post from failing silently:
        # the chosen profile must have every selected platform connected.
        if api_key and profile and publisher is not None and publisher.list_profiles:
            try:
                profiles = await publisher.list_profiles()
            except Exception as exc:
                add("profile_reachable", False,
                    f"Could not reach Upload-Post to verify the profile: {exc}")
                profiles = None
            else:
                add("profile_reachable", True, "Upload-Post is reachable.")
                names = [p.get("username") for p in profiles]
                if profile not in names:
                    add("profile_exists", False,
                        f"Upload-Post has no profile named '{profile}'. "
                        f"Available: {', '.join(n for n in names if n) or 'none'}.")
                else:
                    add("profile_exists", True, f"Profile '{profile}' exists.")
                    connected = next((p.get("connected") or [] for p in profiles
                                      if p.get("username") == profile), [])
                    missing = [p for p in wanted if p not in connected]
                    add("platforms_connected", not missing,
                        (f"{', '.join(missing)} selected for publishing, but the "
                         f"Upload-Post profile '{profile}' only has "
                         f"{', '.join(connected) or 'no account'} connected.")
                        if missing else
                        f"'{profile}' has {', '.join(connected)} connected.")

        rights = config.get("rights") or {}
        policy_ok = bool(rights.get("policy"))
        if rights.get("policy") in ("OWNED_OR_ALLOWLISTED_CHANNELS",
                                    "CREATIVE_COMMONS_OR_ALLOWLISTED"):
            policy_ok = bool(rights.get("allowlisted_channel_ids"))
        add("rights_policy", policy_ok,
            "The selected rights policy needs at least one approved channel id."
            if not policy_ok else f"Rights policy: {rights.get('policy')}.")

        schedule = config.get("schedule") or {}
        add("publish_schedule", bool(schedule.get("publish_times")),
            "At least one publishing slot is required.")
        add("discovery_schedule", bool(schedule.get("discovery_times")),
            "At least one discovery time is required.")

        if "niche_search" in ((config.get("discovery") or {}).get("strategies") or []):
            add("search_topics", bool((config.get("discovery") or {}).get("topics")),
                "Niche search is enabled but no topics are configured.")

        try:
            self.db.log_event("settings", "Preflight check", level="debug")
            add("database", True, f"Database is writable at {self.db.path}.")
        except Exception as exc:
            add("database", False, f"Autopilot database is not writable: {exc}")

        add("runtime", self.orchestrator.runtime.ready,
            "The backend has not registered its Clip Generator / publishing "
            "adapters — restart the backend.")

        errors = [c["message"] for c in checks if not c["ok"]]
        return {"ok": not errors, "checks": checks, "errors": errors}

    async def enable(self) -> Dict[str, Any]:
        """Turn Autopilot on, but only if it can actually run.

        Raises PreflightError with the specific failures — enabling into a
        configuration that cannot publish is how a week of silence happens.
        """
        config = self.get_settings()
        result = await self.preflight(config)
        if not result["ok"]:
            raise PreflightError(result)
        config = self.update_settings({"enabled": True})
        self.db.update_engine_state(engine_status=EngineStatus.IDLE, pause_requested=0,
                                    paused_reason=None, consecutive_failures=0)
        self.db.log_event("engine", "Autopilot enabled")
        return config

    async def list_upload_post_profiles(self) -> List[Dict[str, Any]]:
        """Profiles on the server-side key — usernames and linked platforms only.

        The API key never leaves the server: this returns exactly what the setup
        UI needs to offer a profile picker and to grey out platforms the chosen
        profile cannot post to.
        """
        publisher = self.orchestrator.runtime.publisher
        if publisher is None or not publisher.list_profiles:
            return []
        return await publisher.list_profiles()

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

    async def emergency_stop(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Kill switch: stop the engine and cancel every post we still can.

        Ordering is deliberate. Disable first so no tick can create new work
        while we are cancelling, then drop local PENDING attempts, then ask
        Upload-Post to cancel the future scheduled jobs it is holding for us.

        Only jobs Klippo submitted through Autopilot are touched: the vendor job
        ids come from our own ``publish_attempt`` rows, so Manual Mode posts —
        which Autopilot never records — cannot be cancelled by this.

        A cancellation is marked CANCELED only when the vendor confirms it. A
        404 means "unknown or already executed", which is NOT success, and the
        attempt is left for reconciliation rather than reported as cancelled.
        """
        self.update_settings({"enabled": False})
        self.db.update_engine_state(engine_status=EngineStatus.OFF, pause_requested=1,
                                    paused_reason="Emergency stop")

        report = {"canceled_local": 0, "canceled_vendor": 0, "vendor_not_found": 0,
                  "vendor_errors": 0, "released_sources": 0, "already_published": 0}

        # 1. Local attempts that never left: safe to cancel outright.
        for attempt in self.db.list_publish_attempts(states=[PublishState.PENDING], limit=500):
            if self.db.set_publish_state(attempt.id, PublishState.CANCELED,
                                         error="Emergency stop",
                                         expected=[PublishState.PENDING]):
                self.db.set_clip_state(attempt.clip_id, ClipState.SKIPPED, "emergency_stop")
                report["canceled_local"] += 1

        # 2. Future scheduled jobs Upload-Post is holding for us.
        publisher = self.orchestrator.runtime.publisher
        api_key = None
        if publisher is not None:
            try:
                api_key, _profile = publisher.credentials()
            except Exception:
                api_key = None

        if api_key:
            import publishing_service
            now = now or utcnow()
            for attempt in self.db.vendor_scheduled_attempts():
                if not attempt.vendor_job_id:
                    continue
                slot = parse_iso(attempt.scheduled_for_utc)
                if slot is not None and slot <= now:
                    # Its moment has passed; it may already be live. Reconcile
                    # rather than claim a cancellation we cannot make.
                    report["already_published"] += 1
                    self.db.set_publish_state(
                        attempt.id, attempt.state, mark_checked=False,
                        next_status_check_at=iso(now))
                    continue
                try:
                    outcome, detail = await publishing_service.cancel_scheduled(
                        api_key, attempt.vendor_job_id)
                except Exception as exc:
                    outcome, detail = publishing_service.CancelOutcome.ERROR, str(exc)

                if outcome == publishing_service.CancelOutcome.CANCELED:
                    self.db.set_publish_state(attempt.id, PublishState.CANCELED,
                                              error="Emergency stop — cancelled at Upload-Post",
                                              next_status_check_at=None)
                    self.db.set_clip_state(attempt.clip_id, ClipState.SKIPPED, "emergency_stop")
                    report["canceled_vendor"] += 1
                    self.db.log_event("publishing",
                                      f"Cancelled scheduled job {attempt.vendor_job_id}",
                                      source_id=attempt.source_id,
                                      publish_attempt_id=attempt.id)
                elif outcome == publishing_service.CancelOutcome.NOT_FOUND:
                    # Never reported as cancelled — it may have already run.
                    report["vendor_not_found"] += 1
                    self.db.set_publish_state(
                        attempt.id, PublishState.UNCERTAIN,
                        error=f"Emergency stop: {detail}",
                        next_status_check_at=iso(now))
                    self.db.log_event("publishing",
                                      f"Could not cancel {attempt.vendor_job_id}: {detail}. "
                                      f"Its real outcome will be looked up.",
                                      level="warn", source_id=attempt.source_id,
                                      publish_attempt_id=attempt.id)
                else:
                    report["vendor_errors"] += 1
                    self.db.set_publish_state(
                        attempt.id, attempt.state,
                        error=f"Emergency stop could not cancel: {detail}",
                        next_status_check_at=iso(now))
                    self.db.log_event("publishing",
                                      f"Cancellation failed for {attempt.vendor_job_id}: {detail}",
                                      level="error", source_id=attempt.source_id,
                                      publish_attempt_id=attempt.id)
        elif self.db.vendor_scheduled_attempts():
            self.db.log_event("engine",
                              "Emergency stop could not reach Upload-Post (no API key) — "
                              "scheduled posts remain on the vendor calendar",
                              level="error")

        # 3. Drop the candidate queue.
        for state in (SourceState.SELECTED, SourceState.ELIGIBLE):
            for source in self.db.list_sources(states=[state], limit=500):
                if self.db.transition_source(source.id, SourceState.SKIPPED,
                                             expected=[state],
                                             rejection_reason="emergency_stop"):
                    report["released_sources"] += 1

        self.db.log_event(
            "engine",
            f"EMERGENCY STOP — {report['canceled_local']} queued post(s) dropped, "
            f"{report['canceled_vendor']} cancelled at Upload-Post, "
            f"{report['vendor_not_found']} could not be cancelled (unknown or already run), "
            f"{report['vendor_errors']} error(s), "
            f"{report['released_sources']} candidate(s) dropped.", level="error",
            data=report)
        return report

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
        self.db.set_publish_state(attempt_id, PublishState.PENDING, error=None,
                                  next_status_check_at=None,
                                  expected=[PublishState.FAILED])
        self.db.set_clip_state(attempt.clip_id, ClipState.SCHEDULED)
        self.db.log_event("publishing", "Publish attempt re-queued by the operator",
                          publish_attempt_id=attempt_id, source_id=attempt.source_id)
        return True

    def force_retry_uncertain(self, attempt_id: int) -> bool:
        attempt = self.db.get_publish_attempt(attempt_id)
        if attempt is None or attempt.state != PublishState.UNCERTAIN:
            return False
        self.db.set_publish_state(attempt_id, PublishState.PENDING, error=None,
                                  next_status_check_at=None,
                                  expected=[PublishState.UNCERTAIN])
        self.db.log_event("publishing",
                          "Operator confirmed the uncertain post did NOT go out — re-queued",
                          level="warn", publish_attempt_id=attempt_id,
                          source_id=attempt.source_id)
        return True

    def resolve_uncertain(self, attempt_id: int) -> bool:
        """Operator verified an ambiguous or partial attempt on the vendor calendar.

        Accepts both UNCERTAIN (we never learned the outcome) and PARTIAL_FAILED
        (some platforms failed and the operator has decided to accept it as is).
        Either way this is a human asserting a fact we could not establish, so
        it is recorded as such rather than as a vendor confirmation.
        """
        attempt = self.db.get_publish_attempt(attempt_id)
        if attempt is None or attempt.state not in (PublishState.UNCERTAIN,
                                                    PublishState.PARTIAL_FAILED):
            return False
        self.db.set_publish_state(
            attempt_id, PublishState.PUBLISHED,
            error="Confirmed by the operator, not by an Upload-Post status check",
            next_status_check_at=None,
            expected=[PublishState.UNCERTAIN, PublishState.PARTIAL_FAILED])
        self.db.set_clip_state(attempt.clip_id, ClipState.PUBLISHED)
        self.db.log_event("publishing", "Operator confirmed the post is live",
                          publish_attempt_id=attempt_id, source_id=attempt.source_id)
        return True

    def abandon_attempt(self, attempt_id: int) -> bool:
        """Give up on a partial/uncertain attempt without publishing anything more.

        The counterpart to :meth:`resolve_uncertain`: for a partial failure the
        operator may prefer to leave the successful platforms alone and simply
        stop. Klippo must never "fix" a partial by resending — that would
        duplicate the platforms that already succeeded.
        """
        attempt = self.db.get_publish_attempt(attempt_id)
        if attempt is None or attempt.state not in (PublishState.UNCERTAIN,
                                                    PublishState.PARTIAL_FAILED):
            return False
        self.db.set_publish_state(
            attempt_id, PublishState.FAILED, error="Abandoned by the operator",
            next_status_check_at=None,
            expected=[PublishState.UNCERTAIN, PublishState.PARTIAL_FAILED])
        self.db.set_clip_state(attempt.clip_id, ClipState.FAILED, "abandoned_by_operator")
        self.db.log_event("publishing", "Operator abandoned this attempt",
                          level="warn", publish_attempt_id=attempt_id,
                          source_id=attempt.source_id)
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

        from .youtube_client import (
            BUCKET_GENERAL, BUCKET_SEARCH, DEFAULT_GENERAL_UNITS, DEFAULT_SEARCH_CALLS,
        )
        general = self.db.get_quota("youtube", BUCKET_GENERAL)
        search = self.db.get_quota("youtube", BUCKET_SEARCH)
        general_block = parse_iso(general.get("exhausted_until"))
        search_block = parse_iso(search.get("exhausted_until"))

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
                "posts_published": self._published_since(day_start),
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
            # Two independent allocations since 1-jun-2026 — reported separately
            # because exhausting one does not stop the other.
            "youtube_quota": {
                "general_units_used": general.get("units_used", 0),
                "general_budget": (config.get("limits") or {}).get(
                    "youtube_daily_quota_units", DEFAULT_GENERAL_UNITS),
                "general_blocked_until": iso(general_block) if general_block and general_block > now else None,
                "search_calls_used": search.get("units_used", 0),
                "search_budget": (config.get("limits") or {}).get(
                    "youtube_daily_search_calls", DEFAULT_SEARCH_CALLS),
                "search_blocked_until": iso(search_block) if search_block and search_block > now else None,
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
        """Accepted by the vendor — NOT necessarily live anywhere."""
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM publish_attempt WHERE submitted_at >= ?", (iso(since),))
        return int(row["n"]) if row else 0

    def _published_since(self, since: datetime) -> int:
        """Confirmed live by an Upload-Post status check."""
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM publish_attempt WHERE finalized_at >= ? "
            "AND state = ?", (iso(since), PublishState.PUBLISHED))
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
        # Vendor evidence. The identifier is truncated for display — it is not a
        # secret, but the full value is noise in a list.
        "vendor_status": attempt.vendor_status,
        "vendor_tracking_id": (attempt.tracking_id or "")[:24] or None,
        "vendor_is_scheduled": attempt.is_vendor_scheduled,
        "vendor_results": attempt.vendor_results,
        "last_status_check_at": attempt.last_status_check_at,
        "next_status_check_at": attempt.next_status_check_at,
        "finalized_at": attempt.finalized_at,
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
