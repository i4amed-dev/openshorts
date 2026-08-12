"""Vendor acceptance is not publication.

The defect this file exists to prevent: v1 marked a clip PUBLISHED the moment
Upload-Post returned 2xx. For a post scheduled three days out that is simply
false — nothing exists on any platform yet — and for a multi-platform post it
can stay false forever if one network rejects the upload.

Everything here is mocked. No real social post is made.

Contract these tests encode, verified against docs.upload-post.com (12-aug-2026):
  * async upload  → 200 + request_id
  * scheduled     → 202 + job_id
  * GET /uploadposts/status → pending | queued | processing | in_progress |
    completed | failed, plus not_found (404); per-platform results carry
    queued | processing | completed | failed | retryable
  * `completed` means ALL platforms succeeded and `failed` means ALL failed, so
    a mixed outcome is only visible in the results array
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import publishing_service as svc
from automation.db import AutopilotDB, iso, parse_iso, utcnow
from automation.models import ClipState, GeneratedClip, PublishAttempt, PublishState
from automation.publishing import reconcile_attempt, reconcile_in_flight
from autopilot_fakes import platform_result, run_async, status_payload

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


# --- parsing the vendor's own vocabulary -------------------------------------

class TestStatusParsing:
    @pytest.mark.parametrize("status,terminal", [
        ("pending", False), ("queued", False), ("processing", False),
        ("in_progress", False), ("completed", True), ("failed", True),
        ("not_found", True),
    ])
    def test_terminality_matches_the_documented_values(self, status, terminal):
        assert svc.parse_status(status_payload(status)).is_terminal is terminal

    def test_per_platform_results_are_extracted(self):
        parsed = svc.parse_status(status_payload("in_progress", [
            platform_result("youtube", "completed"),
            platform_result("instagram", "failed", "Aspect ratio not supported"),
            platform_result("tiktok", "queued"),
        ]))
        assert parsed.succeeded_platforms == ["youtube"]
        assert parsed.failed_platforms == ["instagram"]
        assert parsed.pending_platforms == ["tiktok"]

    def test_a_platform_carrying_only_success_still_parses(self):
        # The documented results shape is {platform, success, message,
        # upload_timestamp}; per-platform `status` is described separately, so
        # neither may be assumed present.
        parsed = svc.parse_status({"status": "completed", "results": [
            {"platform": "youtube", "success": True, "message": "Published"}]})
        assert parsed.succeeded_platforms == ["youtube"]

    def test_retryable_is_not_treated_as_failed(self):
        """The vendor retries these itself — resending would double-post."""
        parsed = svc.parse_status(status_payload("processing", [
            platform_result("youtube", "completed"),
            platform_result("tiktok", "retryable", "Temporary upstream error"),
        ]))
        assert parsed.failed_platforms == []
        assert parsed.pending_platforms == ["tiktok"]
        assert not parsed.is_partial_failure   # still in motion, not settled

    def test_mixed_settled_result_is_a_partial_failure(self):
        parsed = svc.parse_status(status_payload("in_progress", [
            platform_result("youtube", "completed"),
            platform_result("instagram", "failed"),
        ]))
        assert parsed.is_partial_failure

    def test_all_succeeded_is_not_a_partial_failure(self):
        parsed = svc.parse_status(status_payload("completed", [
            platform_result("youtube", "completed"),
            platform_result("tiktok", "completed"),
        ]))
        assert not parsed.is_partial_failure

    def test_not_found_is_flagged(self):
        parsed = svc.parse_status({"status": "not_found", "message": "No upload request"},
                                  http_status=404)
        assert parsed.not_found and parsed.is_terminal

    @pytest.mark.parametrize("junk", [None, "text", 42, []])
    def test_junk_never_reads_as_success(self, junk):
        parsed = svc.parse_status(junk)
        assert parsed.status == "unknown"
        assert not parsed.is_partial_failure
        assert parsed.succeeded_platforms == []

    def test_vendor_payloads_are_sanitised_before_storage(self):
        parsed = svc.parse_status({"status": "completed", "api_key": "sk-live-secret",
                                   "Authorization": "Apikey sk-live-secret"})
        assert "sk-live-secret" not in repr(parsed.raw)


class TestStatusTransport:
    def _client(self, monkeypatch, handler):
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(svc.httpx, "AsyncClient", factory)

    def test_queries_by_job_id_for_a_scheduled_post(self, monkeypatch):
        captured = {}

        def handler(request):
            captured.update(dict(request.url.params))
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=status_payload("pending"))

        self._client(monkeypatch, handler)
        run_async(svc.get_status("k", job_id="scheduler_job_1"))
        assert captured["job_id"] == "scheduler_job_1"
        assert captured["auth"] == "Apikey k"

    def test_queries_by_request_id_for_an_async_upload(self, monkeypatch):
        captured = {}

        def handler(request):
            captured.update(dict(request.url.params))
            return httpx.Response(200, json=status_payload("processing"))

        self._client(monkeypatch, handler)
        run_async(svc.get_status("k", request_id="klippo-7-abc"))
        assert captured["request_id"] == "klippo-7-abc"

    def test_404_becomes_not_found_rather_than_an_exception(self, monkeypatch):
        self._client(monkeypatch, lambda r: httpx.Response(
            404, json={"status": "not_found", "message": "No upload request found"}))
        assert run_async(svc.get_status("k", job_id="gone")).not_found

    def test_a_server_error_raises_and_is_retryable(self, monkeypatch):
        self._client(monkeypatch, lambda r: httpx.Response(500, text="boom"))
        with pytest.raises(svc.PublishError) as exc:
            run_async(svc.get_status("k", job_id="x"))
        assert exc.value.retryable

    def test_needs_an_identifier(self):
        with pytest.raises(svc.PublishError):
            run_async(svc.get_status("k"))


class TestCancellation:
    def _client(self, monkeypatch, handler):
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(svc.httpx, "AsyncClient", factory)

    def test_success_is_confirmed(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["method"] = request.method
            captured["path"] = request.url.path
            return httpx.Response(200, json={"success": True, "message": "Job cancelled"})

        self._client(monkeypatch, handler)
        outcome, _detail = run_async(svc.cancel_scheduled("k", "scheduler_job_9"))
        assert outcome == svc.CancelOutcome.CANCELED
        assert captured["method"] == "DELETE"
        assert captured["path"].endswith("/uploadposts/schedule/scheduler_job_9")

    def test_404_is_never_reported_as_a_cancellation(self, monkeypatch):
        """It may simply have already executed — claiming success would be a lie."""
        self._client(monkeypatch, lambda r: httpx.Response(
            404, json={"success": False, "error": "Job not found"}))
        outcome, _ = run_async(svc.cancel_scheduled("k", "gone"))
        assert outcome == svc.CancelOutcome.NOT_FOUND

    def test_readonly_calendar_is_distinguished(self, monkeypatch):
        self._client(monkeypatch, lambda r: httpx.Response(403, json={}))
        outcome, _ = run_async(svc.cancel_scheduled("k", "x"))
        assert outcome == svc.CancelOutcome.FORBIDDEN

    def test_a_network_failure_is_an_error_not_a_success(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("no route", request=request)

        self._client(monkeypatch, handler)
        outcome, _ = run_async(svc.cancel_scheduled("k", "x"))
        assert outcome == svc.CancelOutcome.ERROR


# --- the reconciliation state machine ----------------------------------------

@pytest.fixture
def db():
    database = AutopilotDB(":memory:").connect()
    yield database
    database.close()


def seed_attempt(db, *, state=PublishState.SUBMITTED, job_id="scheduler_job_1",
                 request_id="klippo-1-abc", scheduled_in=timedelta(hours=-1),
                 platforms=("youtube", "instagram", "tiktok")):
    """One clip that has been accepted by the vendor."""
    from automation.models import DiscoveredSource
    source_id, _ = db.upsert_source(DiscoveredSource(
        youtube_video_id="vid00000001", url="https://youtu.be/vid00000001",
        published_at=iso(NOW)))
    clip_id = db.upsert_clip(GeneratedClip(source_id=source_id, job_id="job-1",
                                           clip_index=0, filename="clip_1.mp4"))
    attempt_id = db.reserve_publish_attempt(PublishAttempt(
        clip_id=clip_id, source_id=source_id, job_id="job-1", clip_index=0,
        idempotency_key="key-1", platforms=list(platforms),
        scheduled_for_utc=iso(NOW + scheduled_in)))
    db.set_clip_state(clip_id, ClipState.SCHEDULED)
    # Walk the legal path into the requested state.
    db.set_publish_state(attempt_id, PublishState.IN_FLIGHT)
    if state != PublishState.IN_FLIGHT:
        db.set_publish_state(attempt_id, state, vendor_job_id=job_id,
                             vendor_request_id=request_id)
    else:
        db.record_vendor_ids(attempt_id, request_id=request_id, job_id=None)
    return db.get_publish_attempt(attempt_id)


def reconcile(db, attempt, payload, monkeypatch, now=NOW):
    """Run one reconciliation against a scripted vendor status."""
    async def fake_get_status(api_key, *, request_id=None, job_id=None, timeout=30.0):
        http = 404 if payload.get("status") == "not_found" else 200
        return svc.parse_status(payload, http_status=http)

    monkeypatch.setattr(svc, "get_status", fake_get_status)
    return run_async(reconcile_attempt(db, attempt, api_key="k", now=now))


class TestReconciliation:
    def test_a_scheduled_post_is_not_published_on_acceptance(self, db):
        attempt = seed_attempt(db)
        assert attempt.state == PublishState.SUBMITTED
        assert db.get_clip(attempt.clip_id).state == ClipState.SCHEDULED

    def test_pending_keeps_it_submitted_and_schedules_another_look(self, db, monkeypatch):
        attempt = seed_attempt(db, scheduled_in=timedelta(hours=6))
        assert reconcile(db, attempt, status_payload("pending"), monkeypatch) is None
        refreshed = db.get_publish_attempt(attempt.id)
        assert refreshed.state == PublishState.SUBMITTED
        # A post six hours out must not be polled every ten seconds.
        assert parse_iso(refreshed.next_status_check_at) > NOW + timedelta(hours=5)

    def test_processing_moves_it_to_publishing(self, db, monkeypatch):
        attempt = seed_attempt(db)
        assert reconcile(db, attempt, status_payload("processing", [
            platform_result("youtube", "processing")]), monkeypatch) == PublishState.PUBLISHING

    def test_completed_is_the_only_route_to_published(self, db, monkeypatch):
        attempt = seed_attempt(db)
        state = reconcile(db, attempt, status_payload("completed", [
            platform_result("youtube", "completed"),
            platform_result("instagram", "completed"),
            platform_result("tiktok", "completed"),
        ]), monkeypatch)
        assert state == PublishState.PUBLISHED
        refreshed = db.get_publish_attempt(attempt.id)
        assert refreshed.state == PublishState.PUBLISHED
        assert refreshed.finalized_at
        assert refreshed.next_status_check_at is None      # polling stops
        assert db.get_clip(attempt.clip_id).state == ClipState.PUBLISHED

    def test_vendor_failure_is_recorded_as_failure(self, db, monkeypatch):
        attempt = seed_attempt(db)
        state = reconcile(db, attempt, status_payload("failed", [
            platform_result("youtube", "failed", "Quota exceeded")],
            message="All platforms failed"), monkeypatch)
        assert state == PublishState.FAILED
        assert db.get_clip(attempt.clip_id).state == ClipState.FAILED

    def test_a_mixed_outcome_becomes_partial_and_is_never_retried(self, db, monkeypatch):
        """The duplicate-post trap.

        YouTube and TikTok are live; Instagram failed. Resending the request
        would publish it a second time on the two that worked.
        """
        attempt = seed_attempt(db)
        state = reconcile(db, attempt, status_payload("in_progress", [
            platform_result("youtube", "completed"),
            platform_result("tiktok", "completed"),
            platform_result("instagram", "failed", "Aspect ratio"),
        ]), monkeypatch)
        assert state == PublishState.PARTIAL_FAILED

        refreshed = db.get_publish_attempt(attempt.id)
        assert refreshed.state == PublishState.PARTIAL_FAILED
        assert refreshed.next_status_check_at is None
        # The evidence an operator needs to fix it by hand.
        assert "youtube" in refreshed.error and "instagram" in refreshed.error
        platforms = {r["platform"]: r["status"] for r in refreshed.vendor_results}
        assert platforms == {"youtube": "completed", "tiktok": "completed",
                             "instagram": "failed"}
        assert db.get_clip(attempt.clip_id).state == ClipState.PARTIAL

    def test_a_partial_is_not_reachable_by_the_automatic_dispatcher(self, db, monkeypatch):
        attempt = seed_attempt(db)
        reconcile(db, attempt, status_payload("in_progress", [
            platform_result("youtube", "completed"),
            platform_result("instagram", "failed"),
        ]), monkeypatch)
        # Nothing PENDING means the dispatcher has nothing to send.
        assert db.list_publish_attempts(states=[PublishState.PENDING]) == []

    def test_a_retryable_platform_keeps_the_attempt_open_without_resending(
            self, db, monkeypatch):
        attempt = seed_attempt(db)
        state = reconcile(db, attempt, status_payload("processing", [
            platform_result("youtube", "completed"),
            platform_result("tiktok", "retryable", "Upstream hiccup"),
        ]), monkeypatch)
        assert state == PublishState.PUBLISHING
        assert db.list_publish_attempts(states=[PublishState.PENDING]) == []

    def test_a_failed_status_check_changes_nothing_but_backs_off(self, db, monkeypatch):
        attempt = seed_attempt(db)

        async def boom(api_key, *, request_id=None, job_id=None, timeout=30.0):
            raise svc.PublishError("status endpoint down", status=500, retryable=True)

        monkeypatch.setattr(svc, "get_status", boom)
        assert run_async(reconcile_attempt(db, attempt, api_key="k", now=NOW)) is None
        refreshed = db.get_publish_attempt(attempt.id)
        assert refreshed.state == PublishState.SUBMITTED
        assert parse_iso(refreshed.next_status_check_at) > NOW

    def test_an_attempt_without_identifiers_is_left_alone(self, db, monkeypatch):
        attempt = seed_attempt(db, job_id=None, request_id=None)
        assert run_async(reconcile_attempt(db, attempt, api_key="k", now=NOW)) is None


class TestUncertainResolution:
    def test_uncertain_is_resolved_to_pending_when_the_vendor_never_got_it(
            self, db, monkeypatch):
        """The improvement the client-supplied request_id buys us.

        We know the id we sent, so an ambiguous timeout is answerable: if
        Upload-Post has no record, nothing was posted and resending is safe.
        """
        attempt = seed_attempt(db, state=PublishState.UNCERTAIN)
        state = reconcile(db, attempt, {"status": "not_found"}, monkeypatch)
        assert state == PublishState.PENDING
        assert db.get_publish_attempt(attempt.id).state == PublishState.PENDING

    def test_uncertain_becomes_published_when_the_vendor_did_get_it(
            self, db, monkeypatch):
        attempt = seed_attempt(db, state=PublishState.UNCERTAIN)
        state = reconcile(db, attempt, status_payload("completed", [
            platform_result("youtube", "completed")]), monkeypatch)
        assert state == PublishState.PUBLISHED

    def test_uncertain_that_the_vendor_is_still_working_stays_open(self, db, monkeypatch):
        attempt = seed_attempt(db, state=PublishState.UNCERTAIN)
        state = reconcile(db, attempt, status_payload("processing"), monkeypatch)
        assert state == PublishState.PUBLISHING

    def test_a_crash_mid_upload_is_recoverable_because_we_stored_our_id(self, db):
        attempt = seed_attempt(db, state=PublishState.IN_FLIGHT)
        # The id was written down BEFORE the request went out.
        assert db.get_publish_attempt(attempt.id).vendor_request_id

        assert reconcile_in_flight(db) == 1
        recovered = db.get_publish_attempt(attempt.id)
        assert recovered.state == PublishState.UNCERTAIN
        # ...and it is queued for a lookup rather than parked for a human.
        assert recovered.next_status_check_at is not None


class TestPollingSchedule:
    def test_terminal_attempts_are_not_polled(self, db, monkeypatch):
        attempt = seed_attempt(db)
        reconcile(db, attempt, status_payload("completed", [
            platform_result("youtube", "completed")]), monkeypatch)
        assert db.attempts_due_for_status_check(NOW + timedelta(days=1)) == []

    def test_open_attempts_become_due(self, db):
        seed_attempt(db)
        due = db.attempts_due_for_status_check(NOW + timedelta(days=1))
        assert len(due) == 1

    def test_a_future_check_time_is_respected_across_a_restart(self, tmp_path):
        """Polling cadence survives a restart instead of stampeding."""
        path = str(tmp_path / "poll.db")
        first = AutopilotDB(path).connect()
        attempt = seed_attempt(first)
        first.set_publish_state(attempt.id, PublishState.SUBMITTED,
                                next_status_check_at=iso(NOW + timedelta(hours=5)))
        first.close()

        second = AutopilotDB(path).connect()
        assert second.attempts_due_for_status_check(NOW) == []
        assert len(second.attempts_due_for_status_check(NOW + timedelta(hours=6))) == 1
        second.close()

    @pytest.mark.parametrize("status,expected", [
        ("queued", 10), ("pending", 10), ("processing", 10), ("in_progress", 10),
        ("completed", 0), ("failed", 0),
    ])
    def test_intervals_follow_the_vendor_guidance(self, status, expected):
        assert svc.poll_interval_seconds(status) == expected
