"""End-to-end Autopilot, with the edges faked and the machinery real.

Every test here drives the actual orchestrator against actual SQLite. Only
YouTube, the clip queue and Upload-Post are substituted — the state machine,
the ranking, the slot allocation and the idempotency constraints are the
production ones.

The restart tests matter most: they rebuild the orchestrator from the database
between stages, which is precisely what a `docker compose restart` does.
"""
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from automation.db import AutopilotDB, iso, parse_iso, utcnow
from automation.models import ClipState, EngineStatus, PublishState, Reason, SourceState
from automation.orchestrator import Orchestrator
from automation.ports import Runtime
from automation.youtube_client import QuotaExhausted
from autopilot_fakes import (
    FakeClipGenerator, FakePublisher, FakeYouTubeClient, base_config, install_fake_vendor,
    make_record, platform_result, run_async, status_payload,
)

NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)


class Harness:
    """One Autopilot instance whose orchestrator can be rebuilt at will."""

    def __init__(self, db_path=":memory:", *, records=None, config=None,
                 clips_per_job=3):
        self.db = AutopilotDB(db_path).connect()
        self.db.save_settings(config or base_config())
        self.clip_gen = FakeClipGenerator(clips_per_job=clips_per_job)
        self.publisher = FakePublisher()
        self.youtube = FakeYouTubeClient(records or default_records())
        self.runtime = Runtime(clip_generator=self.clip_gen.port(),
                               publisher=self.publisher.port())
        self.orchestrator = self._build()
        _LIVE_PUBLISHERS.append(self.publisher)

    def _build(self):
        return Orchestrator(self.db, self.runtime, client_factory=lambda: self.youtube)

    def restart(self):
        """Simulate a backend restart: new orchestrator, same database."""
        self.orchestrator = self._build()
        self.orchestrator.reconcile_on_start(now=NOW)
        return self.orchestrator

    def tick(self, now=NOW):
        return run_async(self.orchestrator.tick(now=now))

    def schedule_only(self, now=NOW):
        """Advance to "clips scheduled" but stop before any upload.

        Models the machine dying in the window between reserving a slot and
        sending the bytes — the gap where a naive design double-posts.
        """
        active = self.db.active_processing_source()
        config = self.orchestrator.config()
        if active is not None:
            run_async(self.orchestrator._reconcile_active(active, config, now=now))
        run_async(self.orchestrator._schedule_ready_sources(config, now=now))

    def discover(self, now=NOW):
        return run_async(self.orchestrator.run_discovery(now=now, force=True))

    def close(self):
        if self.publisher in _LIVE_PUBLISHERS:
            _LIVE_PUBLISHERS.remove(self.publisher)
        self.db.close()


# Harnesses register here so the autouse patch below can route vendor calls to
# the right fake. Scoped through monkeypatch so the real functions are restored
# after every test — a module-global patch leaked into the transport tests that
# exercise the genuine HTTP paths.
_LIVE_PUBLISHERS: List["FakePublisher"] = []


@pytest.fixture(autouse=True)
def _fake_vendor(monkeypatch):
    import publishing_service

    def _route(request_id=None, job_id=None):
        key = job_id or request_id or ""
        for publisher in reversed(_LIVE_PUBLISHERS):
            if key in publisher.status_script or publisher.default_status is not None:
                return publisher
        return _LIVE_PUBLISHERS[-1] if _LIVE_PUBLISHERS else None

    async def fake_get_status(api_key, *, request_id=None, job_id=None, timeout=30.0):
        publisher = _route(request_id, job_id)
        if publisher is None:
            return publishing_service.parse_status({"status": "pending"})
        payload = publisher.next_status(request_id=request_id, job_id=job_id)
        http = 404 if payload.get("status") == "not_found" else 200
        return publishing_service.parse_status(payload, http_status=http)

    async def fake_cancel(api_key, job_id, *, timeout=30.0):
        publisher = _LIVE_PUBLISHERS[-1] if _LIVE_PUBLISHERS else None
        if publisher is None:
            return "error", "no publisher"
        publisher.cancel_calls.append(job_id)
        return publisher.cancel_outcome, f"{publisher.cancel_outcome} for {job_id}"

    monkeypatch.setattr(publishing_service, "get_status", fake_get_status)
    monkeypatch.setattr(publishing_service, "cancel_scheduled", fake_cancel)
    yield
    _LIVE_PUBLISHERS.clear()


