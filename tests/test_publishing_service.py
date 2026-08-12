"""The shared Upload-Post publishing service.

One implementation, two callers (the manual button and Autopilot). These tests
pin the payload shape — the platform-specific quirks are exactly what a second
copy would drift on — plus the two operational fixes: the clip is streamed
rather than buffered, and an ambiguous outcome is reported as ambiguous instead
of guessed either way.
"""
import pathlib

import httpx
import pytest

import publishing_service as svc
from autopilot_fakes import run_async

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "clip_1.mp4"
    path.write_bytes(b"\x00" * 4096)
    return str(path)


def request_for(clip_path, **overrides):
    fields = dict(file_path=clip_path, platforms=["tiktok"], user="profile",
                  api_key="secret", title="Title", description="Desc")
    fields.update(overrides)
    return svc.PublishRequest(**fields)


class TestPlatformValidation:
    def test_normalises_case_and_whitespace(self):
        assert svc.validate_platforms([" TikTok ", "YouTube"]) == ["tiktok", "youtube"]

    def test_drops_unknown_platforms(self):
        # An unrecognised platform is a silent no-op at the vendor, which looks
        # like a successful post that never appears anywhere.
        assert svc.validate_platforms(["tiktok", "myspace"]) == ["tiktok"]

    def test_deduplicates(self):
        assert svc.validate_platforms(["tiktok", "tiktok"]) == ["tiktok"]

    @pytest.mark.parametrize("bad", [None, [], ["myspace"]])
    def test_nothing_valid_is_an_error(self, bad):
        with pytest.raises(svc.PublishError):
            svc.validate_platforms(bad)


class TestPayload:
    def test_carries_the_common_fields(self, clip):
        payload = svc.build_payload(request_for(clip, platforms=["tiktok"]))
        assert payload["user"] == "profile"
        assert payload["title"] == "Title"
        assert payload["platform[]"] == ["tiktok"]
        assert payload["async_upload"] == "true"

    def test_tiktok_gets_its_own_caption_field(self, clip):
        payload = svc.build_payload(request_for(clip, platforms=["tiktok"]))
        assert payload["tiktok_title"] == "Desc"

    def test_instagram_is_posted_as_a_reel(self, clip):
        payload = svc.build_payload(request_for(clip, platforms=["instagram"]))
        assert payload["media_type"] == "REELS"
        assert payload["instagram_title"] == "Desc"

    def test_youtube_gets_title_description_and_privacy(self, clip):
        payload = svc.build_payload(request_for(clip, platforms=["youtube"],
                                                youtube_title="YT Title"))
        assert payload["youtube_title"] == "YT Title"
        assert payload["youtube_description"] == "Desc"
        assert payload["privacyStatus"] == "public"

    def test_youtube_falls_back_to_the_generic_title(self, clip):
        payload = svc.build_payload(request_for(clip, platforms=["youtube"],
                                                youtube_title=None))
        assert payload["youtube_title"] == "Title"

    def test_scheduling_sends_the_date_and_the_zone_together(self, clip):
        payload = svc.build_payload(request_for(
            clip, scheduled_date="2026-08-13T11:30:00", timezone="Europe/Madrid"))
        assert payload["scheduled_date"] == "2026-08-13T11:30:00"
        assert payload["timezone"] == "Europe/Madrid"

    def test_an_unscheduled_post_sends_no_date(self, clip):
        payload = svc.build_payload(request_for(clip))
        assert "scheduled_date" not in payload
        assert "timezone" not in payload

    def test_empty_title_and_description_get_safe_defaults(self, clip):
        payload = svc.build_payload(request_for(clip, title="", description=""))
        assert payload["title"] == "Viral Short"
        assert payload["tiktok_title"] == "Check this out!"

    def test_all_three_platforms_in_one_call(self, clip):
        payload = svc.build_payload(request_for(
            clip, platforms=["tiktok", "instagram", "youtube"]))
        assert set(payload["platform[]"]) == {"tiktok", "instagram", "youtube"}
        assert {"tiktok_title", "instagram_title", "youtube_title"} <= set(payload)


