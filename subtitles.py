import os
import re
import subprocess
import sys
import time

from ffmpeg_utils import video_encode_args, QUALITY, METADATA_SCRUB


_STDIO_CONFIGURED = False

# Shared faster-whisper config so both transcription paths (this module and
# main.transcribe_video) behave identically. "small" is meaningfully better at
# German than "base" without being much slower on CPU.
DEFAULT_WHISPER_MODEL = "small"


def get_whisper_config():
    """Return the faster-whisper model config, overridable via env vars."""
    return {
        "model_size": os.environ.get("WHISPER_MODEL", DEFAULT_WHISPER_MODEL),
        "device": os.environ.get("WHISPER_DEVICE", "cpu"),
        "compute_type": os.environ.get("WHISPER_COMPUTE", "int8"),
    }


# Decode params shared by both transcription paths. condition_on_previous_text
# is off to avoid repetition/hallucination loops; vad_filter drops silence.
WHISPER_TRANSCRIBE_PARAMS = {
    "beam_size": 5,
    "vad_filter": True,
    "condition_on_previous_text": False,
    "word_timestamps": True,
}


# --- Burn geometry -------------------------------------------------------
# Both burn paths render into a virtual frame 288 units tall: generate_ass
# writes "PlayResY: 288", and FFmpeg's own SRT->ASS conversion defaults to the
# same. libass then scales that frame to the real video, so a caption's height
# on screen is  fontsize * ASS_FONT_SCALE * (video_height / ASS_PLAY_RES_Y)  —
# 5.67x the requested size on a 1920-tall clip.
#
# The subtitle modal's live preview MUST apply the same factor, or it shows the
# user a caption 2.5x smaller than the one that gets burned (reported 28-jul-2026).
# Its copy of these numbers is in dashboard/src/components/SubtitleModal.jsx;
# change them here and there together.
ASS_PLAY_RES_Y = 288
ASS_FONT_SCALE = 0.85

# How words are grouped into caption blocks. The preview groups with the same
# two numbers (groupCaptionsIntoBlocks in dashboard/src/remotion/lib/captions.ts),
# so preview and burn break lines at the same places.
CAPTION_MAX_CHARS = 20
CAPTION_MAX_DURATION = 2.0



def merge_continuation_words(words):
    """Merge faster-whisper continuation fragments into their base word.

    faster-whisper marks a word boundary with a LEADING SPACE on each token.
    Compound-word fragments (e.g. "-Kanal.", ".200") arrive WITHOUT a leading
    space and belong to the preceding word. Without merging, "YouTube" and
    "-Kanal." get space-joined into "YouTube -Kanal." or split across subtitle
    blocks. We concatenate such fragments onto the previous word and extend its
    end time. Normal words keep their leading space, so real word boundaries
    (e.g. "ich habe") are never glued together.

    Returns a new list; the input dicts are not mutated.
    """
    merged = []
    for word in words:
        text = word.get("word", "")
        if merged and isinstance(text, str) and text and not text.startswith(" "):
            prev = merged[-1]
            prev["word"] = f"{prev.get('word', '')}{text}"
            if word.get("end") is not None:
                prev["end"] = word["end"]
        else:
            merged.append(dict(word))
    return merged


def _configure_stdio():
    global _STDIO_CONFIGURED
    if _STDIO_CONFIGURED:
        return
    _STDIO_CONFIGURED = True
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _log(message):
    _configure_stdio()
    stream = sys.stdout
    text = str(message)
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        stream.write(safe_text + "\n")
    stream.flush()


def _escape_ffmpeg_filter_value(value):
    """Escape a path/value for use inside a quoted FFmpeg filter argument."""
    return value.replace('\\', '/').replace(':', '\\:').replace("'", "\\'")


def _normalize_subtitle_word(value):
    return " ".join(str(value or "").split())


def transcribe_audio(video_path):
    """
    Transcribe audio from a video file via the configured ASR backend.
    Returns transcript in the same format as main.py for compatibility.
    """
    # Lazy import: transcribe_backends imports helpers from this module.
    from transcribe_backends import transcribe_media

    _log(f"🎙️  Transcribing audio from: {video_path}")
    transcript = transcribe_media(video_path)
    _log(f"✅ Transcription complete. Language: {transcript['language']}")
    return transcript


