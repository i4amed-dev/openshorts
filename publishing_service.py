"""The one implementation of every Upload-Post call Klippo makes.

Callers — none of which reimplement any of it:

* ``POST /api/social/post`` — the manual clip card and the Schedule Week modal
* ``POST /api/saasshorts/post`` — AI Shorts
* ``POST /api/thumbnail/publish`` — YouTube video + thumbnail
* Autopilot — unattended publishing, status reconciliation and cancellation

Keeping the Upload-Post surface in one module is not tidiness: the payload has
platform-specific quirks (``media_type=REELS``, separate ``youtube_title`` /
``tiktok_title`` fields), and a second copy drifts until the callers publish
subtly different posts.

Vendor contract, verified against the current official documentation
(docs.upload-post.com, checked 12-aug-2026) rather than assumed:

* ``POST /api/upload`` — ``Authorization: Apikey <key>``.
  With ``async_upload=true`` it returns 200 + ``request_id``.
  With ``scheduled_date`` it returns 202 + ``job_id``.
  **A 2xx means the vendor ACCEPTED the job — not that anything is live.**
* ``request_id`` is a *client-provided* parameter: "Client-provided request
  identifier. If omitted, the server generates one. Returned in every response
  and used to track the upload via Upload Status. Useful when async_upload=true
  and the HTTP response might be lost (e.g. timeout)." That is exactly the
  ambiguous-timeout case, so we always send our own.
* ``Idempotency-Key`` header — "Prevents duplicate uploads if the same request
  is retried (e.g., after a timeout). When provided, if a matching upload job
  already exists, the API returns the existing job instead of creating a
  duplicate." Documented, so we send it; we do **not** rely on it alone.
* ``GET /api/uploadposts/status?request_id=|job_id=`` — top-level status is one
  of pending / queued / processing / in_progress / completed / failed, plus
  ``not_found`` with HTTP 404. Per-platform results carry their own status:
  queued / processing / completed / failed / retryable.
  ``completed`` means ALL platforms succeeded; ``failed`` means ALL failed —
  so a mixed outcome must be derived from the ``results`` array, never from the
  top-level value alone.
* ``GET /api/uploadposts/schedule`` lists pending scheduled jobs;
  ``DELETE /api/uploadposts/schedule/<job_id>`` cancels one (404 = unknown or
  already executed).

Two operational fixes over the original inline implementation, both about a
machine that has to stay up for weeks:

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

API_BASE = "https://api.upload-post.com/api"
UPLOAD_ENDPOINT = f"{API_BASE}/upload"
PROFILES_ENDPOINT = f"{API_BASE}/uploadposts/users"
STATUS_ENDPOINT = f"{API_BASE}/uploadposts/status"
SCHEDULE_ENDPOINT = f"{API_BASE}/uploadposts/schedule"

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

    A blind re-POST here risks a duplicate post, so callers must never retry on
    their own. They are not stuck, though: because we always send our own
    ``request_id``, the outcome can be *resolved* by asking the vendor
    ``GET /uploadposts/status?request_id=<ours>`` — which is precisely the case
    the documentation names for that parameter. ``carries_request_id`` says
    whether that lookup is possible for this attempt.
    """

    def __init__(self, message: str, *, cause: Optional[BaseException] = None,
                 request_id: Optional[str] = None):
        super().__init__(message)
        self.cause = cause
        self.request_id = request_id

    @property
    def carries_request_id(self) -> bool:
        return bool(self.request_id)


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
    # Our own stable id, persisted BEFORE the request goes out. The vendor
    # documents this exact use: it makes a lost/timed-out response recoverable
    # through the status endpoint instead of an unanswerable "did it post?".
    request_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishResult:
    """What the vendor said when it ACCEPTED the job.

    ``ok`` means accepted, never "live on the platform" — for a scheduled post
    the content does not exist anywhere yet. Reconciliation through
    :func:`get_status` is what later establishes publication.
    """
    ok: bool
    status_code: int
    response: Dict[str, Any]
    request_id: Optional[str] = None   # async uploads
    job_id: Optional[str] = None       # scheduled posts

    @property
    def tracking_id(self) -> Optional[str]:
        """Whichever identifier the status endpoint should be queried with."""
        return self.job_id or self.request_id


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

    if req.request_id:
        payload["request_id"] = req.request_id

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


