"""Test doubles for the Autopilot pipeline.

Import this as ``from autopilot_fakes import ...``, never
``from tests.autopilot_fakes import ...``. A dependency in the production image
installs its own top-level ``tests`` package into site-packages, which shadows
this directory and makes the dotted form fail inside the container while passing
on a developer machine. pytest's default prepend import mode puts this directory
on sys.path, so the bare name always resolves to the file next to the test.

Nothing here reaches YouTube, Gemini, Upload-Post or the video pipeline. The
fakes implement the same ports ``app.py`` registers in production, so a test
exercises the real orchestrator, the real SQLite state machine and the real
scheduling maths — only the edges are substituted.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from automation.ports import ClipGeneratorPort, JobSnapshot, PublisherPort
from automation.youtube_client import VideoRecord


def run_async(coro):
    """Drive one coroutine to completion.

    Deliberately not pytest-asyncio/anyio: CI installs a minimal dependency set,
    and a plugin that silently fails to load turns every async test into a
    skipped no-op that still reports green.
    """
    return asyncio.run(coro)


def make_record(video_id: str, **overrides) -> VideoRecord:
    """A candidate that passes the default filters unless a field is overridden."""
    now = overrides.pop("now", datetime.now(timezone.utc))
    defaults: Dict[str, Any] = dict(
        video_id=video_id,
        title=f"Video {video_id}",
        description="A long interesting talk about things.",
        channel_id="UC" + video_id.ljust(22, "x")[:22],
        channel_title=f"Channel {video_id}",
        published_at=now - timedelta(hours=6),
        duration_seconds=1200,
        category_id="22",
        view_count=100_000,
        like_count=5_000,
        comment_count=800,
        license="creativeCommon",
        definition="hd",
        caption=True,
        live_state="none",
        made_for_kids=False,
        privacy_status="public",
        embeddable=True,
        upload_status="processed",
        discovery_source="most_popular",
    )
    defaults.update(overrides)
    return VideoRecord(**defaults)


class FakeClipGenerator:
    """Stands in for the Klippo job queue.

    Jobs advance only when the test says so, which is what makes "restart while
    processing" reproducible instead of timing-dependent.
    """

    def __init__(self, *, clips_per_job: int = 3, quality_height: int = 1080):
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.submissions: List[Dict[str, Any]] = []
        self.clips_per_job = clips_per_job
        self.quality_height = quality_height
        self.fail_submit = False
        self.missing_files: set = set()

    # --- port implementation ---
    async def submit_url(self, *, url: str, output_format: str = "vertical",
                         origin: str = "autopilot", rights_policy: str = "",
                         label: str = "") -> str:
        if self.fail_submit:
            raise RuntimeError("clip queue rejected the job")
        job_id = str(uuid.uuid4())
        self.jobs[job_id] = {"status": "queued", "clips": [], "error": None}
        self.submissions.append({"job_id": job_id, "url": url, "origin": origin,
                                 "rights_policy": rights_policy, "label": label,
                                 "output_format": output_format})
        return job_id

    def get_job(self, job_id: str) -> JobSnapshot:
        job = self.jobs.get(job_id)
        if job is None:
            return JobSnapshot(job_id=job_id, status="missing", error="not found")
        return JobSnapshot(job_id=job_id, status=job["status"], clips=job["clips"],
                           error=job["error"])

    def clip_path(self, job_id: str, filename: str) -> Optional[str]:
        if not filename or (job_id, filename) in self.missing_files:
            return None
        return f"/fake/output/{job_id}/{filename}"

    async def probe_quality(self, url: str) -> Dict[str, Any]:
        return {"max_height": self.quality_height}

    def active_heavy_jobs(self) -> int:
        return sum(1 for j in self.jobs.values() if j["status"] in ("queued", "processing"))

    # --- test controls ---
    def port(self) -> ClipGeneratorPort:
        return ClipGeneratorPort(
            submit_url=self.submit_url, get_job=self.get_job, clip_path=self.clip_path,
            probe_quality=self.probe_quality, active_heavy_jobs=self.active_heavy_jobs)

    def start(self, job_id: str) -> None:
        self.jobs[job_id]["status"] = "processing"

    def complete(self, job_id: str, clips: Optional[List[Dict]] = None) -> None:
        job = self.jobs[job_id]
        job["status"] = "completed"
        job["clips"] = clips if clips is not None else [
            {
                "start": 10.0 + i * 100,
                "end": 40.0 + i * 100,
                "video_title_for_youtube_short": f"Clip {i + 1}",
                "video_description_for_instagram": f"Description {i + 1}",
                "video_url": f"/videos/{job_id}/base_clip_{i + 1}.mp4",
            }
            for i in range(self.clips_per_job)
        ]

    def fail(self, job_id: str, error: str = "ffmpeg exploded") -> None:
        self.jobs[job_id]["status"] = "failed"
        self.jobs[job_id]["error"] = error


class FakePublisher:
    """Records every publish call; can be told to fail in specific ways."""

    def __init__(self, *, api_key: str = "test-key", user: str = "test-profile"):
        self.calls: List[Dict[str, Any]] = []
        self.api_key = api_key
        self.user = user
        self.mode = "ok"        # ok | fail | fail_permanent | uncertain
        self.fail_times = 0

    async def publish(self, **kwargs) -> Dict[str, Any]:
        from publishing_service import PublishError, PublishUncertain
        self.calls.append(kwargs)
        if self.mode == "uncertain":
            raise PublishUncertain("no answer from the vendor")
        if self.mode == "fail" and self.fail_times > 0:
            self.fail_times -= 1
            raise PublishError("temporary vendor error", status=503, retryable=True)
        if self.mode == "fail_permanent":
            raise PublishError("bad request", status=400, retryable=False)
        return {"success": True, "id": f"vendor-{len(self.calls)}"}

    def credentials(self):
        return (self.api_key, self.user)

    def port(self) -> PublisherPort:
        return PublisherPort(publish=self.publish, credentials=self.credentials)


class FakeYouTubeClient:
    """Returns a scripted candidate set; counts calls so quota use is assertable."""

    def __init__(self, records: Optional[List[VideoRecord]] = None, *,
                 raise_on_popular: Optional[Exception] = None):
        self.records = records or []
        self.raise_on_popular = raise_on_popular
        self.popular_calls = 0
        self.search_calls = 0
        self.configured = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def most_popular(self, **kwargs) -> List[VideoRecord]:
        self.popular_calls += 1
        if self.raise_on_popular:
            raise self.raise_on_popular
        for index, record in enumerate(self.records, start=1):
            if record.chart_rank is None:
                record.chart_rank = index
        return list(self.records)

    async def search_video_ids(self, query: str, **kwargs) -> List[str]:
        self.search_calls += 1
        return [r.video_id for r in self.records]

    async def hydrate(self, ids, **kwargs) -> List[VideoRecord]:
        wanted = set(ids)
        return [r for r in self.records if r.video_id in wanted]


def base_config(**overrides) -> Dict[str, Any]:
    """Normalised settings with the knobs a test usually wants to move."""
    from automation.config import normalise
    config = {
        "enabled": True,
        "timezone": "Europe/Madrid",
        "discovery": {"strategies": ["most_popular"], "region_code": "US"},
        "eligibility": {"min_views": 1000, "min_view_velocity_per_hour": 1,
                        "min_engagement_rate": 0.0, "channel_cooldown_hours": 0},
        "rights": {"policy": "CREATIVE_COMMONS_ONLY"},
        "schedule": {"discovery_times": ["03:00"],
                     "publish_times": ["11:30", "16:30", "21:00"],
                     "max_posts_per_day": 3, "max_sources_per_day": 1,
                     "min_spacing_minutes": 120},
        "clips": {"max_clips_per_source": 3},
        "publishing": {"platforms": ["tiktok", "instagram", "youtube"]},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    return normalise(config)
