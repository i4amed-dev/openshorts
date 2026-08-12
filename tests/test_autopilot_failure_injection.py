"""Kill the process at every vendor boundary and prove nothing is duplicated.

The orchestrator is rebuilt from SQLite between stages — the same thing a
`docker compose restart` or a `kill -9` does. Each test names the exact instant
of death and then asserts the four properties that matter:

    no duplicate upload · no duplicate platform publication ·
    no lost scheduled job · no incorrect "published" label

Everything is mocked. No real social post is made.
"""
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

import publishing_service
from automation.db import AutopilotDB, iso, parse_iso, utcnow
from automation.models import ClipState, PublishState, SourceState
from automation.orchestrator import Orchestrator
from automation.ports import Runtime
from autopilot_fakes import (
    FakeClipGenerator, FakePublisher, FakeYouTubeClient, base_config, make_record,
    platform_result, run_async, status_payload,
)

NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)

_LIVE: List[FakePublisher] = []


@pytest.fixture(autouse=True)
def _fake_vendor(monkeypatch):
    async def fake_get_status(api_key, *, request_id=None, job_id=None, timeout=30.0):
        publisher = _LIVE[-1] if _LIVE else None
        if publisher is None:
            return publishing_service.parse_status({"status": "pending"})
        payload = publisher.next_status(request_id=request_id, job_id=job_id)
        http = 404 if payload.get("status") == "not_found" else 200
        return publishing_service.parse_status(payload, http_status=http)

    async def fake_cancel(api_key, job_id, *, timeout=30.0):
        publisher = _LIVE[-1] if _LIVE else None
        publisher.cancel_calls.append(job_id)
        return publisher.cancel_outcome, "ok"

    monkeypatch.setattr(publishing_service, "get_status", fake_get_status)
    monkeypatch.setattr(publishing_service, "cancel_scheduled", fake_cancel)
    yield
    _LIVE.clear()


class Rig:
    """A restartable Autopilot whose vendor behaviour a test can steer."""

    def __init__(self, path, *, config=None, clips=1):
        self.path = path
        self.db = AutopilotDB(path).connect()
        self.db.save_settings(config or base_config(clips={"max_clips_per_source": clips}))
        self.clip_gen = FakeClipGenerator(clips_per_job=clips)
        self.publisher = FakePublisher()
        _LIVE.append(self.publisher)
        self.youtube = FakeYouTubeClient([make_record("vid00000001", now=NOW)])
        self._build()

    def _build(self):
        self.runtime = Runtime(clip_generator=self.clip_gen.port(),
                               publisher=self.publisher.port())
        self.orchestrator = Orchestrator(self.db, self.runtime,
                                         client_factory=lambda: self.youtube)

    def crash_and_restart(self):
        """Drop everything in memory; keep only what reached the disk."""
        self.db.close()
        self.db = AutopilotDB(self.path).connect()
        self._build()
        self.orchestrator.reconcile_on_start(now=NOW)

    def tick(self, now=NOW):
        return run_async(self.orchestrator.tick(now=now))

    def run_to_scheduled(self, now=NOW):
        run_async(self.orchestrator.run_discovery(now=now, force=True))
        run_async(self.orchestrator._start_next_source(
            self.orchestrator.config(), now=now))
        job_id = self.clip_gen.submissions[0]["job_id"]
        self.clip_gen.complete(job_id)
        active = self.db.active_processing_source()
        run_async(self.orchestrator._reconcile_active(
            active, self.orchestrator.config(), now=now))
        run_async(self.orchestrator._schedule_ready_sources(
            self.orchestrator.config(), now=now))
        return job_id

    def attempts(self, states=None):
        return self.db.list_publish_attempts(states=states, limit=50)

    def close(self):
        if self.publisher in _LIVE:
            _LIVE.remove(self.publisher)
        self.db.close()


@pytest.fixture
def rig(tmp_path):
    r = Rig(str(tmp_path / "fi.db"))
    yield r
    r.close()


class TestCrashBeforeTheRequest:
    def test_a_pending_attempt_is_sent_exactly_once_after_a_restart(self, rig):
        rig.run_to_scheduled()
        assert rig.attempts(states=[PublishState.PENDING])

        rig.crash_and_restart()
        rig.tick()
        rig.crash_and_restart()
        rig.tick()

        # One upload, no matter how many restarts.
        assert len(rig.publisher.calls) == 1


