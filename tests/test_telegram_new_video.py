"""The manual URL flow: URL validation, the metadata/quality preview, and —
the part the spec is strict about — the rights confirmation tap must be a
real, recorded attestation before `submit_clip_job` (the exact function
`/api/process` uses) is ever called. `/cancel` must always work.

`app.py` itself needs boto3/ultralytics/mediapipe/faster-whisper, which this
test environment (deliberately) doesn't install — see .github/workflows/ci.yml.
So a fake `app` module is injected into `sys.modules` before each test, the
same way production `app.py` would already be loaded into `sys.modules` by
the time a real Telegram update reaches this handler.
"""
from __future__ import annotations

import os
import sys
import types
from types import SimpleNamespace

import pytest

from autopilot_fakes import run_async
from telegram_bot.handlers import new_video


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)


class FakeQuery:
    def __init__(self):
        self.edits = []
        self.answers = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))

    async def answer(self, *a, **kw):
        self.answers.append((a, kw))


class FakeContext:
    def __init__(self):
        self.user_data = {}
        self.sent = []

    async def _send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))

    @property
    def bot(self):
        return SimpleNamespace(send_message=self._send)


class FakeTelegramFile:
    def __init__(self, content: bytes):
        self.content = content

    async def download_to_drive(self, path):
        with open(path, "wb") as fh:
            fh.write(self.content)


class FakeVideo:
    def __init__(self, file_size: int, content: bytes = b"fake video bytes"):
        self.file_size = file_size
        self._content = content

    async def get_file(self):
        return FakeTelegramFile(self._content)


def _update(*, text=None, has_query=False, video=None, document=None, user_id=1, chat_id=1):
    message = None
    if text is not None or video is not None or document is not None:
        message = FakeMessage(text or "")
        message.video = video
        message.document = document
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="admin"),
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
        callback_query=FakeQuery() if has_query else None)


@pytest.fixture(autouse=True)
def _admin(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "1")
    monkeypatch.setenv("AUTOPILOT_DB_PATH", str(tmp_path / "bot.db"))
    from telegram_bot import persistence
    persistence.reset_store()
    yield
    persistence.reset_store()


@pytest.fixture
def fake_app(monkeypatch, tmp_path):
    """A stand-in for the real app.py module, injected into sys.modules."""
    calls = {"submit_clip_job": []}

    async def probe_quality(url):
        return {"title": "Great Video", "channel": "Great Channel",
                "duration": 125, "max_height": 1080}

    def submit_clip_job(**kwargs):
        calls["submit_clip_job"].append(kwargs)
        return kwargs["job_id"]

    module = types.ModuleType("app")
    module._probe_youtube_quality = probe_quality
    module.submit_clip_job = submit_clip_job
    module.OUTPUT_DIR = str(tmp_path / "output")
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    module.UPLOAD_DIR = str(upload_dir)
    monkeypatch.setitem(sys.modules, "app", module)
    return calls


class TestStart:
    def test_prompts_for_a_url_and_sets_conversation_state(self):
        update = _update(text="/new")
        context = FakeContext()
        run_async(new_video.start(update, context))
        assert context.user_data[new_video.STAGE_KEY]["stage"] == "await_url"
        assert "YouTube URL" in update.message.sent[0]

    def test_viewer_cannot_start(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "999")
        update = _update(text="/new", user_id=1)
        context = FakeContext()
        run_async(new_video.start(update, context))
        assert new_video.STAGE_KEY not in context.user_data


class TestUrlValidation:
    def test_non_youtube_text_is_rejected_and_stage_unchanged(self):
        update = _update(text="not a url")
        context = FakeContext()
        context.user_data[new_video.STAGE_KEY] = {"stage": "await_url"}
        run_async(new_video.handle_message(update, context))
        assert context.user_data[new_video.STAGE_KEY]["stage"] == "await_url"
        assert "doesn't look like" in update.message.sent[-1]

    def test_ignores_plain_text_outside_the_conversation(self):
        update = _update(text="hello")
        context = FakeContext()  # no STAGE_KEY set — not mid-flow
        run_async(new_video.handle_message(update, context))
        assert update.message.sent == []

    def test_valid_url_shows_the_real_probe_and_asks_to_confirm(self, fake_app):
        update = _update(text="https://youtu.be/abc123")
        context = FakeContext()
        context.user_data[new_video.STAGE_KEY] = {"stage": "await_url"}
        run_async(new_video.handle_message(update, context))

        assert context.user_data[new_video.STAGE_KEY]["stage"] == "confirm"
        preview = context.sent[-1][1]
        assert "Great Video" in preview
        assert "Great Channel" in preview
        assert "I own this video" in preview


