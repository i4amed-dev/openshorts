"""The one implementation of "post this clip to social".

Both callers use it and neither reimplements it:

* ``POST /api/social/post`` — the manual button in the clip card and the
  Schedule Week modal.
* Autopilot — unattended publishing.

Keeping the Upload-Post parameter construction in one function is not tidiness:
the payload has platform-specific quirks (``media_type=REELS``, separate
``youtube_title``/``tiktok_title`` fields), and a second copy drifts until
Autopilot and the manual button publish subtly different posts.

Two fixes over the previous inline implementation, both about a machine that has
to stay up for weeks:

* The whole clip used to be read into RAM (``f.read()``) before upload. On an
  8 GB M1 running Whisper and FFmpeg alongside, a 200 MB buffer is real. The
  file object is handed to httpx instead, which streams the multipart body.
* The blocking ``httpx.Client`` call ran directly inside an ``async def``
  endpoint, so a slow upload stalled the event loop — and with it every status
  poll and the Autopilot tick. It now runs in a worker thread.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

UPLOAD_ENDPOINT = "https://api.upload-post.com/api/upload"
PROFILES_ENDPOINT = "https://api.upload-post.com/api/uploadposts/users"

SUPPORTED_PLATFORMS = ("tiktok", "instagram", "youtube")

DEFAULT_TIMEOUT = 300.0  # a scheduled upload of a 60s clip over a home line


class PublishError(RuntimeError):
    """A publish that definitively failed, carrying the vendor's own words."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 body: Optional[str] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retryable = retryable


class PublishUncertain(RuntimeError):
    """The request left the process but no verdict came back.

    Upload-Post exposes no idempotency key, so a blind retry here risks a
    duplicate post. Callers must record this state and let a human decide —
    inventing an ``Idempotency-Key`` header the vendor ignores would be worse
    than admitting the ambiguity.
    """

    def __init__(self, message: str, *, cause: Optional[BaseException] = None):
        super().__init__(message)
        self.cause = cause


@dataclass
class PublishRequest:
    file_path: str
    platforms: List[str]
    user: str
    api_key: str
    title: str = "Viral Short"
    description: str = "Check this out!"
    youtube_title: Optional[str] = None
    scheduled_date: Optional[str] = None   # ISO-8601, vendor interprets in `timezone`
    timezone: str = "UTC"
    filename: Optional[str] = None
    privacy_status: str = "public"
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishResult:
    ok: bool
    status_code: int
    response: Dict[str, Any]


def validate_platforms(platforms: Optional[List[str]]) -> List[str]:
    """Normalise and reject anything not in the supported set.

    Unknown values are dropped rather than forwarded: the vendor treats an
    unrecognised platform as a silent no-op, which looks like a successful post
    that never appears anywhere.
    """
    seen: List[str] = []
    for platform in platforms or []:
        name = str(platform).strip().lower()
        if name in SUPPORTED_PLATFORMS and name not in seen:
            seen.append(name)
    if not seen:
        raise PublishError(
            f"No supported platform selected (expected any of {', '.join(SUPPORTED_PLATFORMS)})")
    return seen


def build_payload(req: PublishRequest) -> Dict[str, Any]:
    """Exactly the form fields Upload-Post expects. Pure — unit-testable."""
    platforms = validate_platforms(req.platforms)
    title = req.title or "Viral Short"
    description = req.description or "Check this out!"

    payload: Dict[str, Any] = {
        "user": req.user,
        "title": title,
        "platform[]": platforms,
        "async_upload": "true",
    }

    if req.scheduled_date:
        payload["scheduled_date"] = req.scheduled_date
        if req.timezone:
            payload["timezone"] = req.timezone

    if "tiktok" in platforms:
        payload["tiktok_title"] = description

    if "instagram" in platforms:
        payload["instagram_title"] = description
        payload["media_type"] = "REELS"

    if "youtube" in platforms:
        payload["youtube_title"] = req.youtube_title or title
        payload["youtube_description"] = description
        payload["privacyStatus"] = req.privacy_status

    for key, value in (req.extra or {}).items():
        payload.setdefault(key, value)

    return payload


def _post_blocking(req: PublishRequest, payload: Dict[str, Any],
                   timeout: float) -> PublishResult:
    """The blocking half, always called from a worker thread.

    The file object goes straight to httpx so the multipart body streams off
    disk instead of being materialised in memory.
    """
    filename = req.filename or os.path.basename(req.file_path)
    headers = {"Authorization": f"Apikey {req.api_key}"}
    with open(req.file_path, "rb") as handle:
        files = {"video": (filename, handle, "video/mp4")}
        with httpx.Client(timeout=timeout) as client:
            response = client.post(UPLOAD_ENDPOINT, headers=headers, data=payload, files=files)

    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:2000]}
    if not isinstance(body, dict):
        body = {"raw": body}

    if response.status_code not in (200, 201, 202):
        raise PublishError(
            f"Upload-Post rejected the post (HTTP {response.status_code})",
            status=response.status_code,
            body=response.text[:2000],
            # 5xx and 429 are worth another attempt; a 4xx means the request
            # itself is wrong and retrying just burns the vendor's rate limit.
            retryable=response.status_code >= 500 or response.status_code == 429,
        )
    return PublishResult(ok=True, status_code=response.status_code, response=body)


async def publish(req: PublishRequest, *, timeout: float = DEFAULT_TIMEOUT) -> PublishResult:
    """Submit one clip. Raises PublishError / PublishUncertain.

    A transport error after the request was written is genuinely ambiguous — the
    post may or may not exist — so it surfaces as PublishUncertain rather than
    being silently retried into a duplicate.
    """
    if not req.api_key:
        raise PublishError("Missing Upload-Post API key")
    if not req.user:
        raise PublishError("Missing Upload-Post user profile")
    if not req.file_path or not os.path.exists(req.file_path):
        raise PublishError(f"Video file not found: {req.file_path}")

    payload = build_payload(req)
    try:
        return await asyncio.to_thread(_post_blocking, req, payload, timeout)
    except PublishError:
        raise
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise PublishUncertain(f"Upload-Post did not answer: {exc}", cause=exc)
    except OSError as exc:
        raise PublishError(f"Could not read the clip: {exc}")


async def list_profiles(api_key: str, *, timeout: float = 30.0) -> List[Dict[str, Any]]:
    """Profiles on an Upload-Post account, with the platforms each has linked."""
    if not api_key:
        raise PublishError("Missing Upload-Post API key")
    headers = {"Authorization": f"Apikey {api_key}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(PROFILES_ENDPOINT, headers=headers)
    if resp.status_code != 200:
        raise PublishError(f"Failed to fetch profiles (HTTP {resp.status_code})",
                           status=resp.status_code, body=resp.text[:1000])
    return parse_profiles(resp.json())


def parse_profiles(data: Any) -> List[Dict[str, Any]]:
    """Flatten the vendor's profile payload to ``[{username, connected}]``."""
    profiles: List[Dict[str, Any]] = []
    if not isinstance(data, dict):
        return profiles
    for entry in data.get("profiles") or []:
        if not isinstance(entry, dict):
            continue
        username = entry.get("username")
        if not username:
            continue
        socials = entry.get("social_accounts") or {}
        connected = [p for p in SUPPORTED_PLATFORMS if isinstance(socials.get(p), dict)]
        profiles.append({"username": username, "connected": connected})
    return profiles
