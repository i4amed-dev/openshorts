"""Upgrading an existing Autopilot database must not lose anything.

A v1 database is a real operator's history: which YouTube sources were already
processed and which posts were already sent. Recreating it would reintroduce
exactly the duplicates the whole design exists to prevent, so the migration is
forward-only, in-place and tested against a genuine v1 file.
"""
import json
import sqlite3

import pytest

from automation.db import SCHEMA_VERSION, AutopilotDB, iso, utcnow
from automation.models import PublishState

# The v1 schema, verbatim enough to be a faithful "old database". Deliberately
# not imported from the current module — that would test nothing.
V1_SCHEMA = """
CREATE TABLE autopilot_settings (id INTEGER PRIMARY KEY CHECK (id = 1),
    config_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE autopilot_state (id INTEGER PRIMARY KEY CHECK (id = 1),
    engine_status TEXT NOT NULL DEFAULT 'OFF', pause_requested INTEGER NOT NULL DEFAULT 0,
    paused_reason TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_discovery_at TEXT, last_tick_at TEXT, updated_at TEXT);
CREATE TABLE scheduler_lease (id INTEGER PRIMARY KEY CHECK (id = 1), holder TEXT NOT NULL,
    acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE autopilot_run (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT, stats_json TEXT, error TEXT);
CREATE TABLE discovered_source (id INTEGER PRIMARY KEY AUTOINCREMENT,
    youtube_video_id TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
    channel_id TEXT NOT NULL DEFAULT '', channel_title TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
    published_at TEXT, duration_seconds INTEGER NOT NULL DEFAULT 0,
    category_id TEXT NOT NULL DEFAULT '', view_count INTEGER NOT NULL DEFAULT 0,
    like_count INTEGER, comment_count INTEGER, license TEXT NOT NULL DEFAULT '',
    definition TEXT NOT NULL DEFAULT '', caption_available INTEGER NOT NULL DEFAULT 0,
    live_state TEXT NOT NULL DEFAULT 'none', made_for_kids INTEGER NOT NULL DEFAULT 0,
    age_restricted INTEGER NOT NULL DEFAULT 0, privacy_status TEXT NOT NULL DEFAULT 'public',
    embeddable INTEGER NOT NULL DEFAULT 1, discovery_source TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL, chart_rank INTEGER, run_id TEXT,
    score REAL NOT NULL DEFAULT 0, score_breakdown TEXT NOT NULL DEFAULT '{}',
    eligible INTEGER NOT NULL DEFAULT 0, rejection_reason TEXT,
    state TEXT NOT NULL DEFAULT 'DISCOVERED', state_changed_at TEXT NOT NULL,
    selected_at TEXT, job_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, rights_policy TEXT, next_retry_at TEXT);
CREATE UNIQUE INDEX ux_source_job ON discovered_source (job_id) WHERE job_id IS NOT NULL;
CREATE TABLE processing_attempt (id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES discovered_source (id) ON DELETE CASCADE,
    job_id TEXT NOT NULL UNIQUE, attempt_no INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, error TEXT);
CREATE TABLE generated_clip (id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES discovered_source (id) ON DELETE CASCADE,
    job_id TEXT NOT NULL, clip_index INTEGER NOT NULL, filename TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
    start_seconds REAL NOT NULL DEFAULT 0, end_seconds REAL NOT NULL DEFAULT 0,
    rank INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'PENDING',
    skip_reason TEXT, created_at TEXT NOT NULL, UNIQUE (job_id, clip_index));
CREATE TABLE publish_attempt (id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL REFERENCES generated_clip (id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES discovered_source (id) ON DELETE CASCADE,
    job_id TEXT NOT NULL, clip_index INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE, platforms TEXT NOT NULL,
    scheduled_for_utc TEXT, timezone TEXT NOT NULL DEFAULT 'UTC',
    state TEXT NOT NULL DEFAULT 'PENDING', vendor_response TEXT, error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0, submitted_at TEXT, created_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX ux_publish_live_clip ON publish_attempt (clip_id)
    WHERE state IN ('PENDING', 'IN_FLIGHT', 'SUBMITTED', 'UNCERTAIN');
CREATE UNIQUE INDEX ux_publish_live_slot ON publish_attempt (scheduled_for_utc)
    WHERE state IN ('PENDING', 'IN_FLIGHT', 'SUBMITTED', 'UNCERTAIN');
CREATE TABLE event_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info', stage TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '', run_id TEXT, source_id INTEGER,
    youtube_video_id TEXT, job_id TEXT, publish_attempt_id INTEGER, data_json TEXT);
CREATE TABLE api_quota (provider TEXT PRIMARY KEY, day TEXT NOT NULL,
    units_used INTEGER NOT NULL DEFAULT 0, exhausted_until TEXT, last_error TEXT,
    updated_at TEXT NOT NULL);
"""