class TestConfirm:
    def test_confirm_submits_through_the_shared_pipeline_with_a_real_attestation(self, fake_app, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        update = _update(has_query=True)
        context = FakeContext()
        context.user_data[new_video.STAGE_KEY] = {"stage": "confirm", "url": "https://youtu.be/abc123"}
        run_async(new_video.confirm(update, context, cb=None))

        assert len(fake_app["submit_clip_job"]) == 1
        submitted = fake_app["submit_clip_job"][0]
        assert submitted["url"] == "https://youtu.be/abc123"
        assert submitted["attestation"]["acknowledged"] is True
        assert "telegram:1" in submitted["attestation"]["ip"]
        # State is cleared — a second tap can't double-submit.
        assert new_video.STAGE_KEY not in context.user_data

    def test_confirm_without_a_pending_request_is_refused(self, fake_app):
        update = _update(has_query=True)
        context = FakeContext()  # nothing staged — e.g. bot restarted mid-flow
        run_async(new_video.confirm(update, context, cb=None))
        assert fake_app["submit_clip_job"] == []
        assert "expired" in update.callback_query.answers[0][0][0].lower()

    def test_missing_gemini_key_refuses_clearly(self, fake_app, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        update = _update(has_query=True)
        context = FakeContext()
        context.user_data[new_video.STAGE_KEY] = {"stage": "confirm", "url": "https://youtu.be/abc123"}
        run_async(new_video.confirm(update, context, cb=None))
        assert fake_app["submit_clip_job"] == []


class TestCancel:
    def test_cancel_command_clears_state(self):
        from telegram_bot.app import _cancel_any
        update = _update(text="/cancel")
        context = FakeContext()
        context.user_data[new_video.STAGE_KEY] = {"stage": "confirm", "url": "x"}
        run_async(_cancel_any(update, context))
        assert new_video.STAGE_KEY not in context.user_data
        assert "Cancelled" in update.message.sent[0]

    def test_cancel_with_nothing_pending_says_so(self):
        from telegram_bot.app import _cancel_any
        update = _update(text="/cancel")
        context = FakeContext()
        run_async(_cancel_any(update, context))
        assert "Nothing to cancel" in update.message.sent[0]

    def test_cancel_button_clears_state_without_submitting(self, fake_app):
        update = _update(has_query=True)
        context = FakeContext()
        context.user_data[new_video.STAGE_KEY] = {"stage": "confirm", "url": "https://youtu.be/abc123"}
        run_async(new_video.cancel_flow(update, context, cb=None))
        assert new_video.STAGE_KEY not in context.user_data
        assert fake_app["submit_clip_job"] == []


class TestVideoUpload:
    def test_oversized_upload_is_refused_before_any_download(self, fake_app):
        update = _update(video=FakeVideo(file_size=25 * 1024 * 1024))
        context = FakeContext()
        run_async(new_video.handle_upload(update, context))
        assert "20 MB" in update.message.sent[-1]
        assert new_video.STAGE_KEY not in context.user_data

    def test_valid_upload_downloads_and_asks_for_confirmation(self, fake_app):
        update = _update(video=FakeVideo(file_size=5 * 1024 * 1024))
        context = FakeContext()
        run_async(new_video.handle_upload(update, context))
        state = context.user_data[new_video.STAGE_KEY]
        assert state["stage"] == "confirm"
        assert os.path.exists(state["input_path"])
        assert "I own this video" in update.message.sent[-1]

    def test_video_mimetype_document_is_also_accepted(self, fake_app):
        doc = SimpleNamespace(mime_type="video/mp4", file_size=1024,
                              get_file=FakeVideo(file_size=1024).get_file)
        update = _update(document=doc)
        context = FakeContext()
        run_async(new_video.handle_upload(update, context))
        assert new_video.STAGE_KEY in context.user_data

    def test_non_video_document_is_ignored(self, fake_app):
        doc = SimpleNamespace(mime_type="application/pdf", file_size=1024)
        update = _update(document=doc)
        context = FakeContext()
        run_async(new_video.handle_upload(update, context))
        assert new_video.STAGE_KEY not in context.user_data
        assert update.message.sent == []

    def test_viewer_cannot_upload(self, fake_app, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "999")
        update = _update(video=FakeVideo(file_size=1024), user_id=1)
        context = FakeContext()
        run_async(new_video.handle_upload(update, context))
        assert new_video.STAGE_KEY not in context.user_data

    def test_confirming_an_upload_submits_via_input_path_not_url(self, fake_app, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        update = _update(video=FakeVideo(file_size=1024))
        context = FakeContext()
        run_async(new_video.handle_upload(update, context))
        uploaded_path = context.user_data[new_video.STAGE_KEY]["input_path"]

        confirm_update = _update(has_query=True)
        run_async(new_video.confirm(confirm_update, context, cb=None))

        submitted = fake_app["submit_clip_job"][0]
        assert submitted["input_path"] == uploaded_path
        assert submitted["url"] is None
        assert submitted["attestation"]["source"] == "file"

    def test_cancelling_an_upload_deletes_the_downloaded_file(self, fake_app):
        update = _update(video=FakeVideo(file_size=1024))
        context = FakeContext()
        run_async(new_video.handle_upload(update, context))
        uploaded_path = context.user_data[new_video.STAGE_KEY]["input_path"]
        assert os.path.exists(uploaded_path)

        cancel_update = _update(has_query=True)
        run_async(new_video.cancel_flow(cancel_update, context, cb=None))
        assert not os.path.exists(uploaded_path)

    def test_missing_gemini_key_cleans_up_the_orphaned_upload(self, fake_app):
        update = _update(video=FakeVideo(file_size=1024))
        context = FakeContext()
        run_async(new_video.handle_upload(update, context))
        uploaded_path = context.user_data[new_video.STAGE_KEY]["input_path"]

        confirm_update = _update(has_query=True)
        run_async(new_video.confirm(confirm_update, context, cb=None))  # no GEMINI_API_KEY set
        assert not os.path.exists(uploaded_path)
        assert fake_app["submit_clip_job"] == []