def generate_srt_from_video(video_path, output_path,
                            max_chars=CAPTION_MAX_CHARS,
                            max_duration=CAPTION_MAX_DURATION,
                            style="classic", **style_opts):
    """
    Transcribe a video and generate a subtitle file directly (SRT, or karaoke
    ASS when style="karaoke"). Used for dubbed videos without a transcript.
    """
    transcript = transcribe_audio(video_path)

    # Get video duration to use as clip_end
    import cv2
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = frame_count / fps if fps else 0
    cap.release()

    if style == "karaoke":
        return generate_ass(transcript, 0, duration, output_path, max_chars, max_duration, **style_opts)
    return generate_srt(transcript, 0, duration, output_path, max_chars, max_duration)


def _collect_word_blocks(transcript, clip_start, clip_end,
                         max_chars=CAPTION_MAX_CHARS,
                         max_duration=CAPTION_MAX_DURATION):
    """
    Flatten transcript words for a clip range and group them into short blocks
    suitable for vertical video. Returns a list of blocks; each block is a list
    of {'word', 'start', 'end'} dicts with times relative to the clip.

    Continuation fragments are merged defensively here too, because transcripts
    from old jobs on disk store unmerged tokens (the leading space is still
    present, so the boundary signal survives).
    """
    flat_words = []
    for segment in transcript.get('segments', []):
        flat_words.extend(segment.get('words', []))
    flat_words = merge_continuation_words(flat_words)

    words = []
    for word_info in flat_words:
        if word_info.get('end', 0) > clip_start and word_info.get('start', 0) < clip_end:
            cleaned_word = _normalize_subtitle_word(word_info.get('word', ''))
            if not cleaned_word:
                continue
            words.append({
                'word': cleaned_word,
                'start': max(0, word_info['start'] - clip_start),
                'end': max(0, word_info['end'] - clip_start),
            })

    blocks = []
    current_block = []
    block_start = None

    for word in words:
        if not current_block:
            current_block = [word]
            block_start = word['start']
            continue

        current_text_len = sum(len(w['word']) + 1 for w in current_block)
        duration = word['end'] - block_start

        if current_text_len + len(word['word']) > max_chars or duration > max_duration:
            blocks.append(current_block)
            current_block = [word]
            block_start = word['start']
        else:
            current_block.append(word)

    if current_block:
        blocks.append(current_block)
    return blocks


def generate_srt(transcript, clip_start, clip_end, output_path,
                 max_chars=CAPTION_MAX_CHARS, max_duration=CAPTION_MAX_DURATION):
    """
    Generates an SRT file from the transcript for a specific time range.
    Groups words into short lines suitable for vertical video.
    """
    blocks = _collect_word_blocks(transcript, clip_start, clip_end, max_chars, max_duration)
    if not blocks:
        return False

    srt_content = ""
    for index, block in enumerate(blocks, 1):
        text = " ".join(w['word'] for w in block).strip()
        srt_content += format_srt_block(index, block[0]['start'], block[-1]['end'], text)

    # Write UTF-8 with BOM so Windows/FFmpeg subtitle readers reliably detect Unicode text.
    with open(output_path, 'w', encoding='utf-8-sig') as f:
        f.write(srt_content)

    return True


# Vertical margin for burned captions, in PlayResY=288 units (so ~15% of the
# frame height). The old hardcoded 25 (8.7%) put captions underneath TikTok's
# and Reels' own bottom UI — the caption/username block and the music ticker —
# where they were partly covered on the platform even though the exported file
# looked fine.
SAFE_MARGIN_V = 43