@pytest.fixture
def v1_database(tmp_path):
    """A populated v1 file: two sources, clips, and posts in various states."""
    path = str(tmp_path / "autopilot.db")
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    now = iso(utcnow())

    conn.execute("INSERT INTO autopilot_settings VALUES (1, ?, ?)",
                 (json.dumps({"enabled": True, "timezone": "Europe/Madrid",
                              "discovery": {"creative_commons_search_only": True,
                                            "region_code": "ES"}}), now))
    for index, (video_id, state) in enumerate(
            [("vid00000001", "DONE"), ("vid00000002", "ELIGIBLE")], start=1):
        conn.execute(
            "INSERT INTO discovered_source (youtube_video_id, url, title, discovered_at,"
            " state, state_changed_at, job_id, score) VALUES (?,?,?,?,?,?,?,?)",
            (video_id, f"https://youtu.be/{video_id}", f"Video {index}", now, state, now,
             f"job-{index}" if state == "DONE" else None, 70.0 + index))
    conn.execute(
        "INSERT INTO generated_clip (source_id, job_id, clip_index, filename, state,"
        " created_at) VALUES (1, 'job-1', 0, 'clip_1.mp4', 'PUBLISHED', ?)", (now,))
    conn.execute(
        "INSERT INTO generated_clip (source_id, job_id, clip_index, filename, state,"
        " created_at) VALUES (1, 'job-1', 1, 'clip_2.mp4', 'SCHEDULED', ?)", (now,))
    # v1 recorded the whole vendor body but never broke out the identifiers.
    conn.execute(
        "INSERT INTO publish_attempt (clip_id, source_id, job_id, clip_index,"
        " idempotency_key, platforms, scheduled_for_utc, state, vendor_response,"
        " submitted_at, created_at) VALUES (1, 1, 'job-1', 0, 'key-1', ?, ?, 'SUBMITTED',"
        " ?, ?, ?)",
        (json.dumps(["tiktok", "youtube"]), now,
         json.dumps({"success": True, "job_id": "scheduler_job_77"}), now, now))
    conn.execute(
        "INSERT INTO publish_attempt (clip_id, source_id, job_id, clip_index,"
        " idempotency_key, platforms, state, created_at) VALUES (2, 1, 'job-1', 1,"
        " 'key-2', ?, 'PENDING', ?)", (json.dumps(["tiktok"]), now))
    conn.execute("INSERT INTO api_quota VALUES ('youtube', ?, 4200, NULL, NULL, ?)",
                 (utcnow().strftime("%Y-%m-%d"), now))
    conn.execute("INSERT INTO event_log (ts, stage, message) VALUES (?, 'engine', 'old event')",
                 (now,))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    return path


