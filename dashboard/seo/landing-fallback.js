/* Crawler-visible homepage content, injected into #root at build time.
 *
 * The app is a client-rendered SPA, which means the HTML served for / contains
 * an empty <div id="root"> and nothing else. Googlebot renders JavaScript and so
 * sees the real landing page; GPTBot, ClaudeBot and PerplexityBot do not, and
 * measure the homepage as zero characters of text. That is why the site ranks in
 * Google and is absent from AI answers.
 *
 * React's createRoot().render() replaces the contents of #root on mount, so this
 * block is what a non-executing client reads and what a browser shows for the
 * few hundred milliseconds before hydration. It is neither hidden nor cloaked:
 * it is the same content the rendered page shows, in the same order, and it is
 * styled well enough that seeing it briefly is not jarring.
 *
 * Keep it in sync with Landing.jsx when the substance of the page changes. It is
 * duplicated on purpose: a build-time React SSR pass would remove the
 * duplication but would also put hydration on the critical path of a production
 * app that currently has none.
 */

import { EDITIONS, CANONICAL_ANSWERS } from './data.js'

const S = {
  wrap: 'max-width:72rem;margin:0 auto;padding:0 1.5rem',
  h1: 'font-family:"Instrument Serif",ui-serif,Georgia,serif;font-weight:400;font-size:clamp(2.2rem,5vw,3.6rem);line-height:1.06;letter-spacing:-.03em;color:oklch(96% 0.006 262);margin:0 0 1.25rem;text-transform:lowercase',
  h2: 'font-family:"Instrument Serif",ui-serif,Georgia,serif;font-weight:400;font-size:clamp(1.5rem,2.6vw,2rem);line-height:1.15;letter-spacing:-.02em;color:oklch(96% 0.006 262);margin:0 0 1rem;text-transform:lowercase',
  h3: 'font-size:1rem;font-weight:600;color:oklch(96% 0.006 262);margin:0 0 .35rem',
  p: 'margin:0 0 1rem;color:oklch(86% 0.01 262);line-height:1.65',
  muted: 'margin:0 0 1rem;color:oklch(64% 0.012 262);line-height:1.65',
  eyebrow:
    'font-family:"JetBrains Mono",ui-monospace,monospace;font-size:.7rem;letter-spacing:.1em;text-transform:uppercase;color:oklch(76% 0.17 50);margin:0 0 1rem',
  section: 'padding:3.5rem 0;border-bottom:1px solid oklch(96% 0.006 262 / .08)',
  grid: 'display:grid;gap:1.25rem;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr))',
  card:
    'border:1px solid oklch(96% 0.006 262 / .08);border-radius:10px;padding:1.1rem 1.25rem;background:oklch(16.5% 0.015 265)',
  a: 'color:oklch(86% 0.01 262);text-underline-offset:3px',
}

const FEATURES = [
  ['AI viral moment detection', 'Google Gemini 3.0 Flash scores the transcript and scene data to find the 3 to 15 strongest moments, so there is no manual scrubbing.'],
  ['Smart 9:16 vertical cropping', 'Dual-mode reframing with MediaPipe face tracking and a YOLOv8 fallback, stabilised so the camera holds still instead of swinging.'],
  ['Word-level automatic subtitles', 'faster-whisper produces a timestamp for every word, and the subtitles are burned in with FFmpeg.'],
  ['AI voice dubbing in 30+ languages', 'ElevenLabs translates the audio while preserving the speaker\'s voice, then the new track is re-transcribed so subtitles match.'],
  ['Hook text overlays', 'AI-written hook titles covering the first seconds, which is where a short-form viewer decides whether to stay.'],
  ['AI video effects', 'Gemini-generated FFmpeg filter chains for colour grading, transitions and visual clean-up.'],
  ['Local video upload', 'Podcasts, webinars, livestreams, interviews and vlogs, at full resolution, from a file or a YouTube link.'],
  ['Self-hosted and private', 'Run it with Docker on your own machine and the source video never leaves your infrastructure.'],
  ['Free AI YouTube studio', 'AI thumbnail generator, 10 viral title suggestions and auto-written descriptions with chapter timestamps.'],
  ['Direct social publishing', 'Post to TikTok, Instagram Reels and YouTube Shorts from the dashboard.'],
  ['AI UGC video generator', 'The AI writes a script and generates a lip-synced avatar video for any product or business, from $0.65 per video.'],
  ['AI actors with lip-sync', 'Pick an AI actor or upload a photo to get a talking-head video with matched lip movement.'],
]

