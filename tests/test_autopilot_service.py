"""The service layer: operator actions, the dashboard view, and the singleton loop.

This is the surface a human touches after leaving the machine alone for two days,
so the tests are about honesty as much as correctness — the status payload has to
answer "what happened and what's next", and it must never leak a credential while
doing it.
"""
from datetime import datetime, timedelta, timezone

import pytest

from automation.config import ConfigError
from automation.db import iso, parse_iso, utcnow
from automation.models import ClipState, EngineStatus, PublishState, SourceState
from automation.ports import Runtime
from automation.service import AutopilotService
from autopilot_fakes import (
    FakeClipGenerator, FakePublisher, FakeYouTubeClient, base_config, install_fake_vendor,
    make_record, platform_result, run_async, status_payload,
)

NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_DATA_API_KEY", "test-yt-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-up-key")
    svc = AutopilotService(db_path=str(tmp_path / "svc.db")).open()
    clip_gen = FakeClipGenerator()
    publisher = FakePublisher()
    youtube = FakeYouTubeClient([make_record(f"vid0000000{i}", now=NOW)
                                 for i in range(1, 4)])
    svc._orchestrator.runtime = Runtime(clip_generator=clip_gen.port(),
                                        publisher=publisher.port())
    svc._orchestrator._client_factory = lambda: youtube
    svc.clip_gen, svc.publisher, svc.youtube = clip_gen, publisher, youtube
    install_fake_vendor(monkeypatch, publisher)
    # A profile the preflight can resolve, so enabling is possible in tests.
    svc.update_settings({"publishing": {"upload_post_user": publisher.user}})
    yield svc
    svc.db.close()


def enable(service):
    return run_async(service.enable())


class TestSettings:
    def test_a_fresh_install_gets_valid_defaults(self, service):
        config = service.get_settings()
        assert config["enabled"] is False
        assert config["rights"]["policy"] == "CREATIVE_COMMONS_ONLY"

    def test_a_partial_patch_only_changes_what_it_names(self, service):
        service.update_settings({"timezone": "Asia/Tokyo"})
        service.update_settings({"schedule": {"max_posts_per_day": 2}})
        config = service.get_settings()
        assert config["timezone"] == "Asia/Tokyo"          # not reset by the second patch
        assert config["schedule"]["max_posts_per_day"] == 2
        assert config["schedule"]["publish_times"] == ["11:30", "16:30", "21:00"]

    def test_invalid_settings_are_rejected_and_nothing_is_stored(self, service):
        service.update_settings({"timezone": "Asia/Tokyo"})
        with pytest.raises(ConfigError):
            service.update_settings({"timezone": "Nowhere/Fake"})
        assert service.get_settings()["timezone"] == "Asia/Tokyo"

    def test_enabling_with_an_impossible_config_fails_loudly(self, service):
        # Better to fail at the click than at 03:00 with nobody watching.
        service.db.save_settings({**service.get_settings(),
                                  "discovery": {"strategies": ["niche_search"],
                                                "topics": []}})
        with pytest.raises(ConfigError):
            enable(service)


