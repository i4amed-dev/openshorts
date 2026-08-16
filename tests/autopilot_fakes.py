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
from automation.youtube_client import ChannelRecord, VideoRecord


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
    """Models the real Upload-Post contract, not a boolean.

    ``/upload`` returns an identifier and NOTHING else is implied — the status
    endpoint is what later says whether anything went live. ``status_script``
    lets a test walk an attempt through queued → processing → completed, or
    straight to a mixed per-platform outcome.
    """

    def __init__(self, *, api_key: str = "test-key", user: str = "test-profile"):
        self.calls: List[Dict[str, Any]] = []
        self.status_calls: List[Dict[str, Any]] = []
        self.cancel_calls: List[str] = []
        self.api_key = api_key
        self.user = user
        self.mode = "ok"        # ok | fail | fail_permanent | uncertain
        self.fail_times = 0
        self.scheduled = True   # return a job_id (scheduled) vs request_id (async)
        self.profiles = [{"username": user,
                          "connected": ["tiktok", "instagram", "youtube"]}]
        # request_id/job_id -> list of status payloads, consumed in order
        self.status_script: Dict[str, List[Dict[str, Any]]] = {}
        self.default_status: Optional[Dict[str, Any]] = None
        self.cancel_outcome = "canceled"
        self._seq = 0

    async def publish(self, **kwargs) -> Dict[str, Any]:
        from publishing_service import PublishError, PublishUncertain
        self.calls.append(kwargs)
        request_id = kwargs.get("request_id")
        if self.mode == "uncertain":
            raise PublishUncertain("no answer from the vendor", request_id=request_id)
        if self.mode == "fail" and self.fail_times > 0:
            self.fail_times -= 1
            raise PublishError("temporary vendor error", status=503, retryable=True)
        if self.mode == "fail_permanent":
            raise PublishError("bad request", status=400, retryable=False)
        self._seq += 1
        job_id = f"scheduler_job_{self._seq}" if self.scheduled else None
        return {"response": {"success": True, "request_id": request_id, "job_id": job_id},
                "request_id": request_id, "job_id": job_id,
                "status_code": 202 if self.scheduled else 200}

    def credentials(self):
        return (self.api_key, self.user)

    async def list_profiles(self):
        return list(self.profiles)

    def port(self) -> PublisherPort:
        return PublisherPort(publish=self.publish, credentials=self.credentials,
                             list_profiles=self.list_profiles)

    # --- vendor status scripting ---
    def script(self, tracking_id: str, *payloads: Dict[str, Any]) -> None:
        self.status_script[tracking_id] = list(payloads)

    def set_default_status(self, payload: Optional[Dict[str, Any]]) -> None:
        self.default_status = payload

    def next_status(self, request_id=None, job_id=None) -> Dict[str, Any]:
        key = job_id or request_id
        self.status_calls.append({"request_id": request_id, "job_id": job_id})
        queue = self.status_script.get(key)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if self.default_status is not None:
            return self.default_status
        return {"status": "pending", "completed": 0, "total": 3, "results": []}


def status_payload(status: str, results=None, **extra) -> Dict[str, Any]:
    """Build an Upload-Post status body in the documented shape."""
    payload = {"status": status, "results": results or [],
               "completed": sum(1 for r in (results or [])
                                if r.get("status") == "completed"),
               "total": len(results or [])}
    payload.update(extra)
    return payload


def platform_result(platform: str, status: str, message: str = "") -> Dict[str, Any]:
    return {"platform": platform, "status": status,
            "success": True if status == "completed" else (
                False if status == "failed" else None),
            "message": message or status.title(),
            "upload_timestamp": "2026-08-12T12:00:00Z"}


def install_fake_vendor(monkeypatch, publisher: "FakePublisher"):
    """Point publishing_service's status/cancel calls at the fake."""
    import publishing_service

    async def fake_get_status(api_key, *, request_id=None, job_id=None, timeout=30.0):
        payload = publisher.next_status(request_id=request_id, job_id=job_id)
        http = 404 if payload.get("status") == "not_found" else 200
        return publishing_service.parse_status(payload, http_status=http)

    async def fake_cancel(api_key, job_id, *, timeout=30.0):
        publisher.cancel_calls.append(job_id)
        outcome = publisher.cancel_outcome
        return outcome, f"{outcome} for {job_id}"

    monkeypatch.setattr(publishing_service, "get_status", fake_get_status)
    monkeypatch.setattr(publishing_service, "cancel_scheduled", fake_cancel)
    return publisher


class FakeYouTubeClient:
    """Returns a scripted candidate set; counts calls so quota use is assertable."""

    def __init__(self, records: Optional[List[VideoRecord]] = None, *,
                 raise_on_popular: Optional[Exception] = None,
                 channel_records: Optional[List[ChannelRecord]] = None):
        self.records = records or []
        self.raise_on_popular = raise_on_popular
        self.popular_calls = 0
        self.search_calls = 0
        self.channels_calls = 0
        self.channel_records = channel_records or []
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

    async def channels(self, channel_ids, **kwargs) -> List[ChannelRecord]:
        self.channels_calls += 1
        wanted = set(channel_ids)
        return [c for c in self.channel_records if c.channel_id in wanted]


def base_config(**overrides) -> Dict[str, Any]:
    """Normalised settings with the knobs a test usually wants to move."""
    from automation.config import normalise
    config = {
        "enabled": True,
        "timezone": "Europe/Madrid",
        # exploration_rate=0: deterministic "best score wins" is what most
        # pipeline/service tests assert on. Tests specifically about
        # exploration override this themselves (see test_autopilot_diversity.py).
        "discovery": {"strategies": ["most_popular"], "region_code": "US",
                      "exploration_rate": 0.0},
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
