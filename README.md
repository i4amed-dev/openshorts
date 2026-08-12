# Klippo

[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)

**AI video platform** with 3 tools in one: **Clip Generator**, **AI Shorts (UGC videos with AI actors)**, and **YouTube Studio**.

**Two ways to run it, same software either way:**

|  | Self-hosted (this repo) | Hosted on [klippo.one](https://klippo.one/) |
|---|---|---|
| **Price** | Free forever | Free plan, paid from $12/mo |
| **Speed** | 5 to 8 min per 8-min video on CPU | About 50s on our NVIDIA GPU |
| **API keys** | Bring your own Gemini, ElevenLabs, fal.ai | Gemini included, nothing to set up |
| **Watermark / limits** | None, ever | Watermark and 20 min/mo on the free plan, neither on paid |
| **Setup** | Docker, 8GB+ RAM, model downloads | Sign in and paste a link |
| **Your data** | Your server | Ours |

Self-hosting is genuinely free and always will be. It costs you a machine, your own API keys and the time to keep it running. The hosted plans exist to cover that hardware and those keys, not to unlock features.

https://github.com/user-attachments/assets/b45fa983-16b4-48b5-ac5b-a267836b9ad9



### Video Tutorial: How it works
[![Klippo Tutorial](https://img.youtube.com/vi/xlyjD1qCaX0/maxresdefault.jpg)](https://www.youtube.com/watch?v=xlyjD1qCaX0 "Click to watch the video on YouTube")

*Click the image above to watch the full walkthrough.*

---

## 3 Tools in 1 Platform

> **New: Autopilot.** The Clip Generator can now run itself — discover trending
> or niche YouTube sources, rank them, generate clips and schedule the posts, on
> a timetable, with no browser open. [Jump to Autopilot ↓](#autopilot--unattended-mode)

### 1. Clip Generator
Turn your long-form videos — podcasts, webinars, livestreams, vlogs, interviews — into viral-ready 9:16 shorts for TikTok, Instagram Reels, and YouTube Shorts.

![Clip Results](screenshots/clip-results.png)

### 2. AI Shorts (UGC Video Creator)
Generate marketing videos with AI actors for **any product or business**. No camera, no studio, no influencer budget. Just describe your product or paste a URL.

![AI Shorts Setup](screenshots/ai-shorts.png)

- **Two cost modes**: Low Cost (~$0.65/video) and Premium (~$2/video)
- Works for any business: SaaS, restaurants, e-commerce, coaching, local businesses
- AI-generated actors with lip-sync, voiceover, b-roll, and TikTok-style subtitles
- Choose from a shared avatar gallery or upload your own photo
- Publish directly to TikTok, Instagram, and YouTube

### 3. YouTube Studio
Complete free AI YouTube toolkit: thumbnails, titles, descriptions, and direct publishing.

![YouTube Studio](screenshots/youtube-studio.png)

- AI thumbnail generator with face overlay
- 10 viral title suggestions with refinement chat
- Auto-generated descriptions with chapter timestamps
- One-click publish to YouTube

### UGC Video Gallery
All generated videos and avatars are saved to a public gallery with SEO pages for each video.

![UGC Gallery](screenshots/ugc-gallery.png)

- Public gallery page with hover-to-play (`/gallery`)
- Individual SEO video pages with og:video meta tags (`/video/{id}`)
- JSON-LD structured data for search engines
- Avatar gallery with prompt history

---

## Key Features

### Clip Generator
- **Viral Moment Detection**: Google Gemini 3.0 Flash analyzes transcripts and scene boundaries to detect 3-15 high-potential moments
- **Smart 9:16 Cropping**: Dual-mode AI reframing — TRACK mode (MediaPipe + YOLOv8 face tracking) and GENERAL mode (blurred background)
- **Auto Subtitles**: faster-whisper with word-level timestamps, styled and burned into clips
- **AI Voice Dubbing**: ElevenLabs integration for 30+ languages with voice cloning
- **Hook Text Overlays**: AI-generated attention-grabbing text overlays
- **AI Video Effects**: Gemini-generated FFmpeg filters for professional effects

### AI Shorts Pipeline
1. **Analyze**: Scrape website URL + web research, or generate from manual description
2. **Script**: AI writes viral scripts (hook - problem - solution - CTA format)
3. **Actor**: Generate AI actors with Flux 2 Pro or select from shared gallery
4. **Voice**: ElevenLabs TTS voiceover (English/Spanish, male/female)
5. **Video**: Talking head generation (Hailuo 2.3 Fast img2video + VEED Lipsync)
6. **B-roll**: AI-generated visuals with Ken Burns effect
7. **Composite**: FFmpeg final assembly with subtitles and hook overlays
8. **Publish**: Direct posting to TikTok, Instagram Reels, YouTube Shorts via Upload-Post

### YouTube Studio
- AI-powered title generation with 10 viral options
- Interactive refinement chat for titles
- AI thumbnail generation with custom face + background
- Auto descriptions with chapter timestamps from Whisper transcript
- Direct YouTube publishing via Upload-Post

### Social Auto-Publishing
- **One-click posting** to TikTok, Instagram Reels, and YouTube Shorts simultaneously
- **Schedule uploads** for any date and time — plan your content calendar and let Klippo publish automatically
- **Multi-platform distribution** — publish to all your social networks at once from a single interface
- Upload-Post integration with async uploads

### Infrastructure
- S3 cloud backup (private bucket for clips, public bucket for gallery/avatars)
- SEO gallery pages served by FastAPI with JSON-LD structured data
- Shared avatar gallery across all users
- Async job queue with configurable concurrency

---

## Who Is This For?

- **Content creators** — Turn long videos into shorts automatically, publish to all platforms at once
- **Marketing agencies** — Generate UGC videos for clients at scale, no actors or studios needed
- **SaaS founders** — Create product demos and marketing shorts from just a URL
- **E-commerce brands** — Product videos with AI actors for TikTok Shop, Instagram, YouTube
- **Local businesses** — Restaurants, gyms, real estate, coaching — affordable video marketing
- **Developers** — Self-host, customize the pipeline, integrate via API

---

## AI Shorts Showcase

Videos generated with Klippo AI Shorts — no camera, no studio, no actors:

| | | |
|:---:|:---:|:---:|
| [![Biohacking for Investors](https://test-videos-upload-post.s3.eu-west-3.amazonaws.com/videos/cdceec1b/actor.png)](https://klippo.one/video/cdceec1b) | [![Secret Weapon for Devs](https://test-videos-upload-post.s3.eu-west-3.amazonaws.com/videos/d3a80b6b/actor.png)](https://klippo.one/video/d3a80b6b) | [![El Secreto de los Agentes de IA](https://test-videos-upload-post.s3.eu-west-3.amazonaws.com/videos/8ab7de92/actor.png)](https://klippo.one/video/8ab7de92) |
| **Biohacking for Investors** · LOW COST | **Secret Weapon for Devs** · LOW COST | **El Secreto de los Agentes de IA** · PREMIUM |

> Browse all videos at [klippo.one/gallery](https://klippo.one/gallery)

---

## Klippo vs Competitors

| Feature | Klippo | Opus Clip | CapCut | Vizard | Klap | Descript |
|---------|:---:|:---:|:---:|:---:|:---:|:---:|
| **Price** | **Free self-hosted**<br>from $12/mo hosted | $15-29/mo | $8/mo | $15-20/mo | $23-63/mo | $24-65/mo |
| **Self-hosted** | **Yes** | No | No | No | No | No |
| **Self-hostable** | **Yes** | No | No | No | No | No |
| **Watermark** | **Never self-hosted**<br>free plan only when hosted | Free tier | Some | Free tier | Free tier | Free tier |
| **Upload limits** | **None self-hosted**<br>by plan when hosted | 10-30GB | Credit-based | 60min-10hr | 10-100 vids/mo | 60min-40hr |
| **AI clip detection** | Yes | Yes | Yes | Yes | Yes | Yes |
| **Smart 9:16 reframing** | Yes | Yes | Yes | Yes | Yes | No |
| **Auto subtitles** | Yes | Yes | Yes | Yes | Yes | Yes |
| **Voice dubbing (30+ langs)** | Yes | No | Pro only | No | Pro only | Business only |
| **AI UGC actors** | **Yes** | No | No | No | No | No |
| **AI video effects** | Yes | No | Yes | No | No | No |
| **Hook text overlays** | Yes | No | No | No | No | No |
| **YouTube Studio (titles, thumbnails)** | **Yes** | No | No | No | No | No |
| **Social auto-publishing** | Yes | Pro only | TikTok only | Paid only | Paid only | No |
| **Schedule uploads** | Yes | Pro only | No | Paid only | Paid only | No |
| **Data privacy** | **Your server** | Their cloud | Their cloud | Their cloud | Their cloud | Their cloud |

---

## How Much Does It Cost?

Self-hosting Klippo is free. You provide the machine and you only pay for the AI APIs you use, and most have generous free tiers:

| Service | Free Tier | Paid Cost | Used For |
|---------|-----------|-----------|----------|
| **Google Gemini** | Free trial with generous limits | < $0.01 per 10-min video | Viral moment detection, script generation, web research |
| **fal.ai** | Pay-per-use | ~$0.50-1.50 per AI Short | Actor generation, talking head video, lip-sync |
| **ElevenLabs** | Free tier available | Pay-per-use | Voiceover, voice dubbing |
| **Upload-Post** | **10 free uploads/month** to all networks (no credit card) | Pay-per-use | Auto-publishing to TikTok, Instagram, YouTube |
| **AWS S3** | Optional | ~$0.023/GB | Cloud backup for clips and gallery |

**Bottom line:** You can clip videos for practically free with Gemini, and publish 10 videos/month to all social networks at zero cost with Upload-Post.

**Don't want to run any of that?** [klippo.one](https://klippo.one/) is the same software on our hardware: our NVIDIA GPU clips an 8-minute video in about 50 seconds instead of the 5 to 8 minutes it takes on a typical CPU, the Gemini key is included, and auto-publishing is already wired up. Free plan is 20 minutes a month with a watermark and no credit card; paid plans start at $12/mo for 100 minutes without watermark.

---

## Requirements

- **Docker & Docker Compose**
- **Google Gemini API Key** ([Free — get it here](https://aistudio.google.com/app/apikey)) — required for all AI features
- **fal.ai API Key** ([Pay-per-use](https://fal.ai)) — required for AI Shorts (actor generation, video, lip-sync)
- **ElevenLabs API Key** ([Free tier](https://elevenlabs.io)) — required for voiceover/dubbing
- **Upload-Post API Key** ([free tier](https://upload-post.com)) — required for direct social posting

---

## Getting Started

### 1. Clone
```bash
git clone https://github.com/your-username/Klippo.git
cd Klippo
```

### 2. Configure (optional)
```bash
cp .env.example .env
# Edit .env with your AWS keys for S3 backup
```

### 3. Launch
```bash
docker compose up --build
```

### 4. Open Dashboard
Navigate to **`http://localhost:5175`**

1. Go to **Settings** and enter your API keys (Gemini, fal.ai, ElevenLabs, Upload-Post)
2. **Clip Generator**: Upload a long-form video to generate viral shorts
3. **AI Shorts**: Describe your product or paste a URL to generate UGC marketing videos
4. **YouTube Studio**: Generate thumbnails, titles, and descriptions for YouTube
5. **UGC Gallery**: Browse all generated videos and avatars

---

## Autopilot — unattended mode

Autopilot turns Klippo from a tool you drive into a pipeline that runs. Once
configured it will, on its own schedule:

**discover** YouTube sources → **rank** them → **pick** one that is eligible →
send it to the **existing** Clip Generator → wait → **select** the best clips →
**schedule** them into your publishing slots → **submit** them through the
**existing** Upload-Post integration → record every decision → repeat.

Manual mode is untouched. Everything you do by hand still works exactly as
before, and Autopilot uses the same job queue and the same publishing code — it
decides *what* to run, never *how*.

### Setting it up

**1. Server-side credentials.** Autopilot cannot read your browser, so put these
in `.env` (see `.env.example` for the full annotated list):

```bash
GEMINI_API_KEY=...            # the clip pipeline
YOUTUBE_DATA_API_KEY=...      # discovery
UPLOAD_POST_API_KEY=...       # publishing
UPLOAD_POST_USER=...          # WHICH Upload-Post profile to post as
```

**2. A YouTube Data API key.** [console.cloud.google.com](https://console.cloud.google.com)
→ new project → *APIs & Services* → enable **YouTube Data API v3** →
*Credentials* → **Create API key**. Restrict it to that one API.

Quota is the thing to watch: the free tier is 10,000 units/day. A "trending"
discovery run costs ~1 unit; each keyword topic costs 100. The dashboard shows
what you have spent today, and Autopilot parks itself until the reset rather
than retrying into an empty quota.

**3. Upload-Post for unattended posting.** Set up the profile as usual
(see [Social Media Setup](#social-media-setup-upload-post)), then put its
username in `UPLOAD_POST_USER`. Verify with:

```bash
docker compose exec backend python ops/healthcheck.py
```

**4. Configure and enable** in the dashboard's **Autopilot** tab.

### Source rights policy — read this one

Manual mode asks you to confirm you have the rights to each video, one at a
time. Autopilot has nobody at the keyboard, so it carries a **persistent policy**
instead, and stores which policy authorised each source it processed. It never
fabricates the manual confirmation.

| Policy | What it allows |
|---|---|
| `CREATIVE_COMMONS_ONLY` *(default)* | Only sources the uploader licensed for reuse (CC-BY). |
| `OWNED_OR_ALLOWLISTED_CHANNELS` | Only channel IDs you list — normally your own, or ones with written permission. Licence ignored. |
| `CREATIVE_COMMONS_OR_ALLOWLISTED` | Either of the above. |

"It was trending" is not a licence. Creative Commons is the default for a
reason; only widen it for channels whose rights position you actually know.

### How sources are ranked

Deterministic code, not an LLM — the result has to be reproducible and
explainable, and a language model adds nothing to arithmetic over YouTube
statistics. Gemini stays where semantics genuinely help: choosing the moments
*inside* the video, which the existing pipeline already does.

Every signal is normalised **within the candidate set** before it is weighted,
so no single raw count can dominate:

| Signal | Default weight | What it captures |
|---|---|---|
| view velocity | 0.30 | views/hour since publication — momentum, not lifetime totals |
| engagement | 0.20 | (likes + comments) ÷ views |
| recency | 0.15 | newer inside your age window |
| views | 0.10 | absolute reach, log-compressed |
| chart position | 0.10 | rank in YouTube's `mostPopular` response |
| topic relevance | 0.10 | keyword match against your niche |
| comment activity | 0.05 | discussion tends to mean clippable moments |
| length fit | 0.05 | can this source yield the clips you asked for |

Minus penalties for channel repetition and for candidates seen and passed over
before. Every weight is configurable, and the dashboard shows the full breakdown
per source — so "why did it pick that one" always has an answer.

### Scheduling

Two independent schedules, deliberately not merged:

- **Discovery** — when Klippo looks for content and starts generating (default 03:00).
- **Publishing slots** — when finished clips go out (default 11:30 / 16:30 / 21:00).

All times are wall-clock in your configured IANA timezone. Internally everything
is UTC, so DST transitions never duplicate or drop a post. Clips finishing after
today's last slot roll into tomorrow's.

### What it will not do

Hard limits, because unattended software spends money:

- **One heavy pipeline at a time**, enforced from Autopilot's own state — even if
  `MAX_CONCURRENT_JOBS` is raised, Autopilot will not start a second source.
- Bounded retries per source and per post, then it gives up on that item.
- A **circuit breaker**: after N consecutive failures it pauses itself and waits
  for you, rather than burning API credits all night.
- Caps on sources/day, posts/day and source duration.
- A low-quality source is **skipped with a reason** — Autopilot never enters the
  interactive "process at 360p anyway?" confirmation manual mode uses.

### Restart and downtime behaviour

State lives in SQLite (WAL, foreign keys, unique constraints), on a persistent
Docker volume. On startup Autopilot reconciles what it finds:

| Killed at | What happens on restart |
|---|---|
| during discovery | Nothing lost, nothing duplicated — sources are keyed by YouTube `videoId`. |
| while processing | Reattaches to the same Klippo job. It is never resubmitted. |
| after processing, before scheduling | Scheduling resumes. Clips are keyed by `(job_id, clip_index)`. |
| after scheduling | Nothing is re-sent; the slot is already claimed. |
| **mid-upload** | The attempt is marked **UNCERTAIN** and is *never* auto-retried. Upload-Post has no idempotency key, so a blind retry could post twice. You resolve it from the dashboard. |
| machine asleep past a slot | Catch-up policy applies — by default the clip moves to the next free slot. It never bursts every overdue post at once. |

### Running it on a dedicated Mac

See **[`ops/macos/README.md`](ops/macos/README.md)** for sleep settings, Docker
Desktop resources, what a cold boot actually does, and the optional LaunchAgent.
Short version: keep it plugged in, turn on *Prevent automatic sleeping on power
adapter when display is off*, give Docker 6–8 GB, and know that Docker Desktop
only starts after you log in.

```bash
# Is this machine fit to run unattended?
docker compose exec backend python ops/healthcheck.py

# What does one real source cost here?
docker compose exec backend python ops/benchmark.py --url "https://youtu.be/..." -v
```

### Turning it off

Dashboard → Autopilot → **Pause** (finish current, start nothing new) →
**Disable** (stop the engine) → **Emergency stop** (also cancels every post not
yet sent). Or `AUTOPILOT_ENABLED=0` in `.env` to remove the subsystem entirely.

Posts Upload-Post has already accepted stay on its calendar — only Upload-Post
can cancel those, and Klippo says so rather than pretending otherwise.

### Troubleshooting

| Symptom | Cause |
|---|---|
| Tab missing | `AUTOPILOT_ENABLED=0`, or you are running in cloud/billing mode where it is off by design. |
| "Missing server-side credentials" | A key is only in the browser. Autopilot needs it in `.env`. |
| Nothing is ever eligible | Usually the rights policy: with the CC-only default, most trending videos are standard-licence. Check the **not used** list — every rejection carries its reason. |
| "quota exhausted" | Too many keyword topics. Each costs 100 of 10,000 daily units. |
| Stuck at "waiting for the clip queue" | A manual job is occupying the pipeline. Autopilot waits by design. |
| Publish attempt shows UNCERTAIN | The backend died mid-upload. Check the Upload-Post calendar, then tell the dashboard which way it went. |

---

## Technical Pipeline

### Clip Generator
1. **Ingest** — Local video upload (or self-hosted URL ingest via yt-dlp)
2. **Transcribe** — faster-whisper with word-level timestamps
3. **Detect** — PySceneDetect for scene boundaries
4. **Analyze** — Gemini identifies 3-15 viral moments (15-60s each)
5. **Extract** — FFmpeg precise clip cutting
6. **Reframe** — AI vertical cropping with subject tracking
7. **Effects** — Subtitles, hooks, AI video effects
8. **Publish** — S3 backup + Upload-Post social distribution

### AI Shorts
1. **Analyze** — Website scraping + Gemini web research (or manual description)
2. **Script** — Gemini generates viral scripts with segments
3. **Actor** — Flux 2 Pro portrait generation (or gallery/upload)
4. **Voice** — ElevenLabs TTS voiceover
5. **Video** — Hailuo 2.3 Fast img2video + VEED Lipsync (Low Cost) or Kling Avatar v2 (Premium)
6. **B-roll** — Flux 2 Pro image generation + Ken Burns effect
7. **Composite** — FFmpeg assembly with ASS subtitles and hook overlays
8. **Gallery** — Upload to public S3 with metadata for SEO pages
9. **Publish** — Upload-Post to TikTok, Instagram, YouTube

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI, google-genai, faster-whisper, ultralytics (YOLOv8), mediapipe, opencv-python, yt-dlp, FFmpeg, httpx |
| Frontend | React 18, Vite 4, Tailwind CSS 3.4 |
| AI APIs | Google Gemini, fal.ai (Flux, Hailuo, VEED, Kling), ElevenLabs |
| Infrastructure | Docker + Docker Compose, AWS S3 |
| Publishing | Upload-Post API (TikTok, Instagram, YouTube) |

---

## Environment Variables

**Server-side (.env):**
| Variable | Description |
|----------|------------|
| `AWS_ACCESS_KEY_ID` | AWS access key for S3 |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |
| `AWS_REGION` | AWS region (default: us-east-1) |
| `AWS_S3_BUCKET` | Private bucket for clip backup |
| `AWS_S3_PUBLIC_BUCKET` | Public bucket for gallery/avatars |
| `MAX_CONCURRENT_JOBS` | Concurrent heavy jobs (default: 5; **use 1 on a laptop**) |
| `GEMINI_API_KEY` | Google Gemini — self-host fallback, **required by Autopilot** |
| `YOUTUBE_DATA_API_KEY` | YouTube Data API v3 — **required by Autopilot** discovery |
| `UPLOAD_POST_API_KEY` | Upload-Post — **required by Autopilot** publishing |
| `UPLOAD_POST_USER` | Which Upload-Post profile Autopilot posts as |
| `AUTOPILOT_ENABLED` | Mount the Autopilot routes + scheduler (default: on for self-host) |
| `AUTOPILOT_DB_PATH` | Autopilot SQLite state — must be on a persistent volume |
| `AUTOPILOT_TICK_SECONDS` | Orchestrator loop interval (default: 30) |
| `AUTOPILOT_LEASE_TTL` | Scheduler lease lifetime, seconds (default: 120) |

**Client-side (encrypted in localStorage):**
| Key | Description |
|-----|------------|
| `GEMINI_API_KEY` | Google Gemini — required |
| `FAL_KEY` | fal.ai — required for AI Shorts |
| `ELEVENLABS_API_KEY` | ElevenLabs — required for voiceover/dubbing |
| `UPLOAD_POST_API_KEY` | Upload-Post — required, for social posting |

---

## Security & Performance

- **Non-Root Execution**: Containers run as dedicated `appuser`
- **Concurrency Control**: Semaphore-based job queue (`MAX_CONCURRENT_JOBS`)
- **Auto-Cleanup**: Automatic purging of old jobs (1h retention)
- **Encrypted Keys**: API keys encrypted client-side, never stored server-side
- **Upload Validation**: Image uploads validated for format and minimum size
- **File Limits**: 2GB upload limit protection
- **Autopilot Secrets**: server-side credentials are never written to the
  Autopilot database, returned by any API response, or printed to a log — the
  status endpoint reports only whether each one is *set*
- **Autopilot Safety**: one heavy pipeline at a time, bounded retries, a
  consecutive-failure circuit breaker, and hard daily caps on sources and posts

---

## Social Media Setup (Upload-Post)

1. **Register**: [app.upload-post.com/login](https://app.upload-post.com/login)
2. **Create Profile**: Go to [Manage Users](https://app.upload-post.com/manage-users)
3. **Connect Accounts**: Link TikTok, Instagram, and/or YouTube
4. **Get API Key**: Navigate to [API Keys](https://app.upload-post.com/api-keys)
5. **Use in Klippo**: Paste the key in Settings

---

## Contributions

Contributions are welcome! Whether it's adding new AI models, improving the lip-sync pipeline, or building new features — feel free to open a PR.

## License and attribution

Klippo is a rebranded distribution of an upstream codebase; the branding is ours,
the licences are not, and both are reproduced unchanged in this repository.

- The core application is under the **MIT License** — see [`LICENSE`](LICENSE).
  The copyright notice in that file is the upstream author's and stays as it is.
- The [`cloud/`](cloud/LICENSE) directory (billing, managed keys and the
  hosted-service infrastructure behind the optional `BILLING_ENABLED` flag) is
  **not** MIT. It is source-available under the **OpenShorts Commercial License**
  reproduced in [`cloud/LICENSE`](cloud/LICENSE): readable and modifiable, and
  self-hostable for personal or internal use, but not to be offered to third
  parties as a paid or hosted service. Self-hosting the core app never requires
  this directory.