class TestOperatorActions:
    def test_enable_and_disable_flip_the_stored_flag(self, service):
        enable(service)
        assert service.get_settings()["enabled"] is True
        service.disable()
        assert service.get_settings()["enabled"] is False

    def test_enable_clears_a_tripped_breaker(self, service):
        service.db.update_engine_state(engine_status=EngineStatus.PAUSED_ERROR,
                                       consecutive_failures=9,
                                       paused_reason="everything broke")
        enable(service)
        state = service.db.load_engine_state()
        assert state["consecutive_failures"] == 0
        assert state["paused_reason"] is None

    def test_pause_is_a_soft_stop(self, service):
        enable(service)
        service.pause()
        assert service.db.load_engine_state()["pause_requested"] == 1
        service.resume()
        assert service.db.load_engine_state()["pause_requested"] == 0

    def test_emergency_stop_cancels_local_queue_and_disables(self, service):
        enable(service)
        _run_to_scheduled(service)
        pending = service.db.list_publish_attempts(states=[PublishState.PENDING], limit=10)
        assert pending

        report = run_async(service.emergency_stop(now=NOW))
        assert report["canceled_local"] == len(pending)
        assert service.get_settings()["enabled"] is False
        assert all(a.state == PublishState.CANCELED
                   for a in service.db.list_publish_attempts(limit=20))

    def test_emergency_stop_cancels_future_vendor_jobs(self, service):
        """The correction: Upload-Post DOES expose scheduled-job cancellation."""
        enable(service)
        attempt = _submitted_attempt(service, hours_ahead=6)

        report = run_async(service.emergency_stop(now=NOW))
        assert service.publisher.cancel_calls == [attempt.vendor_job_id]
        assert report["canceled_vendor"] == 1
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.CANCELED

    def test_a_vendor_404_is_never_reported_as_cancelled(self, service):
        """It may have already run — saying "cancelled" would be a lie."""
        enable(service)
        attempt = _submitted_attempt(service, hours_ahead=6)
        service.publisher.cancel_outcome = "not_found"

        report = run_async(service.emergency_stop(now=NOW))
        assert report["canceled_vendor"] == 0
        assert report["vendor_not_found"] == 1
        refreshed = service.db.get_publish_attempt(attempt.id)
        assert refreshed.state == PublishState.UNCERTAIN
        assert refreshed.next_status_check_at is not None   # reconcile, don't guess

    def test_a_post_whose_slot_already_passed_is_reconciled_not_cancelled(self, service):
        """It may already be live; cancellation is not the right question."""
        enable(service)
        attempt = _submitted_attempt(service, hours_ahead=-1)

        report = run_async(service.emergency_stop(now=NOW))
        assert service.publisher.cancel_calls == []
        assert report["already_published"] == 1
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.SUBMITTED

    def test_emergency_stop_never_touches_manual_posts(self, service):
        """Ownership boundary: only jobs in Autopilot's own table are cancelled."""
        enable(service)
        _submitted_attempt(service, hours_ahead=6)
        run_async(service.emergency_stop(now=NOW))
        # Every cancelled id came from a publish_attempt row Autopilot created.
        ours = {a.vendor_job_id for a in service.db.list_publish_attempts(limit=50)}
        assert set(service.publisher.cancel_calls) <= ours

    def test_skip_removes_a_candidate_from_the_queue(self, service):
        enable(service)
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        candidate = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        assert service.skip_source(candidate.id)
        assert service.db.get_source(candidate.id).state == SourceState.SKIPPED
        # Skipping twice is not an error the caller should have to handle twice.
        assert not service.skip_source(candidate.id)

    def test_a_failed_source_can_be_re_queued(self, service):
        enable(service)
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        source = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        service.db.transition_source(source.id, SourceState.SELECTED)
        service.db.transition_source(source.id, SourceState.PROCESS_FAILED,
                                     attempts=2, last_error="boom")
        service.db.transition_source(source.id, SourceState.FAILED)

        assert service.retry_source(source.id)
        refreshed = service.db.get_source(source.id)
        assert refreshed.state == SourceState.ELIGIBLE
        assert refreshed.attempts == 0
        assert refreshed.last_error is None

    def test_a_failed_publish_can_be_retried(self, service):
        enable(service)
        _run_to_scheduled(service)
        attempt = service.db.list_publish_attempts(limit=1)[0]
        service.db.set_publish_state(attempt.id, PublishState.FAILED, error="nope")
        assert service.retry_publish(attempt.id)
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.PENDING

    def test_an_uncertain_publish_needs_a_deliberate_decision(self, service):
        """No accidental double-post: plain retry refuses an ambiguous attempt."""
        enable(service)
        _run_to_scheduled(service)
        attempt = service.db.list_publish_attempts(limit=1)[0]
        service.db.set_publish_state(attempt.id, PublishState.IN_FLIGHT)
        service.db.set_publish_state(attempt.id, PublishState.UNCERTAIN)

        assert not service.retry_publish(attempt.id)       # ordinary retry refused
        assert service.force_retry_uncertain(attempt.id)   # explicit override works
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.PENDING

    def test_an_uncertain_publish_can_be_confirmed_as_landed(self, service):
        enable(service)
        _run_to_scheduled(service)
        attempt = service.db.list_publish_attempts(limit=1)[0]
        service.db.set_publish_state(attempt.id, PublishState.IN_FLIGHT)
        service.db.set_publish_state(attempt.id, PublishState.UNCERTAIN)

        assert service.resolve_uncertain(attempt.id)
        refreshed = service.db.get_publish_attempt(attempt.id)
        assert refreshed.state == PublishState.PUBLISHED
        # Recorded as a human assertion, not as a vendor confirmation.
        assert "operator" in (refreshed.error or "").lower()
        assert service.db.get_clip(attempt.clip_id).state == ClipState.PUBLISHED

    def test_a_partial_failure_can_be_accepted_or_abandoned(self, service):
        enable(service)
        _run_to_scheduled(service)
        attempts = service.db.list_publish_attempts(states=[PublishState.PENDING], limit=2)
        for a in attempts:
            service.db.set_publish_state(a.id, PublishState.IN_FLIGHT)
            service.db.set_publish_state(a.id, PublishState.SUBMITTED)
            service.db.set_publish_state(a.id, PublishState.PARTIAL_FAILED)

        assert service.resolve_uncertain(attempts[0].id)
        assert service.db.get_publish_attempt(attempts[0].id).state == PublishState.PUBLISHED

        assert service.abandon_attempt(attempts[1].id)
        assert service.db.get_publish_attempt(attempts[1].id).state == PublishState.FAILED
        # Neither is re-queued: resending would duplicate the platforms that
        # already succeeded.
        requeued = {a.id for a in service.db.list_publish_attempts(
            states=[PublishState.PENDING], limit=20)}
        assert requeued.isdisjoint({attempts[0].id, attempts[1].id})

    def test_process_next_refuses_while_something_is_running(self, service):
        enable(service)
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        assert run_async(service.process_next_now())["ok"] is True
        result = run_async(service.process_next_now())
        assert result["ok"] is False
        assert "already processing" in result["reason"]

    def test_process_source_starts_a_specific_candidate_out_of_score_order(self, service):
        """The point of `process_source`: an operator can pick a candidate the
        ranking did not put first, and it still goes through the exact same
        submission path (state transitions, job claim) as the automatic one."""
        enable(service)
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        eligible = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=10)
        assert len(eligible) >= 2
        chosen = eligible[-1]  # deliberately not the top-ranked one

        result = run_async(service.process_source(chosen.id))
        assert result["ok"] is True

        refreshed = service.db.get_source(chosen.id)
        assert refreshed.state == SourceState.PROCESS_QUEUED
        assert refreshed.job_id

    def test_process_source_gives_a_real_reason_for_an_ineligible_candidate(self, service):
        enable(service)
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        eligible = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        service.skip_source(eligible.id)

        result = run_async(service.process_source(eligible.id))
        assert result["ok"] is False
        assert "skipped" in result["reason"].lower()

    def test_process_source_rejects_an_unknown_id(self, service):
        result = run_async(service.process_source(999999))
        assert result["ok"] is False
        assert "not found" in result["reason"].lower()

    def test_process_source_refuses_while_something_else_is_running(self, service):
        enable(service)
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        eligible = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=10)
        assert run_async(service.process_source(eligible[0].id))["ok"] is True

        result = run_async(service.process_source(eligible[1].id))
        assert result["ok"] is False
        assert "already processing" in result["reason"].lower()


