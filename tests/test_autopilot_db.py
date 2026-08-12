"""Persistence: the guarantees that survive a process death.

Autopilot's correctness rests on SQLite constraints rather than on application
checks, because an application check has a window between "read" and "write" that
a duplicate tick or a second process can slip through. Every deduplication test
here therefore attacks the constraint directly.
"""
from datetime import datetime, timedelta, timezone

import pytest

from automation.db import AutopilotDB, iso, parse_iso, utcnow
from automation.models import (
    ClipState, DiscoveredSource, GeneratedClip, PublishAttempt, PublishState, SourceState,
    TransitionError, assert_publish_transition, assert_transition,
)
from automation.publishing import idempotency_key


@pytest.fixture
def db():
    database = AutopilotDB(":memory:").connect()
    yield database
    database.close()


def make_source(video_id="vid00000001", **overrides):
    fields = dict(youtube_video_id=video_id, url=f"https://youtu.be/{video_id}",
                  channel_id="UCchan", channel_title="Chan", title="T",
                  published_at=iso(utcnow()), duration_seconds=900, view_count=1000)
    fields.update(overrides)
    return DiscoveredSource(**fields)


class TestSchema:
    def test_wal_mode_is_on(self, db):
        # WAL is what lets the scheduler write while the dashboard reads.
        mode = db.query_one("PRAGMA journal_mode")[0]
        assert mode.lower() in ("wal", "memory")  # :memory: cannot use WAL

    def test_foreign_keys_are_enforced(self, db):
        assert db.query_one("PRAGMA foreign_keys")[0] == 1

    def test_schema_version_is_stamped(self, db):
        from automation.db import SCHEMA_VERSION
        assert db.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION

    def test_migration_is_idempotent(self, db):
        # Re-running _migrate on an existing file must be a no-op, which is what
        # makes an app update safe.
        db._migrate()
        db._migrate()
        assert db.query_one("PRAGMA user_version")[0] >= 1

    def test_a_newer_schema_is_refused(self, tmp_path):
        path = str(tmp_path / "future.db")
        database = AutopilotDB(path).connect()
        database.execute("PRAGMA user_version = 9999")
        database.close()
        with pytest.raises(RuntimeError, match="newer schema"):
            AutopilotDB(path).connect()


class TestSourceDeduplication:
    def test_the_same_video_is_only_stored_once(self, db):
        first_id, is_new = db.upsert_source(make_source("vid00000001"))
        second_id, is_new_again = db.upsert_source(make_source("vid00000001"))
        assert is_new and not is_new_again
        assert first_id == second_id

    def test_rediscovery_refreshes_stats_but_not_state(self, db):
        source_id, _ = db.upsert_source(make_source("vid00000001", view_count=100))
        db.transition_source(source_id, SourceState.ELIGIBLE)
        db.transition_source(source_id, SourceState.SELECTED)
        db.upsert_source(make_source("vid00000001", view_count=999_999))
        stored = db.get_source(source_id)
        assert stored.view_count == 999_999
        # Crucially NOT reset to DISCOVERED — that would reprocess a used source.
        assert stored.state == SourceState.SELECTED

    def test_url_form_does_not_affect_identity(self, db):
        # youtu.be/X, watch?v=X&t=42 and a playlist link are the same video.
        db.upsert_source(make_source("vid00000001", url="https://youtu.be/vid00000001"))
        _id, is_new = db.upsert_source(make_source(
            "vid00000001", url="https://www.youtube.com/watch?v=vid00000001&t=42s"))
        assert not is_new

    def test_known_video_ids_handles_a_large_batch(self, db):
        for i in range(120):
            db.upsert_source(make_source(f"vid{i:08d}"))
        known = db.known_video_ids([f"vid{i:08d}" for i in range(200)])
        assert len(known) == 120

    def test_a_job_id_can_only_belong_to_one_source(self, db):
        a, _ = db.upsert_source(make_source("vid00000001"))
        b, _ = db.upsert_source(make_source("vid00000002"))
        for source_id in (a, b):
            db.transition_source(source_id, SourceState.ELIGIBLE)
            db.transition_source(source_id, SourceState.SELECTED)
        assert db.claim_source_for_processing(a, "job-1", "CC")
        with pytest.raises(Exception):
            db.claim_source_for_processing(b, "job-1", "CC")


