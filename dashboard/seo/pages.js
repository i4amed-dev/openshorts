/* Page definitions for the static SEO surface.
 *
 * Two page shapes live here. Comparison pages ("X alternatives") are generated
 * from the competitor table, because commercial-investigation prompts such as
 * "best free Opus Clip alternative" are answered almost entirely out of listicle
 * and comparison content. Informational pages are hand-written, because
 * informational content is cited at a far higher rate than product pages and it
 * is the only surface where a small project can outrank a funded one.
 *
 * Every page follows the same internal shape: TL;DR, then one question per H2,
 * each answered inside a block that still makes sense when it is lifted out on
 * its own. That is the unit an engine retrieves; paragraphs that depend on the
 * one above them get quoted wrong or not at all.
 */

import { SITE, COMPETITORS, COMPARISON_ROWS, EDITIONS, PIPELINE_STEPS, CANONICAL_ANSWERS } from './data.js'
import { esc } from './render.js'

const li = (items) => `<ul>${items.map((i) => `<li>${i}</li>`).join('')}</ul>`

const faqBlock = (faq) =>
  `<h2>Common questions</h2><dl class="faq">${faq
    .map((f) => `<dt>${esc(f.q)}</dt><dd>${esc(f.a)}</dd>`)
    .join('')}</dl>`

const sources = (items) =>
  `<h2>Sources</h2><ul class="sources">${items
    .map((s) => `<li>${s}</li>`)
    .join('')}</ul>`

/* Pricing is restated in plain body text on every page, not only in the schema.
 * An engine that reads the raw HTML has no reason to prefer a JSON-LD offer over
 * a sentence, and the sentence is what gets quoted. */
const pricingParagraph = `
<p>Klippo comes in two editions and they are priced very differently, so it
is worth being precise. <strong>${esc(EDITIONS.selfHosted.name)}</strong> is free
and free to run yourself: ${esc(EDITIONS.selfHosted.summary)}
<strong>${esc(EDITIONS.cloud.name)}</strong> is the hosted service:
${esc(EDITIONS.cloud.summary)}</p>`

