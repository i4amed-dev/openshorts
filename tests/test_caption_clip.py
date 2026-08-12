"""Captions are user-chosen, not automatic.

Every generated clip used to ship with one hardcoded caption look burned in
(28-jul-2026): users got captions they never asked for, in a style they never
picked, and the subtitle modal only changed them after the fact. Clips are now
delivered clean and the burn happens when the user applies a style — so these
tests pin (a) the generation pipeline never captions, and (b) a burn uses the
style it was handed, not the house default.
"""
import pathlib

import pytest

import subtitles as subs

REPO = pathlib.Path(__file__).resolve().parent.parent


def _transcript():
    return {"segments": [{"words": [
        {"word": " hello", "start": 0.2, "end": 0.6},
        {"word": " world", "start": 0.6, "end": 1.1},
    ]}]}


USER_STYLE = {
    "style": "karaoke",
    "alignment": "top",
    "font_name": "Impact",
    "font_size": 61,
    "font_color": "#00FFFF",
    "highlight_color": "#FF4444",
    "border_color": "#123456",
    "border_width": 3,
    "effect": "glow",
    "base_opacity": 0.55,
    "uppercase": False,
}


class _Spy:
    """Stand-in for the module's two subtitle writers and the FFmpeg burn."""

    def __init__(self, monkeypatch, generated=True, burn_raises=None):
        self.ass_kwargs = self.srt_kwargs = self.burn_kwargs = None
        self.burn_args = None

        def generate_ass(transcript, start, end, out, **kw):
            self.ass_kwargs = kw
            pathlib.Path(out).write_text("[Events]\n")
            return generated

        def generate_srt(transcript, start, end, out, **kw):
            self.srt_kwargs = kw
            pathlib.Path(out).write_text("1\n")
            return generated

        def burn_subtitles(video, subs_path, out, **kw):
            self.burn_args = (video, subs_path, out)
            self.burn_kwargs = kw
            if burn_raises:
                raise burn_raises
            pathlib.Path(out).write_bytes(b"mp4")

        monkeypatch.setattr(subs, "generate_ass", generate_ass)
        monkeypatch.setattr(subs, "generate_srt", generate_srt)
        monkeypatch.setattr(subs, "burn_subtitles", burn_subtitles)


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "My_Video_clip_1.mp4"
    path.write_bytes(b"mp4")
    return path


class TestGenerationShipsClean:
    """Source-level, because the clip worker needs ffmpeg + cv2 to run."""

    @staticmethod
    def _code(text):
        """The text with comment lines dropped, so prose can't satisfy a check."""
        return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))

    def test_pipeline_never_burns_captions(self):
        # The per-clip worker cuts, renders, watermarks — and stops. Captions
        # are /api/subtitle's job, in the style the user picked there.
        src = (REPO / "main.py").read_text(encoding="utf-8")
        worker = src.split("def _process_one_clip", 1)[1].split("clip_workers", 1)[0]
        assert "caption" not in self._code(worker)

    def test_captions_have_no_style_less_entry_point(self):
        # Nothing may caption a clip without being told the look: the old
        # auto_caption_clip() hardcoded it, caption_clip() takes `style`.
        import inspect
        assert not hasattr(subs, "auto_caption_clip")
        assert "auto_caption_clip" not in (REPO / "main.py").read_text(encoding="utf-8")
        assert "style" in inspect.signature(subs.caption_clip).parameters


class TestPreviewMatchesTheBurn:
    """The modal preview must show the caption that actually gets burned.

    It showed one 2.5x smaller (reported 28-jul-2026): the preview scaled the
    style's font size by a flat 2.2, while the burn renders it into a 288-unit
    frame that libass blows up to the real 1920. The preview now mirrors the
    burn geometry, so these pin the numbers it copies.
    """

    MODAL = (REPO / "dashboard" / "src" / "components" / "SubtitleModal.jsx").read_text(
        encoding="utf-8")

    def test_the_burn_frame_is_288_units_tall(self, monkeypatch, tmp_path):
        out = tmp_path / "subs.ass"
        subs.generate_ass(_transcript(), 0, 5, str(out))
        assert f"PlayResY: {subs.ASS_PLAY_RES_Y}" in out.read_text(encoding="utf-8-sig")

    def test_the_font_is_shrunk_by_the_published_factor(self, tmp_path):
        out = tmp_path / "subs.ass"
        subs.generate_ass(_transcript(), 0, 5, str(out), fontsize=40)
        style = [l for l in out.read_text(encoding="utf-8-sig").splitlines()
                 if l.startswith("Style: Default")][0]
        assert style.split(",")[2] == str(int(40 * subs.ASS_FONT_SCALE))

    def test_the_preview_copies_both_numbers(self):
        assert f"1920 / {subs.ASS_PLAY_RES_Y}" in self.MODAL
        assert f"ASS_FONT_SCALE = {subs.ASS_FONT_SCALE}" in self.MODAL
        # The flat scale factor that caused the mismatch must not come back.
        assert "fontSize * 2.2" not in self.MODAL

    def test_the_preview_places_captions_at_the_burn_margin(self):
        composition = (REPO / "dashboard" / "src" / "remotion" / "compositions"
                       / "Subtitles.tsx").read_text(encoding="utf-8")
        assert f"{subs.SAFE_MARGIN_V} / {subs.ASS_PLAY_RES_Y}" in composition

    def test_preview_and_burn_group_words_the_same_way(self):
        captions = (REPO / "dashboard" / "src" / "remotion" / "lib" / "captions.ts").read_text(
            encoding="utf-8")
        assert f"maxChars = {subs.CAPTION_MAX_CHARS}" in captions
        assert f"maxDurationMs = {int(subs.CAPTION_MAX_DURATION * 1000)}" in captions