class TestTransport:
    def test_a_successful_post_returns_the_vendor_body(self, clip, monkeypatch):
        captured = {}

        def handler(request):
            captured["body_len"] = len(request.content)
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"success": True, "id": "abc"})

        _patch_sync_client(monkeypatch, handler)
        result = run_async(svc.publish(request_for(clip)))
        assert result.ok and result.response["id"] == "abc"
        assert captured["auth"] == "Apikey secret"
        assert captured["body_len"] > 4096   # the clip really was uploaded

    @pytest.mark.parametrize("status", [200, 201, 202])
    def test_every_accepted_status_counts_as_success(self, clip, monkeypatch, status):
        _patch_sync_client(monkeypatch, lambda r: httpx.Response(status, json={"ok": 1}))
        assert run_async(svc.publish(request_for(clip))).ok

    def test_a_client_error_is_not_retryable(self, clip, monkeypatch):
        _patch_sync_client(monkeypatch,
                           lambda r: httpx.Response(400, json={"error": "bad title"}))
        with pytest.raises(svc.PublishError) as exc:
            run_async(svc.publish(request_for(clip)))
        assert exc.value.status == 400
        assert not exc.value.retryable

    def test_a_server_error_is_retryable(self, clip, monkeypatch):
        _patch_sync_client(monkeypatch, lambda r: httpx.Response(503, text="down"))
        with pytest.raises(svc.PublishError) as exc:
            run_async(svc.publish(request_for(clip)))
        assert exc.value.retryable

    def test_a_timeout_is_uncertain_not_a_failure(self, clip, monkeypatch):
        """The distinction that prevents duplicate posts.

        A timeout means the vendor may or may not hold the post. Calling it a
        failure invites a retry that double-posts; calling it a success loses
        the record.
        """
        def handler(request):
            raise httpx.ReadTimeout("timed out", request=request)

        _patch_sync_client(monkeypatch, handler)
        with pytest.raises(svc.PublishUncertain):
            run_async(svc.publish(request_for(clip)))

    def test_a_non_json_response_still_surfaces(self, clip, monkeypatch):
        _patch_sync_client(monkeypatch, lambda r: httpx.Response(200, text="OK"))
        result = run_async(svc.publish(request_for(clip)))
        assert result.response["raw"] == "OK"

    def test_a_missing_file_fails_before_any_request(self, clip):
        with pytest.raises(svc.PublishError, match="not found"):
            run_async(svc.publish(request_for("/does/not/exist.mp4")))

    def test_missing_credentials_fail_before_any_request(self, clip):
        with pytest.raises(svc.PublishError, match="API key"):
            run_async(svc.publish(request_for(clip, api_key="")))
        with pytest.raises(svc.PublishError, match="profile"):
            run_async(svc.publish(request_for(clip, user="")))


class TestProfileParsing:
    def test_extracts_usernames_and_connected_platforms(self):
        data = {"success": True, "profiles": [
            {"username": "me", "social_accounts": {"tiktok": {"id": 1},
                                                   "instagram": {}, "youtube": ""}},
        ]}
        assert svc.parse_profiles(data) == [
            {"username": "me", "connected": ["tiktok", "instagram"]}]

    def test_entries_without_a_username_are_dropped(self):
        assert svc.parse_profiles({"profiles": [{"social_accounts": {}}]}) == []

    @pytest.mark.parametrize("junk", [None, "text", {}, {"profiles": "nope"}])
    def test_junk_does_not_raise(self, junk):
        assert svc.parse_profiles(junk) == []


class TestSourceLevelGuarantees:
    """Properties easier to assert on the source than to observe at runtime."""

    def test_the_clip_is_never_slurped_into_memory(self):
        body = _code_of(REPO / "publishing_service.py")
        # The previous inline implementation did `file_content = f.read()`, which
        # buffers a whole clip in RAM on an 8 GB machine already running Whisper.
        assert "f.read()" not in body and "handle.read()" not in body
        assert '"video": (filename, handle, "video/mp4")' in body

    def test_the_blocking_call_runs_off_the_event_loop(self):
        body = _code_of(REPO / "publishing_service.py")
        # A sync httpx.Client inside an async endpoint stalls every status poll
        # and the Autopilot tick along with it.
        assert "asyncio.to_thread(_post_blocking" in body

    def test_no_endpoint_reimplements_the_clip_payload(self):
        """Every clip publisher — manual, AI Shorts, Autopilot — shares one payload.

        (The Thumbnail Studio publisher is a genuinely different vendor call: it
        posts a video *and* a thumbnail part to YouTube only, so it is not a
        duplicate of this service.)
        """
        app_source = _code_of(REPO / "app.py")
        for field in ('data_payload["tiktok_title"]', 'data_payload["instagram_title"]',
                      '"media_type"'):
            assert field not in app_source, f"{field} is built in two places again"

    def test_every_clip_publisher_calls_the_shared_service(self):
        app_source = _code_of(REPO / "app.py")
        # /api/social/post, /api/saasshorts/post and the Autopilot adapter.
        assert app_source.count("publishing_service.publish(") == 3

    def test_autopilot_submits_through_the_shared_job_path(self):
        app_source = _code_of(REPO / "app.py")
        # One submission function; /api/process and Autopilot both reach it.
        assert app_source.count("def submit_clip_job(") == 1
        assert "submit_clip_job(\n" in app_source           # the endpoint's call
        assert "asyncio.to_thread(\n        submit_clip_job," in app_source


def _code_of(path):
    """Executable source only — comments and docstrings stripped.

    These assertions are about what the code does, and prose that *describes*
    the old implementation ("it used to call f.read()") would otherwise fail
    them. Docstring line ranges come from the AST; comments are dropped by hand.
    """
    import ast

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    prose_lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            prose_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))

    kept = []
    for number, line in enumerate(source.splitlines(), start=1):
        if number in prose_lines or line.strip().startswith("#"):
            continue
        kept.append(line.split("  # ")[0])
    return "\n".join(kept)


def _patch_sync_client(monkeypatch, handler):
    """Route publishing_service's synchronous httpx.Client at a mock transport."""
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(svc.httpx, "Client", factory)