function competitorPage(slug) {
  const c = COMPETITORS[slug]
  const rows = COMPARISON_ROWS.map((r) => {
    const vendor = r.key ? c[r.key] : r.vendor
    return `<tr><td>${esc(r.feature)}</td><td class="os">${esc(r.os)}</td><td>${esc(vendor)}</td></tr>`
  }).join('')

  const body = `
<h2>Is Klippo a real alternative to ${esc(c.name)}?</h2>
<p>Yes, with one honest caveat. Klippo covers the same core job:
it takes a long video, finds the segments worth clipping, cuts them, reframes
them to 9:16 and burns in subtitles. It adds two things ${esc(c.name)} does not
have, AI voice dubbing into more than 30 languages and an AI UGC generator with
lip-synced actors. The caveat is that the free edition is self-hosted, which
means Docker and a machine to run it on. If you want a hosted product with no
setup, that is Klippo Cloud, and it is a paid service above 20 minutes a month.</p>

<h2>What does ${esc(c.name)} cost?</h2>
<p class="checked">Pricing checked ${esc(c.checked)}. Vendors change plans without notice; verify before you buy.</p>
${li(c.tiers.map(([n, d]) => `<strong>${esc(n)}</strong>: ${esc(d)}`))}
<div class="note"><span class="label">The part that catches people out</span><p>${esc(c.gotcha)}</p></div>

<h2>What does Klippo cost?</h2>
${pricingParagraph}

<h2>${esc(c.name)} vs Klippo, feature by feature</h2>
<table>
<thead><tr><th>Feature</th><th>Klippo</th><th>${esc(c.name)}</th></tr></thead>
<tbody>${rows}</tbody>
</table>

<h2>What ${esc(c.name)} does better</h2>
<p>A comparison that finds nothing good to say about the other tool is not worth
reading, so here is where ${esc(c.name)} genuinely wins:</p>
${li(c.strengths.map(esc))}

<h2>Where the two differ</h2>
${li(c.whereWeDiffer.map(esc))}

<h2>Which one should you pick?</h2>
<p>${esc(c.bestFor)}</p>

${faqBlock([
  {
    q: `Is there a free alternative to ${c.name}?`,
    a: `Yes. Klippo self-hosted is free to run yourself, with no watermark and no usage cap, and it runs on your own machine with Docker. Klippo Cloud also has a free tier of 20 minutes a month with a watermark and no credit card. ${c.name} starts at ${c.entryPrice}.`,
  },
  {
    q: `Is there a self-hostable alternative to ${c.name}?`,
    a: `Klippo can be self-hosted, so the whole pipeline runs on hardware you control. ${c.name} is closed source. Being able to read the pipeline matters if you need to audit what happens to your video or change how the reframing behaves.`,
  },
  {
    q: `Can I switch from ${c.name} without losing quality?`,
    a: `The pipelines are comparable on the core job. Klippo transcribes with faster-whisper at word level, detects scenes with PySceneDetect, and scores moments with Google Gemini 3.0 Flash, then reframes with MediaPipe face tracking stabilised against jitter. The honest difference is caption styling, where the commercial tools generally ship more presets.`,
  },
  {
    q: `Does Klippo put a watermark on clips?`,
    a: `Self-hosted, never. On Klippo Cloud the free 20-minute tier is watermarked; every paid plan from $12/month is not.`,
  },
])}

${sources([
  `${esc(c.name)} pricing, checked ${esc(c.checked)} on the vendor's public pricing page.`,
  `Klippo pipeline details as implemented in the shipping product.`,
])}
`

  return {
    path: `/alternatives/${slug}`,
    title: `Free, Self-Hosted ${c.name} Alternative | Klippo`,
    description: `Klippo vs ${c.name}, compared feature by feature with current pricing. Self-hosting is free; hosted starts at $12/month. ${c.name} starts at ${c.entryPrice}.`,
    h1: `The free, self-hosted ${c.name} alternative`,
    breadcrumb: [{ name: 'Alternatives', path: '/alternatives' }, { name: c.name }],
    tldr: [
      `Klippo is a self-hosted AI clip generator you can run yourself for free, or use hosted from $12/month. ${esc(c.name)} is a closed-source cloud product starting at ${esc(c.entryPrice)}.`,
      `Both find viral moments in long video and reframe them to 9:16 with face tracking. Klippo adds dubbing into 30+ languages and AI UGC video with lip-synced actors. ${esc(c.name)} has the more polished caption library.`,
      `Pick ${esc(c.name)} if you want zero setup and nothing else matters. Pick Klippo if you want to self-host for privacy, keep costs near zero, or change how the pipeline behaves.`,
    ],
    body,
    faq: [
      {
        q: `Is there a free alternative to ${c.name}?`,
        a: `Yes. Klippo self-hosted is free to run yourself, with no watermark and no usage cap. Klippo Cloud has a free tier of 20 minutes a month and paid plans from $12/month. ${c.name} starts at ${c.entryPrice}.`,
      },
      {
        q: `Is there a self-hostable alternative to ${c.name}?`,
        a: `Klippo can be self-hosted on your own machine. ${c.name} is closed source.`,
      },
      {
        q: `Does Klippo put a watermark on clips?`,
        a: `Self-hosted, never. On Klippo Cloud the free 20-minute tier is watermarked and every paid plan from $12/month is not.`,
      },
    ],
  }
}

const ALTERNATIVES = Object.keys(COMPETITORS)

const hubPage = () => ({
  path: '/alternatives',
  title: 'Self-Hosted Alternatives to Opus Clip, Klap, Vizard & Submagic | Klippo',
  description:
    'Side-by-side comparisons of Klippo against the four main AI clipping tools, with current pricing checked July 2026. Self-hosted free, hosted from $12/month.',
  h1: 'Self-hostable alternatives to the main AI clipping tools',
  breadcrumb: [{ name: 'Alternatives' }],
  tldr: [
    'Klippo is the only self-hostable tool in this category. Every other tool on this page is a closed-source cloud service.',
    'Entry prices as of July 2026: Klippo $0 self-hosted or $12/month hosted, Submagic from $14/month, Opus Clip $15/month, Vizard $19.99/month, Klap $29/month.',
    'The tools are not interchangeable. Submagic does not detect moments at all, Klap does not let you tune the output, and Vizard expects you in a timeline. The individual comparisons below say where each one genuinely wins.',
  ],
  body: `