class TestForwardMigration:
    def test_the_version_advances(self, v1_database):
        db = AutopilotDB(v1_database).connect()
        assert db.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION == 2
        db.close()

    def test_no_row_is_lost(self, v1_database):
        db = AutopilotDB(v1_database).connect()
        counts = {t: db.query_one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
                  for t in ("discovered_source", "generated_clip", "publish_attempt",
                            "event_log", "api_quota")}
        assert counts == {"discovered_source": 2, "generated_clip": 2,
                          "publish_attempt": 2, "event_log": 1, "api_quota": 1}
        db.close()

    def test_source_history_survives(self, v1_database):
        """The dedup guarantee: an already-processed video stays known."""
        db = AutopilotDB(v1_database).connect()
        source = db.get_source_by_video_id("vid00000001")
        assert source.state == "DONE"
        assert source.job_id == "job-1"
        assert db.known_video_ids(["vid00000001", "vid00000002"]) == {
            "vid00000001", "vid00000002"}
        db.close()

    def test_settings_survive(self, v1_database):
        db = AutopilotDB(v1_database).connect()
        stored = db.load_settings()
        assert stored["timezone"] == "Europe/Madrid"
        assert stored["discovery"]["region_code"] == "ES"
        db.close()

    def test_the_retired_config_flag_is_dropped_without_touching_the_rest(self, v1_database):
        """`creative_commons_search_only` is gone; nothing else resets."""
        from automation.service import AutopilotService
        service = AutopilotService(db_path=v1_database).open()
        config = service.get_settings()
        assert "creative_commons_search_only" not in config["discovery"]
        assert config["timezone"] == "Europe/Madrid"
        assert config["discovery"]["region_code"] == "ES"
        service.db.close()

    def test_vendor_identifiers_are_recovered_from_the_stored_response(self, v1_database):
        """v1 kept the body but not the ids — so historic posts stay reconcilable."""
        db = AutopilotDB(v1_database).connect()
        attempt = db.get_publish_attempt(1)
        assert attempt.vendor_job_id == "scheduler_job_77"
        db.close()

    def test_previously_submitted_posts_are_queued_for_a_status_check(self, v1_database):
        """v1 called these 'published' without ever asking. Now we ask."""
        db = AutopilotDB(v1_database).connect()
        attempt = db.get_publish_attempt(1)
        assert attempt.state == PublishState.SUBMITTED
        assert attempt.next_status_check_at is not None
        assert len(db.attempts_due_for_status_check(utcnow())) == 1
        db.close()

    def test_quota_rows_move_into_the_general_bucket(self, v1_database):
        db = AutopilotDB(v1_database).connect()
        assert db.get_quota("youtube", "general")["units_used"] == 4200
        assert db.get_quota("youtube", "search")["units_used"] == 0
        db.close()

    def test_the_widened_uniqueness_indexes_replace_the_old_ones(self, v1_database):
        db = AutopilotDB(v1_database).connect()
        names = {row["name"] for row in db.query(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "ux_publish_live_clip_v2" in names
        assert "ux_publish_live_slot_v2" in names
        # The narrow v1 indexes are gone: they would let a PUBLISHING or
        # PARTIAL_FAILED attempt double-book a clip.
        assert "ux_publish_live_clip" not in names
        assert "ux_publish_live_slot" not in names
        db.close()

    def test_idempotency_still_holds_after_the_upgrade(self, v1_database):
        from automation.models import PublishAttempt
        db = AutopilotDB(v1_database).connect()
        duplicate = db.reserve_publish_attempt(PublishAttempt(
            clip_id=1, source_id=1, job_id="job-1", clip_index=0,
            idempotency_key="key-1", platforms=["tiktok", "youtube"]))
        assert duplicate is None       # UNIQUE(idempotency_key) survived
        db.close()

    def test_migrating_twice_is_a_no_op(self, v1_database):
        first = AutopilotDB(v1_database).connect()
        first.close()
        second = AutopilotDB(v1_database).connect()
        assert second.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
        assert second.query_one("SELECT COUNT(*) AS n FROM publish_attempt")["n"] == 2
        second.close()

    def test_the_upgraded_database_is_fully_usable(self, v1_database):
        """Not just readable — the new lifecycle works on the migrated file."""
        db = AutopilotDB(v1_database).connect()
        attempt = db.get_publish_attempt(1)
        assert db.set_publish_state(attempt.id, PublishState.PUBLISHING,
                                    vendor_status="processing")
        assert db.set_publish_state(attempt.id, PublishState.PUBLISHED,
                                    vendor_results=[{"platform": "tiktok",
                                                     "status": "completed"}])
        refreshed = db.get_publish_attempt(attempt.id)
        assert refreshed.state == PublishState.PUBLISHED
        assert refreshed.finalized_at
        db.close()


class TestFreshDatabase:
    def test_a_new_file_starts_at_the_current_version(self, tmp_path):
        db = AutopilotDB(str(tmp_path / "new.db")).connect()
        assert db.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
        db.close()

    def test_a_new_file_has_the_v2_columns(self, tmp_path):
        db = AutopilotDB(str(tmp_path / "new.db")).connect()
        columns = {row[1] for row in db.query("PRAGMA table_info(publish_attempt)")}
        assert {"vendor_request_id", "vendor_job_id", "vendor_status", "vendor_results",
                "last_status_check_at", "next_status_check_at", "finalized_at"} <= columns
        db.close()

    def test_a_newer_schema_is_still_refused(self, tmp_path):
        path = str(tmp_path / "future.db")
        db = AutopilotDB(path).connect()
        db.execute("PRAGMA user_version = 99")
        db.close()
        with pytest.raises(RuntimeError, match="newer schema"):
            AutopilotDB(path).connect()