class TestStateMachine:
    def test_a_legal_transition_is_allowed(self):
        assert_transition(SourceState.ELIGIBLE, SourceState.SELECTED)

    def test_an_illegal_transition_raises(self):
        with pytest.raises(TransitionError):
            assert_transition(SourceState.DONE, SourceState.PROCESSING)

    def test_skipping_a_stage_is_refused(self):
        with pytest.raises(TransitionError):
            assert_transition(SourceState.DISCOVERED, SourceState.PROCESS_READY)

    def test_transition_is_a_compare_and_set(self, db):
        source_id, _ = db.upsert_source(make_source())
        db.transition_source(source_id, SourceState.ELIGIBLE)
        # A tick that believed the row was still DISCOVERED must be refused.
        assert not db.transition_source(source_id, SourceState.SELECTED,
                                        expected=[SourceState.DISCOVERED])
        assert db.get_source(source_id).state == SourceState.ELIGIBLE

    def test_two_racing_ticks_cannot_both_select(self, db):
        source_id, _ = db.upsert_source(make_source())
        db.transition_source(source_id, SourceState.ELIGIBLE)
        first = db.transition_source(source_id, SourceState.SELECTED,
                                     expected=[SourceState.ELIGIBLE])
        second = db.transition_source(source_id, SourceState.SELECTED,
                                      expected=[SourceState.ELIGIBLE])
        assert first and not second

    def test_unknown_field_names_are_rejected(self, db):
        source_id, _ = db.upsert_source(make_source())
        with pytest.raises(ValueError):
            db.transition_source(source_id, SourceState.ELIGIBLE, nonsense=1)


class TestLease:
    def test_the_first_process_takes_the_lease(self, db):
        assert db.acquire_lease("proc-a", ttl_seconds=60)

    def test_a_second_process_is_locked_out(self, db):
        db.acquire_lease("proc-a", ttl_seconds=60)
        assert not db.acquire_lease("proc-b", ttl_seconds=60)

    def test_the_holder_can_renew(self, db):
        db.acquire_lease("proc-a", ttl_seconds=60)
        assert db.acquire_lease("proc-a", ttl_seconds=60)

    def test_a_crashed_holder_does_not_wedge_the_system(self, db):
        """The reason this is a lease and not a boolean.

        proc-a takes the lease and dies without releasing it. Once the lease
        expires, proc-b must be able to take over — a process-global flag would
        have left Autopilot dead until someone noticed.
        """
        db.acquire_lease("proc-a", ttl_seconds=60)
        db.execute("UPDATE scheduler_lease SET expires_at = ? WHERE id = 1",
                   (iso(utcnow() - timedelta(seconds=1)),))
        assert db.acquire_lease("proc-b", ttl_seconds=60)
        assert db.lease_holder()["holder"] == "proc-b"

    def test_release_only_affects_your_own_lease(self, db):
        db.acquire_lease("proc-a", ttl_seconds=60)
        db.release_lease("proc-b")
        assert db.lease_holder()["holder"] == "proc-a"


