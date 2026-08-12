"""The YouTube Data API client: parsing, validation, retries and quota.

The API is the one part of Autopilot that spends a finite, daily-reset budget.
Two failure modes matter more than throughput: silently trusting a malformed item
(which surfaces 20 minutes later as a failed video job), and retrying a quota
error into a hot loop that eats tomorrow's allowance too.
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from automation.youtube_client import (
    COST_SEARCH_LIST, COST_VIDEOS_LIST, QuotaExhausted, YouTubeClient, YouTubeError,
    is_valid_video_id, parse_iso8601_duration, parse_video_item, quota_reset_time,
)
from autopilot_fakes import run_async


async def _no_sleep(_attempt):
    return None


class TestDurationParsing:
    @pytest.mark.parametrize("text,expected", [
        ("PT1M30S", 90),
        ("PT2H15M10S", 8110),
        ("PT45S", 45),
        ("P1DT2H", 93600),
        ("PT1H", 3600),
    ])
    def test_parses_real_durations(self, text, expected):
        assert parse_iso8601_duration(text) == expected

    def test_live_streams_report_p0d_as_zero(self):
        assert parse_iso8601_duration("P0D") == 0

    @pytest.mark.parametrize("text", [None, "", "banana", "1:30", "PT"])
    def test_unparseable_input_is_zero_not_an_exception(self, text):
        # Discovery must survive one odd item, not abort the whole run.
        assert parse_iso8601_duration(text) == 0

    def test_fractional_seconds_truncate(self):
        assert parse_iso8601_duration("PT1M30.5S") == 90


class TestVideoIdValidation:
    def test_accepts_a_real_id(self):
        assert is_valid_video_id("dQw4w9WgXcQ")

    @pytest.mark.parametrize("bad", [None, "", "short", "a" * 12, "has space!!", 12345])
    def test_rejects_anything_else(self, bad):
        assert not is_valid_video_id(bad)


def _item(**overrides):
    base = {
        "id": "dQw4w9WgXcQ",
        "snippet": {"title": "T", "channelId": "UC123", "channelTitle": "C",
                    "publishedAt": "2026-08-01T10:00:00Z", "categoryId": "22",
                    "liveBroadcastContent": "none", "description": "d"},
        "statistics": {"viewCount": "1000", "likeCount": "50", "commentCount": "7"},
        "contentDetails": {"duration": "PT10M", "definition": "hd", "caption": "true"},
        "status": {"license": "creativeCommon", "privacyStatus": "public",
                   "madeForKids": False, "embeddable": True, "uploadStatus": "processed"},
    }
    base.update(overrides)
    return base


class TestItemParsing:
    def test_parses_a_complete_item(self):
        record = parse_video_item(_item())
        assert record.video_id == "dQw4w9WgXcQ"
        assert record.duration_seconds == 600
        assert record.license == "creativeCommon"
        assert record.caption is True
        assert record.view_count == 1000

    def test_missing_statistics_do_not_crash(self):
        # Channels can hide counts; that means "unknown", not "malformed".
        record = parse_video_item(_item(statistics={}))
        assert record is not None
        assert record.view_count == 0
        assert record.like_count is None

    def test_an_item_without_a_usable_id_is_dropped(self):
        assert parse_video_item(_item(id="nope")) is None
        assert parse_video_item({}) is None
        assert parse_video_item("a string") is None

    def test_engagement_rate_is_zero_when_nobody_watched(self):
        record = parse_video_item(_item(statistics={"viewCount": "0"}))
        assert record.engagement_rate() == 0.0

    def test_velocity_is_clamped_for_a_brand_new_upload(self):
        now = datetime.now(timezone.utc)
        snippet = dict(_item()["snippet"])
        snippet["publishedAt"] = (now - timedelta(minutes=3)).isoformat().replace(
            "+00:00", "Z")
        record = parse_video_item(_item(snippet=snippet))
        # A 3-minute-old video must not report 20,000 views/hour off 1,000 views.
        assert record.views_per_hour(now) == 1000.0


class TestQuotaAndRetries:
    def test_quota_exceeded_is_not_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(403, json={"error": {"errors": [
                {"reason": "quotaExceeded"}]}})

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client = YouTubeClient("k", client=http)
                with pytest.raises(QuotaExhausted):
                    await client.most_popular()

        run_async(scenario())
        # Exactly one call: retrying a quota error is what turns an exhausted
        # key into a loop that burns the next reset too.
        assert calls["n"] == 1

    def test_server_errors_are_retried_then_surface(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(503, json={})

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client = YouTubeClient("k", client=http, max_retries=2)
                client._backoff = _no_sleep
                with pytest.raises(YouTubeError) as exc:
                    await client.most_popular()
                assert exc.value.retryable

        run_async(scenario())
        assert calls["n"] == 2

    def test_a_transient_failure_recovers_on_retry(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"items": [_item()]})

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client = YouTubeClient("k", client=http, max_retries=3)
                client._backoff = _no_sleep
                return await client.most_popular()

        assert len(run_async(scenario())) == 1

    def test_units_are_reported_only_for_successful_calls(self):
        spent = []
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"items": []})

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client = YouTubeClient("k", client=http, max_retries=3, on_units=spent.append)
                client._backoff = _no_sleep
                await client.most_popular()

        run_async(scenario())
        assert spent == [COST_VIDEOS_LIST]

    def test_search_costs_a_hundred_units(self):
        spent = []

        def handler(request):
            return httpx.Response(200, json={"items": [{"id": {"videoId": "dQw4w9WgXcQ"}}]})

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client = YouTubeClient("k", client=http, on_units=spent.append)
                return await client.search_video_ids("cooking")

        assert run_async(scenario()) == ["dQw4w9WgXcQ"]
        assert spent == [COST_SEARCH_LIST]

    def test_pagination_is_bounded(self):
        # Every page hands back a nextPageToken; the client must still stop.
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json={"items": [_item()], "nextPageToken": "more"})

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client = YouTubeClient("k", client=http)
                await client.most_popular(max_results=50, max_pages=99)

        run_async(scenario())
        assert calls["n"] <= 4

    def test_missing_api_key_fails_before_any_request(self):
        async def scenario():
            client = YouTubeClient("")
            assert not client.configured
            with pytest.raises(YouTubeError):
                await client.most_popular()

        run_async(scenario())

    def test_hydrate_batches_fifty_ids_per_call(self):
        seen_batches = []

        def handler(request):
            seen_batches.append(request.url.params["id"].split(","))
            return httpx.Response(200, json={"items": []})

        async def scenario():
            ids = [f"vid{i:08d}" for i in range(120)]
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client = YouTubeClient("k", client=http)
                await client.hydrate(ids)

        run_async(scenario())
        assert [len(batch) for batch in seen_batches] == [50, 50, 20]


def test_quota_reset_is_within_the_next_day():
    now = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)
    reset = quota_reset_time(now)
    assert now < reset <= now + timedelta(hours=24)
