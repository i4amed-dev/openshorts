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
    """Adapter over the shared Upload-Post publishing service.

    Every vendor call Autopilot makes goes through here, so ``publishing_service``
    stays the only module that speaks HTTP to Upload-Post. ``publish`` returns
    ``{response, request_id, job_id}`` — the identifiers matter because a 2xx
    only means the job was accepted, and reconciliation needs something to ask
    about later.
    """
    publish: Callable[..., Awaitable[Dict[str, Any]]]
    credentials: Callable[[], tuple]   # -> (api_key or None, profile or None)
    list_profiles: Optional[Callable[..., Awaitable[List[Dict[str, Any]]]]] = None


@dataclass
class SemanticEvaluatorPort:
    """Optional Gemini shortlist refinement (automation/opportunity.py's
    ``semantic`` component).

    Deliberately optional and narrow: called with at most
    ``discovery.semantic_shortlist_size`` already-ranked candidates (never
    every raw discovery result), and only for the shortlist a discovery run
    is about to store. ``evaluate`` takes a list of ``{video_id, title,
    description}`` dicts and returns ``{video_id: {clipability_score,
    hook_potential, standalone_moment_probability, overall_score}}`` — any
    video id absent from the result is simply treated as "no semantic
    refinement available" rather than an error.
    """
    evaluate: Callable[[List[Dict[str, Any]]], Awaitable[Dict[str, Dict[str, float]]]]
    model_version: str = "v1"


@dataclass
class Runtime:
    clip_generator: Optional[ClipGeneratorPort] = None
    publisher: Optional[PublisherPort] = None
    semantic_evaluator: Optional[SemanticEvaluatorPort] = None

    @property
    def ready(self) -> bool:
        return self.clip_generator is not None and self.publisher is not None


_runtime = Runtime()


def register(clip_generator: Optional[ClipGeneratorPort] = None,
             publisher: Optional[PublisherPort] = None,
             semantic_evaluator: Optional[SemanticEvaluatorPort] = None) -> Runtime:
    if clip_generator is not None:
        _runtime.clip_generator = clip_generator
    if publisher is not None:
        _runtime.publisher = publisher
    if semantic_evaluator is not None:
        _runtime.semantic_evaluator = semantic_evaluator
    return _runtime


def runtime() -> Runtime:
    return _runtime


def reset() -> None:
    """Drop the registered adapters (tests install fakes through `register`)."""
    _runtime.clip_generator = None
    _runtime.publisher = None
    _runtime.semantic_evaluator = None