class TestBorderNoneMeansNone:
    """"None" on the border slider has to burn no outline.

    Both burn paths floored the outline at 1 unit — ~7px once libass scales the
    288-unit frame to 1920 — so captions the user asked to have no border came
    back with a visible black edge (reported 28-jul-2026).
    """

    def _style_line(self, tmp_path, **kw):
        out = tmp_path / "subs.ass"
        subs.generate_ass(_transcript(), 0, 5, str(out), **kw)
        return [l for l in out.read_text(encoding="utf-8-sig").splitlines()
                if l.startswith("Style: Default")][0]

    def test_ass_burn_writes_a_zero_outline(self, tmp_path):
        # Format: ..., BorderStyle, Outline, Shadow, Alignment, ...
        assert self._style_line(tmp_path, border_width=0).split(",")[16] == "0"

    def test_ass_burn_keeps_a_requested_outline(self, tmp_path):
        assert self._style_line(tmp_path, border_width=3).split(",")[16] == "3"

    def _burn_cmd(self, monkeypatch, tmp_path, **kw):
        captured = {}

        def fake_run(cmd, **_):
            captured["cmd"] = cmd
            return type("R", (), {"returncode": 0, "stderr": b""})()

        monkeypatch.setattr(subs.subprocess, "run", fake_run)
        srt = tmp_path / "subs.srt"
        srt.write_text("1\n")
        subs.burn_subtitles(str(tmp_path / "in.mp4"), str(srt),
                            str(tmp_path / "out.mp4"), **kw)
        return " ".join(captured["cmd"])

    def test_srt_burn_writes_a_zero_outline(self, monkeypatch, tmp_path):
        assert "Outline=0," in self._burn_cmd(monkeypatch, tmp_path, border_width=0)

    def test_srt_burn_keeps_a_requested_outline(self, monkeypatch, tmp_path):
        assert "Outline=3," in self._burn_cmd(monkeypatch, tmp_path, border_width=3)

    def test_the_preview_draws_no_stroke_either(self):
        modal = (REPO / "dashboard" / "src" / "components" / "SubtitleModal.jsx").read_text(
            encoding="utf-8")
        # previewBorderPx must pass 0 through; Subtitles.tsx renders no stroke at 0.
        assert "Math.max(1, Math.floor(width))" not in modal


class TestPreviewRendersTheBurnsEffects:
    """The preview's active-word cases must be the burn's, one for one."""

    COMPOSITION = (REPO / "dashboard" / "src" / "remotion" / "compositions"
                   / "Subtitles.tsx").read_text(encoding="utf-8")

    def test_the_preview_knows_every_effect_the_burn_can_render(self):
        for effect in ("static", "pop", "glow", "box"):
            assert f'"{effect}"' in self.COMPOSITION, effect

    def test_glow_turns_the_word_white_in_both(self, tmp_path):
        # Burn: \c&HFFFFFF& fill + \3c halo. The preview used to leave the word
        # in the highlight color, which is a different look entirely.
        out = tmp_path / "subs.ass"
        subs.generate_ass(_transcript(), 0, 5, str(out), effect="glow",
                          highlight_color="#FF0000")
        assert "\\c&HFFFFFF&" in out.read_text(encoding="utf-8-sig")
        assert 'color = "#FFFFFF"' in self.COMPOSITION

    def test_classic_burns_no_per_word_highlight(self, tmp_path):
        # The plain SRT path has no highlight at all; the preview's "static"
        # case must skip the recolor to match.
        out = tmp_path / "subs.srt"
        assert subs.generate_srt(_transcript(), 0, 5, str(out)) is True
        assert "-->" in out.read_text(encoding="utf-8-sig")
        assert 'effect !== "static"' in self.COMPOSITION