def _auth_headers(api_key: str, *, idempotency_key: Optional[str] = None) -> Dict[str, str]:
    headers = {"Authorization": f"Apikey {api_key}"}
    if idempotency_key:
        # Documented vendor behaviour: "if a matching upload job already exists,
        # the API returns the existing job instead of creating a duplicate".
        # A belt to the request_id braces — we never rely on it alone.
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _decode(response: httpx.Response) -> Dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {"raw": response.text[:2000]}
    return body if isinstance(body, dict) else {"raw": body}


def _post_blocking(req: PublishRequest, payload: Dict[str, Any],
                   timeout: float, files_spec: List[tuple]) -> PublishResult:
    """The blocking half, always called from a worker thread.

    File objects go straight to httpx so the multipart body streams off disk
    instead of being materialised in memory.
    """
    handles = []
    try:
        files = {}
        for field_name, path, mime in files_spec:
            handle = open(path, "rb")
            handles.append(handle)
            files[field_name] = (os.path.basename(path), handle, mime)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                UPLOAD_ENDPOINT,
                headers=_auth_headers(req.api_key, idempotency_key=req.request_id),
                data=payload, files=files)
    finally:
        for handle in handles:
            handle.close()

    body = _decode(response)

    if response.status_code not in (200, 201, 202):
        raise PublishError(
            f"Upload-Post rejected the post (HTTP {response.status_code})",
            status=response.status_code,
            body=response.text[:2000],
            # 5xx and 429 are worth another attempt; a 4xx means the request
            # itself is wrong and retrying just burns the vendor's rate limit.
            retryable=response.status_code >= 500 or response.status_code == 429,
        )

    # Parse identifiers defensively: the shape differs between async (200 +
    # request_id) and scheduled (202 + job_id), and we must never invent one.
    returned_request_id = body.get("request_id") or req.request_id
    job_id = body.get("job_id")
    return PublishResult(ok=True, status_code=response.status_code, response=body,
                         request_id=str(returned_request_id) if returned_request_id else None,
                         job_id=str(job_id) if job_id else None)


async def publish(req: PublishRequest, *, timeout: float = DEFAULT_TIMEOUT,
                  extra_files: Optional[List[tuple]] = None) -> PublishResult:
    """Submit one video. Raises PublishError / PublishUncertain.

    Success here means **the vendor accepted the job**. For a scheduled post
    nothing exists on any platform yet; for an async upload the platforms are
    still being processed. Establishing publication is :func:`get_status`'s job.

    A transport error after the request was written is genuinely ambiguous — the
    post may or may not exist — so it surfaces as PublishUncertain rather than
    being silently retried into a duplicate. The exception carries our
    ``request_id`` so the caller can resolve it through the status endpoint.

    ``extra_files`` adds further multipart parts as ``(field, path, mime)``,
    used by the Thumbnail Studio to attach a custom YouTube thumbnail.
    """
    if not req.api_key:
        raise PublishError("Missing Upload-Post API key")
    if not req.user:
        raise PublishError("Missing Upload-Post user profile")
    if not req.file_path or not os.path.exists(req.file_path):
        raise PublishError(f"Video file not found: {req.file_path}")

    files_spec = [("video", req.file_path, "video/mp4")]
    for field_name, path, mime in (extra_files or []):
        if not os.path.exists(path):
            raise PublishError(f"Attachment not found: {path}")
        files_spec.append((field_name, path, mime))

    payload = build_payload(req)
    try:
        return await asyncio.to_thread(_post_blocking, req, payload, timeout, files_spec)
    except PublishError:
        raise
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise PublishUncertain(f"Upload-Post did not answer: {exc}", cause=exc,
                               request_id=req.request_id)
    except OSError as exc:
        raise PublishError(f"Could not read the video: {exc}")


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