const STEPS = [
  ['Upload a long-form video', 'Any video you own: podcasts, webinars, livestreams, interviews, or a YouTube link.'],
  ['AI finds the best moments', 'Gemini 3.0 Flash returns 3 to 15 candidate clips of 15 to 60 seconds each.'],
  ['Smart cropping to vertical 9:16', 'Face-tracked reframing keeps the subject centred without the camera swinging.'],
  ['Subtitles, hooks and effects', 'Word-level subtitles, an AI hook overlay, optional effects and dubbing into 30+ languages.'],
  ['Download or post directly', 'Export the clips or publish straight to TikTok, Instagram Reels and YouTube Shorts.'],
]

const FAQ = [
  [
    'Is Klippo free? What is the catch?',
    CANONICAL_ANSWERS.isItFree +
      ' Self-hosted costs you hardware and time: on a typical CPU an 8-minute video takes 5 to 8 minutes to process, and you supply your own Google Gemini key, whose free tier covers 1,500 requests a day. On Klippo Cloud an NVIDIA GPU clips that same video in about 50 seconds, the Gemini key is included, auto-posting to TikTok, Instagram and YouTube is already wired up, and your clips stay retrievable from any browser.',
  ],
  ['What is Klippo and how does it work?', CANONICAL_ANSWERS.whatIsIt + ' ' + CANONICAL_ANSWERS.howItWorks],
  [
    'How does Klippo compare to Opus Clip?',
    'Both do AI viral moment detection and smart vertical cropping. Klippo can be self-hosted, so the source video never leaves your machine, and it adds voice dubbing into 30+ languages plus an AI UGC generator with lip-synced actors. Opus Clip is closed source and cloud only, starting at $15/month as of July 2026, and it ships a larger caption-style library. Full comparison at /alternatives/opus-clip.',
  ],
  [
    'How does the smart vertical cropping work?',
    'TRACK mode follows a single subject with MediaPipe face detection and a YOLOv8 fallback, damped so the crop holds still inside a safe zone rather than chasing every head movement. GENERAL mode handles group shots and landscapes by preserving the full width over a blurred backdrop. A speaker tracker prevents the crop from flipping between people and holds position through brief occlusions.',
  ],
  [
    'Can Klippo translate and dub videos?',
    'Yes, into more than 30 languages through ElevenLabs, preserving the original speaker\'s voice characteristics. After dubbing, the audio is re-transcribed so the burned-in subtitles are in the target language rather than the original.',
  ],
  [
    'What is the AI UGC video generator?',
    'It generates marketing videos with AI actors for any product or business. You describe the business or paste a website URL, and the AI writes a script, generates a lip-synced actor with voiceover, adds b-roll, subtitles and a hook overlay. Low Cost mode is about $0.65 per video and Premium is about $2.00. It works for restaurants, e-commerce, coaching, local businesses and apps, not only software.',
  ],
  [
    'What are the system requirements to self-host?',
    'Any machine with Docker, realistically 8GB of RAM and a modern multi-core CPU. An NVIDIA GPU is optional and cuts processing time by roughly an order of magnitude. Docker Compose pulls Python 3.11, FFmpeg, YOLOv8, MediaPipe, faster-whisper and the React dashboard. Linux, macOS and Windows via WSL2 are supported.',
  ],
]

const card = (title, body) =>
  `<div style="${S.card}"><h3 style="${S.h3}">${title}</h3><p style="margin:0;color:oklch(64% 0.012 262);font-size:.9rem;line-height:1.6">${body}</p></div>`