def default_records(count=4):
    return [
        make_record(f"vid0000000{i}", now=NOW,
                    view_count=100_000 * i, like_count=5_000 * i,
                    comment_count=500 * i,
                    channel_id=f"UC{'c' * 20}{i:02d}",
                    published_at=NOW - timedelta(hours=4 + i))
        for i in range(1, count + 1)
    ]


@pytest.fixture
def harness():
    h = Harness()
    yield h
    h.close()


# --- discovery ---------------------------------------------------------------

class TestDiscovery:
    def test_candidates_are_stored_scored_and_classified(self, harness):
        assert harness.discover()
        eligible = harness.db.list_sources(states=[SourceState.ELIGIBLE], limit=50)
        assert len(eligible) == 4
        assert all(s.score > 0 for s in eligible)
        assert all(s.score_breakdown.get("components") for s in eligible)

    def test_rejected_candidates_keep_their_reason(self):
        h = Harness(records=[
            make_record("vid00000001", now=NOW, license="youtube"),        # rights
            make_record("vid00000002", now=NOW, duration_seconds=30),      # too short
            make_record("vid00000003", now=NOW),                           # fine
        ])
        h.discover()
        rejected = {s.youtube_video_id: s.rejection_reason
                    for s in h.db.list_sources(states=[SourceState.FILTERED], limit=50)}
        assert rejected["vid00000001"] == Reason.RIGHTS_POLICY
        assert rejected["vid00000002"] == Reason.TOO_SHORT
        assert "vid00000003" not in rejected
        h.close()

    def test_rediscovering_the_same_videos_creates_nothing_new(self, harness):
        harness.discover()
        before = len(harness.db.list_sources(limit=100))
        harness.discover()
        assert len(harness.db.list_sources(limit=100)) == before

    def test_quota_exhaustion_parks_that_bucket_instead_of_looping(self, harness):
        harness.youtube.raise_on_popular = QuotaExhausted("out of units",
                                                          bucket="general")
        assert harness.discover() is False
        quota = harness.db.get_quota("youtube", "general")
        assert parse_iso(quota["exhausted_until"]) > utcnow()
        # A second attempt must not even reach the API.
        calls_before = harness.youtube.popular_calls
        assert run_async(harness.orchestrator.run_discovery(now=NOW)) is False
        assert harness.youtube.popular_calls == calls_before

    def test_an_exhausted_search_bucket_leaves_chart_discovery_working(self):
        """Independent allocations: one 403 must not disable the other strategy."""
        h = Harness(config=base_config(discovery={
            "strategies": ["most_popular", "niche_search"], "topics": ["chess"]}))
        h.db.mark_quota_exhausted(utcnow() + timedelta(hours=5), "quotaExceeded",
                                  bucket="search")
        assert h.discover() is True
        assert h.youtube.popular_calls == 1     # chart discovery still ran
        assert h.youtube.search_calls == 0      # search was skipped
        assert h.db.list_sources(states=[SourceState.ELIGIBLE], limit=10)
        h.close()


# --- selection and submission ------------------------------------------------