# --- Vendor reconciliation ----------------------------------------------------
# A 2xx from /upload means "accepted". Everything below is how Klippo finds out
# what actually happened afterwards. All of it lives here so Autopilot never
# builds an Upload-Post HTTP call of its own.

# Terminal top-level statuses: polling stops here.
TERMINAL_STATUSES = frozenset({"completed", "failed", "not_found"})
# Per-platform outcome that the VENDOR retries itself. Klippo must not resubmit
# on top of it — that is how one platform gets posted twice.
PLATFORM_RETRYABLE = "retryable"


@dataclass
class PlatformResult:
    platform: str
    status: str                    # queued|processing|completed|failed|retryable
    success: Optional[bool] = None
    message: str = ""
    timestamp: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.status == "completed" or (self.success is True and self.status != "failed")

    @property
    def failed(self) -> bool:
        return self.status == "failed" or (self.success is False and self.status != PLATFORM_RETRYABLE)

    @property
    def in_progress(self) -> bool:
        return self.status in ("queued", "processing", PLATFORM_RETRYABLE)


@dataclass
class VendorStatus:
    """A parsed /uploadposts/status response.

    The derived properties exist because the top-level status is not sufficient
    on its own: the vendor defines ``completed`` as *all* platforms succeeding
    and ``failed`` as *all* failing, which leaves a mixed outcome describable
    only by walking ``results``.
    """
    status: str
    http_status: int = 200
    completed: int = 0
    total: int = 0
    results: List[PlatformResult] = field(default_factory=list)
    last_update: Optional[str] = None
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def not_found(self) -> bool:
        return self.status == "not_found"

    @property
    def succeeded_platforms(self) -> List[str]:
        return [r.platform for r in self.results if r.succeeded]

    @property
    def failed_platforms(self) -> List[str]:
        return [r.platform for r in self.results if r.failed]

    @property
    def pending_platforms(self) -> List[str]:
        return [r.platform for r in self.results if r.in_progress]

    @property
    def is_partial_failure(self) -> bool:
        """Some platforms succeeded and others definitively failed.

        This is the state that must never be blanket-retried: re-sending the
        whole request would publish the successful platforms a second time.
        """
        return bool(self.succeeded_platforms) and bool(self.failed_platforms) \
            and not self.pending_platforms


def parse_status(payload: Any, *, http_status: int = 200) -> VendorStatus:
    """Defensive parse. An unrecognised shape is never read as success."""
    if not isinstance(payload, dict):
        return VendorStatus(status="unknown", http_status=http_status,
                            message="Unrecognised status response",
                            raw={"raw": str(payload)[:500]})

    results = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        platform = str(item.get("platform") or "").strip().lower()
        if not platform:
            continue
        success = item.get("success")
        # Per-platform `status` is documented; fall back to the boolean when a
        # response only carries `success`.
        status = str(item.get("status") or "").strip().lower()
        if not status:
            status = "completed" if success is True else ("failed" if success is False else "queued")
        results.append(PlatformResult(
            platform=platform, status=status,
            success=success if isinstance(success, bool) else None,
            message=str(item.get("message") or "")[:500],
            timestamp=item.get("upload_timestamp"),
        ))

    status = str(payload.get("status") or "").strip().lower()
    if not status:
        status = "not_found" if http_status == 404 else "unknown"

    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return VendorStatus(
        status=status, http_status=http_status,
        completed=_int(payload.get("completed")), total=_int(payload.get("total")),
        results=results, last_update=payload.get("last_update"),
        message=str(payload.get("message") or "")[:500],
        raw=sanitize_vendor_payload(payload),
    )


