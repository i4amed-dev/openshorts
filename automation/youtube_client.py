"""The single place Autopilot talks to YouTube.

Official Data API v3 only — no HTML scraping, which breaks silently and violates
YouTube's terms. Everything the orchestrator needs (charts, search, statistics)
goes through this client so retries, timeouts, quota accounting and response
validation exist exactly once.

Quota costs (units, per the Data API documentation):
  videos.list          1
  search.list        100
  videoCategories.list 1
The daily default allowance is 10,000 units, so a naive "search on every tick"
design exhausts a key before lunch. Discovery is therefore scheduled, batched
(50 ids per videos.list) and hard-stopped by the caller's page budget.
"""
from __future__ import annotations

import asyncio
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

import httpx

API_BASE = "https://www.googleapis.com/youtube/v3"

COST_VIDEOS_LIST = 1
COST_SEARCH_LIST = 100
COST_CATEGORIES_LIST = 1

_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


class YouTubeError(RuntimeError):
    """Any failure talking to the Data API, with the useful bits kept."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 reason: Optional[str] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retryable = retryable


class QuotaExhausted(YouTubeError):
    """The API key is out of daily quota (or rate-limited)."""

    def __init__(self, message: str, *, reason: str = "quotaExceeded"):
        super().__init__(message, status=403, reason=reason, retryable=False)


def parse_iso8601_duration(text: Optional[str]) -> int:
    """ISO-8601 duration → whole seconds. 0 when absent/unparseable.

    YouTube reports live streams as ``P0D``; the eligibility filter treats a
    zero duration as "unknown", which its live-broadcast rule rejects anyway.
    """
    if not text:
        return 0
    match = _ISO_DURATION_RE.match(str(text).strip())
    if not match:
        return 0
    parts = match.groupdict()
    total = 0.0
    total += int(parts["days"] or 0) * 86400
    total += int(parts["hours"] or 0) * 3600
    total += int(parts["minutes"] or 0) * 60
    total += float(parts["seconds"] or 0)
    return int(total)


def canonical_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def is_valid_video_id(video_id: Any) -> bool:
    return bool(video_id) and bool(_VIDEO_ID_RE.match(str(video_id)))


@dataclass
class VideoRecord:
    """A validated ``videos.list`` item, flattened to what ranking needs."""
    video_id: str
    title: str = ""
    description: str = ""
    channel_id: str = ""
    channel_title: str = ""
    published_at: Optional[datetime] = None
    duration_seconds: int = 0
    category_id: str = ""
    view_count: int = 0
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    license: str = "youtube"
    definition: str = "sd"
    caption: bool = False
    live_state: str = "none"
    made_for_kids: bool = False
    privacy_status: str = "public"
    embeddable: bool = True
    upload_status: str = "processed"
    chart_rank: Optional[int] = None
    discovery_source: str = ""

    @property
    def url(self) -> str:
        return canonical_watch_url(self.video_id)

    def age_hours(self, now: Optional[datetime] = None) -> float:
        if self.published_at is None:
            return float("inf")
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.published_at).total_seconds() / 3600.0)

    def views_per_hour(self, now: Optional[datetime] = None) -> float:
        age = self.age_hours(now)
        if age in (0.0, float("inf")):
            # A video minutes old has a meaningless velocity; clamp the window to
            # one hour so a 3-view/3-minute upload doesn't out-rank everything.
            age = 1.0
        return self.view_count / max(1.0, age)

    def engagement_rate(self) -> float:
        if self.view_count <= 0:
            return 0.0
        return ((self.like_count or 0) + (self.comment_count or 0)) / self.view_count


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_published(text: Any) -> Optional[datetime]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_video_item(item: Dict[str, Any], *, chart_rank: Optional[int] = None,
                     discovery_source: str = "") -> Optional[VideoRecord]:
    """Validate one ``videos.list`` item into a VideoRecord (None if unusable).

    The API happily returns items with missing parts (a deleted channel, a
    region-blocked video), and trusting them produces sources that fail 20
    minutes into processing instead of at discovery.
    """
    if not isinstance(item, dict):
        return None
    video_id = item.get("id")
    if isinstance(video_id, dict):  # search.list shape
        video_id = video_id.get("videoId")
    if not is_valid_video_id(video_id):
        return None
    snippet = item.get("snippet") or {}
    stats = item.get("statistics") or {}
    content = item.get("contentDetails") or {}
    status = item.get("status") or {}
    if not isinstance(snippet, dict) or not isinstance(stats, dict):
        return None

    return VideoRecord(
        video_id=str(video_id),
        title=str(snippet.get("title") or "")[:500],
        description=str(snippet.get("description") or "")[:2000],
        channel_id=str(snippet.get("channelId") or ""),
        channel_title=str(snippet.get("channelTitle") or "")[:200],
        published_at=_parse_published(snippet.get("publishedAt")),
        duration_seconds=parse_iso8601_duration(content.get("duration")),
        category_id=str(snippet.get("categoryId") or ""),
        view_count=_to_int(stats.get("viewCount")) or 0,
        like_count=_to_int(stats.get("likeCount")),
        comment_count=_to_int(stats.get("commentCount")),
        license=str(status.get("license") or "youtube"),
        definition=str(content.get("definition") or "sd").lower(),
        caption=str(content.get("caption") or "false").lower() == "true",
        live_state=str(snippet.get("liveBroadcastContent") or "none").lower(),
        made_for_kids=bool(status.get("madeForKids")),
        privacy_status=str(status.get("privacyStatus") or "public"),
        embeddable=status.get("embeddable") is not False,
        upload_status=str(status.get("uploadStatus") or "processed"),
        chart_rank=chart_rank,
        discovery_source=discovery_source,
    )


class YouTubeClient:
    """Async Data API client with retries, timeouts and quota accounting.

    ``on_units`` is called with the unit cost of every *successful* request so
    the caller can persist consumption; ``QuotaExhausted`` is raised (not
    retried) so the orchestrator can park discovery until the quota resets.
    """

    def __init__(self, api_key: Optional[str] = None, *, timeout: float = 20.0,
                 max_retries: int = 3, on_units=None,
                 client: Optional[httpx.AsyncClient] = None):
        self.api_key = api_key or os.environ.get("YOUTUBE_DATA_API_KEY") or ""
        self.timeout = timeout
        self.max_retries = max_retries
        self._on_units = on_units
        self._client = client
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def __aenter__(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True
        return self

    async def __aexit__(self, *exc):
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: Dict[str, Any], cost: int) -> Dict[str, Any]:
        if not self.api_key:
            raise YouTubeError("YOUTUBE_DATA_API_KEY is not configured")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True

        query = {k: v for k, v in params.items() if v not in (None, "", [])}
        query["key"] = self.api_key

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = await self._client.get(f"{API_BASE}/{path}", params=query,
                                              timeout=self.timeout)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = YouTubeError(f"{path}: {exc}", retryable=True)
                await self._backoff(attempt)
                continue

            if resp.status_code == 200:
                if self._on_units:
                    self._on_units(cost)
                try:
                    data = resp.json()
                except ValueError:
                    raise YouTubeError(f"{path}: response was not JSON")
                if not isinstance(data, dict):
                    raise YouTubeError(f"{path}: unexpected response shape")
                return data

            reason = _error_reason(resp)
            if resp.status_code == 403 and reason in (
                    "quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded",
                    "userRateLimitExceeded"):
                # Never retried: the key is out of units. Retrying is what turns
                # an exhausted quota into a hot loop.
                raise QuotaExhausted(f"{path}: {reason}", reason=reason)
            if resp.status_code in (500, 502, 503, 504) or resp.status_code == 429:
                last_error = YouTubeError(f"{path}: HTTP {resp.status_code}",
                                          status=resp.status_code, reason=reason,
                                          retryable=True)
                await self._backoff(attempt)
                continue
            raise YouTubeError(f"{path}: HTTP {resp.status_code} ({reason or 'error'})",
                               status=resp.status_code, reason=reason)

        raise last_error or YouTubeError(f"{path}: exhausted retries")

    async def _backoff(self, attempt: int) -> None:
        # Exponential with jitter: several Autopilot instances (or a retry after
        # a network blip) must not line up on the same retry instant.
        delay = min(30.0, (2 ** attempt)) * (0.7 + 0.6 * random.random())
        await asyncio.sleep(delay)

    # --- Strategy A: what the region is actually watching ---------------------

    async def most_popular(self, *, region_code: str = "US", category_id: str = "",
                           max_results: int = 50,
                           max_pages: int = 1) -> List[VideoRecord]:
        """``videos.list(chart=mostPopular)`` — 1 unit per page, ≤50 items each.

        ``max_pages`` is a hard cap, never "follow nextPageToken until it stops":
        an unbounded walk is how a client silently spends a whole day's quota.
        """
        out: List[VideoRecord] = []
        page_token = None
        rank = 0
        source = "most_popular" + (f":{category_id}" if category_id else "")
        for _ in range(max(1, min(4, max_pages))):
            data = await self._get("videos", {
                "part": "snippet,statistics,contentDetails,status",
                "chart": "mostPopular",
                "regionCode": region_code,
                "videoCategoryId": category_id or None,
                "maxResults": max(1, min(50, max_results)),
                "pageToken": page_token,
            }, COST_VIDEOS_LIST)
            items = data.get("items")
            if not isinstance(items, list):
                break
            for item in items:
                rank += 1
                record = parse_video_item(item, chart_rank=rank, discovery_source=source)
                if record is not None:
                    out.append(record)
            page_token = data.get("nextPageToken")
            if not page_token or len(out) >= max_results:
                break
        return out[:max_results]

    # --- Strategy B: the operator's niche ------------------------------------

    async def search_video_ids(self, query: str, *, region_code: str = "US",
                               relevance_language: str = "en",
                               published_after: Optional[datetime] = None,
                               max_results: int = 25,
                               creative_commons: bool = False) -> List[str]:
        """``search.list`` — 100 units per call, so one call per topic per run."""
        params: Dict[str, Any] = {
            "part": "id",
            "type": "video",
            "q": query[:200],
            "order": "viewCount",
            "regionCode": region_code,
            "relevanceLanguage": relevance_language,
            "maxResults": max(1, min(50, max_results)),
            "safeSearch": "moderate",
        }
        if published_after is not None:
            params["publishedAfter"] = published_after.astimezone(
                timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if creative_commons:
            params["videoLicense"] = "creativeCommon"

        data = await self._get("search", params, COST_SEARCH_LIST)
        ids: List[str] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            video_id = (item.get("id") or {}).get("videoId") if isinstance(
                item.get("id"), dict) else None
            if is_valid_video_id(video_id) and video_id not in ids:
                ids.append(str(video_id))
        return ids

    async def hydrate(self, video_ids: Sequence[str], *,
                      discovery_source: str = "") -> List[VideoRecord]:
        """Batch ids through ``videos.list`` — 50 per call, 1 unit each."""
        clean = [vid for vid in dict.fromkeys(video_ids) if is_valid_video_id(vid)]
        out: List[VideoRecord] = []
        for start in range(0, len(clean), 50):
            chunk = clean[start:start + 50]
            data = await self._get("videos", {
                "part": "snippet,statistics,contentDetails,status",
                "id": ",".join(chunk),
                "maxResults": 50,
            }, COST_VIDEOS_LIST)
            for item in data.get("items") or []:
                record = parse_video_item(item, discovery_source=discovery_source)
                if record is not None:
                    out.append(record)
        return out

    async def ping(self) -> bool:
        """Cheap reachability check for the health endpoint (1 quota unit)."""
        await self._get("videoCategories", {"part": "id", "regionCode": "US"},
                        COST_CATEGORIES_LIST)
        return True


def _error_reason(resp: httpx.Response) -> Optional[str]:
    try:
        payload = resp.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return errors[0].get("reason")
    return error.get("status")


def quota_reset_time(now: Optional[datetime] = None) -> datetime:
    """Next midnight Pacific — when the Data API resets a project's daily quota.

    Approximated as UTC-8 without DST: the cost of being an hour conservative is
    an hour of extra idling, while being early means a burst of 403s.
    """
    now = now or datetime.now(timezone.utc)
    pacific_now = now - timedelta(hours=8)
    next_midnight = (pacific_now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return next_midnight + timedelta(hours=8)