class TestSelection:
    def test_the_highest_scoring_source_is_submitted(self, harness):
        harness.discover()
        best = harness.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        harness.tick()
        assert len(harness.clip_gen.submissions) == 1
        assert best.youtube_video_id in harness.clip_gen.submissions[0]["url"]
        assert harness.db.get_source(best.id).state == SourceState.PROCESS_QUEUED

    def test_the_rights_policy_is_recorded_with_the_submission(self, harness):
        harness.discover()
        harness.tick()
        submission = harness.clip_gen.submissions[0]
        assert submission["rights_policy"] == "CREATIVE_COMMONS_ONLY"
        source = harness.db.get_source_by_job(submission["job_id"])
        assert source.rights_policy == "CREATIVE_COMMONS_ONLY"

    def test_only_one_heavy_job_runs_at_a_time(self, harness):
        """The invariant that keeps an M1 alive.

        Four eligible sources, four ticks — still exactly one submission, because
        the first source is occupying the pipeline.
        """
        harness.discover()
        for _ in range(4):
            harness.tick()
        assert len(harness.clip_gen.submissions) == 1

    def test_the_daily_source_cap_is_enforced(self, harness):
        harness.discover()
        harness.tick()
        job_id = harness.clip_gen.submissions[0]["job_id"]
        harness.clip_gen.complete(job_id)
        harness.tick()   # completes + schedules
        harness.tick()   # would start the next source
        # max_sources_per_day is 1 in the base config.
        assert len(harness.clip_gen.submissions) == 1

    def test_a_low_quality_source_is_skipped_not_confirmed(self, harness):
        """Autopilot must never enter the interactive quality-gate flow."""
        harness.clip_gen.quality_height = 360
        harness.discover()
        harness.tick()
        assert harness.clip_gen.submissions == []
        skipped = harness.db.list_sources(states=[SourceState.SKIPPED], limit=5)
        assert skipped[0].rejection_reason == Reason.QUALITY_GATE

    def test_two_ticks_in_the_same_instant_submit_once(self, harness):
        """Overlapping timers must not double-submit."""
        harness.discover()
        harness.tick()
        harness.tick()
        assert len(harness.clip_gen.submissions) == 1


# --- processing --------------------------------------------------------------

class TestProcessing:
    def _submit(self, harness):
        harness.discover()
        harness.tick()
        return harness.clip_gen.submissions[0]["job_id"]

    def test_progress_is_tracked_without_blocking(self, harness):
        job_id = self._submit(harness)
        harness.clip_gen.start(job_id)
        harness.tick()
        assert harness.db.get_source_by_job(job_id).state == SourceState.PROCESSING

    def test_completion_schedules_but_does_not_declare_victory(self, harness):
        """A source is not DONE just because the vendor accepted its clips."""
        job_id = self._submit(harness)
        harness.clip_gen.complete(job_id)
        harness.schedule_only()
        assert harness.db.get_source_by_job(job_id).state == SourceState.CLIPS_SCHEDULED

        harness.tick()   # dispatches to Upload-Post
        # Still not DONE: the posts are scheduled with the vendor, not live.
        assert harness.db.get_source_by_job(job_id).state == SourceState.CLIPS_SCHEDULED
        assert all(a.state == PublishState.SUBMITTED
                   for a in harness.db.list_publish_attempts(limit=10))

        # Only once the vendor confirms every clip does the source finish.
        harness.publisher.set_default_status(status_payload("completed", [
            platform_result(p, "completed")
            for p in ("tiktok", "instagram", "youtube")]))
        harness.db.execute("UPDATE publish_attempt SET next_status_check_at = NULL")
        harness.tick()
        assert harness.db.get_source_by_job(job_id).state == SourceState.DONE

    def test_a_failed_job_is_retried_with_backoff(self, harness):
        job_id = self._submit(harness)
        harness.clip_gen.fail(job_id, "yt-dlp could not download")
        harness.tick()
        source = harness.db.get_source(harness.db.get_source_by_job(job_id).id
                                       if harness.db.get_source_by_job(job_id) else 1)
        eligible = harness.db.list_sources(states=[SourceState.ELIGIBLE], limit=10)
        retried = [s for s in eligible if s.next_retry_at]
        assert retried, "the failed source should be re-queued with a backoff"
        assert parse_iso(retried[0].next_retry_at) > NOW

    def test_a_source_that_keeps_failing_is_given_up_on(self):
        """Bounded retries: a poison source is abandoned, not retried forever."""
        # One candidate only, so every attempt lands on the same source. The
        # daily cap counts retries too (each costs a pipeline run), so it is
        # raised here to isolate the attempt limit.
        harness = Harness(records=[make_record("vid00000001", now=NOW)],
                          config=base_config(schedule={"max_sources_per_day": 5}))
        harness.discover()
        config = harness.orchestrator.config()

        for _ in range(2):   # limits.max_process_attempts defaults to 2
            run_async(harness.orchestrator._start_next_source(config, now=NOW))
            harness.clip_gen.fail(harness.clip_gen.submissions[-1]["job_id"])
            harness.tick()
            # Skip past the exponential backoff so the next attempt may start.
            harness.db.execute("UPDATE discovered_source SET next_retry_at = NULL")

        failed = harness.db.list_sources(states=[SourceState.FAILED], limit=5)
        assert len(failed) == 1
        assert failed[0].attempts == 2
        assert len(harness.clip_gen.submissions) == 2
        harness.close()

    def test_a_job_producing_no_clips_is_a_failure_not_a_success(self, harness):
        job_id = self._submit(harness)
        harness.clip_gen.complete(job_id, clips=[])
        harness.tick()
        source = harness.db.get_source_by_video_id(
            harness.clip_gen.submissions[0]["label"])
        assert source.state in (SourceState.PROCESS_FAILED, SourceState.ELIGIBLE,
                                SourceState.FAILED)