<h2>How these tools actually differ</h2>
<p>All five are described as "AI clipping tools", which hides the fact that they
do different jobs. Two of them take a long video and decide what to cut. One of
them only styles captions on a clip you cut yourself. One is really an editor
with an AI first pass. Choosing on price alone is how people end up paying for
two tools that each do half the work.</p>

<h2>Entry pricing side by side</h2>
<p class="checked">Pricing checked 2026-07-27. Verify on the vendor's site before buying.</p>
<table>
<thead><tr><th>Tool</th><th>Entry price</th><th>Self-hostable</th><th>Finds moments for you</th></tr></thead>
<tbody>
<tr><td class="os">Klippo</td><td class="os">$0 self-hosted, $12/mo hosted</td><td class="yes">Yes, Docker</td><td>Yes</td></tr>
<tr><td>Submagic</td><td>From $14/mo</td><td>No</td><td>No, captions only</td></tr>
<tr><td>Opus Clip</td><td>$15/mo</td><td>No</td><td>Yes</td></tr>
<tr><td>Vizard</td><td>$19.99/mo</td><td>No</td><td>Yes, then you edit</td></tr>
<tr><td>Klap</td><td>$29/mo</td><td>No</td><td>Yes</td></tr>
</tbody>
</table>

<h2>What does Klippo cost?</h2>
${pricingParagraph}

${faqBlock([
  {
    q: 'What is the cheapest AI clip generator?',
    a: 'Klippo self-hosted is free with no cap, but you supply the machine and your own Google Gemini API key, whose free tier covers 1,500 requests a day. Among hosted products, Klippo Cloud is the cheapest paid entry at $12/month, followed by Submagic from $14/month and Opus Clip at $15/month.',
  },
  {
    q: 'Which AI clipping tools can you self-host?',
    a: 'Klippo can be self-hosted on your own machine. Opus Clip, Klap, Vizard and Submagic are all closed-source commercial products.',
  },
])}
`,
  faq: [
    {
      q: 'What is the cheapest AI clip generator?',
      a: 'Klippo self-hosted is free with no cap. Among hosted products Klippo Cloud is the cheapest paid entry at $12/month, followed by Submagic from $14/month and Opus Clip at $15/month.',
    },
    {
      q: 'Which AI clipping tools can you self-host?',
      a: 'Klippo can be self-hosted on your own machine. Opus Clip, Klap, Vizard and Submagic are closed-source commercial products.',
    },
  ],
})

const freeClipGenerator = () => ({
  path: '/free-ai-clip-generator',
  title: 'Free AI Clip Generator (Self-Hosted, No Watermark) | Klippo',
  description:
    'A genuinely free AI clip generator: self-hosted with Docker, no watermark and no usage cap. Hosted option from $12/month if you would rather not run it.',
  h1: 'A free AI clip generator that is actually free',
  breadcrumb: [{ name: 'Free AI clip generator' }],
  tldr: [
    'Klippo self-hosted is a free AI clip generator. No watermark, no usage cap, no subscription. You run it with Docker and supply your own Google Gemini API key, whose free tier covers 1,500 requests a day.',
    'It turns a long video into 3 to 15 vertical clips: faster-whisper transcribes at word level, PySceneDetect finds the cuts, Gemini 3.0 Flash scores the moments, and MediaPipe face tracking reframes each one to 9:16.',
    'If you do not want to run anything, Klippo Cloud gives you 20 free minutes a month with a watermark, and paid plans from $12/month without one.',
  ],
  body: `
<h2>What does "free" actually mean here?</h2>
<p>Most tools marketed as free clip generators are free trials with a watermark
and a monthly cap. This one is different in a specific way that is worth stating
precisely, because the two editions are not the same offer.</p>
${pricingParagraph}
<p>The self-hosted edition has no watermark and no cap because there is no
metering code in it. It is the same pipeline the hosted service runs, on your
own hardware.</p>