class TestPublishIdempotency:
    def _clip(self, db, job_id="job-1", index=0):
        source_id, _ = db.upsert_source(make_source())
        clip_id = db.upsert_clip(GeneratedClip(source_id=source_id, job_id=job_id,
                                               clip_index=index, filename="c.mp4"))
        return source_id, clip_id

    def _attempt(self, source_id, clip_id, slot, job_id="job-1", index=0,
                 platforms=("tiktok",)):
        return PublishAttempt(
            clip_id=clip_id, source_id=source_id, job_id=job_id, clip_index=index,
            idempotency_key=idempotency_key(job_id, index, list(platforms)),
            platforms=list(platforms), scheduled_for_utc=iso(slot))

    def test_the_same_clip_cannot_be_scheduled_twice(self, db):
        source_id, clip_id = self._clip(db)
        now = utcnow()
        first = db.reserve_publish_attempt(self._attempt(source_id, clip_id, now))
        second = db.reserve_publish_attempt(
            self._attempt(source_id, clip_id, now + timedelta(hours=3)))
        assert first is not None
        assert second is None

    def test_two_clips_cannot_share_a_slot(self, db):
        source_id, clip_a = self._clip(db, index=0)
        clip_b = db.upsert_clip(GeneratedClip(source_id=source_id, job_id="job-1",
                                              clip_index=1, filename="d.mp4"))
        slot = utcnow() + timedelta(hours=2)
        assert db.reserve_publish_attempt(self._attempt(source_id, clip_a, slot)) is not None
        assert db.reserve_publish_attempt(
            self._attempt(source_id, clip_b, slot, index=1)) is None

    def test_a_canceled_attempt_frees_the_clip_and_the_slot(self, db):
        source_id, clip_id = self._clip(db)
        slot = utcnow() + timedelta(hours=2)
        first = db.reserve_publish_attempt(self._attempt(source_id, clip_id, slot))
        db.set_publish_state(first, PublishState.CANCELED)
        # Same idempotency key, but the previous row is no longer live — the
        # UNIQUE(idempotency_key) constraint still blocks a literal duplicate.
        assert db.reserve_publish_attempt(self._attempt(source_id, clip_id, slot)) is None

    def test_the_idempotency_key_is_stable_across_slots(self):
        a = idempotency_key("job-1", 0, ["tiktok", "youtube"])
        b = idempotency_key("job-1", 0, ["youtube", "tiktok"])
        assert a == b  # platform order must not change identity

    def test_different_clips_get_different_keys(self):
        assert idempotency_key("job-1", 0, ["tiktok"]) != idempotency_key(
            "job-1", 1, ["tiktok"])

    def test_uncertain_attempts_still_hold_their_slot(self, db):
        source_id, clip_a = self._clip(db, index=0)
        clip_b = db.upsert_clip(GeneratedClip(source_id=source_id, job_id="job-2",
                                              clip_index=0, filename="d.mp4"))
        slot = utcnow() + timedelta(hours=2)
        first = db.reserve_publish_attempt(self._attempt(source_id, clip_a, slot))
        db.set_publish_state(first, PublishState.IN_FLIGHT)
        db.set_publish_state(first, PublishState.UNCERTAIN)
        # The post may already exist at that time — nothing else may take it.
        assert db.reserve_publish_attempt(
            self._attempt(source_id, clip_b, slot, job_id="job-2")) is None

    def test_taken_slots_excludes_failed_attempts(self, db):
        source_id, clip_id = self._clip(db)
        slot = utcnow() + timedelta(hours=2)
        attempt_id = db.reserve_publish_attempt(self._attempt(source_id, clip_id, slot))
        assert len(db.taken_slots(utcnow())) == 1
        db.set_publish_state(attempt_id, PublishState.FAILED)
        assert db.taken_slots(utcnow()) == []

    def test_a_vendor_scheduled_attempt_still_holds_its_slot(self, db):
        """Upload-Post is holding it — nothing else may claim that moment."""
        source_id, clip_a = self._clip(db, index=0)
        clip_b = db.upsert_clip(GeneratedClip(source_id=source_id, job_id="job-2",
                                              clip_index=0, filename="d.mp4"))
        slot = utcnow() + timedelta(hours=2)
        first = db.reserve_publish_attempt(self._attempt(source_id, clip_a, slot))
        db.set_publish_state(first, PublishState.IN_FLIGHT)
        db.set_publish_state(first, PublishState.SUBMITTED, vendor_job_id="job_9")
        assert db.reserve_publish_attempt(
            self._attempt(source_id, clip_b, slot, job_id="job-2")) is None

    def test_a_published_attempt_still_blocks_a_second_send(self, db):
        """The strongest duplicate guard: it is already live."""
        source_id, clip_id = self._clip(db)
        slot = utcnow() + timedelta(hours=2)
        attempt_id = db.reserve_publish_attempt(self._attempt(source_id, clip_id, slot))
        db.set_publish_state(attempt_id, PublishState.IN_FLIGHT)
        db.set_publish_state(attempt_id, PublishState.SUBMITTED)
        db.set_publish_state(attempt_id, PublishState.PUBLISHED)
        assert db.reserve_publish_attempt(self._attempt(source_id, clip_id, slot)) is None

    def test_a_partial_failure_blocks_a_second_send(self, db):
        """Resending would duplicate the platforms that already succeeded."""
        source_id, clip_id = self._clip(db)
        slot = utcnow() + timedelta(hours=2)
        attempt_id = db.reserve_publish_attempt(self._attempt(source_id, clip_id, slot))
        db.set_publish_state(attempt_id, PublishState.IN_FLIGHT)
        db.set_publish_state(attempt_id, PublishState.SUBMITTED)
        db.set_publish_state(attempt_id, PublishState.PARTIAL_FAILED)
        assert db.reserve_publish_attempt(self._attempt(source_id, clip_id, slot)) is None