class TestCaptionClipUsesTheGivenStyle:
    def test_every_user_field_reaches_the_renderer(self, monkeypatch, clip):
        spy = _Spy(monkeypatch)
        assert subs.caption_clip(str(clip), _transcript(), 0, 5, style=USER_STYLE)

        assert spy.ass_kwargs["font_name"] == "Impact"
        assert spy.ass_kwargs["fontsize"] == 61
        assert spy.ass_kwargs["font_color"] == "#00FFFF"
        assert spy.ass_kwargs["highlight_color"] == "#FF4444"
        assert spy.ass_kwargs["border_color"] == "#123456"
        assert spy.ass_kwargs["border_width"] == 3
        assert spy.ass_kwargs["effect"] == "glow"
        assert spy.ass_kwargs["base_opacity"] == 0.55
        assert spy.ass_kwargs["uppercase"] is False
        assert spy.ass_kwargs["alignment"] == "top"
        # The burn pass must agree with the generated file, or the two describe
        # different looks (libass takes position/size from the burn side).
        assert spy.burn_kwargs["alignment"] == "top"
        assert spy.burn_kwargs["font_name"] == "Impact"
        assert spy.burn_kwargs["fontsize"] == 61

    def test_unspecified_fields_fall_back_to_the_default_style(self, monkeypatch, clip):
        # The modal doesn't expose line length / on-screen duration.
        spy = _Spy(monkeypatch)
        subs.caption_clip(str(clip), _transcript(), 0, 5, style={"font_name": "Anton"})
        assert spy.ass_kwargs["max_chars"] == subs.AUTO_CAPTION_STYLE["max_chars"]
        assert spy.ass_kwargs["max_duration"] == subs.AUTO_CAPTION_STYLE["max_duration"]

    def test_classic_style_writes_an_srt_not_a_karaoke_ass(self, monkeypatch, clip):
        spy = _Spy(monkeypatch)
        out = subs.caption_clip(str(clip), _transcript(), 0, 5,
                                style={**USER_STYLE, "style": "classic"})
        assert out and spy.ass_kwargs is None and spy.srt_kwargs is not None
        assert spy.burn_args[1].endswith(".srt")

    def test_no_style_uses_the_house_default(self, monkeypatch, clip):
        # Legacy clips captioned before the style was recorded still restyle.
        spy = _Spy(monkeypatch)
        subs.caption_clip(str(clip), _transcript(), 0, 5)
        assert spy.ass_kwargs["font_name"] == subs.AUTO_CAPTION_STYLE["font_name"]

    def test_background_box_survives_the_round_trip(self, monkeypatch, clip):
        spy = _Spy(monkeypatch)
        subs.caption_clip(str(clip), _transcript(), 0, 5,
                          style={**USER_STYLE, "bg_color": "#101010", "bg_opacity": 0.5})
        assert spy.burn_kwargs["bg_color"] == "#101010"
        assert spy.burn_kwargs["bg_opacity"] == 0.5


class TestCaptionClipOutput:
    def test_name_keeps_the_subtitled_prefix(self, monkeypatch, clip):
        # _strip_burned_captions and _canonical_clip_file both reconstruct the
        # clean original from this name — change it and the pair is orphaned.
        _Spy(monkeypatch)
        out = subs.caption_clip(str(clip), _transcript(), 0, 5, style=USER_STYLE)
        name = pathlib.Path(out).name
        assert name.startswith("subtitled_")
        assert name.endswith(clip.name)

    def test_original_stays_on_disk(self, monkeypatch, clip):
        _Spy(monkeypatch)
        subs.caption_clip(str(clip), _transcript(), 0, 5, style=USER_STYLE)
        assert clip.exists()


class TestCaptionClipNeverCostsTheClip:
    def test_silent_video_is_skipped(self, monkeypatch, clip):
        spy = _Spy(monkeypatch)
        assert subs.caption_clip(str(clip), {"segments": []}, 0, 5) is None
        assert spy.burn_kwargs is None

    def test_no_words_in_range_is_skipped(self, monkeypatch, clip):
        spy = _Spy(monkeypatch, generated=False)
        assert subs.caption_clip(str(clip), _transcript(), 0, 5) is None
        assert spy.burn_kwargs is None

    def test_a_failing_burn_returns_none_instead_of_raising(self, monkeypatch, clip):
        _Spy(monkeypatch, burn_raises=OSError(36, "File name too long"))
        assert subs.caption_clip(str(clip), _transcript(), 0, 5) is None