class TestCrashInsideTheTransport:
    def test_an_ambiguous_upload_is_never_blindly_resent(self, rig):
        rig.run_to_scheduled()
        rig.publisher.mode = "uncertain"
        rig.tick()

        attempt = rig.attempts()[0]
        assert attempt.state == PublishState.UNCERTAIN
        sent = len(rig.publisher.calls)

        rig.crash_and_restart()
        rig.publisher.mode = "ok"
        rig.publisher.set_default_status(status_payload("pending"))
        rig.tick()
        rig.tick()

        # It is resolved by ASKING, never by re-POSTing.
        assert len(rig.publisher.calls) == sent

    def test_our_request_id_is_persisted_before_the_request_leaves(self, rig):
        """Why the ambiguity is answerable at all."""
        rig.run_to_scheduled()
        rig.publisher.mode = "uncertain"
        rig.tick()

        rig.crash_and_restart()
        recovered = rig.attempts()[0]
        assert recovered.vendor_request_id
        # And it is the same id the vendor was given.
        assert rig.publisher.calls[0]["request_id"] == recovered.vendor_request_id

    def test_an_uncertain_upload_the_vendor_never_saw_is_resent_once(self, rig):
        rig.run_to_scheduled()
        rig.publisher.mode = "uncertain"
        rig.tick()
        rig.publisher.mode = "ok"

        rig.crash_and_restart()
        # The vendor has no record → nothing was posted → safe to send again.
        # The lookup is scheduled a short interval out, so tick past it rather
        # than pretending reconciliation is instantaneous.
        rig.publisher.set_default_status({"status": "not_found"})
        later = NOW + timedelta(minutes=2)
        rig.tick(later)     # one pass: resolves to PENDING, then dispatches it

        assert len(rig.publisher.calls) == 2         # the original + one resend
        assert rig.attempts()[0].state == PublishState.SUBMITTED

        # And exactly one resend — further ticks add nothing.
        rig.publisher.set_default_status(status_payload("pending"))
        rig.tick(later)
        rig.tick(later)
        assert len(rig.publisher.calls) == 2


class TestCrashAfterTheVendorAnswered:
    def test_a_response_lost_before_the_db_write_does_not_double_post(self, rig):
        """Vendor accepted; we died before persisting. IN_FLIGHT catches it."""
        rig.run_to_scheduled()
        attempt = rig.attempts(states=[PublishState.PENDING])[0]
        # Freeze exactly at "request sent, nothing recorded".
        rig.db.record_vendor_ids(attempt.id, request_id="klippo-1-frozen", job_id=None)
        rig.db.set_publish_state(attempt.id, PublishState.IN_FLIGHT)

        rig.crash_and_restart()
        assert rig.attempts()[0].state == PublishState.UNCERTAIN

        # The vendor DID have it — reconciliation, not a resend, settles this.
        rig.publisher.set_default_status(status_payload("completed", [
            platform_result("tiktok", "completed")]))
        rig.tick()
        assert rig.attempts()[0].state == PublishState.PUBLISHED
        assert rig.publisher.calls == []             # nothing was ever re-sent


class TestCrashAfterSubmission:
    def test_a_scheduled_job_survives_a_restart_and_is_not_resubmitted(self, rig):
        rig.run_to_scheduled()
        rig.publisher.set_default_status(status_payload("pending"))
        rig.tick()
        attempt = rig.attempts()[0]
        assert attempt.state == PublishState.SUBMITTED
        assert attempt.vendor_job_id

        rig.crash_and_restart()
        rig.tick()
        rig.crash_and_restart()
        rig.tick()

        assert len(rig.publisher.calls) == 1
        # The vendor job id is not lost.
        assert rig.attempts()[0].vendor_job_id == attempt.vendor_job_id

    def test_the_clip_is_not_labelled_published_while_merely_scheduled(self, rig):
        rig.run_to_scheduled()
        rig.publisher.set_default_status(status_payload("pending"))
        rig.tick()

        rig.crash_and_restart()
        attempt = rig.attempts()[0]
        assert attempt.state == PublishState.SUBMITTED
        assert rig.db.get_clip(attempt.clip_id).state == ClipState.SCHEDULED
        # And the source is not finished either.
        assert rig.db.list_sources(states=[SourceState.DONE]) == []


class TestCrashWhilePolling:
    def test_completion_recorded_after_a_restart_is_still_seen(self, rig):
        rig.run_to_scheduled()
        rig.publisher.set_default_status(status_payload("pending"))
        rig.tick()

        # The vendor completed it while Klippo was down.
        rig.crash_and_restart()
        rig.publisher.set_default_status(status_payload("completed", [
            platform_result("tiktok", "completed")]))
        rig.db.execute("UPDATE publish_attempt SET next_status_check_at = NULL")
        rig.tick()

        assert rig.attempts()[0].state == PublishState.PUBLISHED
        assert len(rig.publisher.calls) == 1

    def test_a_crash_between_vendor_completion_and_the_local_write_self_heals(self, rig):
        rig.run_to_scheduled()
        rig.publisher.set_default_status(status_payload("pending"))
        rig.tick()
        rig.publisher.set_default_status(status_payload("completed", [
            platform_result("tiktok", "completed")]))

        # Die before the reconciliation result is written: state is still SUBMITTED.
        rig.crash_and_restart()
        assert rig.attempts()[0].state == PublishState.SUBMITTED

        rig.db.execute("UPDATE publish_attempt SET next_status_check_at = NULL")
        rig.tick()
        assert rig.attempts()[0].state == PublishState.PUBLISHED

    def test_polling_is_not_restarted_from_scratch(self, rig):
        """A restart must not stampede every open attempt at the vendor."""
        rig.run_to_scheduled()
        rig.publisher.set_default_status(status_payload("pending"))
        rig.tick()
        rig.db.execute("UPDATE publish_attempt SET next_status_check_at = ?",
                       (iso(NOW + timedelta(hours=4)),))
        checks_before = len(rig.publisher.status_calls)

        rig.crash_and_restart()
        rig.tick()
        assert len(rig.publisher.status_calls) == checks_before