class TestStatusView:
    def test_answers_the_questions_an_operator_returns_with(self, service):
        enable(service)
        _run_to_scheduled(service)
        status = service.status(now=NOW)

        assert status["enabled"] is True
        assert status["stage"]
        assert status["next_discovery_at"]
        assert status["next_publish_at"]
        assert status["today"]["sources_selected"] == 1
        assert status["today"]["clips_generated"] == 3
        assert status["today"]["posts_scheduled"] == 3
        assert status["today"]["posts_published"] == 0   # nothing confirmed yet
        assert status["publish_attempts"]
        assert status["recent_selected"]

    def test_rejected_candidates_are_shown_with_their_reason(self, service, monkeypatch):
        enable(service)
        service.youtube.records = [
            make_record("vid00000001", now=NOW, license="youtube"),
            make_record("vid00000002", now=NOW),
        ]
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        rejected = service.status(now=NOW)["rejected"]
        assert any(r["rejection_reason"] == "rights_policy" for r in rejected)

    def test_the_score_breakdown_travels_with_each_candidate(self, service):
        enable(service)
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        candidate = service.status(now=NOW)["queue"][0]
        assert candidate["score"] > 0
        assert candidate["score_breakdown"]["components"]
        assert candidate["score_breakdown"]["contributions"]

    def test_the_discovery_funnel_explains_the_last_run(self, service):
        enable(service)
        service.youtube.records = [
            make_record("vid00000001", now=NOW, license="youtube"),
            make_record("vid00000002", now=NOW),
        ]
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        funnel = service.status(now=NOW)["discovery_funnel"]
        assert funnel["fetched"] == 2
        assert funnel["eligible"] == 1
        assert funnel["rejected"] == 1
        assert "rights_policy" in funnel["rejection_reasons"]
        assert funnel["lanes_run"]

    def test_each_candidate_carries_a_dashboard_bucket(self, service):
        enable(service)
        service.youtube.records = [
            make_record("vid00000001", now=NOW, license="youtube"),
            make_record("vid00000002", now=NOW),
        ]
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        status = service.status(now=NOW)
        assert status["queue"][0]["bucket"] in ("SHORTLISTED", "PROMISING_NOT_SELECTED")
        assert status["rejected"][0]["bucket"] == "POLICY_BLOCKED"

    def test_a_selection_diagnostic_appears_when_nothing_was_selected(self, service):
        enable(service)
        service.youtube.records = [make_record("vid00000001", now=NOW, license="youtube")]
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        status = service.status(now=NOW)
        assert status["selection_diagnostic"] is not None
        assert status["selection_diagnostic"]["bottleneck"] == "rights_policy"

    def test_no_selection_diagnostic_once_something_has_been_selected(self, service):
        enable(service)
        _run_to_scheduled(service)
        status = service.status(now=NOW)
        assert status["selection_diagnostic"] is None

    def test_slots_are_reported_in_local_time_as_well_as_utc(self, service):
        enable(service)
        service.update_settings({"timezone": "Europe/Madrid"})
        _run_to_scheduled(service)
        attempt = service.status(now=NOW)["publish_attempts"][0]
        assert attempt["scheduled_for_utc"].endswith("+00:00")
        assert attempt["scheduled_local"]
        assert attempt["scheduled_local"] != attempt["scheduled_for_utc"]

    def test_credentials_are_reported_as_present_never_as_values(self, service):
        status = service.status(now=NOW)
        assert status["credentials"] == {
            "gemini": True, "youtube_data_api": True,
            "upload_post_key": True, "upload_post_user": True}
        blob = repr(status)
        for secret in ("test-yt-key", "test-gemini-key", "test-key", "test-up-key"):
            assert secret not in blob

    def test_both_quota_buckets_are_visible(self, service):
        service.db.add_quota_units(101, bucket="general")
        service.db.add_quota_units(3, bucket="search")
        quota = service.status(now=NOW)["youtube_quota"]
        assert quota["general_units_used"] == 101
        assert quota["search_calls_used"] == 3
        assert quota["general_budget"] == 10000
        assert quota["search_budget"] == 100
        assert quota["general_blocked_until"] is None
        assert quota["search_blocked_until"] is None

    def test_a_parked_bucket_shows_when_it_lifts_without_blocking_the_other(self, service):
        service.db.mark_quota_exhausted(NOW + timedelta(hours=4), "quotaExceeded",
                                        bucket="search")
        # status() must be asked at the same instant the block was written
        # against, or it correctly reports a lapsed block as clear.
        quota = service.status(now=NOW)["youtube_quota"]
        assert quota["search_blocked_until"]
        assert quota["general_blocked_until"] is None

    def test_storage_headroom_is_reported(self, service):
        storage = service.status(now=NOW)["storage"]
        assert storage["available"] is True
        assert storage["free_gb"] > 0
        assert isinstance(storage["low"], bool)