# Fallback caption look, and the base every partial style is merged onto (it is
# what supplies max_chars/max_duration, which the modal doesn't expose). Clips
# are NOT captioned with it automatically — the user picks the look in the
# subtitle modal; this is only what a re-burn falls back to when the clip has
# burned captions but no recorded style (clips captioned before the style was
# persisted). Chosen by rendering four candidates on a real clip and comparing
# them (25-jul-2026): white Anton uppercase with a yellow active word, heavy
# black outline, gentle pop. Yellow because it is the one colour that almost
# never occurs in footage, so the active word reads instantly on any
# background; the base text stays fully opaque (dimming it tested worse over
# bright scenes).
AUTO_CAPTION_STYLE = {
    "style": "karaoke",
    "alignment": "bottom",
    "font_name": "Anton",
    "font_size": 44,
    "font_color": "#FFFFFF",
    "highlight_color": "#FFE500",
    "border_color": "#000000",
    "border_width": 4,
    "effect": "pop",
    "base_opacity": 1.0,
    "uppercase": True,
    "max_chars": 16,
    "max_duration": 1.4,
}


def caption_clip(clip_path, transcript, clip_start, clip_end, style=None):
    """Burn one caption layer onto a finished clip, in the given style.

    Clips do NOT get captions at generation time: burning a fixed house style
    onto every clip meant every delivered clip carried captions nobody chose,
    and the only way out was to restyle or remove them afterwards. Captions are
    applied when the user asks for them, in the style they picked in the
    subtitle modal (/api/subtitle).

    This helper is what puts those captions BACK after an edit or a hook — both
    derive from the clean file so a later restyle can't stack a second caption
    layer — so ``style`` is the style the user chose for this clip, persisted in
    metadata.json. It falls back to ``AUTO_CAPTION_STYLE`` for clips captioned
    before that was recorded.

    The captioned file is written ALONGSIDE the clip as
    ``subtitled_<ts>_<clip>.mp4`` — the same convention /api/subtitle uses — so
    the untouched original stays on disk and re-styling replaces the captions
    instead of burning a second layer over them.

    Returns the captioned path, or None when captions were skipped (silent
    video, no words in range, or any failure — a caption problem must never
    cost the user the clip they already paid for).
    """
    if not transcript or not transcript.get('segments'):
        return None  # silent video: nothing to caption
    try:
        style = {**AUTO_CAPTION_STYLE, **(style or {})}
        output_dir = os.path.dirname(clip_path)
        stem = os.path.basename(clip_path)
        generation_id = int(time.time())
        # The output name MUST stay exactly "subtitled_<ts>_<clip filename>":
        # the modal's walk-back and _canonical_clip_file both reconstruct the
        # clean original from it, so trimming the stem here would orphan the
        # pair. Length is bounded upstream instead, by MAX_TITLE_BYTES at
        # download time. A legacy clip whose name predates that budget can still
        # overflow — that raises OSError 36, which the except below turns into
        # "ship the clip uncaptioned" rather than a broken filename.
        is_karaoke = str(style.get("style", "karaoke")).lower() != "classic"
        subs_path = os.path.join(
            output_dir,
            f"autosubs_{generation_id}_{stem}.{'ass' if is_karaoke else 'srt'}")
        out_path = os.path.join(output_dir, f"subtitled_{generation_id}_{stem}")

        if is_karaoke:
            generated = generate_ass(
                transcript, clip_start, clip_end, subs_path,
                max_chars=style["max_chars"], max_duration=style["max_duration"],
                alignment=style["alignment"], fontsize=style["font_size"],
                font_name=style["font_name"], font_color=style["font_color"],
                border_color=style["border_color"], border_width=style["border_width"],
                highlight_color=style["highlight_color"], effect=style["effect"],
                bg_color=style.get("bg_color", "#000000"),
                bg_opacity=style.get("bg_opacity", 0.0),
                base_opacity=style["base_opacity"], uppercase=style["uppercase"])
        else:
            generated = generate_srt(
                transcript, clip_start, clip_end, subs_path,
                max_chars=style["max_chars"], max_duration=style["max_duration"])
        if not generated:
            print("   ℹ️ No words in range — clip stays uncaptioned.")
            return None

        burn_subtitles(
            clip_path, subs_path, out_path,
            alignment=style["alignment"], fontsize=style["font_size"],
            font_name=style["font_name"], font_color=style["font_color"],
            border_color=style["border_color"], border_width=style["border_width"],
            bg_color=style.get("bg_color", "#000000"),
            bg_opacity=style.get("bg_opacity", 0.0))
        print(f"   💬 Captions burned: {os.path.basename(out_path)}")
        return out_path
    except Exception as e:
        print(f"   ⚠️ Captions failed ({type(e).__name__}: {e}) — "
              f"delivering the clip without them.")
        return None