class TestPartialFailure:
    def test_a_mixed_result_is_never_retried_across_restarts(self, tmp_path):
        """The duplicate-platform trap, held across two restarts."""
        rig = Rig(str(tmp_path / "partial.db"))
        rig.run_to_scheduled()
        rig.publisher.set_default_status(status_payload("pending"))
        rig.tick()
        uploads = len(rig.publisher.calls)

        rig.publisher.set_default_status(status_payload("in_progress", [
            platform_result("tiktok", "completed"),
            platform_result("instagram", "failed", "Aspect ratio"),
        ]))
        rig.db.execute("UPDATE publish_attempt SET next_status_check_at = NULL")
        rig.tick()
        assert rig.attempts()[0].state == PublishState.PARTIAL_FAILED

        rig.crash_and_restart()
        rig.tick()
        rig.crash_and_restart()
        rig.tick()

        # Never re-sent: TikTok already succeeded.
        assert len(rig.publisher.calls) == uploads
        assert rig.attempts()[0].state == PublishState.PARTIAL_FAILED
        assert rig.db.get_clip(rig.attempts()[0].clip_id).state == ClipState.PARTIAL
        rig.close()

    def test_the_operator_can_see_exactly_which_platform_failed(self, rig):
        rig.run_to_scheduled()
        rig.publisher.set_default_status(status_payload("pending"))
        rig.tick()
        rig.publisher.set_default_status(status_payload("in_progress", [
            platform_result("tiktok", "completed"),
            platform_result("instagram", "failed", "Aspect ratio not supported"),
        ]))
        rig.db.execute("UPDATE publish_attempt SET next_status_check_at = NULL")
        rig.tick()

        results = {r["platform"]: r for r in rig.attempts()[0].vendor_results}
        assert results["tiktok"]["status"] == "completed"
        assert results["instagram"]["status"] == "failed"
        assert "Aspect ratio" in results["instagram"]["message"]


class TestCrashDuringCancellation:
    def test_an_interrupted_emergency_stop_reconciles_rather_than_guesses(self, tmp_path):
        from automation.service import AutopilotService

        path = str(tmp_path / "cancel.db")
        rig = Rig(path)
        rig.run_to_scheduled()
        rig.publisher.set_default_status(status_payload("pending"))
        rig.tick()
        attempt = rig.attempts()[0]
        # Push the slot into the future so cancellation is the right question.
        rig.db.execute("UPDATE publish_attempt SET scheduled_for_utc = ? WHERE id = ?",
                       (iso(NOW + timedelta(hours=6)), attempt.id))

        # The vendor cannot confirm the cancellation.
        rig.publisher.cancel_outcome = "error"
        service = AutopilotService(db=rig.db).open()
        service._orchestrator.runtime = rig.runtime
        run_async(service.emergency_stop(now=NOW))

        refreshed = rig.db.get_publish_attempt(attempt.id)
        # NOT reported as cancelled, and queued to find out the truth.
        assert refreshed.state != PublishState.CANCELED
        assert refreshed.next_status_check_at is not None
        rig.close()

    def test_a_vendor_404_during_cancellation_becomes_uncertain_not_cancelled(self, tmp_path):
        from automation.service import AutopilotService

        rig = Rig(str(tmp_path / "cancel404.db"))
        rig.run_to_scheduled()
        rig.publisher.set_default_status(status_payload("pending"))
        rig.tick()
        attempt = rig.attempts()[0]
        rig.db.execute("UPDATE publish_attempt SET scheduled_for_utc = ? WHERE id = ?",
                       (iso(NOW + timedelta(hours=6)), attempt.id))

        rig.publisher.cancel_outcome = "not_found"
        service = AutopilotService(db=rig.db).open()
        service._orchestrator.runtime = rig.runtime
        report = run_async(service.emergency_stop(now=NOW))

        assert report["vendor_not_found"] == 1
        assert report["canceled_vendor"] == 0
        assert rig.db.get_publish_attempt(attempt.id).state == PublishState.UNCERTAIN
        rig.close()


class TestNoDuplicateSourceProcessing:
    def test_repeated_crashes_never_submit_the_source_twice(self, rig):
        run_async(rig.orchestrator.run_discovery(now=NOW, force=True))
        run_async(rig.orchestrator._start_next_source(rig.orchestrator.config(), now=NOW))
        job_id = rig.clip_gen.submissions[0]["job_id"]

        for _ in range(3):
            rig.crash_and_restart()
            rig.tick()

        assert len(rig.clip_gen.submissions) == 1
        assert len(rig.db.list_processing_attempts(1)) == 1
        assert rig.db.get_source_by_job(job_id) is not None