async def get_status(api_key: str, *, request_id: Optional[str] = None,
                     job_id: Optional[str] = None,
                     timeout: float = 30.0) -> VendorStatus:
    """Ask Upload-Post what actually happened. 404 → status ``not_found``."""
    if not api_key:
        raise PublishError("Missing Upload-Post API key")
    if not request_id and not job_id:
        raise PublishError("get_status needs a request_id or a job_id")

    params = {}
    if job_id:
        params["job_id"] = job_id
    if request_id:
        params["request_id"] = request_id

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(STATUS_ENDPOINT, headers=_auth_headers(api_key),
                                params=params)

    if resp.status_code == 404:
        return parse_status(_decode(resp), http_status=404)
    if resp.status_code != 200:
        raise PublishError(f"Status check failed (HTTP {resp.status_code})",
                           status=resp.status_code, body=resp.text[:1000],
                           retryable=resp.status_code >= 500 or resp.status_code == 429)
    return parse_status(_decode(resp), http_status=200)


async def list_scheduled(api_key: str, *, timeout: float = 30.0) -> List[Dict[str, Any]]:
    """Pending scheduled jobs on this account.

    Response is a bare JSON array of
    ``{job_id, scheduled_date, post_type, profile_username, title, preview_url}``.
    """
    if not api_key:
        raise PublishError("Missing Upload-Post API key")
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(SCHEDULE_ENDPOINT, headers=_auth_headers(api_key))
    if resp.status_code != 200:
        raise PublishError(f"Could not list scheduled posts (HTTP {resp.status_code})",
                           status=resp.status_code, body=resp.text[:1000])
    try:
        payload = resp.json()
    except ValueError:
        return []
    if isinstance(payload, dict):          # tolerate a wrapped shape
        payload = payload.get("jobs") or payload.get("scheduled") or []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


class CancelOutcome:
    CANCELED = "canceled"          # vendor confirmed
    NOT_FOUND = "not_found"        # unknown id, or it already executed
    FORBIDDEN = "forbidden"        # read-only calendar
    ERROR = "error"


async def cancel_scheduled(api_key: str, job_id: str, *,
                           timeout: float = 30.0) -> tuple:
    """Cancel one scheduled job. Returns ``(outcome, detail)``.

    A 404 is deliberately NOT reported as success: the job may simply be
    unknown, but it may equally have already executed, and telling the operator
    a live post was cancelled would be a lie. The caller reconciles instead.
    """
    if not api_key:
        raise PublishError("Missing Upload-Post API key")
    if not job_id:
        raise PublishError("cancel_scheduled needs a job_id")

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.delete(f"{SCHEDULE_ENDPOINT}/{job_id}",
                                       headers=_auth_headers(api_key))
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        # Ambiguous: the cancellation may or may not have landed.
        return CancelOutcome.ERROR, f"No answer from Upload-Post: {exc}"

    if resp.status_code == 200:
        return CancelOutcome.CANCELED, (_decode(resp).get("message") or "Cancelled")
    if resp.status_code == 404:
        return CancelOutcome.NOT_FOUND, "Upload-Post does not have this job (unknown or already executed)"
    if resp.status_code == 403:
        return CancelOutcome.FORBIDDEN, "This Upload-Post profile has a read-only calendar"
    return CancelOutcome.ERROR, f"HTTP {resp.status_code}: {resp.text[:200]}"


# Keys a vendor payload must never carry into our database or logs, in case the
# vendor ever echoes the request back to us.
_SECRET_KEYS = {"api_key", "apikey", "authorization", "token", "access_token",
                "secret", "password", "idempotency-key"}


def sanitize_vendor_payload(payload: Any, _depth: int = 0) -> Any:
    """Strip anything credential-shaped before persisting a vendor response."""
    if _depth > 6:
        return "…"
    if isinstance(payload, dict):
        return {k: ("***" if str(k).strip().lower() in _SECRET_KEYS
                    else sanitize_vendor_payload(v, _depth + 1))
                for k, v in payload.items()}
    if isinstance(payload, list):
        return [sanitize_vendor_payload(v, _depth + 1) for v in payload[:50]]
    if isinstance(payload, str):
        return payload[:1000]
    return payload


def poll_interval_seconds(status: str) -> int:
    """Vendor's documented cadence: queued/pending 5–10s, processing 10s, stop when terminal."""
    if status in ("queued", "pending"):
        return 10
    if status in ("processing", "in_progress"):
        return 10
    return 0
