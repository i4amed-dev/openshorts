"""SQLite persistence for Autopilot.

Why SQLite and not the cloud Postgres: Autopilot is the *self-hosted* unattended
mode. ``cloud/`` only exists when ``BILLING_ENABLED`` is set, so depending on it
would make Autopilot impossible in exactly the deployment it is built for. A
single dedicated Mac does not need a database server; it needs a file that
survives a restart. SQLite in WAL mode is that, with real transactions, foreign
keys and uniqueness constraints doing the idempotency work.

Everything here is stdlib. One connection guarded by a re-entrant lock: writes
are tiny (a few rows per minute at most) and a single writer sidesteps WAL
writer contention entirely, while `check_same_thread=False` lets both the
scheduler task and FastAPI's threadpool reach it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from .models import (
    ClipState, DiscoveredSource, GeneratedClip, PublishAttempt, PublishState,
    SourceState, assert_publish_transition, assert_transition,
)

SCHEMA_VERSION = 3

# The DB lives beside the job output on the persistent volume, never in the
# ephemeral container layer. AUTOPILOT_DB_PATH overrides it for tests/ops.
DEFAULT_DB_PATH = os.environ.get(
    "AUTOPILOT_DB_PATH",
    os.path.join(os.environ.get("AUTOPILOT_STATE_DIR", "output"), "autopilot", "autopilot.db"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS autopilot_settings (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    config_json   TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS autopilot_state (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    engine_status        TEXT NOT NULL DEFAULT 'OFF',
    pause_requested      INTEGER NOT NULL DEFAULT 0,
    paused_reason        TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_discovery_at    TEXT,
    last_tick_at         TEXT,
    updated_at           TEXT
);

CREATE TABLE IF NOT EXISTS scheduler_lease (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    holder      TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS autopilot_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    stats_json  TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_run_started ON autopilot_run (started_at DESC);

CREATE TABLE IF NOT EXISTS discovered_source (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    youtube_video_id  TEXT NOT NULL UNIQUE,
    url               TEXT NOT NULL,
    channel_id        TEXT NOT NULL DEFAULT '',
    channel_title     TEXT NOT NULL DEFAULT '',
    title             TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL DEFAULT '',
    published_at      TEXT,
    duration_seconds  INTEGER NOT NULL DEFAULT 0,
    category_id       TEXT NOT NULL DEFAULT '',
    view_count        INTEGER NOT NULL DEFAULT 0,
    like_count        INTEGER,
    comment_count     INTEGER,
    license           TEXT NOT NULL DEFAULT '',
    definition        TEXT NOT NULL DEFAULT '',
    caption_available INTEGER NOT NULL DEFAULT 0,
    live_state        TEXT NOT NULL DEFAULT 'none',
    made_for_kids     INTEGER NOT NULL DEFAULT 0,
    age_restricted    INTEGER NOT NULL DEFAULT 0,
    privacy_status    TEXT NOT NULL DEFAULT 'public',
    embeddable        INTEGER NOT NULL DEFAULT 1,
    discovery_source  TEXT NOT NULL DEFAULT '',
    discovered_at     TEXT NOT NULL,
    chart_rank        INTEGER,
    run_id            TEXT,
    score             REAL NOT NULL DEFAULT 0,
    score_breakdown   TEXT NOT NULL DEFAULT '{}',
    eligible          INTEGER NOT NULL DEFAULT 0,
    rejection_reason  TEXT,
    state             TEXT NOT NULL DEFAULT 'DISCOVERED',
    state_changed_at  TEXT NOT NULL,
    selected_at       TEXT,
    job_id            TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    rights_policy     TEXT,
    next_retry_at     TEXT,
    -- v3: opportunity-scoring metadata (see automation/opportunity.py). Split
    -- out from `eligible`/`rejection_reason` so a candidate can show a high
    -- score AND a rights block at the same time instead of one flag hiding
    -- the other — see automation/eligibility.py's module docstring.
    discovery_lane     TEXT,
    age_cohort         TEXT,
    selection_tier     TEXT,
    technical_eligible INTEGER NOT NULL DEFAULT 1,
    policy_eligible    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_source_state ON discovered_source (state, score DESC);
CREATE INDEX IF NOT EXISTS ix_source_channel ON discovered_source (channel_id, selected_at);
CREATE INDEX IF NOT EXISTS ix_source_selected ON discovered_source (selected_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_source_job ON discovered_source (job_id)
    WHERE job_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS processing_attempt (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES discovered_source (id) ON DELETE CASCADE,
    job_id      TEXT NOT NULL UNIQUE,
    attempt_no  INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_attempt_source ON processing_attempt (source_id);

CREATE TABLE IF NOT EXISTS generated_clip (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      INTEGER NOT NULL REFERENCES discovered_source (id) ON DELETE CASCADE,
    job_id         TEXT NOT NULL,
    clip_index     INTEGER NOT NULL,
    filename       TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    start_seconds  REAL NOT NULL DEFAULT 0,
    end_seconds    REAL NOT NULL DEFAULT 0,
    rank           INTEGER NOT NULL DEFAULT 0,
    state          TEXT NOT NULL DEFAULT 'PENDING',
    skip_reason    TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (job_id, clip_index)
);
CREATE INDEX IF NOT EXISTS ix_clip_state ON generated_clip (state);

CREATE TABLE IF NOT EXISTS publish_attempt (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id           INTEGER NOT NULL REFERENCES generated_clip (id) ON DELETE CASCADE,
    source_id         INTEGER NOT NULL REFERENCES discovered_source (id) ON DELETE CASCADE,
    job_id            TEXT NOT NULL,
    clip_index        INTEGER NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    platforms         TEXT NOT NULL,
    scheduled_for_utc TEXT,
    timezone          TEXT NOT NULL DEFAULT 'UTC',
    state             TEXT NOT NULL DEFAULT 'PENDING',
    vendor_response   TEXT,
    error             TEXT,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    submitted_at      TEXT,
    created_at        TEXT NOT NULL,
    title             TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL DEFAULT '',
    -- v2: vendor reconciliation. A 2xx from /upload only means "accepted", so
    -- these are what let us later ask Upload-Post what actually happened.
    vendor_request_id TEXT,          -- ours, sent with the request (async tracking)
    vendor_job_id     TEXT,          -- theirs, returned for scheduled posts
    vendor_status     TEXT,          -- last top-level status seen
    vendor_results    TEXT,          -- per-platform outcomes, JSON
    last_status_check_at TEXT,
    next_status_check_at TEXT,
    finalized_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_publish_state ON publish_attempt (state, scheduled_for_utc);
CREATE INDEX IF NOT EXISTS ix_publish_recheck ON publish_attempt (next_status_check_at)
    WHERE next_status_check_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_publish_vendor_job ON publish_attempt (vendor_job_id)
    WHERE vendor_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_publish_slot ON publish_attempt (scheduled_for_utc);
CREATE INDEX IF NOT EXISTS ix_publish_clip ON publish_attempt (clip_id);
-- One live attempt per clip: a duplicate scheduler tick can insert a second row
-- only if the first was canceled or hard-failed.
-- v2 widened these to the new lifecycle states. A clip that is PUBLISHING,
-- PUBLISHED or PARTIAL_FAILED must still block a second attempt: the vendor has
-- the content, so another request would post it twice.
CREATE UNIQUE INDEX IF NOT EXISTS ux_publish_live_clip_v2 ON publish_attempt (clip_id)
    WHERE state IN ('PENDING', 'IN_FLIGHT', 'SUBMITTED', 'PUBLISHING', 'PUBLISHED',
                    'PARTIAL_FAILED', 'UNCERTAIN');
-- One post per slot: two clips can never be handed the same publishing time.
CREATE UNIQUE INDEX IF NOT EXISTS ux_publish_live_slot_v2 ON publish_attempt (scheduled_for_utc)
    WHERE state IN ('PENDING', 'IN_FLIGHT', 'SUBMITTED', 'PUBLISHING', 'PUBLISHED',
                    'PARTIAL_FAILED', 'UNCERTAIN');

CREATE TABLE IF NOT EXISTS event_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT NOT NULL,
    level              TEXT NOT NULL DEFAULT 'info',
    stage              TEXT NOT NULL DEFAULT '',
    message            TEXT NOT NULL DEFAULT '',
    run_id             TEXT,
    source_id          INTEGER,
    youtube_video_id   TEXT,
    job_id             TEXT,
    publish_attempt_id INTEGER,
    data_json          TEXT
);
CREATE INDEX IF NOT EXISTS ix_event_ts ON event_log (ts DESC);
CREATE INDEX IF NOT EXISTS ix_event_source ON event_log (source_id, ts DESC);

-- v2: one row per QUOTA BUCKET, not per provider. Since 1-jun-2026 the YouTube
-- Data API bills search.list against its own daily allocation (~100 calls),
-- independent of the 10,000-unit pool every other endpoint shares. Collapsing
-- them into one counter meant a search exhaustion disabled chart discovery too.
CREATE TABLE IF NOT EXISTS api_quota (
    provider     TEXT NOT NULL,
    bucket       TEXT NOT NULL DEFAULT 'general',
    day          TEXT NOT NULL,
    units_used   INTEGER NOT NULL DEFAULT 0,
    exhausted_until TEXT,
    last_error   TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (provider, bucket)
);

-- v3: cheap per-channel baseline for channel_outperformance (see
-- automation/channel_context.py). TTL'd in application code, not here — a
-- stale-but-present row is preferable to a synchronous API call on every
-- candidate.
CREATE TABLE IF NOT EXISTS channel_stats_cache (
    channel_id       TEXT PRIMARY KEY,
    subscriber_count INTEGER,
    view_count       INTEGER,
    video_count      INTEGER,
    fetched_at       TEXT NOT NULL
);

-- v3: optional Gemini shortlist evaluation (see automation/ports.py's
-- SemanticEvaluatorPort), cached forever per (video, model_version) so an
-- evergreen source that resurfaces across runs is never re-scored.
CREATE TABLE IF NOT EXISTS semantic_evaluation (
    youtube_video_id TEXT NOT NULL,
    model_version    TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    evaluated_at      TEXT NOT NULL,
    PRIMARY KEY (youtube_video_id, model_version)
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    """Canonical storage format: UTC, second precision, explicit +00:00."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _json_loads(text: Optional[str], fallback):
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback


