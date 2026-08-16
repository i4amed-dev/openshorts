"""The dashboard link must never point a phone at localhost — that URL is
only reachable from the machine Klippo itself runs on."""
from __future__ import annotations

from telegram_bot import keyboards


class TestDashboardUrl:
    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_DASHBOARD_URL", raising=False)
        assert keyboards.dashboard_url() is None

    def test_localhost_is_rejected(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_DASHBOARD_URL", "http://localhost:5175")
        assert keyboards.dashboard_url() is None

    def test_loopback_ip_is_rejected(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_DASHBOARD_URL", "http://127.0.0.1:5175")
        assert keyboards.dashboard_url() is None

    def test_real_url_is_accepted(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_DASHBOARD_URL", "https://klippo.example.com")
        assert keyboards.dashboard_url() == "https://klippo.example.com"

    def test_home_grid_hides_the_button_when_unset(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_DASHBOARD_URL", raising=False)
        kb = keyboards.home_grid(autopilot_enabled=True, paused=False)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert not any("Dashboard" in l for l in labels)

    def test_home_grid_shows_the_button_when_configured(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_DASHBOARD_URL", "https://klippo.example.com")
        kb = keyboards.home_grid(autopilot_enabled=True, paused=False)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert any("Dashboard" in l for l in labels)