# --- clip selection and scheduling -------------------------------------------

class TestClipScheduling:
    def _run_to_clips(self, harness, clips=None):
        harness.discover()
        harness.tick()
        job_id = harness.clip_gen.submissions[0]["job_id"]
        harness.clip_gen.complete(job_id, clips=clips)
        harness.tick()
        return job_id

    def test_top_n_clips_are_scheduled_and_the_rest_skipped(self):
        h = Harness(clips_per_job=6)
        job_id = self._run_to_clips(h)
        clips = h.db.list_clips(job_id=job_id)
        assert len(clips) == 6
        scheduled = [c for c in clips if c.state in (ClipState.SCHEDULED,
                                                     ClipState.PUBLISHED)]
        skipped = [c for c in clips if c.state == ClipState.SKIPPED]
        assert len(scheduled) == 3       # max_clips_per_source
        assert all(c.skip_reason == "beyond_max_clips_per_source" for c in skipped)
        h.close()

    def test_clips_outside_the_duration_rules_are_dropped(self, harness):
        job_id = self._run_to_clips(harness, clips=[
            {"start": 0, "end": 5, "video_url": "/videos/j/a.mp4",
             "video_title_for_youtube_short": "too short"},
            {"start": 0, "end": 30, "video_url": "/videos/j/b.mp4",
             "video_title_for_youtube_short": "just right"},
            {"start": 0, "end": 300, "video_url": "/videos/j/c.mp4",
             "video_title_for_youtube_short": "too long"},
        ])
        by_index = {c.clip_index: c for c in harness.db.list_clips(job_id=job_id)}
        assert by_index[0].skip_reason == "clip_shorter_than_minimum"
        assert by_index[1].state in (ClipState.SCHEDULED, ClipState.PUBLISHED)
        assert by_index[2].skip_reason == "clip_longer_than_maximum"

    def test_one_long_source_cannot_fill_a_day_with_posts(self):
        """The default that stops 15 clips landing in one afternoon."""
        h = Harness(clips_per_job=15)
        self._run_to_clips(h)
        attempts = h.db.list_publish_attempts(limit=50)
        assert len(attempts) == 3
        zone = "Europe/Madrid"
        from automation.scheduler import get_zone
        days = {parse_iso(a.scheduled_for_utc).astimezone(get_zone(zone)).date()
                for a in attempts}
        assert len(days) <= 2
        h.close()

    def test_scheduled_slots_match_the_configured_publish_times(self, harness):
        self._run_to_clips(harness)
        from automation.scheduler import get_zone
        zone = get_zone("Europe/Madrid")
        times = sorted(parse_iso(a.scheduled_for_utc).astimezone(zone).strftime("%H:%M")
                       for a in harness.db.list_publish_attempts(limit=10))
        assert times == ["11:30", "16:30", "21:00"]

    def test_reconciling_the_same_job_twice_creates_no_duplicate_clips(self, harness):
        job_id = self._run_to_clips(harness)
        before = len(harness.db.list_clips(job_id=job_id))
        harness.tick()
        harness.tick()
        assert len(harness.db.list_clips(job_id=job_id)) == before
        assert len(harness.db.list_publish_attempts(limit=50)) == 3