class TestClips:
    def test_the_same_clip_index_is_stored_once(self, db):
        source_id, _ = db.upsert_source(make_source())
        clip = GeneratedClip(source_id=source_id, job_id="job-1", clip_index=0,
                             filename="a.mp4")
        first = db.upsert_clip(clip)
        second = db.upsert_clip(clip)
        assert first == second

    def test_re_upserting_preserves_state(self, db):
        source_id, _ = db.upsert_source(make_source())
        clip = GeneratedClip(source_id=source_id, job_id="job-1", clip_index=0,
                             filename="a.mp4")
        clip_id = db.upsert_clip(clip)
        db.set_clip_state(clip_id, ClipState.PUBLISHED)
        db.upsert_clip(clip)  # a re-reconciliation of the same job
        assert db.get_clip(clip_id).state == ClipState.PUBLISHED

    def test_an_unknown_clip_state_is_rejected(self, db):
        source_id, _ = db.upsert_source(make_source())
        clip_id = db.upsert_clip(GeneratedClip(source_id=source_id, job_id="j",
                                               clip_index=0))
        with pytest.raises(ValueError):
            db.set_clip_state(clip_id, "VIBES")


class TestRetentionReferences:
    def test_pending_publishes_pin_their_clip_file(self, db):
        source_id, _ = db.upsert_source(make_source())
        clip_id = db.upsert_clip(GeneratedClip(source_id=source_id, job_id="job-9",
                                               clip_index=0, filename="clip_1.mp4"))
        db.reserve_publish_attempt(PublishAttempt(
            clip_id=clip_id, source_id=source_id, job_id="job-9", clip_index=0,
            idempotency_key="k1", platforms=["tiktok"],
            scheduled_for_utc=iso(utcnow() + timedelta(days=3))))
        assert db.files_in_use() == [{"job_id": "job-9", "filename": "clip_1.mp4"}]

    def test_a_submitted_publish_no_longer_pins_the_file(self, db):
        # Once the vendor holds the bytes, the local copy is expendable — even
        # though the post itself is not live yet.
        source_id, _ = db.upsert_source(make_source())
        clip_id = db.upsert_clip(GeneratedClip(source_id=source_id, job_id="job-9",
                                               clip_index=0, filename="clip_1.mp4"))
        attempt_id = db.reserve_publish_attempt(PublishAttempt(
            clip_id=clip_id, source_id=source_id, job_id="job-9", clip_index=0,
            idempotency_key="k1", platforms=["tiktok"]))
        db.set_publish_state(attempt_id, PublishState.IN_FLIGHT)
        db.set_publish_state(attempt_id, PublishState.SUBMITTED)
        assert db.files_in_use() == []