class AutopilotDB:
    """Thread-safe repository over one SQLite file."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_DB_PATH
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    # --- lifecycle ------------------------------------------------------------

    def connect(self) -> "AutopilotDB":
        with self._lock:
            if self._conn is not None:
                return self
            if self.path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0,
                                   isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")   # survive a hard power loss
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._conn = conn
            self._migrate()
            return self

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    def _migrate(self) -> None:
        """Create/upgrade the schema, versioned through PRAGMA user_version.

        Forward-only and non-destructive: an existing database is upgraded in
        place, never recreated. Each version's block runs once, inside one
        transaction, so a crash mid-upgrade leaves the old version intact rather
        than a half-migrated file.
        """
        conn = self._conn
        assert conn is not None
        version = conn.execute("PRAGMA user_version").fetchone()[0]

        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Autopilot database is at schema v{version}, this build understands "
                f"v{SCHEMA_VERSION}. Refusing to run against a newer schema.")

        if version == 0:
            # Fresh database: the current schema is already the target.
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return

        if version < 2:
            self._migrate_v1_to_v2(conn)
            version = 2
            conn.execute("PRAGMA user_version = 2")

        if version < 3:
            self._migrate_v2_to_v3(conn)
            version = 3
            conn.execute("PRAGMA user_version = 3")

        # Idempotent tail: creates anything additive the running version added
        # (new tables/indexes) without touching existing data.
        conn.executescript(_SCHEMA)

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        """v1 → v2: vendor reconciliation columns and split quota buckets.

        v1 treated "Upload-Post accepted the job" as "published". v2 separates
        them, which needs somewhere to keep the vendor identifiers and the
        polling schedule. Existing rows are preserved and, where v1 already
        stored the vendor response, their identifiers are recovered from it so
        historic attempts can still be reconciled rather than being stranded.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(publish_attempt)")}
            for column, ddl in (
                ("vendor_request_id", "TEXT"),
                ("vendor_job_id", "TEXT"),
                ("vendor_status", "TEXT"),
                ("vendor_results", "TEXT"),
                ("last_status_check_at", "TEXT"),
                ("next_status_check_at", "TEXT"),
                ("finalized_at", "TEXT"),
            ):
                if column not in existing:
                    conn.execute(f"ALTER TABLE publish_attempt ADD COLUMN {column} {ddl}")

            # Recover identifiers v1 never broke out of the stored response, so
            # an already-submitted post keeps a route to reconciliation.
            now = iso(utcnow())
            for row in conn.execute(
                    "SELECT id, vendor_response FROM publish_attempt "
                    "WHERE vendor_response IS NOT NULL").fetchall():
                payload = _json_loads(row["vendor_response"], None)
                if not isinstance(payload, dict):
                    continue
                request_id = payload.get("request_id")
                job_id = payload.get("job_id")
                if request_id or job_id:
                    conn.execute(
                        "UPDATE publish_attempt SET vendor_request_id = ?, vendor_job_id = ? "
                        "WHERE id = ?",
                        (str(request_id) if request_id else None,
                         str(job_id) if job_id else None, row["id"]))

            # v1 SUBMITTED rows were shown to the user as "published" without
            # ever asking the vendor. Queue them for a status check instead of
            # guessing either way; reconciliation will settle each one.
            conn.execute(
                "UPDATE publish_attempt SET next_status_check_at = ? WHERE state = 'SUBMITTED'",
                (now,))

            # The old partial indexes named a narrower state set; they would now
            # let a PUBLISHING/PARTIAL_FAILED attempt double-book a clip or slot.
            conn.execute("DROP INDEX IF EXISTS ux_publish_live_clip")
            conn.execute("DROP INDEX IF EXISTS ux_publish_live_slot")

            # api_quota gains `bucket` in its primary key, which SQLite cannot do
            # in place — rebuild it, carrying every existing row into 'general'.
            quota_columns = {row[1] for row in conn.execute("PRAGMA table_info(api_quota)")}
            if "bucket" not in quota_columns:
                conn.execute("ALTER TABLE api_quota RENAME TO api_quota_v1")
                conn.execute("""
                    CREATE TABLE api_quota (
                        provider     TEXT NOT NULL,
                        bucket       TEXT NOT NULL DEFAULT 'general',
                        day          TEXT NOT NULL,
                        units_used   INTEGER NOT NULL DEFAULT 0,
                        exhausted_until TEXT,
                        last_error   TEXT,
                        updated_at   TEXT NOT NULL,
                        PRIMARY KEY (provider, bucket)
                    )""")
                conn.execute("""
                    INSERT INTO api_quota
                        (provider, bucket, day, units_used, exhausted_until, last_error, updated_at)
                    SELECT provider, 'general', day, units_used, exhausted_until, last_error,
                           updated_at FROM api_quota_v1""")
                conn.execute("DROP TABLE api_quota_v1")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def _migrate_v2_to_v3(self, conn: sqlite3.Connection) -> None:
        """v2 → v3: opportunity-scoring metadata columns.

        Age/views/velocity/engagement stopped being hard eligibility gates in
        this version (see eligibility.py) — the new columns let the dashboard
        show *why* a candidate scored the way it did (lane, age cohort,
        selection tier) and let rights-blocked candidates keep a visible
        opportunity score instead of the old single `eligible` flag
        conflating "good candidate" with "allowed to process". New tables
        (channel_stats_cache, semantic_evaluation) are created by the
        idempotent _SCHEMA tail that runs right after this, so nothing to do
        for those here — only existing-table column adds need explicit DDL.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(discovered_source)")}
            for column, ddl in (
                ("discovery_lane", "TEXT"),
                ("age_cohort", "TEXT"),
                ("selection_tier", "TEXT"),
                ("technical_eligible", "INTEGER NOT NULL DEFAULT 1"),
                ("policy_eligible", "INTEGER NOT NULL DEFAULT 1"),
            ):
                if column not in existing:
                    conn.execute(f"ALTER TABLE discovered_source ADD COLUMN {column} {ddl}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    @contextmanager
    def tx(self):
        """One serialized write transaction.

        BEGIN IMMEDIATE takes the write lock up front, so two racing ticks
        cannot both read "no attempt exists" and then both insert one.
        """
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def query(self, sql: str, params: Iterable = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, tuple(params)))

    def query_one(self, sql: str, params: Iterable = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    # --- settings -------------------------------------------------------------

    def load_settings(self) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT config_json FROM autopilot_settings WHERE id = 1")
        return _json_loads(row["config_json"], None) if row else None

    def save_settings(self, config: Dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO autopilot_settings (id, config_json, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET config_json = excluded.config_json, "
            "updated_at = excluded.updated_at",
            (json.dumps(config), iso(utcnow())),
        )

    # --- engine state ---------------------------------------------------------

    def load_engine_state(self) -> Dict[str, Any]:
        row = self.query_one("SELECT * FROM autopilot_state WHERE id = 1")
        if row is None:
            self.execute(
                "INSERT OR IGNORE INTO autopilot_state (id, engine_status, updated_at) "
                "VALUES (1, 'OFF', ?)", (iso(utcnow()),))
            row = self.query_one("SELECT * FROM autopilot_state WHERE id = 1")
        return dict(row) if row else {}

    def update_engine_state(self, **fields) -> None:
        if not fields:
            return
        allowed = {"engine_status", "pause_requested", "paused_reason",
                   "consecutive_failures", "last_discovery_at", "last_tick_at"}
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Unknown engine state field {key!r}")
            sets.append(f"{key} = ?")
            params.append(value)
        sets.append("updated_at = ?")
        params.append(iso(utcnow()))
        self.load_engine_state()  # ensure the row exists
        self.execute(f"UPDATE autopilot_state SET {', '.join(sets)} WHERE id = 1", params)

    # --- scheduler lease ------------------------------------------------------

    def acquire_lease(self, holder: str, ttl_seconds: int = 90) -> bool:
        """Take (or renew) the singleton scheduler lease.

        A crashed holder cannot wedge the system: the lease is only respected
        until ``expires_at``, so the next process picks it up one TTL later.
        """
        now = utcnow()
        expires = iso(now + timedelta(seconds=ttl_seconds))
        with self.tx() as conn:
            row = conn.execute("SELECT holder, expires_at FROM scheduler_lease WHERE id = 1"
                               ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO scheduler_lease (id, holder, acquired_at, expires_at) "
                    "VALUES (1, ?, ?, ?)", (holder, iso(now), expires))
                return True
            current_expiry = parse_iso(row["expires_at"])
            if row["holder"] == holder or current_expiry is None or current_expiry <= now:
                conn.execute(
                    "UPDATE scheduler_lease SET holder = ?, acquired_at = "
                    "CASE WHEN holder = ? THEN acquired_at ELSE ? END, expires_at = ? "
                    "WHERE id = 1", (holder, holder, iso(now), expires))
                return True
            return False

    def release_lease(self, holder: str) -> None:
        self.execute("DELETE FROM scheduler_lease WHERE id = 1 AND holder = ?", (holder,))

    def lease_holder(self) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT * FROM scheduler_lease WHERE id = 1")
        return dict(row) if row else None

    # --- runs -----------------------------------------------------------------

    def start_run(self, run_id: str, kind: str) -> None:
        self.execute(
            "INSERT OR IGNORE INTO autopilot_run (run_id, kind, status, started_at) "
            "VALUES (?, ?, ?, ?)", (run_id, kind, "RUNNING", iso(utcnow())))

    def finish_run(self, run_id: str, status: str, stats: Optional[Dict] = None,
                   error: Optional[str] = None) -> None:
        self.execute(
            "UPDATE autopilot_run SET status = ?, finished_at = ?, stats_json = ?, error = ? "
            "WHERE run_id = ?",
            (status, iso(utcnow()), json.dumps(stats or {}), error, run_id))

    def recent_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM autopilot_run ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for row in rows:
            item = dict(row)
            item["stats"] = _json_loads(item.pop("stats_json", None), {})
            out.append(item)
        return out

    # --- events ---------------------------------------------------------------

    def log_event(self, stage: str, message: str, *, level: str = "info",
                  run_id: Optional[str] = None, source_id: Optional[int] = None,
                  youtube_video_id: Optional[str] = None, job_id: Optional[str] = None,
                  publish_attempt_id: Optional[int] = None,
                  data: Optional[Dict] = None) -> None:
        self.execute(
            "INSERT INTO event_log (ts, level, stage, message, run_id, source_id, "
            "youtube_video_id, job_id, publish_attempt_id, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (iso(utcnow()), level, stage, message[:2000], run_id, source_id,
             youtube_video_id, job_id, publish_attempt_id,
             json.dumps(data) if data else None))

    def recent_events(self, limit: int = 100, level: Optional[str] = None,
                      source_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM event_log"
        clauses, params = [], []
        if level:
            clauses.append("level = ?")
            params.append(level)
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        out = []
        for row in self.query(sql, params):
            item = dict(row)
            item["data"] = _json_loads(item.pop("data_json", None), None)
            out.append(item)
        return out

    def prune_events(self, keep: int = 5000) -> int:
        """Bound the audit log so a machine left running for months stays small."""
        cur = self.execute(
            "DELETE FROM event_log WHERE id NOT IN "
            "(SELECT id FROM event_log ORDER BY id DESC LIMIT ?)", (keep,))
        return cur.rowcount or 0

    # --- sources --------------------------------------------------------------

    def upsert_source(self, source: DiscoveredSource) -> tuple[int, bool]:
        """Insert a freshly discovered source; returns ``(row_id, is_new)``.

        Identity is the YouTube ``videoId`` — never the URL, which varies by
        ``youtu.be`` form, playlist parameters, timestamps and tracking suffixes.
        An already-known video is left in whatever state it reached; only its
        volatile statistics are refreshed so a re-discovery cannot resurrect a
        source we already processed.
        """
        now = iso(utcnow())
        with self.tx() as conn:
            row = conn.execute(
                "SELECT id, state FROM discovered_source WHERE youtube_video_id = ?",
                (source.youtube_video_id,)).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE discovered_source SET view_count = ?, like_count = ?, "
                    "comment_count = ? WHERE id = ?",
                    (source.view_count, source.like_count, source.comment_count, row["id"]))
                return int(row["id"]), False
            cur = conn.execute(
                "INSERT INTO discovered_source ("
                " youtube_video_id, url, channel_id, channel_title, title, description,"
                " published_at, duration_seconds, category_id, view_count, like_count,"
                " comment_count, license, definition, caption_available, live_state,"
                " made_for_kids, age_restricted, privacy_status, embeddable,"
                " discovery_source, discovered_at, chart_rank, run_id, score,"
                " score_breakdown, eligible, rejection_reason, state, state_changed_at,"
                " discovery_lane, age_cohort, technical_eligible, policy_eligible"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source.youtube_video_id, source.watch_url, source.channel_id,
                 source.channel_title, source.title, source.description[:2000],
                 source.published_at, source.duration_seconds, source.category_id,
                 source.view_count, source.like_count, source.comment_count,
                 source.license, source.definition, int(source.caption_available),
                 source.live_state, int(source.made_for_kids), int(source.age_restricted),
                 source.privacy_status, int(source.embeddable), source.discovery_source,
                 now, source.chart_rank, source.run_id, source.score,
                 json.dumps(source.score_breakdown), int(source.eligible),
                 source.rejection_reason, SourceState.DISCOVERED, now,
                 source.discovery_lane, source.age_cohort,
                 int(source.technical_eligible), int(source.policy_eligible)))
            return int(cur.lastrowid), True

    def get_source(self, source_id: int) -> Optional[DiscoveredSource]:
        row = self.query_one("SELECT * FROM discovered_source WHERE id = ?", (source_id,))
        return _row_to_source(row) if row else None

    def get_source_by_video_id(self, video_id: str) -> Optional[DiscoveredSource]:
        row = self.query_one("SELECT * FROM discovered_source WHERE youtube_video_id = ?",
                             (video_id,))
        return _row_to_source(row) if row else None

    def get_source_by_job(self, job_id: str) -> Optional[DiscoveredSource]:
        row = self.query_one("SELECT * FROM discovered_source WHERE job_id = ?", (job_id,))
        return _row_to_source(row) if row else None

    def list_sources(self, states: Optional[Iterable[str]] = None, limit: int = 50,
                     order: str = "score") -> List[DiscoveredSource]:
        sql = "SELECT * FROM discovered_source"
        params: List[Any] = []
        if states:
            states = list(states)
            sql += f" WHERE state IN ({','.join('?' * len(states))})"
            params.extend(states)
        sql += " ORDER BY score DESC, id DESC" if order == "score" else " ORDER BY id DESC"
        sql += " LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        return [_row_to_source(row) for row in self.query(sql, params)]

    def set_source_score(self, source_id: int, score: float, breakdown: Dict[str, Any],
                         eligible: bool, rejection_reason: Optional[str], *,
                         discovery_lane: Optional[str] = None, age_cohort: Optional[str] = None,
                         technical_eligible: Optional[bool] = None,
                         policy_eligible: Optional[bool] = None) -> None:
        self.execute(
            "UPDATE discovered_source SET score = ?, score_breakdown = ?, eligible = ?, "
            "rejection_reason = ?, discovery_lane = COALESCE(?, discovery_lane), "
            "age_cohort = COALESCE(?, age_cohort), "
            "technical_eligible = COALESCE(?, technical_eligible), "
            "policy_eligible = COALESCE(?, policy_eligible) WHERE id = ?",
            (float(score), json.dumps(breakdown), int(eligible), rejection_reason,
             discovery_lane, age_cohort,
             None if technical_eligible is None else int(technical_eligible),
             None if policy_eligible is None else int(policy_eligible), source_id))

    def transition_source(self, source_id: int, target: str, *,
                          expected: Optional[Iterable[str]] = None, **fields) -> bool:
        """Move a source to ``target``, refusing illegal or racing transitions.

        Returns False when the row was not in ``expected`` (another tick or
        another process already moved it) — the caller must then do nothing.
        This compare-and-set is what makes overlapping ticks harmless.
        """
        allowed_fields = {"job_id", "selected_at", "attempts", "last_error", "rights_policy",
                          "next_retry_at", "rejection_reason", "eligible", "score",
                          "selection_tier"}
        now = iso(utcnow())
        with self.tx() as conn:
            row = conn.execute("SELECT state FROM discovered_source WHERE id = ?",
                               (source_id,)).fetchone()
            if row is None:
                return False
            current = row["state"]
            if expected is not None and current not in set(expected):
                return False
            assert_transition(current, target)
            sets = ["state = ?", "state_changed_at = ?"]
            params: List[Any] = [target, now]
            for key, value in fields.items():
                if key not in allowed_fields:
                    raise ValueError(f"Unknown source field {key!r}")
                sets.append(f"{key} = ?")
                params.append(value)
            params.append(source_id)
            conn.execute(f"UPDATE discovered_source SET {', '.join(sets)} WHERE id = ?", params)
            return True

    def claim_source_for_processing(self, source_id: int, job_id: str, rights_policy: str) -> bool:
        """Atomically bind a job id to a source.

        The unique index on ``job_id`` plus the SELECTED→PROCESS_QUEUED guard
        means a duplicate tick (or a second process that somehow held the lease)
        cannot submit the same source twice.
        """
        return self.transition_source(
            source_id, SourceState.PROCESS_QUEUED, expected=[SourceState.SELECTED],
            job_id=job_id, rights_policy=rights_policy, selected_at=iso(utcnow()))

    def active_processing_source(self) -> Optional[DiscoveredSource]:
        row = self.query_one(
            "SELECT * FROM discovered_source WHERE state IN (?, ?, ?) ORDER BY id LIMIT 1",
            (SourceState.SELECTED, SourceState.PROCESS_QUEUED, SourceState.PROCESSING))
        return _row_to_source(row) if row else None

    def sources_selected_since(self, since: datetime) -> int:
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM discovered_source WHERE selected_at IS NOT NULL "
            "AND selected_at >= ?", (iso(since),))
        return int(row["n"]) if row else 0

    def channel_last_selected(self, channel_id: str) -> Optional[datetime]:
        row = self.query_one(
            "SELECT MAX(selected_at) AS last FROM discovered_source "
            "WHERE channel_id = ? AND selected_at IS NOT NULL", (channel_id,))
        return parse_iso(row["last"]) if row and row["last"] else None

    def top_channels(self, limit: int = 5) -> List[str]:
        """Channels whose best-known candidate scored highest — seeds for the
        CHANNEL_WINNERS discovery lane. Not "channels we selected from before"
        (that is channel_use_count); this is "channels that have produced a
        good-looking candidate at all", so a promising channel can be revisited
        even before Autopilot has ever picked from it.
        """
        rows = self.query(
            "SELECT channel_id, MAX(score) AS best FROM discovered_source "
            "WHERE channel_id != '' GROUP BY channel_id ORDER BY best DESC LIMIT ?",
            (max(0, int(limit)),))
        return [row["channel_id"] for row in rows]

    def channel_use_count(self, channel_id: str) -> int:
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM discovered_source WHERE channel_id = ? "
            "AND selected_at IS NOT NULL", (channel_id,))
        return int(row["n"]) if row else 0

    def known_video_ids(self, video_ids: Iterable[str]) -> set:
        ids = list(video_ids)
        if not ids:
            return set()
        found = set()
        for start in range(0, len(ids), 400):  # stay under SQLite's variable limit
            chunk = ids[start:start + 400]
            rows = self.query(
                f"SELECT youtube_video_id FROM discovered_source "
                f"WHERE youtube_video_id IN ({','.join('?' * len(chunk))})", chunk)
            found.update(row["youtube_video_id"] for row in rows)
        return found

    def filtered_video_ids(self, video_ids: Iterable[str]) -> set:
        """Subset of ``video_ids`` whose rows are in the FILTERED state.

        Used by discovery to re-evaluate previously rejected candidates when
        eligibility settings have been relaxed.
        """
        ids = list(video_ids)
        if not ids:
            return set()
        found = set()
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            rows = self.query(
                f"SELECT youtube_video_id FROM discovered_source "
                f"WHERE state = ? AND youtube_video_id IN ({','.join('?' * len(chunk))})",
                [SourceState.FILTERED] + chunk)
            found.update(row["youtube_video_id"] for row in rows)
        return found

    # --- processing attempts --------------------------------------------------

    def record_processing_attempt(self, source_id: int, job_id: str, attempt_no: int) -> None:
        self.execute(
            "INSERT OR IGNORE INTO processing_attempt "
            "(source_id, job_id, attempt_no, status, started_at) VALUES (?, ?, ?, ?, ?)",
            (source_id, job_id, attempt_no, "RUNNING", iso(utcnow())))

    def finish_processing_attempt(self, job_id: str, status: str,
                                  error: Optional[str] = None) -> None:
        self.execute(
            "UPDATE processing_attempt SET status = ?, finished_at = ?, error = ? "
            "WHERE job_id = ?", (status, iso(utcnow()), (error or "")[:1000] or None, job_id))

    def list_processing_attempts(self, source_id: int) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.query(
            "SELECT * FROM processing_attempt WHERE source_id = ? ORDER BY id", (source_id,))]

    # --- clips ----------------------------------------------------------------

    def upsert_clip(self, clip: GeneratedClip) -> int:
        """Record a generated clip. ``UNIQUE(job_id, clip_index)`` is the guard
        that stops a re-reconciliation of the same job creating duplicates."""
        with self.tx() as conn:
            row = conn.execute(
                "SELECT id FROM generated_clip WHERE job_id = ? AND clip_index = ?",
                (clip.job_id, clip.clip_index)).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE generated_clip SET filename = ?, title = ?, description = ? "
                    "WHERE id = ?", (clip.filename, clip.title, clip.description, row["id"]))
                return int(row["id"])
            cur = conn.execute(
                "INSERT INTO generated_clip (source_id, job_id, clip_index, filename, title,"
                " description, start_seconds, end_seconds, rank, state, skip_reason, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (clip.source_id, clip.job_id, clip.clip_index, clip.filename, clip.title,
                 clip.description, clip.start_seconds, clip.end_seconds, clip.rank,
                 clip.state, clip.skip_reason, iso(utcnow())))
            return int(cur.lastrowid)

    def set_clip_state(self, clip_id: int, state: str, skip_reason: Optional[str] = None) -> None:
        if state not in ClipState.ALL:
            raise ValueError(f"Unknown clip state {state!r}")
        self.execute("UPDATE generated_clip SET state = ?, skip_reason = ? WHERE id = ?",
                     (state, skip_reason, clip_id))

    def list_clips(self, source_id: Optional[int] = None, job_id: Optional[str] = None,
                   states: Optional[Iterable[str]] = None) -> List[GeneratedClip]:
        sql = "SELECT * FROM generated_clip"
        clauses, params = [], []
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        if states:
            states = list(states)
            clauses.append(f"state IN ({','.join('?' * len(states))})")
            params.extend(states)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rank, clip_index"
        return [_row_to_clip(row) for row in self.query(sql, params)]

    def get_clip(self, clip_id: int) -> Optional[GeneratedClip]:
        row = self.query_one("SELECT * FROM generated_clip WHERE id = ?", (clip_id,))
        return _row_to_clip(row) if row else None

    # --- publish attempts -----------------------------------------------------

    def reserve_publish_attempt(self, attempt: PublishAttempt) -> Optional[int]:
        """Claim a publishing slot for a clip, or return None if already claimed.

        Three separate uniqueness constraints do the deduplication, and all of
        them are enforced by SQLite rather than by application checks:
          * ``idempotency_key``  — same clip + platforms + slot, resubmitted
          * ``ux_publish_live_clip`` — a clip already has a live attempt
          * ``ux_publish_live_slot`` — that slot already belongs to another clip
        """
        try:
            with self.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO publish_attempt (clip_id, source_id, job_id, clip_index,"
                    " idempotency_key, platforms, scheduled_for_utc, timezone, state,"
                    " retry_count, created_at, title, description)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (attempt.clip_id, attempt.source_id, attempt.job_id, attempt.clip_index,
                     attempt.idempotency_key, json.dumps(attempt.platforms),
                     attempt.scheduled_for_utc, attempt.timezone, PublishState.PENDING,
                     0, iso(utcnow()), attempt.title, attempt.description))
                return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def set_publish_state(self, attempt_id: int, state: str, *,
                          vendor_response: Optional[Dict] = None,
                          error: Optional[str] = None,
                          increment_retry: bool = False,
                          vendor_request_id: Optional[str] = None,
                          vendor_job_id: Optional[str] = None,
                          vendor_status: Optional[str] = None,
                          vendor_results: Optional[List[Dict]] = None,
                          next_status_check_at: Optional[str] = None,
                          mark_checked: bool = False,
                          expected: Optional[Iterable[str]] = None) -> bool:
        """Move a publish attempt, optionally recording what the vendor said.

        ``expected`` makes this a compare-and-set, which is what stops a status
        poll and an operator action racing each other into an inconsistent
        state. Returns False when the row was not in ``expected``.
        """
        if state not in PublishState.ALL:
            raise ValueError(f"Unknown publish state {state!r}")

        with self.tx() as conn:
            row = conn.execute("SELECT state FROM publish_attempt WHERE id = ?",
                               (attempt_id,)).fetchone()
            if row is None:
                return False
            current = row["state"]
            if expected is not None and current not in set(expected):
                return False
            assert_publish_transition(current, state)

            sets = ["state = ?"]
            params: List[Any] = [state]
            if vendor_response is not None:
                sets.append("vendor_response = ?")
                params.append(json.dumps(vendor_response)[:8000])
            if error is not None:
                sets.append("error = ?")
                params.append(str(error)[:1000])
            if increment_retry:
                sets.append("retry_count = retry_count + 1")
            if vendor_request_id is not None:
                sets.append("vendor_request_id = ?")
                params.append(str(vendor_request_id)[:200])
            if vendor_job_id is not None:
                sets.append("vendor_job_id = ?")
                params.append(str(vendor_job_id)[:200])
            if vendor_status is not None:
                sets.append("vendor_status = ?")
                params.append(str(vendor_status)[:60])
            if vendor_results is not None:
                sets.append("vendor_results = ?")
                params.append(json.dumps(vendor_results)[:8000])
            if mark_checked:
                sets.append("last_status_check_at = ?")
                params.append(iso(utcnow()))
            # An explicit None clears the schedule; that is how a terminal state
            # stops being polled.
            sets.append("next_status_check_at = ?")
            params.append(next_status_check_at)
            if state == PublishState.SUBMITTED and current != PublishState.SUBMITTED:
                sets.append("submitted_at = ?")
                params.append(iso(utcnow()))
            if state in PublishState.TERMINAL or state == PublishState.PARTIAL_FAILED:
                sets.append("finalized_at = ?")
                params.append(iso(utcnow()))

            params.append(attempt_id)
            conn.execute(f"UPDATE publish_attempt SET {', '.join(sets)} WHERE id = ?", params)
            return True

    def record_vendor_ids(self, attempt_id: int, *, request_id: Optional[str],
                          job_id: Optional[str]) -> None:
        """Persist our request_id BEFORE the network call.

        This is the whole reason an ambiguous timeout is recoverable: without a
        stored identifier there is nothing to ask the vendor about afterwards.
        """
        self.execute(
            "UPDATE publish_attempt SET vendor_request_id = COALESCE(?, vendor_request_id), "
            "vendor_job_id = COALESCE(?, vendor_job_id) WHERE id = ?",
            (request_id, job_id, attempt_id))

    def attempts_due_for_status_check(self, now: datetime, limit: int = 10
                                      ) -> List[PublishAttempt]:
        """Non-terminal attempts whose next poll is due.

        Driven by a persisted ``next_status_check_at`` so a restart resumes the
        vendor's recommended cadence instead of stampeding every open attempt.
        """
        states = list(PublishState.NEEDS_RECONCILIATION)
        rows = self.query(
            f"SELECT * FROM publish_attempt WHERE state IN ({','.join('?' * len(states))}) "
            "AND (vendor_request_id IS NOT NULL OR vendor_job_id IS NOT NULL) "
            "AND (next_status_check_at IS NULL OR next_status_check_at <= ?) "
            "ORDER BY COALESCE(next_status_check_at, created_at) LIMIT ?",
            (*states, iso(now), max(1, min(50, int(limit)))))
        return [_row_to_publish(row) for row in rows]

    def vendor_scheduled_attempts(self, *, after: Optional[datetime] = None
                                  ) -> List[PublishAttempt]:
        """Attempts Upload-Post is holding as future scheduled jobs.

        Emergency Stop uses this to know exactly which vendor jobs are Klippo's
        to cancel — Manual Mode posts are not in this table at all.
        """
        sql = ("SELECT * FROM publish_attempt WHERE vendor_job_id IS NOT NULL "
               "AND state IN (?, ?, ?)")
        params: List[Any] = [PublishState.SUBMITTED, PublishState.PUBLISHING,
                             PublishState.UNCERTAIN]
        if after is not None:
            sql += " AND scheduled_for_utc >= ?"
            params.append(iso(after))
        sql += " ORDER BY scheduled_for_utc"
        return [_row_to_publish(row) for row in self.query(sql, params)]

    def get_publish_attempt(self, attempt_id: int) -> Optional[PublishAttempt]:
        row = self.query_one("SELECT * FROM publish_attempt WHERE id = ?", (attempt_id,))
        return _row_to_publish(row) if row else None

    def list_publish_attempts(self, states: Optional[Iterable[str]] = None,
                              limit: int = 100,
                              source_id: Optional[int] = None) -> List[PublishAttempt]:
        sql = "SELECT * FROM publish_attempt"
        clauses, params = [], []
        if states:
            states = list(states)
            clauses.append(f"state IN ({','.join('?' * len(states))})")
            params.extend(states)
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        return [_row_to_publish(row) for row in self.query(sql, params)]

    def taken_slots(self, after: datetime) -> List[datetime]:
        """Publishing slots already spoken for, so allocation never double-books."""
        rows = self.query(
            "SELECT scheduled_for_utc FROM publish_attempt WHERE scheduled_for_utc >= ? "
            "AND state IN (?, ?, ?, ?) ORDER BY scheduled_for_utc",
            (iso(after), PublishState.PENDING, PublishState.IN_FLIGHT,
             PublishState.SUBMITTED, PublishState.UNCERTAIN))
        return [dt for dt in (parse_iso(row["scheduled_for_utc"]) for row in rows) if dt]

    def posts_scheduled_between(self, start: datetime, end: datetime) -> int:
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM publish_attempt WHERE scheduled_for_utc >= ? "
            "AND scheduled_for_utc < ? AND state IN (?, ?, ?, ?)",
            (iso(start), iso(end), PublishState.PENDING, PublishState.IN_FLIGHT,
             PublishState.SUBMITTED, PublishState.UNCERTAIN))
        return int(row["n"]) if row else 0

    def files_in_use(self) -> List[Dict[str, str]]:
        """(job_id, filename) pairs an unfinished publish attempt still depends on.

        Retention must not delete these: the vendor upload happens at submission
        time, but a PENDING/UNCERTAIN attempt has not uploaded the bytes yet.
        """
        rows = self.query(
            "SELECT DISTINCT p.job_id AS job_id, c.filename AS filename "
            "FROM publish_attempt p JOIN generated_clip c ON c.id = p.clip_id "
            "WHERE p.state IN (?, ?, ?)",
            (PublishState.PENDING, PublishState.IN_FLIGHT, PublishState.UNCERTAIN))
        return [{"job_id": row["job_id"], "filename": row["filename"]} for row in rows]

    # --- quota ----------------------------------------------------------------

    def get_quota(self, provider: str = "youtube", bucket: str = "general") -> Dict[str, Any]:
        """Usage for one quota bucket, resetting the counter on a new day."""
        row = self.query_one(
            "SELECT * FROM api_quota WHERE provider = ? AND bucket = ?", (provider, bucket))
        today = utcnow().strftime("%Y-%m-%d")
        if row is None:
            return {"provider": provider, "bucket": bucket, "day": today,
                    "units_used": 0, "exhausted_until": None, "last_error": None}
        data = dict(row)
        if data.get("day") != today:
            data["units_used"] = 0
            data["day"] = today
        return data

    def add_quota_units(self, units: int, provider: str = "youtube",
                        bucket: str = "general") -> None:
        today = utcnow().strftime("%Y-%m-%d")
        with self.tx() as conn:
            row = conn.execute(
                "SELECT day, units_used FROM api_quota WHERE provider = ? AND bucket = ?",
                (provider, bucket)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO api_quota (provider, bucket, day, units_used, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (provider, bucket, today, int(units), iso(utcnow())))
            else:
                used = int(units) if row["day"] != today else int(row["units_used"]) + int(units)
                conn.execute(
                    "UPDATE api_quota SET day = ?, units_used = ?, updated_at = ? "
                    "WHERE provider = ? AND bucket = ?",
                    (today, used, iso(utcnow()), provider, bucket))

    def mark_quota_exhausted(self, until: datetime, error: str,
                             provider: str = "youtube",
                             bucket: str = "general") -> None:
        """Park ONE bucket until ``until``.

        Per bucket, deliberately: since the search allocation became independent
        of the general pool, an exhausted search quota must not stop chart-based
        discovery, which draws from a pool that still has units left.
        """
        today = utcnow().strftime("%Y-%m-%d")
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO api_quota (provider, bucket, day, units_used, exhausted_until,"
                " last_error, updated_at) VALUES (?, ?, ?, 0, ?, ?, ?) "
                "ON CONFLICT(provider, bucket) DO UPDATE SET "
                " exhausted_until = excluded.exhausted_until,"
                " last_error = excluded.last_error, updated_at = excluded.updated_at",
                (provider, bucket, today, iso(until), str(error)[:500], iso(utcnow())))

    def clear_quota_block(self, provider: str = "youtube",
                          bucket: Optional[str] = None) -> None:
        if bucket is None:
            self.execute("UPDATE api_quota SET exhausted_until = NULL WHERE provider = ?",
                         (provider,))
        else:
            self.execute(
                "UPDATE api_quota SET exhausted_until = NULL "
                "WHERE provider = ? AND bucket = ?", (provider, bucket))

    def quota_blocked(self, now: datetime, provider: str = "youtube",
                      bucket: str = "general") -> Optional[datetime]:
        """When this bucket frees up, or None. Clears a lapsed block in passing."""
        quota = self.get_quota(provider, bucket)
        until = parse_iso(quota.get("exhausted_until"))
        if until and until > now:
            return until
        if until:
            self.clear_quota_block(provider, bucket)
        return None

    # --- channel context (automation/channel_context.py) ----------------------

    def get_channel_stats(self, channel_id: str) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT * FROM channel_stats_cache WHERE channel_id = ?",
                             (channel_id,))
        return dict(row) if row else None

    def get_channel_stats_bulk(self, channel_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        ids = [c for c in dict.fromkeys(channel_ids) if c]
        out: Dict[str, Dict[str, Any]] = {}
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            if not chunk:
                continue
            rows = self.query(
                f"SELECT * FROM channel_stats_cache WHERE channel_id IN "
                f"({','.join('?' * len(chunk))})", chunk)
            out.update({row["channel_id"]: dict(row) for row in rows})
        return out

    def save_channel_stats(self, channel_id: str, *, subscriber_count: Optional[int],
                           view_count: Optional[int], video_count: Optional[int]) -> None:
        self.execute(
            "INSERT INTO channel_stats_cache "
            "(channel_id, subscriber_count, view_count, video_count, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET subscriber_count = excluded.subscriber_count,"
            " view_count = excluded.view_count, video_count = excluded.video_count,"
            " fetched_at = excluded.fetched_at",
            (channel_id, subscriber_count, view_count, video_count, iso(utcnow())))

    # --- semantic evaluation cache (automation/ports.py SemanticEvaluatorPort) -

    def get_semantic_evaluation(self, video_id: str, model_version: str) -> Optional[Dict[str, Any]]:
        row = self.query_one(
            "SELECT result_json FROM semantic_evaluation "
            "WHERE youtube_video_id = ? AND model_version = ?", (video_id, model_version))
        return _json_loads(row["result_json"], None) if row else None

    def save_semantic_evaluation(self, video_id: str, model_version: str,
                                 result: Dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO semantic_evaluation "
            "(youtube_video_id, model_version, result_json, evaluated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(youtube_video_id, model_version) DO UPDATE SET "
            " result_json = excluded.result_json, evaluated_at = excluded.evaluated_at",
            (video_id, model_version, json.dumps(result), iso(utcnow())))


# --- row mappers --------------------------------------------------------------

def _row_to_source(row: sqlite3.Row) -> DiscoveredSource:
    return DiscoveredSource(
        id=row["id"], youtube_video_id=row["youtube_video_id"], url=row["url"],
        channel_id=row["channel_id"], channel_title=row["channel_title"], title=row["title"],
        description=row["description"], published_at=row["published_at"],
        duration_seconds=row["duration_seconds"], category_id=row["category_id"],
        view_count=row["view_count"], like_count=row["like_count"],
        comment_count=row["comment_count"], license=row["license"],
        definition=row["definition"], caption_available=bool(row["caption_available"]),
        live_state=row["live_state"], made_for_kids=bool(row["made_for_kids"]),
        age_restricted=bool(row["age_restricted"]), privacy_status=row["privacy_status"],
        embeddable=bool(row["embeddable"]), discovery_source=row["discovery_source"],
        discovered_at=row["discovered_at"], chart_rank=row["chart_rank"], run_id=row["run_id"],
        score=row["score"], score_breakdown=_json_loads(row["score_breakdown"], {}),
        eligible=bool(row["eligible"]), rejection_reason=row["rejection_reason"],
        state=row["state"], state_changed_at=row["state_changed_at"],
        selected_at=row["selected_at"], job_id=row["job_id"], attempts=row["attempts"],
        last_error=row["last_error"], rights_policy=row["rights_policy"],
        next_retry_at=row["next_retry_at"],
        discovery_lane=_column(row, "discovery_lane"),
        age_cohort=_column(row, "age_cohort"),
        selection_tier=_column(row, "selection_tier"),
        technical_eligible=bool(_column(row, "technical_eligible")
                                if _column(row, "technical_eligible") is not None else True),
        policy_eligible=bool(_column(row, "policy_eligible")
                             if _column(row, "policy_eligible") is not None else True),
    )


def _row_to_clip(row: sqlite3.Row) -> GeneratedClip:
    return GeneratedClip(
        id=row["id"], source_id=row["source_id"], job_id=row["job_id"],
        clip_index=row["clip_index"], filename=row["filename"], title=row["title"],
        description=row["description"], start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"], rank=row["rank"], state=row["state"],
        skip_reason=row["skip_reason"], created_at=row["created_at"],
    )


def _row_to_publish(row: sqlite3.Row) -> PublishAttempt:
    return PublishAttempt(
        id=row["id"], clip_id=row["clip_id"], source_id=row["source_id"], job_id=row["job_id"],
        clip_index=row["clip_index"], idempotency_key=row["idempotency_key"],
        platforms=_json_loads(row["platforms"], []),
        scheduled_for_utc=row["scheduled_for_utc"], timezone=row["timezone"],
        state=row["state"], vendor_response=_json_loads(row["vendor_response"], None),
        error=row["error"], retry_count=row["retry_count"], submitted_at=row["submitted_at"],
        created_at=row["created_at"], title=row["title"], description=row["description"],
        vendor_request_id=_column(row, "vendor_request_id"),
        vendor_job_id=_column(row, "vendor_job_id"),
        vendor_status=_column(row, "vendor_status"),
        vendor_results=_json_loads(_column(row, "vendor_results"), None),
        last_status_check_at=_column(row, "last_status_check_at"),
        next_status_check_at=_column(row, "next_status_check_at"),
        finalized_at=_column(row, "finalized_at"),
    )


def _column(row: sqlite3.Row, name: str):
    """Read a column that may not exist yet on a database mid-upgrade."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None
