"""Rendering must never fail to produce something Telegram can send: HTML
entities are escaped once at the boundary, and dynamic text (titles, vendor
errors, Persian strings, emoji, newlines, very long strings) can never break
the surrounding template.
"""
from __future__ import annotations

from telegram_bot import render


class TestEscaping:
    def test_html_special_characters_are_escaped(self):
        out = render.esc("<script>alert('x')</script> & \"quoted\"")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        assert "&amp;" in out

    def test_none_renders_as_em_dash(self):
        assert render.esc(None) == "—"

    def test_persian_text_passes_through_untouched(self):
        title = "چگونه هوش مصنوعی ویدیوهای کوتاه می‌سازد"
        out = render.esc(title)
        assert title in out

    def test_markdown_punctuation_needs_no_escaping_under_html(self):
        # The whole point of switching off MarkdownV2: none of these need escaping.
        text = "1. Title (2024) - _draft_ *final* [note] #tag"
        assert render.esc(text) == text

    def test_link_escapes_href_and_text_independently(self):
        out = render.link('A "cool" title & more', "https://example.com/?a=1&b=2")
        # Attribute value: quotes must be escaped, or they'd break out of the href="...".
        assert 'href="https://example.com/?a=1&amp;b=2"' in out
        # Body text: quotes need no escaping in HTML, only & (and < >, tested elsewhere).
        assert 'A "cool" title &amp; more' in out

    def test_bold_wraps_and_escapes(self):
        assert render.bold("<b>") == "<b>&lt;b&gt;</b>"


class TestFallback:
    def test_strip_tags_recovers_plain_readable_text(self):
        html = render.bold("Klippo") + " — " + render.italic("status") + " &amp; more"
        plain = render.strip_tags(html)
        assert plain == "Klippo — status & more"

    def test_strip_tags_on_link_keeps_only_the_label(self):
        html = render.link("Watch on YouTube", "https://youtu.be/abc")
        assert render.strip_tags(html) == "Watch on YouTube"


class TestLength:
    def test_short_text_untouched(self):
        assert render.truncate("hello") == "hello"

    def test_long_text_is_truncated_under_the_limit(self):
        long_text = "line\n" * 2000
        out = render.truncate(long_text)
        assert len(out) <= render.MAX_MESSAGE_LENGTH
        assert out.endswith("(truncated)")


class TestFormatters:
    def test_count_formats_thousands_and_millions(self):
        assert render.count(950) == "950"
        assert render.count(15_400) == "15.4K"
        assert render.count(2_300_000) == "2.3M"
        assert render.count(None) == "—"

    def test_duration_formats_hours_minutes_seconds(self):
        assert render.duration(45) == "0:45"
        assert render.duration(125) == "2:05"
        assert render.duration(3725) == "1:02:05"
        assert render.duration(0) == "—"
        assert render.duration(None) == "—"

    def test_ago_handles_malformed_timestamps_without_raising(self):
        assert render.ago("not-a-timestamp") == "not-a-timestamp"
        assert render.ago(None) == "—"
        assert render.ago("") == "—"