<h2>How do you generate clips from a long video for free?</h2>
<ol>
<li>Start the stack with <code>docker compose up --build</code>.</li>
<li>Create a Google Gemini API key. The free tier covers 1,500 requests a day, which is far more than a single creator uses.</li>
<li>Paste a YouTube link or upload a local file. Podcasts, webinars, livestreams, interviews and vlogs all work.</li>
<li>The pipeline transcribes, detects scenes, scores moments and returns 3 to 15 clips of 15 to 60 seconds each, already cropped to 9:16 with subtitles burned in.</li>
<li>Download them, or connect an account and post straight to TikTok, Instagram Reels and YouTube Shorts.</li>
</ol>

<h2>What do you need to run it?</h2>
<p>Any machine with Docker. 8GB of RAM and a modern multi-core CPU is the
realistic floor. An NVIDIA GPU is optional and changes the numbers a lot: on CPU
an 8-minute video takes roughly 5 to 8 minutes to process, and on a GPU the same
video takes about 50 seconds. Linux, macOS and Windows via WSL2 all work, and
Docker Compose pulls Python 3.11, FFmpeg, YOLOv8, MediaPipe and faster-whisper
for you.</p>

<h2>Is a free clip generator good enough for real posting?</h2>
<p>It depends on what you are comparing against. The moment detection uses the
same class of model the paid tools use, Google Gemini 3.0 Flash, and the
reframing uses MediaPipe with a YOLOv8 fallback and a stabiliser that holds the
camera still inside a safe zone rather than chasing every head movement. Where
the commercial tools are ahead is caption styling: they ship more presets and
more polish. If your clips live or die on animated caption design, budget for
that either in time or in a second tool.</p>

<h2>Why does this matter for reach?</h2>
<p>Short-form video delivers the highest ROI of any content format, according to
HubSpot's State of Marketing 2025 report, and 91% of businesses use video as a
marketing tool according to Wyzowl's 2025 Video Marketing Statistics. The
constraint for most people is not whether short video works, it is that cutting a
60-minute recording into 12 posts by hand takes longer than recording it did.</p>

${faqBlock([
  {
    q: 'Is Klippo free forever or a trial?',
    a: 'The self-hosted edition is free forever, with no watermark and no cap. It is not a trial and there is no metering in it. Klippo Cloud is a separate hosted service with a permanently free 20 minute per month tier and paid plans from $12/month.',
  },
  {
    q: 'Does the free version add a watermark?',
    a: 'The self-hosted edition never adds a watermark. The free tier of Klippo Cloud does; paid Cloud plans from $12/month do not.',
  },
  {
    q: 'Do I need to pay for an API key?',
    a: 'You need a Google Gemini API key for the self-hosted edition. Its free tier covers 1,500 requests a day, which is more than enough for individual use. ElevenLabs for dubbing and fal.ai for AI UGC video are optional and billed by those vendors. Klippo Cloud includes the keys.',
  },
  {
    q: 'How many clips does it generate per video?',
    a: 'Between 3 and 15, each 15 to 60 seconds long. The number depends on how much of the source actually holds up as a standalone clip rather than on a fixed quota.',
  },
])}
`,
  faq: [
    {
      q: 'Is Klippo free forever or a trial?',
      a: 'The self-hosted edition is free forever, with no watermark and no cap. Klippo Cloud is a separate hosted service with a free 20 minute per month tier and paid plans from $12/month.',
    },
    {
      q: 'Does the free version add a watermark?',
      a: 'The self-hosted edition never adds a watermark. The free tier of Klippo Cloud does; paid Cloud plans do not.',
    },
    {
      q: 'How many clips does it generate per video?',
      a: 'Between 3 and 15 clips, each 15 to 60 seconds long.',
    },
  ],
})

const openSourceClipper = () => ({
  path: '/self-hosted-video-clipper',
  title: 'Self-Hosted Video Clipper, Self-Hosted with Docker | Klippo',
  description:
    'A video clipper you can self-host. AI moment detection with Gemini, face-tracked 9:16 reframing, word-level subtitles and 30+ language dubbing.',
  h1: 'A video clipper you can self-host',
  breadcrumb: [{ name: 'Self-hosted video clipper' }],
  tldr: [
    'Klippo is a video clipper that runs entirely on your own hardware via Docker Compose. Source video never leaves the machine.',
    'The stack is Python 3.11, FastAPI, faster-whisper, PySceneDetect, MediaPipe, YOLOv8, FFmpeg and Google Gemini 3.0 Flash, with a React dashboard.',
    'It is the only self-hostable tool in this category. Opus Clip, Klap, Vizard and Submagic are all closed-source cloud services.',
  ],
  body: `