def _ass_time(seconds):
    """Format seconds as ASS timestamp H:MM:SS.cc (centiseconds)."""
    seconds = max(0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        centis = 99
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _hex_to_ass_inline_color(hex_color, fallback="FFFFFF"):
    """Convert #RRGGBB to the &HBBGGRR& form used by inline \\c override tags."""
    hex_digits = str(hex_color or "").lstrip('#')
    if not _HEX_COLOR_RE.match(hex_digits):
        hex_digits = fallback
    r = hex_digits[0:2]
    g = hex_digits[2:4]
    b = hex_digits[4:6]
    return f"&H{b}{g}{r}&".upper()


def _escape_ass_text(text):
    """Neutralize characters that would start ASS override blocks."""
    return str(text).replace('\\', '/').replace('{', '(').replace('}', ')')


def _dim_hex_color(hex_color, opacity, fallback="FFFFFF"):
    """Fully-opaque 'dimmed' variant of a color (scaled toward black).

    Dimming via alpha looks muddy in ASS: libass draws the outline as a
    filled shape UNDER the fill, so a semi-transparent white fill blends
    with its own black outline into dark grey. Scaling the RGB instead
    keeps the text crisp on every player."""
    hex_digits = str(hex_color or "").lstrip('#')
    if not _HEX_COLOR_RE.match(hex_digits):
        hex_digits = fallback
    # Gentle curve: even strong dimming stays a readable light silver, matching
    # the airy look of browser-alpha dimming over bright video.
    factor = 0.5 + 0.5 * _clamp_number(opacity, 0.05, 1.0, 1.0)
    r = min(255, round(int(hex_digits[0:2], 16) * factor))
    g = min(255, round(int(hex_digits[2:4], 16) * factor))
    b = min(255, round(int(hex_digits[4:6], 16) * factor))
    return f"{r:02X}{g:02X}{b:02X}"


def generate_ass(transcript, clip_start, clip_end, output_path,
                 max_chars=CAPTION_MAX_CHARS, max_duration=CAPTION_MAX_DURATION,
                 alignment='bottom',
                 fontsize=16, font_name="Verdana", font_color="#FFFFFF",
                 border_color="#000000", border_width=2,
                 highlight_color="#FFD700", bg_color="#000000", bg_opacity=0.0,
                 effect="none", base_opacity=1.0, uppercase=False,
                 margin_v=SAFE_MARGIN_V):
    """
    Generates a karaoke-style ASS file: each block is shown like the SRT path,
    but the currently spoken word is rendered in highlight_color (modern
    TikTok/CapCut caption look). One dialogue event per word, back to back, so
    the highlight moves with the audio without flicker.

    effect: "none" | "glow" (neon shine around the active word) |
            "pop" (active word scales up) | "box" (thick colored outline).
    base_opacity: opacity of the non-active words — dimmed base text is the
    modern captioneer look (e.g. 0.4).
    """
    blocks = _collect_word_blocks(transcript, clip_start, clip_end, max_chars, max_duration)
    if not blocks:
        return False

    # Match the SRT burn path: PlayResY 288 keeps font sizes consistent.
    final_fontsize = int(_clamp_number(fontsize, 10, 200, 16) * ASS_FONT_SCALE)
    if final_fontsize < 10:
        final_fontsize = 10

    align_map = {'top': 8, 'middle': 5, 'bottom': 2}
    ass_alignment = align_map.get(str(alignment).lower(), 2)

    safe_font = _sanitize_font_name(font_name)
    base_opacity = _clamp_number(base_opacity, 0.05, 1.0, 1.0)
    # Dim inactive words via a fully-opaque scaled color (NOT alpha — see
    # _dim_hex_color); the active word overrides the color inline.
    primary_colour = hex_to_ass_color(_dim_hex_color(font_color, base_opacity), 1.0)
    bg_opacity = _clamp_number(bg_opacity, 0.0, 1.0, 0.0)
    border_width = _clamp_number(border_width, 0, 10, 2)

    if bg_opacity > 0:
        border_style = 3
        outline_colour = hex_to_ass_color(bg_color, bg_opacity, fallback="000000")
        outline_width = 1
    else:
        border_style = 1
        outline_colour = hex_to_ass_color(border_color, 1.0, fallback="000000")
        # No floor at 1: the modal's border slider goes down to "None", and
        # forcing an outline anyway burned a visible black edge onto captions
        # the user had explicitly asked to have none (reported 28-jul-2026).
        outline_width = int(border_width)

    back_colour = hex_to_ass_color("#000000", 0.0)
    highlight_inline = _hex_to_ass_inline_color(highlight_color, fallback="FFD700")

    # Inline override tags for the active word; {\r} after it resets to the
    # (dimmed) style so the rest of the block stays untouched.
    if effect == "glow":
        glow_bord = max(3, int(outline_width) + 2)
        active_prefix = (f"{{\\c&HFFFFFF&\\3c{highlight_inline}"
                         f"\\bord{glow_bord}\\blur4}}")
    elif effect == "box":
        box_bord = max(4, int(outline_width) + 3)
        active_prefix = (f"{{\\c&HFFFFFF&\\3c{highlight_inline}"
                         f"\\bord{box_bord}\\blur0}}")
    elif effect == "pop":
        # Gentle pop. The old 75->112 range started the word so small that any
        # frame caught mid-animation read as a sizing bug rather than a beat.
        active_prefix = (f"{{\\c{highlight_inline}"
                         f"\\fscx90\\fscy90\\t(0,110,\\fscx108\\fscy108)}}")
    else:
        active_prefix = f"{{\\c{highlight_inline}}}"

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResY: {ASS_PLAY_RES_Y}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{safe_font},{final_fontsize},{primary_colour},{primary_colour},"
        f"{outline_colour},{back_colour},1,0,0,0,100,100,0,0,{border_style},"
        f"{outline_width},0,{ass_alignment},10,10,{int(_clamp_number(margin_v, 0, 200, SAFE_MARGIN_V))},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events = []
    for block in blocks:
        for i, word in enumerate(block):
            # Event runs until the next word starts (no flicker in gaps);
            # the last word holds until the block ends.
            ev_start = block[0]['start'] if i == 0 else word['start']
            ev_end = block[i + 1]['start'] if i < len(block) - 1 else block[-1]['end']
            if ev_end <= ev_start:
                continue

            parts = []
            for j, other in enumerate(block):
                text = _escape_ass_text(other['word'])
                if uppercase:
                    text = text.upper()
                if j == i:
                    parts.append(f"{active_prefix}{text}{{\\r}}")
                else:
                    parts.append(text)

            events.append(
                f"Dialogue: 0,{_ass_time(ev_start)},{_ass_time(ev_end)},Default,,0,0,0,,{' '.join(parts)}"
            )

    if not events:
        return False

    with open(output_path, 'w', encoding='utf-8-sig') as f:
        f.write(header + "\n".join(events) + "\n")

    return True

def format_srt_block(index, start, end, text):
    def format_time(seconds):
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds - int(seconds)) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
        
    return f"{index}\n{format_time(start)} --> {format_time(end)}\n{text}\n\n"

