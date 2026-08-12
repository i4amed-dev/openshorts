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
    FakeClipGenerator, FakePublisher, FakeYouTubeClient, base_config, make_record,
    run_async,
)

NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_DATA_API_KEY", "test-yt-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    svc = AutopilotService(db_path=str(tmp_path / "svc.db")).open()
    clip_gen = FakeClipGenerator()
    publisher = FakePublisher()
    youtube = FakeYouTubeClient([make_record(f"vid0000000{i}", now=NOW)
                                 for i in range(1, 4)])
    svc._orchestrator.runtime = Runtime(clip_generator=clip_gen.port(),
                                        publisher=publisher.port())
    svc._orchestrator._client_factory = lambda: youtube
    svc.clip_gen, svc.publisher, svc.youtube = clip_gen, publisher, youtube
    yield svc
    svc.db.close()


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
            service.enable()


class TestOperatorActions:
    def test_enable_and_disable_flip_the_stored_flag(self, service):
        service.enable()
        assert service.get_settings()["enabled"] is True
        service.disable()
        assert service.get_settings()["enabled"] is False

    def test_enable_clears_a_tripped_breaker(self, service):
        service.db.update_engine_state(engine_status=EngineStatus.PAUSED_ERROR,
                                       consecutive_failures=9,
                                       paused_reason="everything broke")
        service.enable()
        state = service.db.load_engine_state()
        assert state["consecutive_failures"] == 0
        assert state["paused_reason"] is None

    def test_pause_is_a_soft_stop(self, service):
        service.enable()
        service.pause()
        assert service.db.load_engine_state()["pause_requested"] == 1
        service.resume()
        assert service.db.load_engine_state()["pause_requested"] == 0

    def test_emergency_stop_cancels_pending_posts_and_disables(self, service):
        service.enable()
        _run_to_scheduled(service)
        pending = service.db.list_publish_attempts(states=[PublishState.PENDING], limit=10)
        assert pending

        report = service.emergency_stop()
        assert report["canceled_publishes"] == len(pending)
        assert service.get_settings()["enabled"] is False
        assert all(a.state == PublishState.CANCELED
                   for a in service.db.list_publish_attempts(limit=20))

    def test_emergency_stop_does_not_claim_to_unsend_submitted_posts(self, service):
        """Honesty: once Upload-Post holds it, only Upload-Post can cancel it."""
        service.enable()
        _run_to_scheduled(service)
        attempt = service.db.list_publish_attempts(states=[PublishState.PENDING],
                                                   limit=1)[0]
        service.db.set_publish_state(attempt.id, PublishState.SUBMITTED)

        service.emergency_stop()
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.SUBMITTED
        note = service.db.recent_events(limit=1)[0]["message"]
        assert "remain on the Upload-Post calendar" in note

    def test_skip_removes_a_candidate_from_the_queue(self, service):
        service.enable()
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        candidate = service.db.list_sources(states=[SourceState.ELIGIBLE], limit=1)[0]
        assert service.skip_source(candidate.id)
        assert service.db.get_source(candidate.id).state == SourceState.SKIPPED
        # Skipping twice is not an error the caller should have to handle twice.
        assert not service.skip_source(candidate.id)

    def test_a_failed_source_can_be_re_queued(self, service):
        service.enable()
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
        service.enable()
        _run_to_scheduled(service)
        attempt = service.db.list_publish_attempts(limit=1)[0]
        service.db.set_publish_state(attempt.id, PublishState.FAILED, error="nope")
        assert service.retry_publish(attempt.id)
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.PENDING

    def test_an_uncertain_publish_needs_a_deliberate_decision(self, service):
        """No accidental double-post: plain retry refuses an ambiguous attempt."""
        service.enable()
        _run_to_scheduled(service)
        attempt = service.db.list_publish_attempts(limit=1)[0]
        service.db.set_publish_state(attempt.id, PublishState.UNCERTAIN)

        assert not service.retry_publish(attempt.id)       # ordinary retry refused
        assert service.force_retry_uncertain(attempt.id)   # explicit override works
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.PENDING

    def test_an_uncertain_publish_can_be_confirmed_as_landed(self, service):
        service.enable()
        _run_to_scheduled(service)
        attempt = service.db.list_publish_attempts(limit=1)[0]
        service.db.set_publish_state(attempt.id, PublishState.UNCERTAIN)

        assert service.resolve_uncertain(attempt.id)
        assert service.db.get_publish_attempt(attempt.id).state == PublishState.SUBMITTED
        assert service.db.get_clip(attempt.clip_id).state == ClipState.PUBLISHED

    def test_process_next_refuses_while_something_is_running(self, service):
        service.enable()
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        assert run_async(service.process_next_now())["ok"] is True
        result = run_async(service.process_next_now())
        assert result["ok"] is False
        assert "already processing" in result["reason"]


class TestStatusView:
    def test_answers_the_questions_an_operator_returns_with(self, service):
        service.enable()
        _run_to_scheduled(service)
        status = service.status(now=NOW)

        assert status["enabled"] is True
        assert status["stage"]
        assert status["next_discovery_at"]
        assert status["next_publish_at"]
        assert status["today"]["sources_selected"] == 1
        assert status["today"]["clips_generated"] == 3
        assert status["today"]["posts_scheduled"] == 3
        assert status["publish_attempts"]
        assert status["recent_selected"]

    def test_rejected_candidates_are_shown_with_their_reason(self, service, monkeypatch):
        service.enable()
        service.youtube.records = [
            make_record("vid00000001", now=NOW, license="youtube"),
            make_record("vid00000002", now=NOW),
        ]
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        rejected = service.status(now=NOW)["rejected"]
        assert any(r["rejection_reason"] == "rights_policy" for r in rejected)

    def test_the_score_breakdown_travels_with_each_candidate(self, service):
        service.enable()
        run_async(service.orchestrator.run_discovery(now=NOW, force=True))
        candidate = service.status(now=NOW)["queue"][0]
        assert candidate["score"] > 0
        assert candidate["score_breakdown"]["components"]
        assert candidate["score_breakdown"]["contributions"]

    def test_slots_are_reported_in_local_time_as_well_as_utc(self, service):
        service.enable()
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
        for secret in ("test-yt-key", "test-gemini-key", "test-key"):
            assert secret not in blob

    def test_quota_state_is_visible(self, service):
        service.db.add_quota_units(101)
        quota = service.status(now=NOW)["youtube_quota"]
        assert quota["units_used_today"] == 101
        assert quota["daily_budget"] == 10000
        assert quota["blocked_until"] is None

    def test_a_parked_quota_shows_when_it_lifts(self, service):
        service.db.mark_quota_exhausted(utcnow() + timedelta(hours=4), "quotaExceeded")
        assert service.status()["youtube_quota"]["blocked_until"]

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
        service.enable()
        _run_to_scheduled(service)
        in_use = service.files_in_use()
        assert len(in_use) == 3
        assert all(entry["filename"].endswith(".mp4") for entry in in_use)

    def test_nothing_is_pinned_once_everything_is_submitted(self, service):
        service.enable()
        _run_to_scheduled(service)
        for attempt in service.db.list_publish_attempts(limit=10):
            service.db.set_publish_state(attempt.id, PublishState.SUBMITTED)
        assert service.files_in_use() == []


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