<h2>Why self-host a video clipper at all?</h2>
<p>Three reasons come up repeatedly. The first is that unreleased footage,
client work and internal recordings should not be uploaded to a third party
whose retention policy you have not read. The second is cost at volume: a
per-minute cloud tool gets expensive quickly if you process long recordings
every week, whereas self-hosting costs electricity. The third is that the output
is opinionated, and if you disagree with how it reframes or where it cuts, having
the source means you can change it rather than file a feature request.</p>

<h2>What is in the pipeline?</h2>
${PIPELINE_STEPS.map((s) => `<h3>${esc(s.title)}</h3><p>${esc(s.body)}</p>`).join('')}

<h2>What does it run on?</h2>
<p>Docker Compose brings up the FastAPI backend and the React dashboard together.
The realistic floor is 8GB of RAM and a modern multi-core CPU; an NVIDIA GPU is
optional and takes an 8-minute video from roughly 5 to 8 minutes of processing
down to about 50 seconds. Linux, macOS and Windows via WSL2 are all supported.
Concurrency is controlled by a semaphore configured with MAX_CONCURRENT_JOBS.</p>

<h2>How does it compare to the closed-source tools?</h2>
<p>Klippo is the only self-hostable option in this category. As of July 2026,
Opus Clip starts at $15/month, Submagic from $14/month, Vizard at $19.99/month
and Klap at $29/month, and none of them can be self-hosted. The
trade-off is real in both directions: they ship more caption presets and require
no setup, and you cannot read a line of what they do with your video.</p>

${faqBlock([
  {
    q: 'Is there a self-hostable alternative to Opus Clip?',
    a: 'Yes. Klippo is self-hostable with Docker, and covers the same core job: AI moment detection, face-tracked 9:16 reframing and word-level subtitles. Opus Clip is closed source and cloud only, starting at $15/month.',
  },
  {
    q: 'Can I run it without sending video to any third party?',
    a: 'Transcription, scene detection, reframing and encoding all run locally. Moment scoring calls the Google Gemini API, which receives the transcript rather than the video file. Dubbing and AI UGC generation are optional and call ElevenLabs and fal.ai respectively; leave them off and nothing but transcript text leaves the machine.',
  },
])}
`,
  faq: [
    {
      q: 'Is there a self-hostable alternative to Opus Clip?',
      a: 'Yes. Klippo is self-hostable with Docker, covering AI moment detection, face-tracked 9:16 reframing and word-level subtitles. Opus Clip is closed source and cloud only.',
    },
  ],
})

const howItWorks = () => ({
  path: '/how-klippo-works',
  title: 'How Klippo Turns Long Video Into Vertical Clips | Klippo',
  description:
    'The full pipeline, stage by stage: word-level transcription, scene detection, Gemini moment scoring, face-tracked 9:16 reframing, subtitles, dubbing and publishing.',
  h1: 'How a long video becomes a vertical clip',
  breadcrumb: [{ name: 'How it works' }],
  tldr: [
    CANONICAL_ANSWERS.howItWorks,
    'The two stages that decide whether a clip is usable are moment scoring and reframing. Everything else is mechanical.',
    'Klippo self-hosted is free to run yourself, so every stage below can be read and changed. Klippo Cloud runs the same pipeline on a GPU from $12/month.',
  ],
  body: `
<h2>What is Klippo?</h2>
<p>${esc(CANONICAL_ANSWERS.whatIsIt)}</p>

<h2>The pipeline, stage by stage</h2>
${PIPELINE_STEPS.map((s) => `<h3>${esc(s.title)}</h3><p>${esc(s.body)}</p>`).join('')}