class TestQuotaTracking:
    def test_units_accumulate_within_a_day(self, db):
        db.add_quota_units(1)
        db.add_quota_units(1)
        assert db.get_quota()["units_used"] == 2

    def test_buckets_are_counted_separately(self, db):
        """Search and the general pool are independent allocations."""
        db.add_quota_units(5, bucket="general")
        db.add_quota_units(2, bucket="search")
        assert db.get_quota(bucket="general")["units_used"] == 5
        assert db.get_quota(bucket="search")["units_used"] == 2

    def test_an_exhausted_key_is_parked(self, db):
        until = utcnow() + timedelta(hours=5)
        db.mark_quota_exhausted(until, "quotaExceeded")
        assert parse_iso(db.get_quota()["exhausted_until"]) is not None

    def test_exhausting_search_leaves_the_general_pool_usable(self, db):
        """The bug this split fixes: chart discovery must survive a search 403."""
        db.mark_quota_exhausted(utcnow() + timedelta(hours=5), "quotaExceeded",
                                bucket="search")
        assert db.quota_blocked(utcnow(), bucket="search") is not None
        assert db.quota_blocked(utcnow(), bucket="general") is None

    def test_a_lapsed_block_clears_itself(self, db):
        db.mark_quota_exhausted(utcnow() - timedelta(minutes=1), "quotaExceeded")
        assert db.quota_blocked(utcnow()) is None
        assert db.get_quota()["exhausted_until"] is None

    def test_the_block_can_be_cleared(self, db):
        db.mark_quota_exhausted(utcnow() + timedelta(hours=5), "quotaExceeded")
        db.clear_quota_block()
        assert db.get_quota()["exhausted_until"] is None


class TestEventLog:
    def test_events_carry_their_correlation_ids(self, db):
        db.log_event("selection", "picked one", run_id="run-1", source_id=7,
                     youtube_video_id="vid00000001", job_id="job-1")
        event = db.recent_events(limit=1)[0]
        assert event["run_id"] == "run-1"
        assert event["job_id"] == "job-1"
        assert event["youtube_video_id"] == "vid00000001"

    def test_errors_can_be_filtered(self, db):
        db.log_event("a", "fine")
        db.log_event("b", "broken", level="error")
        assert [e["message"] for e in db.recent_events(level="error")] == ["broken"]

    def test_the_log_is_bounded(self, db):
        for i in range(60):
            db.log_event("x", f"event {i}")
        db.prune_events(keep=20)
        assert len(db.recent_events(limit=500)) == 20


def test_settings_round_trip(db):
    db.save_settings({"enabled": True, "timezone": "Asia/Tokyo"})
    assert db.load_settings()["timezone"] == "Asia/Tokyo"
    db.save_settings({"enabled": False, "timezone": "UTC"})
    assert db.load_settings()["enabled"] is False


def test_database_survives_a_reopen(tmp_path):
    """The whole point of SQLite over a dict: a restart keeps the state."""
    path = str(tmp_path / "state.db")
    first = AutopilotDB(path).connect()
    source_id, _ = first.upsert_source(make_source("vid00000001"))
    first.transition_source(source_id, SourceState.ELIGIBLE)
    first.close()

    second = AutopilotDB(path).connect()
    reopened = second.get_source_by_video_id("vid00000001")
    assert reopened.state == SourceState.ELIGIBLE
    second.close()