_HEX_COLOR_RE = re.compile(r'^[0-9A-Fa-f]{6}$')
_FONT_NAME_RE = re.compile(r'[^A-Za-z0-9 _-]')


def hex_to_ass_color(hex_color, opacity=1.0, fallback="FFFFFF"):
    """Convert #RRGGBB to ASS &HAABBGGRR format. opacity: 0.0=transparent, 1.0=opaque.

    Invalid hex (e.g. "#GGGGGG", None, wrong length) falls back to `fallback`
    instead of raising, so a bad color from the client can't 500 the request.
    """
    hex_digits = str(hex_color or "").lstrip('#')
    if not _HEX_COLOR_RE.match(hex_digits):
        hex_digits = fallback
    opacity = _clamp_number(opacity, 0.0, 1.0, 1.0)
    r = int(hex_digits[0:2], 16)
    g = int(hex_digits[2:4], 16)
    b = int(hex_digits[4:6], 16)
    alpha = round((1.0 - opacity) * 255)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _clamp_number(value, lo, hi, default):
    """Coerce value to float and clamp to [lo, hi]; use default if not numeric."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = float(default)
    return max(lo, min(hi, num))


def _sanitize_font_name(name):
    """Strip anything but [A-Za-z0-9 _-] so the font name can't inject extra
    ASS override fields (commas/braces/backslashes) into force_style."""
    cleaned = _FONT_NAME_RE.sub('', str(name or '')).strip()
    return cleaned or "Verdana"


def burn_subtitles(video_path, srt_path, output_path, alignment=2, fontsize=16,
                   font_name="Verdana", font_color="#FFFFFF",
                   border_color="#000000", border_width=2,
                   bg_color="#000000", bg_opacity=0.0):
    """
    Burns subtitles into the video using FFmpeg.
    Supports two modes:
    - Outline mode (bg_opacity=0): Text with colored outline/border
    - Box mode (bg_opacity>0): Text with semi-transparent background box
    """
    # Position mapping
    ass_alignment = 2
    align_lower = str(alignment).lower()
    if align_lower == 'top':
        ass_alignment = 6
    elif align_lower == 'middle':
        ass_alignment = 10
    elif align_lower == 'bottom':
        ass_alignment = 2

    # Font size scaling for ASS virtual resolution (PlayResY=288 default)
    # For vertical 1080x1920 video, we need larger text for readability
    final_fontsize = int(_clamp_number(fontsize, 10, 200, 16) * ASS_FONT_SCALE)
    if final_fontsize < 10:
        final_fontsize = 10

    safe_font_name = _sanitize_font_name(font_name)
    bg_opacity = _clamp_number(bg_opacity, 0.0, 1.0, 0.0)
    border_width = _clamp_number(border_width, 0, 10, 2)

    # Path handling for FFmpeg filter syntax
    safe_srt_path = _escape_ffmpeg_filter_value(srt_path)

    # Convert colors to ASS format and build style
    primary_colour = hex_to_ass_color(font_color, 1.0)

    if bg_opacity > 0:
        # Box mode: opaque background box
        border_style = 3
        outline_colour = hex_to_ass_color(bg_color, bg_opacity, fallback="000000")
        outline_width = 1
    else:
        # Outline mode: text border/outline. Width 0 means the user chose "None"
        # in the modal and must render without an outline (see generate_ass).
        border_style = 1
        outline_colour = hex_to_ass_color(border_color, 1.0, fallback="000000")
        outline_width = int(border_width)

    back_colour = hex_to_ass_color("#000000", 0.0)

    style_string = (
        f"Alignment={ass_alignment},"
        f"Fontname={safe_font_name},"
        f"Fontsize={final_fontsize},"
        f"PrimaryColour={primary_colour},"
        f"OutlineColour={outline_colour},"
        f"BackColour={back_colour},"
        f"BorderStyle={border_style},"
        f"Outline={outline_width},"
        f"Shadow=0,"
        f"MarginV={SAFE_MARGIN_V},"
        f"Bold=1"
    )

    # Let libass see the fonts bundled with the app (e.g. Anton for Impact)
    # even when the system fontconfig has no cache for them.
    fonts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    safe_fonts_dir = _escape_ffmpeg_filter_value(fonts_dir)

    if str(srt_path).lower().endswith('.ass'):
        # ASS files (karaoke style) carry their own styles; force_style would
        # override the per-word color tags.
        vf = f"ass='{safe_srt_path}':fontsdir='{safe_fonts_dir}'"
    else:
        vf = (f"subtitles='{safe_srt_path}':fontsdir='{safe_fonts_dir}'"
              f":charenc=UTF-8:force_style='{style_string}'")

    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        '-vf', vf,
        '-c:a', 'copy',
        *video_encode_args(QUALITY),
        *METADATA_SCRUB,
        '-movflags', '+faststart',
        output_path
    ]

    _log(f"🎬 Burning subtitles: {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    if result.returncode != 0:
        stderr_text = result.stderr.decode(errors='replace')
        _log(f"❌ FFmpeg Subtitle Error: {stderr_text}")
        raise Exception(f"FFmpeg failed: {stderr_text}")

    return True