# --- publishing ---------------------------------------------------------------

class TestPublishing:
    def _run_to_publish(self, harness, now=NOW):
        harness.discover(now)
        harness.tick(now)
        job_id = harness.clip_gen.submissions[0]["job_id"]
        harness.clip_gen.complete(job_id)
        harness.tick(now)   # schedules
        harness.tick(now)   # dispatches
        return job_id

    def test_clips_are_submitted_through_the_shared_publisher(self, harness):
        self._run_to_publish(harness)
        assert len(harness.publisher.calls) == 3
        call = harness.publisher.calls[0]
        assert call["user"] == "test-profile"
        assert call["api_key"] == "test-key"
        assert set(call["platforms"]) == {"tiktok", "instagram", "youtube"}
        assert call["scheduled_date"]

    def test_the_vendor_gets_a_local_datetime_plus_a_zone(self, harness):
        self._run_to_publish(harness)
        call = harness.publisher.calls[0]
        assert call["timezone"] == "Europe/Madrid"
        # Naive local wall-clock, as Upload-Post expects alongside `timezone`.
        assert "+" not in call["scheduled_date"]
        assert call["scheduled_date"].endswith(("11:30:00", "16:30:00", "21:00:00"))

    def test_a_submitted_attempt_is_never_sent_again(self, harness):
        self._run_to_publish(harness)
        harness.tick()
        harness.tick()
        assert len(harness.publisher.calls) == 3

    def test_a_retryable_failure_is_retried_then_gives_up(self, harness):
        harness.publisher.mode = "fail"
        harness.publisher.fail_times = 99
        self._run_to_publish(harness)
        for _ in range(6):
            harness.tick()
        attempts = harness.db.list_publish_attempts(limit=10)
        assert all(a.state == PublishState.FAILED for a in attempts)
        # max_publish_attempts is 3 — bounded, not infinite.
        assert all(a.retry_count <= 3 for a in attempts)

    def test_a_permanent_failure_is_not_retried(self, harness):
        harness.publisher.mode = "fail_permanent"
        self._run_to_publish(harness)
        calls_after_first = len(harness.publisher.calls)
        harness.tick()
        assert len(harness.publisher.calls) == calls_after_first
        assert all(a.state == PublishState.FAILED
                   for a in harness.db.list_publish_attempts(limit=10))

    def test_an_ambiguous_outcome_is_never_auto_retried(self, harness):
        """Upload-Post has no idempotency key, so a blind retry can double-post."""
        harness.publisher.mode = "uncertain"
        self._run_to_publish(harness)
        calls = len(harness.publisher.calls)
        for _ in range(3):
            harness.tick()
        assert len(harness.publisher.calls) == calls
        assert all(a.state == PublishState.UNCERTAIN
                   for a in harness.db.list_publish_attempts(limit=10))

    def test_a_missing_clip_file_does_not_crash_the_tick(self, harness):
        harness.discover()
        harness.tick()
        job_id = harness.clip_gen.submissions[0]["job_id"]
        harness.clip_gen.complete(job_id)
        harness.clip_gen.missing_files = {
            (job_id, f"base_clip_{i}.mp4") for i in range(1, 4)}
        harness.tick()
        assert harness.publisher.calls == []
        assert all(a.state in (PublishState.PENDING, PublishState.FAILED)
                   for a in harness.db.list_publish_attempts(limit=10))