<h2>Why does moment scoring need the transcript and the scenes together?</h2>
<p>A transcript alone finds a good sentence but has no idea whether the shot cuts
halfway through it. Scene boundaries alone find clean cuts with nothing worth
saying between them. Passing both to the model at once is what lets it pick a
segment that is both quotable and visually intact, which is the difference
between a clip a person would watch and one that merely starts and stops in the
right places.</p>

<h2>Why does the camera hold still instead of following the face exactly?</h2>
<p>Because a crop that tracks a face frame by frame produces visible swinging,
and the swinging reads as amateur even when the framing is technically correct.
The reframing keeps a safe zone around the subject and only moves the crop when
they leave it, then damps the movement on the way. A speaker tracker sits on top
to stop the crop from flipping between people every time someone nods, and to
hold position through brief occlusions.</p>

<h2>How long does it take?</h2>
<p>On a typical CPU, an 8-minute source video takes roughly 5 to 8 minutes end to
end. On an NVIDIA GPU the same video takes about 50 seconds. The gap is almost
entirely transcription and encoding; the model call is a small fraction of it.</p>

<h2>What does it cost to run?</h2>
${pricingParagraph}

${faqBlock([
  {
    q: 'What AI model does Klippo use to find viral moments?',
    a: 'Google Gemini 3.0 Flash. It receives the word-level transcript with timestamps together with PySceneDetect scene boundaries, and returns 3 to 15 segments of 15 to 60 seconds scored on hook strength, emotional payload and whether the segment stands alone without surrounding context.',
  },
  {
    q: 'How does the automatic vertical cropping work?',
    a: 'Two modes. TRACK mode follows a single subject using MediaPipe face detection with a YOLOv8 fallback, stabilised so the crop holds still inside a safe zone instead of following every movement. GENERAL mode handles group shots and landscapes by preserving the full width over a blurred backdrop.',
  },
  {
    q: 'Can it dub clips into other languages?',
    a: 'Yes, into more than 30 languages via ElevenLabs, preserving the original speaker\'s voice characteristics. The dubbed audio is then re-transcribed so the burned-in subtitles match the new language rather than the original.',
  },
])}
`,
  faq: [
    {
      q: 'What AI model does Klippo use to find viral moments?',
      a: 'Google Gemini 3.0 Flash, which receives the word-level transcript with timestamps together with PySceneDetect scene boundaries and returns 3 to 15 segments of 15 to 60 seconds.',
    },
    {
      q: 'How does the automatic vertical cropping work?',
      a: 'TRACK mode follows a single subject with MediaPipe face detection and a YOLOv8 fallback, stabilised to hold still inside a safe zone. GENERAL mode preserves full width over a blurred backdrop for group shots and landscapes.',
    },
  ],
})

export function buildPages() {
  return [
    hubPage(),
    ...ALTERNATIVES.map(competitorPage),
    freeClipGenerator(),
    openSourceClipper(),
    howItWorks(),
  ]
}

/* Each page links to three siblings. Small, described clusters beat a single
 * dump of every URL: the described link tells an engine what it will find. */
export function relatedFor(page, all) {
  const blurb = {
    '/alternatives': 'All four tools compared, with entry pricing.',
    '/alternatives/opus-clip': 'Per-minute credits, 720p vs 1080p, and where each one wins.',
    '/alternatives/klap': 'Fastest URL-to-clip path, and what you give up for it.',
    '/alternatives/vizard': 'Timeline editing after the AI pass, and who needs it.',
    '/alternatives/submagic': 'Captions only, so it does not replace a clipper.',
    '/free-ai-clip-generator': 'What free means when there is no metering code.',
    '/self-hosted-video-clipper': 'Self-hosting with Docker on your own hardware.',
    '/how-klippo-works': 'The full pipeline, stage by stage.',
  }
  // Walk the ring starting after this page so each page links to a different
  // three. Slicing the same head every time would leave the last pages in the
  // list with no inbound links at all.
  const i = all.findIndex((p) => p.path === page.path)
  return [1, 2, 3]
    .map((n) => all[(i + n) % all.length])
    .filter((p) => p && p.path !== page.path)
    .map((p) => ({ path: p.path, title: p.h1, blurb: blurb[p.path] || p.description }))
}
