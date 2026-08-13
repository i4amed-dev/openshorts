import os
import sys

import pytest

# Make the repo root importable so tests can import the app modules directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Credentials the app reads from the environment, several of them through
# `load_dotenv()` at module import. Any repo module that does so pulls the
# developer's real .env into the test process, where it stays for the rest of
# the session.
_CREDENTIAL_VARS = (
    "GEMINI_API_KEY",
    "YOUTUBE_DATA_API_KEY",
    "UPLOAD_POST_API_KEY",
    "UPLOAD_POST_USER",
    "ELEVENLABS_API_KEY",
    "FAL_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)


@pytest.fixture(autouse=True)
def _hermetic_credentials(monkeypatch):
    """Every test starts with no real credentials in the environment.

    Without this the suite passes or fails depending on whether the developer
    happens to have a populated .env — which is exactly what happened: adding a
    real .env made `test_missing_api_key_fails_before_any_request` fail, because
    `YouTubeClient("")` legitimately falls back to `YOUTUBE_DATA_API_KEY` and
    suddenly found one.

    A test that wants a credential sets it explicitly with monkeypatch.setenv,
    which still works — this only clears the ambient values.
    """
    for name in _CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