# --- restart / failure injection ---------------------------------------------

class TestRestartRecovery:
    """Kill and rebuild the orchestrator between every stage.

    Each test asserts the same three things in its own way: the item is not lost,
    it is not processed or published twice, and nothing fires outside its slot.
    """

    def test_restart_during_discovery_loses_nothing_and_duplicates_nothing(self, tmp_path):
        path = str(tmp_path / "a.db")
        h = Harness(path)
        h.discover()
        stored = len(h.db.list_sources(limit=100))
        h.db.close()

        h2 = Harness(path, records=h.youtube.records)
        h2.restart()
        h2.discover()
        assert len(h2.db.list_sources(limit=100)) == stored
        h2.close()

    def test_restart_while_processing_reattaches_to_the_same_job(self, tmp_path):
        path = str(tmp_path / "b.db")
        h = Harness(path)
        h.discover()
        h.tick()
        job_id = h.clip_gen.submissions[0]["job_id"]
        h.clip_gen.start(job_id)

        h.restart()
        h.tick()
        # No second submission, and the source still points at the same job.
        assert len(h.clip_gen.submissions) == 1
        assert h.db.get_source_by_job(job_id) is not None
        h.close()

    def test_restart_after_processing_before_scheduling_still_schedules_once(self, tmp_path):
        path = str(tmp_path / "c.db")
        h = Harness(path)
        h.discover()
        h.tick()
        job_id = h.clip_gen.submissions[0]["job_id"]
        h.clip_gen.complete(job_id)

        h.restart()
        h.tick()
        h.restart()
        h.tick()
        assert len(h.db.list_publish_attempts(limit=50)) == 3
        h.close()

    def test_restart_after_scheduling_does_not_publish_twice(self, tmp_path):
        path = str(tmp_path / "d.db")
        h = Harness(path)
        h.discover()
        h.tick()
        job_id = h.clip_gen.submissions[0]["job_id"]
        h.clip_gen.complete(job_id)
        h.tick()   # schedule
        h.tick()   # publish
        published = len(h.publisher.calls)

        h.restart()
        h.tick()
        h.tick()
        assert len(h.publisher.calls) == published
        h.close()

    def test_a_crash_mid_upload_becomes_uncertain_not_a_silent_duplicate(self, tmp_path):
        """The hardest case: the request left, the answer never came back."""
        path = str(tmp_path / "e.db")
        h = Harness(path)
        h.discover()
        h.tick()
        job_id = h.clip_gen.submissions[0]["job_id"]
        h.clip_gen.complete(job_id)
        h.schedule_only()

        # Freeze one attempt mid-request, exactly as a kill -9 would.
        attempt = h.db.list_publish_attempts(states=[PublishState.PENDING], limit=1)[0]
        frozen_file = h.db.get_clip(attempt.clip_id).filename
        h.db.set_publish_state(attempt.id, PublishState.IN_FLIGHT)

        h.restart()
        assert h.db.get_publish_attempt(attempt.id).state == PublishState.UNCERTAIN

        h.tick()
        h.tick()
        # Its siblings publish normally; the ambiguous one is never re-sent,
        # because the vendor may already hold it.
        assert h.publisher.calls, "the other clips should still go out"
        assert all(not call["file_path"].endswith(frozen_file)
                   for call in h.publisher.calls)
        assert h.db.get_publish_attempt(attempt.id).state == PublishState.UNCERTAIN
        h.close()

    def test_downtime_across_a_slot_reschedules_instead_of_bursting(self, tmp_path):
        """Machine asleep through the evening: nothing fires at once on wake-up."""
        path = str(tmp_path / "f.db")
        h = Harness(path)
        h.discover(NOW)
        h.tick(NOW)
        job_id = h.clip_gen.submissions[0]["job_id"]
        h.clip_gen.complete(job_id)
        h.schedule_only(NOW)   # slots 11:30 / 16:30 / 21:00 local, nothing sent

        # The Mac slept before any upload happened.
        assert h.publisher.calls == []
        two_days_later = NOW + timedelta(days=2)

        h.restart()
        h.tick(two_days_later)

        slots = sorted(parse_iso(a.scheduled_for_utc)
                       for a in h.db.list_publish_attempts(limit=10))
        # Every slot moved into the future...
        assert all(slot > two_days_later for slot in slots)
        # ...and they are still spaced, not stacked.
        gaps = [(b - a).total_seconds() / 60 for a, b in zip(slots, slots[1:])]
        assert all(gap >= 120 for gap in gaps)
        h.close()

    def test_orphaned_selection_returns_to_the_queue(self, tmp_path):
        # Crash between "SELECTED" and "job submitted": nothing was started, so
        # the source must become eligible again rather than stick forever.
        path = str(tmp_path / "g.db")
        h = Harness(path)
        h.discover()
        source = h.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        h.db.transition_source(source.id, SourceState.SELECTED)

        h.restart()
        assert h.db.get_source(source.id).state == SourceState.ELIGIBLE
        h.close()

    def test_a_job_lost_across_a_restart_is_retried_not_abandoned(self, tmp_path):
        path = str(tmp_path / "h.db")
        h = Harness(path)
        h.discover()
        h.tick()
        job_id = h.clip_gen.submissions[0]["job_id"]
        # The clip queue no longer knows about it (its output was purged too).
        del h.clip_gen.jobs[job_id]

        h.restart()
        source = h.db.get_source_by_video_id(h.clip_gen.submissions[0]["label"])
        assert source.state in (SourceState.PROCESS_FAILED, SourceState.ELIGIBLE)
        h.close()


