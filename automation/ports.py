"""The seam between Autopilot and the rest of Klippo.

Autopilot must call the real backend services directly — no HTTP loopback into
its own process, no second video pipeline, no browser automation. But
``automation/`` also has to stay importable on its own: CI installs only the
light test dependencies, and ``app.py`` pulls in boto3, ultralytics, mediapipe
and faster-whisper at import time.

So the dependency points the other way. This module declares the small surface
Autopilot needs; ``app.py`` builds a concrete adapter over its *own* functions
(the same ones ``/api/process`` and ``/api/social/post`` call) and registers it
during startup. One implementation, two callers, and the package still imports
with nothing but the standard library.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


@dataclass
class JobSnapshot:
    """What Autopilot needs to know about a Klippo processing job."""
    job_id: str
    status: str                       # queued | processing | completed | failed | missing
    clips: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    output_dir: Optional[str] = None

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "failed", "missing")


@dataclass
class ClipGeneratorPort:
    """Adapter over the existing Klippo clip pipeline.

    ``submit_url`` is the *same* submission path ``/api/process`` uses, so an
    Autopilot job is an ordinary Klippo job: it enters the same priority queue,
    honours the same concurrency semaphore, writes the same resume manifest and
    is recovered by the same restart logic.
    """
    submit_url: Callable[..., Awaitable[str]]
    get_job: Callable[[str], JobSnapshot]
    clip_path: Callable[[str, str], Optional[str]]
    probe_quality: Callable[[str], Awaitable[Dict[str, Any]]]
    active_heavy_jobs: Callable[[], int]


@dataclass
class PublisherPort:
    """Adapter over the shared Upload-Post publishing service."""
    publish: Callable[..., Awaitable[Dict[str, Any]]]
    credentials: Callable[[], tuple]   # -> (api_key or None, profile or None)


@dataclass
class Runtime:
    clip_generator: Optional[ClipGeneratorPort] = None
    publisher: Optional[PublisherPort] = None

    @property
    def ready(self) -> bool:
        return self.clip_generator is not None and self.publisher is not None


_runtime = Runtime()


def register(clip_generator: Optional[ClipGeneratorPort] = None,
             publisher: Optional[PublisherPort] = None) -> Runtime:
    if clip_generator is not None:
        _runtime.clip_generator = clip_generator
    if publisher is not None:
        _runtime.publisher = publisher
    return _runtime


def runtime() -> Runtime:
    return _runtime


def reset() -> None:
    """Drop the registered adapters (tests install fakes through `register`)."""
    _runtime.clip_generator = None
    _runtime.publisher = None
