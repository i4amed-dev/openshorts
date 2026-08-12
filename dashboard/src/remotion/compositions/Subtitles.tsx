import React from "react";
import {
  AbsoluteFill,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
  spring,
  interpolate,
} from "remotion";
import type { SubtitleConfig } from "../lib/types";
import { groupCaptionsIntoBlocks, getActiveWordIndex } from "../lib/captions";
import { getFontStack } from "../lib/fonts";

interface SubtitlesProps {
  config: SubtitleConfig;
}

// Offsets mirror the burn: FFmpeg places captions MarginV units from the edge
// of a 288-unit-tall frame, and SAFE_MARGIN_V in subtitles.py is 43 — ~15%,
// chosen to clear TikTok's and Reels' own bottom UI. The preview used 10%/12%,
// which sat the caption lower than the file the user actually got.
const SAFE_MARGIN_PCT = `${((43 / 288) * 100).toFixed(1)}%`;

const POSITION_MAP: Record<string, React.CSSProperties> = {
  top: { top: SAFE_MARGIN_PCT, bottom: "auto" },
  middle: { top: "45%", bottom: "auto" },
  bottom: { bottom: SAFE_MARGIN_PCT, top: "auto" },
};

export const Subtitles: React.FC<SubtitlesProps> = ({ config }) => {
  const { fps } = useVideoConfig();
  const blocks = groupCaptionsIntoBlocks(config.captions);

  return (
    <AbsoluteFill>
      {blocks.map((block, i) => {
        const startFrame = Math.round((block.startMs / 1000) * fps);
        const durationFrames = Math.max(
          1,
          Math.round(((block.endMs - block.startMs) / 1000) * fps)
        );

        return (
          <Sequence
            key={i}
            from={startFrame}
            durationInFrames={durationFrames}
            layout="none"
          >
            <SubtitleBlock
              block={block}
              config={config}
              blockStartMs={block.startMs}
            />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};

interface SubtitleBlockProps {
  block: ReturnType<typeof groupCaptionsIntoBlocks>[number];
  config: SubtitleConfig;
  blockStartMs: number;
}

const SubtitleBlock: React.FC<SubtitleBlockProps> = ({
  block,
  config,
  blockStartMs,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { style, position } = config;

  // Current time relative to composition start (sequence-relative frame)
  const currentTimeMs = blockStartMs + (frame / fps) * 1000;
  const activeIndex = getActiveWordIndex(block.words, currentTimeMs);

  const positionStyle = POSITION_MAP[position] ?? POSITION_MAP.bottom;
  const fontStack = getFontStack(style.fontFamily);

  // Background box style
  const hasBg = style.bgOpacity > 0;
  const bgStyle: React.CSSProperties = hasBg
    ? {
        backgroundColor: `${style.bgColor}${Math.round(style.bgOpacity * 255)
          .toString(16)
          .padStart(2, "0")}`,
        borderRadius: 8,
        padding: "8px 16px",
      }
    : {};

  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        right: 0,
        display: "flex",
        justifyContent: "center",
        ...positionStyle,
      }}
    >
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          justifyContent: "center",
          gap: "6px 8px",
          maxWidth: "85%",
          ...bgStyle,
        }}
      >
        {block.words.map((word, i) => (
          <WordSpan
            key={i}
            word={word.text}
            isActive={i === activeIndex}
            style={style}
            fontStack={fontStack}
            animation={style.animation}
            frame={frame}
            fps={fps}
            wordStartMs={word.startMs}
            blockStartMs={blockStartMs}
          />
        ))}
      </div>
    </div>
  );
};

interface WordSpanProps {
  word: string;
  isActive: boolean;
  style: SubtitleConfig["style"];
  fontStack: string;
  animation: SubtitleConfig["style"]["animation"];
  frame: number;
  fps: number;
  wordStartMs: number;
  blockStartMs: number;
}

const WordSpan: React.FC<WordSpanProps> = ({
  word,
  isActive,
  style,
  fontStack,
  animation,
  frame,
  fps,
  wordStartMs,
  blockStartMs,
}) => {
  const wordStartFrame = Math.round(
    ((wordStartMs - blockStartMs) / 1000) * fps
  );

  let transform = "";
  let color = style.fontColor;
  let extraStyle: React.CSSProperties = {};

  // Dim inactive words toward the backend's opaque scaled color (matches the
  // burned ASS look; not CSS opacity).
  if (!isActive && style.baseOpacity != null && style.baseOpacity < 1) {
    const m = /^#?([0-9a-fA-F]{6})$/.exec(style.fontColor || "#FFFFFF");
    if (m) {
      const scale = 0.35 + 0.65 * style.baseOpacity;
      const [r, g, b] = [0, 2, 4].map((i) =>
        Math.round(parseInt(m[1].slice(i, i + 2), 16) * scale)
      );
      color = `rgb(${r}, ${g}, ${b})`;
    }
  }

  // What the burn will do to the spoken word (subtitles.py generate_ass builds
  // the same five cases). `animation` is the older preview-only vocabulary and
  // is mapped onto them so existing callers keep working.
  const effect =
    style.effect ??
    (animation === "pop"
      ? "pop"
      : animation === "word-highlight"
      ? "glow"
      : animation === "karaoke"
      ? "none"
      : "static");

  // "static" is the plain SRT burn: every word identical, no highlight. Any
  // other effect recolors the spoken word.
  if (isActive && effect !== "static") {
    color = style.highlightColor;

    switch (effect) {
      case "pop": {
        // Burn: \fscx90\fscy90 -> \fscx108\fscy108 over 110ms.
        const scale = spring({
          frame: frame - wordStartFrame,
          fps,
          config: { mass: 0.5, stiffness: 300, damping: 12 },
          durationInFrames: 4,
        });
        transform = `scale(${interpolate(scale, [0, 1], [0.9, 1.08])})`;
        break;
      }
      case "glow": {
        // Burn: \c white fill, \3c highlight outline, \bord3 \blur4 — the word
        // goes WHITE inside a colored halo, it does not turn the highlight color.
        const halo = Math.max(3, Math.floor(style.borderWidth) + 2) * (1920 / 288);
        color = "#FFFFFF";
        extraStyle = {
          textShadow: `0 0 ${halo}px ${style.highlightColor}, 0 0 ${halo / 2}px ${style.highlightColor}`,
        };
        break;
      }
      case "box": {
        // Burn: same white fill, but an unblurred slab of highlight around it.
        color = "#FFFFFF";
        extraStyle = {
          backgroundColor: style.highlightColor,
          borderRadius: 4,
          padding: "0 8px",
        };
        break;
      }
      default:
        break;
    }
  }

  // Text stroke via textShadow (CSS paint-order not reliable in Remotion).
  // Glow and box replace the outline on the spoken word — in the burn their
  // \bord tag overrides both its width and its color — so it is not drawn.
  const outlineReplaced = isActive && (effect === "glow" || effect === "box");
  const strokeShadow =
    style.borderWidth > 0 && !outlineReplaced
      ? [
          `${style.borderWidth}px 0 0 ${style.borderColor}`,
          `-${style.borderWidth}px 0 0 ${style.borderColor}`,
          `0 ${style.borderWidth}px 0 ${style.borderColor}`,
          `0 -${style.borderWidth}px 0 ${style.borderColor}`,
        ].join(", ")
      : null;

  const shadows = [strokeShadow, extraStyle.textShadow].filter(Boolean).join(", ");

  return (
    <span
      style={{
        fontFamily: fontStack,
        fontSize: style.fontSize,
        fontWeight: 700,
        color,
        transform,
        display: "inline-block",
        transition: "none",
        textTransform: style.uppercase ? "uppercase" : "none",
        ...extraStyle,
        // After the spread: the composed value must win over extraStyle's own.
        textShadow: shadows || undefined,
      }}
    >
      {word}
    </span>
  );
};