class TestSingletonLease:
    def test_a_second_service_cannot_take_the_lease(self, tmp_path):
        path = str(tmp_path / "shared.db")
        first = AutopilotService(db_path=path).open()
        second = AutopilotService(db_path=path).open()
        assert first.db.acquire_lease(first.holder, ttl_seconds=120)
        assert not second.db.acquire_lease(second.holder, ttl_seconds=120)
        first.db.close()
        second.db.close()

    def test_each_process_gets_a_distinct_holder_id(self, tmp_path):
        a = AutopilotService(db_path=str(tmp_path / "a.db"))
        b = AutopilotService(db_path=str(tmp_path / "b.db"))
        assert a.holder != b.holder

    def test_the_status_view_names_the_lease_holder(self, service):
        service.db.acquire_lease(service.holder, ttl_seconds=60)
        scheduler = service.status()["scheduler"]
        assert scheduler["is_this_process"] is True
        assert scheduler["holder"] == service.holder


class TestRetentionIntegration:
    def test_files_awaiting_publication_are_reported_as_in_use(self, service):
        enable(service)
        _run_to_scheduled(service)
        in_use = service.files_in_use()
        assert len(in_use) == 3
        assert all(entry["filename"].endswith(".mp4") for entry in in_use)

    def test_nothing_is_pinned_once_everything_is_submitted(self, service):
        """Once the vendor holds the bytes the local clip is expendable —
        even though the posts are not live yet."""
        enable(service)
        _run_to_scheduled(service)
        for attempt in service.db.list_publish_attempts(limit=10):
            service.db.set_publish_state(attempt.id, PublishState.IN_FLIGHT)
            service.db.set_publish_state(attempt.id, PublishState.SUBMITTED)
        assert service.files_in_use() == []


