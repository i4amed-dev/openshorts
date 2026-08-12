"""What the card shows must be what gets published.

The clip card used to render subtitles, hooks and effects in the browser with
Remotion (@remotion/web-renderer) whenever the server file carried no burned-in
content. That render became a ``blob:`` URL living only in that browser tab: the
card played it and "download" saved it, but /api/social/post uploads the file
named by the clip's ``video_url`` ON THE SERVER — so posting published a clip
with none of the user's edits, and a reload lost them (28-jul-2026).

Auto-captions hid this from 25-jul by making every clip arrive as
``subtitled_<ts>_...``, which forced every edit down the server path; dropping
auto-captions exposed it again. Every edit is now a real file on the server.

Source-level checks: the dashboard has no JS test runner, and CI only builds it.
"""
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
CARD = (REPO / "dashboard" / "src" / "components" / "ResultCard.jsx").read_text(
    encoding="utf-8")


class TestEveryEditLandsOnTheServer:
    def test_the_card_never_renders_in_the_browser(self):
        # renderInBrowser returns a blob: URL that posting cannot see.
        assert "renderInBrowser" not in CARD

    def test_subtitles_call_the_burn_endpoint(self):
        assert "'/api/subtitle'" in CARD

    def test_hooks_call_the_overlay_endpoint(self):
        assert "'/api/hook'" in CARD

    def test_auto_edit_calls_the_edit_endpoint(self):
        assert "'/api/edit'" in CARD

    def test_no_browser_only_layer_state_survives(self):
        # activeLayers accumulated Remotion layers that existed nowhere else.
        assert "activeLayers" not in CARD