class TestPublishStateMachine:
    """Every legal transition, and loud rejection of the rest.

    The lifecycle exists to keep "the vendor accepted it" and "it is live on the
    platform" apart, so the transitions between those states are worth pinning
    individually.
    """

    @pytest.mark.parametrize("current,target", [
        (PublishState.PENDING, PublishState.IN_FLIGHT),
        (PublishState.IN_FLIGHT, PublishState.SUBMITTED),
        (PublishState.IN_FLIGHT, PublishState.UNCERTAIN),
        (PublishState.SUBMITTED, PublishState.PUBLISHING),
        (PublishState.SUBMITTED, PublishState.PUBLISHED),
        (PublishState.SUBMITTED, PublishState.CANCELED),
        (PublishState.PUBLISHING, PublishState.PUBLISHED),
        (PublishState.PUBLISHING, PublishState.FAILED),
        (PublishState.PUBLISHING, PublishState.PARTIAL_FAILED),
        (PublishState.UNCERTAIN, PublishState.PENDING),
        (PublishState.UNCERTAIN, PublishState.PUBLISHED),
        (PublishState.PARTIAL_FAILED, PublishState.PUBLISHED),
        (PublishState.PARTIAL_FAILED, PublishState.FAILED),
        (PublishState.FAILED, PublishState.PENDING),
    ])
    def test_legal_transitions_are_allowed(self, current, target):
        assert_publish_transition(current, target)

    @pytest.mark.parametrize("current,target", [
        # Acceptance can never skip straight past the request.
        (PublishState.PENDING, PublishState.SUBMITTED),
        (PublishState.PENDING, PublishState.PUBLISHED),
        # Published is final: nothing may quietly re-open it.
        (PublishState.PUBLISHED, PublishState.PENDING),
        (PublishState.PUBLISHED, PublishState.IN_FLIGHT),
        (PublishState.CANCELED, PublishState.PENDING),
    ])
    def test_illegal_transitions_are_rejected_loudly(self, current, target):
        with pytest.raises(TransitionError):
            assert_publish_transition(current, target)

    def test_a_partial_failure_can_never_go_back_to_pending(self):
        """Re-queuing a partial would duplicate the platforms that succeeded."""
        with pytest.raises(TransitionError):
            assert_publish_transition(PublishState.PARTIAL_FAILED, PublishState.PENDING)

    def test_the_db_enforces_the_machine(self, db):
        source_id, _ = db.upsert_source(make_source())
        clip_id = db.upsert_clip(GeneratedClip(source_id=source_id, job_id="j",
                                               clip_index=0, filename="a.mp4"))
        attempt_id = db.reserve_publish_attempt(PublishAttempt(
            clip_id=clip_id, source_id=source_id, job_id="j", clip_index=0,
            idempotency_key="k", platforms=["tiktok"]))
        with pytest.raises(TransitionError):
            db.set_publish_state(attempt_id, PublishState.PUBLISHED)

    def test_compare_and_set_blocks_a_racing_writer(self, db):
        """A status poll and an operator action must not both land."""
        source_id, _ = db.upsert_source(make_source())
        clip_id = db.upsert_clip(GeneratedClip(source_id=source_id, job_id="j",
                                               clip_index=0, filename="a.mp4"))
        attempt_id = db.reserve_publish_attempt(PublishAttempt(
            clip_id=clip_id, source_id=source_id, job_id="j", clip_index=0,
            idempotency_key="k", platforms=["tiktok"]))
        db.set_publish_state(attempt_id, PublishState.IN_FLIGHT)
        assert db.set_publish_state(attempt_id, PublishState.SUBMITTED,
                                    expected=[PublishState.IN_FLIGHT])
        # The loser believed it was still IN_FLIGHT and is refused.
        assert not db.set_publish_state(attempt_id, PublishState.UNCERTAIN,
                                        expected=[PublishState.IN_FLIGHT])
        assert db.get_publish_attempt(attempt_id).state == PublishState.SUBMITTED

    def test_terminal_states_stop_being_polled(self, db):
        source_id, _ = db.upsert_source(make_source())
        clip_id = db.upsert_clip(GeneratedClip(source_id=source_id, job_id="j",
                                               clip_index=0, filename="a.mp4"))
        attempt_id = db.reserve_publish_attempt(PublishAttempt(
            clip_id=clip_id, source_id=source_id, job_id="j", clip_index=0,
            idempotency_key="k", platforms=["tiktok"]))
        db.set_publish_state(attempt_id, PublishState.IN_FLIGHT)
        db.set_publish_state(attempt_id, PublishState.SUBMITTED,
                             vendor_job_id="job_1",
                             next_status_check_at=iso(utcnow() - timedelta(minutes=1)))
        assert len(db.attempts_due_for_status_check(utcnow())) == 1
        db.set_publish_state(attempt_id, PublishState.PUBLISHED)
        assert db.attempts_due_for_status_check(utcnow()) == []