class TestDryRun:
    """Discover, score, and preview the pick — never submit or publish."""

    def test_a_dry_run_discovers_and_shows_what_would_be_selected(self, service):
        enable(service)
        result = run_async(service.discover_dry_run())
        assert result["discovery"]["ok"] is True
        assert result["would_select"] is not None
        assert result["selection_tier"] in ("STRICT", "NORMAL", "EXPLORATION",
                                            "EXPLORATION_PICK")
        assert service.clip_gen.submissions == []          # nothing was submitted
        assert service.publisher.calls == []                # nothing was published

    def test_a_dry_run_explains_why_nothing_would_be_selected(self, service):
        enable(service)
        service.youtube.records = [make_record("vid00000001", now=NOW, license="youtube")]
        result = run_async(service.discover_dry_run())
        assert result["would_select"] is None
        assert result["diagnostic"]["bottleneck"] == "rights_policy"
        assert service.clip_gen.submissions == []


def _submitted_attempt(service, *, hours_ahead=6, now=NOW):
    """One attempt Upload-Post has accepted as a scheduled job."""
    _run_to_scheduled(service, now=now)
    attempt = service.db.list_publish_attempts(states=[PublishState.PENDING], limit=1)[0]
    service.db.execute("UPDATE publish_attempt SET scheduled_for_utc = ? WHERE id = ?",
                       (iso(now + timedelta(hours=hours_ahead)), attempt.id))
    service.db.set_publish_state(attempt.id, PublishState.IN_FLIGHT)
    service.db.set_publish_state(attempt.id, PublishState.SUBMITTED,
                                 vendor_job_id=f"scheduler_job_{attempt.id}",
                                 vendor_request_id=f"klippo-{attempt.id}")
    return service.db.get_publish_attempt(attempt.id)


def _run_to_scheduled(service, now=NOW):
    """Discover → submit → complete → schedule, stopping before any upload."""
    run_async(service.orchestrator.run_discovery(now=now, force=True))
    run_async(service.orchestrator._start_next_source(service.get_settings(), now=now))
    job_id = service.clip_gen.submissions[0]["job_id"]
    service.clip_gen.complete(job_id)
    active = service.db.active_processing_source()
    run_async(service.orchestrator._reconcile_active(active, service.get_settings(),
                                                     now=now))
    run_async(service.orchestrator._schedule_ready_sources(service.get_settings(),
                                                           now=now))
    return job_id