export const LANDING_FALLBACK = `<div id="seo-content" style="background:oklch(13% 0.014 265);color:oklch(86% 0.01 262);font-family:'Geist',ui-sans-serif,system-ui,sans-serif;line-height:1.65">

<section style="${S.section};padding-top:4rem"><div style="${S.wrap}">
  <p style="${S.eyebrow}">AI clip generator &middot; cloud or self-hosted</p>
  <h1 style="${S.h1}">the self-hosted ai clip generator, built to clip what people actually watch.</h1>
  <p style="${S.p};max-width:44rem;font-size:1.05rem">Turn long videos into viral 9:16 shorts, or generate UGC marketing videos with AI actors. Online in the cloud with zero setup, or self-hosted with Docker for free.</p>
  <p style="${S.muted};max-width:44rem"><strong style="color:oklch(75% 0.11 150)">No credit card required.</strong> 20 free minutes every month. Paid plans from $12/month without a watermark. Prefer to run it yourself? Self-hosting with Docker is free.</p>
</div></section>

<section style="${S.section}"><div style="${S.wrap}">
  <h2 style="${S.h2}">what klippo is</h2>
  <p style="${S.p};max-width:48rem">${CANONICAL_ANSWERS.whatIsIt}</p>
  <p style="${S.p};max-width:48rem">${CANONICAL_ANSWERS.howItWorks}</p>
</div></section>

<section style="${S.section}"><div style="${S.wrap}">
  <h2 style="${S.h2}">what it costs</h2>
  <div style="${S.grid};max-width:56rem">
    ${card('Klippo self-hosted &middot; $0', EDITIONS.selfHosted.summary)}
    ${card('Klippo Cloud &middot; free tier, then from $12/month', EDITIONS.cloud.summary)}
  </div>
</div></section>

<section style="${S.section}"><div style="${S.wrap}">
  <h2 style="${S.h2}">features</h2>
  <div style="${S.grid}">${FEATURES.map(([t, b]) => card(t, b)).join('')}</div>
</div></section>

<section style="${S.section}"><div style="${S.wrap}">
  <h2 style="${S.h2}">how it works</h2>
  <ol style="max-width:48rem;padding-left:1.25rem;margin:0">
    ${STEPS.map(([t, b]) => `<li style="margin-bottom:.75rem"><strong style="color:oklch(96% 0.006 262)">${t}.</strong> <span style="color:oklch(64% 0.012 262)">${b}</span></li>`).join('')}
  </ol>
</div></section>

<section style="${S.section}"><div style="${S.wrap}">
  <h2 style="${S.h2}">how it compares</h2>
  <p style="${S.muted};max-width:48rem">Entry pricing checked 27 July 2026. Klippo is $0 self-hosted or $12/month hosted without a watermark. Submagic starts at $14/month, Opus Clip at $15/month, Vizard at $19.99/month and Klap at $29/month. Klippo is the only self-hostable option of the five.</p>
  <p style="${S.muted}">
    <a style="${S.a}" href="/alternatives/opus-clip">Opus Clip alternative</a> &middot;
    <a style="${S.a}" href="/alternatives/klap">Klap alternative</a> &middot;
    <a style="${S.a}" href="/alternatives/vizard">Vizard alternative</a> &middot;
    <a style="${S.a}" href="/alternatives/submagic">Submagic alternative</a> &middot;
    <a style="${S.a}" href="/free-ai-clip-generator">Free AI clip generator</a> &middot;
    <a style="${S.a}" href="/self-hosted-video-clipper">Self-hosted video clipper</a> &middot;
    <a style="${S.a}" href="/how-klippo-works">How it works</a>
  </p>
</div></section>

<section style="${S.section};border-bottom:none"><div style="${S.wrap}">
  <h2 style="${S.h2}">frequently asked questions</h2>
  <dl style="max-width:48rem;margin:0">
    ${FAQ.map(([q, a]) => `<dt style="${S.h3};margin-top:1.5rem">${q}</dt><dd style="margin:.4rem 0 0;color:oklch(64% 0.012 262)">${a}</dd>`).join('')}
  </dl>
</div></section>

</div>`