# --- engine controls ----------------------------------------------------------

class TestEngineControls:
    def test_a_disabled_engine_does_nothing(self, harness):
        harness.db.save_settings(base_config(enabled=False))
        harness.discover()
        result = harness.tick()
        assert result["status"] == EngineStatus.OFF
        assert harness.clip_gen.submissions == []

    def test_pause_stops_new_sources_but_finishes_the_current_one(self, harness):
        harness.discover()
        harness.tick()
        job_id = harness.clip_gen.submissions[0]["job_id"]
        harness.db.update_engine_state(pause_requested=1)

        harness.clip_gen.complete(job_id)
        harness.tick()   # still reconciles + schedules
        harness.tick()   # still publishes
        assert len(harness.publisher.calls) == 3
        assert len(harness.clip_gen.submissions) == 1

    def test_the_circuit_breaker_trips_after_repeated_failures(self):
        h = Harness(config=base_config(limits={"consecutive_failure_limit": 2}))
        h.youtube.raise_on_popular = RuntimeError("network down")
        for _ in range(3):
            run_async(h.orchestrator.run_discovery(now=NOW, force=True))
        state = h.db.load_engine_state()
        assert state["engine_status"] == EngineStatus.PAUSED_ERROR
        assert "consecutive failures" in (state["paused_reason"] or "")
        h.close()

    def test_a_tripped_breaker_does_not_self_resume(self):
        h = Harness(config=base_config(limits={"consecutive_failure_limit": 1}))
        h.youtube.raise_on_popular = RuntimeError("network down")
        run_async(h.orchestrator.run_discovery(now=NOW, force=True))
        result = h.tick()
        assert result["status"] == EngineStatus.PAUSED_ERROR
        assert h.clip_gen.submissions == []
        h.close()

    def test_a_success_resets_the_failure_counter(self, harness):
        harness.db.update_engine_state(consecutive_failures=3)
        harness.discover()
        assert harness.db.load_engine_state()["consecutive_failures"] == 0
