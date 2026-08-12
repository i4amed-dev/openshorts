# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Klippo is an AI-powered vertical video generator that transforms long YouTube videos or local uploads into viral-ready short clips (9:16 format) for TikTok, Instagram Reels, and YouTube Shorts. Uses Google Gemini 2.0 Flash for viral moment detection and title generation.

## Development Commands

### Local Development (Docker)
```bash
docker compose up --build   # Build and run full stack
```
- Backend: http://localhost:8000 (FastAPI/Uvicorn)
- Frontend: http://localhost:5175 (Vite proxies API calls to backend)

### Frontend Only (Dashboard)
```bash
cd dashboard
npm install
npm run dev       # Dev server with HMR (port 5173)
npm run build     # Production build
npm run lint      # ESLint (strict, --max-warnings 0)
```

### Backend Only
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Architecture

### Core Processing Pipeline
1. **Ingest** - YouTube download (yt-dlp) or local upload
2. **Transcription** - faster-whisper with word-level timestamps
3. **Scene Detection** - PySceneDetect for segment boundaries
4. **AI Analysis** - Gemini identifies 3-15 viral moments (15-60 sec each)
5. **FFmpeg Extraction** - Precise clip cutting
6. **AI Cropping** - Vertical reframing with subject tracking
7. **Effects/Subtitles** - Optional AI-generated FFmpeg filters
8. **Hook Overlay** - Text overlays with styled fonts
9. **Voice Dubbing** - Optional ElevenLabs AI translation (30+ languages)
10. **S3 Backup** - Silent background upload
11. **Social Distribution** - Upload-Post API (async upload)

### Key Files
| File | Purpose |
|------|---------|
| `main.py` | Core video processing: transcription, scene detection, clip extraction, vertical reframing |
| `app.py` | FastAPI server with async job queue and REST endpoints |
| `editor.py` | Gemini AI integration for dynamic video effects (FFmpeg filter generation) |
| `hooks.py` | Hook text overlay generation with font rendering |
| `s3_uploader.py` | AWS S3 upload with caching |
| `subtitles.py` | SRT generation, FFmpeg subtitle burning, and dubbed video transcription |
| `translate.py` | ElevenLabs dubbing API for AI voice translation |
| `publishing_service.py` | **The one** Upload-Post implementation — payload construction, streaming multipart upload, status reconciliation, scheduled-job listing/cancellation. Used by `/api/social/post`, `/api/saasshorts/post`, `/api/thumbnail/publish` and Autopilot. No other module may speak HTTP to Upload-Post. |
| `automation/` | Autopilot: the unattended content engine (see below) |
| `ops/healthcheck.py` | Machine fitness check for a dedicated Autopilot Mac |
| `ops/benchmark.py` | Memory/CPU/disk cost of one real source on this machine |
| `dashboard/src/App.jsx` | Main React component with state management |
| `dashboard/src/components/AutopilotTab.jsx` | Autopilot operations + setup UI |
| `dashboard/src/components/TranslateModal.jsx` | Voice dubbing UI with language selection |
| `dashboard/vite-plugin-seo.js` | Build-time SEO surface: injects crawler-visible homepage content, emits static pages, sitemap.xml and llms.txt |
| `dashboard/seo/data.js` | Single source of truth for pricing, pipeline and competitor facts used by every generated page |

### Autopilot (`automation/`)

The unattended mode: discover YouTube sources → rank → submit to the **existing**
Clip Generator → select clips → schedule → publish through the **existing**
Upload-Post integration. Self-host only (off when `BILLING_ENABLED`).

Rules that are load-bearing — breaking one reintroduces a class of bug:

- **No second pipeline.** Video work goes through `app.submit_clip_job()`, the
  same function `/api/process` calls. Publishing goes through
  `publishing_service.publish()`. Autopilot decides *what*, never *how*.
- **`automation/` imports with the standard library alone.** CI installs only
  `pytest pillow httpx pydantic sqlalchemy`, and `app.py` pulls in boto3,
  ultralytics, mediapipe and faster-whisper at import time. So `app.py` registers
  adapters into `automation/ports.py` at startup; `automation/` never imports
  `app`. Keep it that way or the whole test suite stops running in CI.
- **State lives in SQLite, never only in a dict.** `automation/db.py`, WAL mode,
  on a persistent volume. Deduplication is enforced by DB constraints (unique
  `youtube_video_id`, unique `job_id`, unique `(job_id, clip_index)`, partial
  unique indexes on live publish attempts and on slots), not by application
  checks — a check has a read-then-write window a duplicate tick slips through.
- **One heavy pipeline at a time**, enforced from Autopilot's own state so a
  raised `MAX_CONCURRENT_JOBS` cannot melt the machine.
- **Timezones**: store UTC, convert only at the boundary, validate the IANA name.
- **Vendor acceptance is not publication.** A 2xx from `/api/upload` means
  Upload-Post accepted the job. `PublishState.SUBMITTED` says exactly that;
  `PUBLISHED` is reached only through a `/uploadposts/status` check. Never set
  `ClipState.PUBLISHED` on acceptance — that was the v1 bug.
- **`PublishState.UNCERTAIN` is never blindly re-POSTed.** Klippo sends its own
  `request_id` (a documented Upload-Post parameter) before every upload, so an
  ambiguous timeout is *resolved by asking* the status endpoint, not by
  resending. Do not "fix" this by adding a plain retry.
- **`PARTIAL_FAILED` is never auto-retried at all.** Some platforms are already
  live; resending duplicates them. A human decides.
- **YouTube quota has two independent buckets** (`general` 10k units,
  `search` ~100 calls/day). Never collapse them: exhausting search must not stop
  chart discovery. `search.list` requires `part=snippet`.
- **The rights policy alone decides the search licence filter**
  (`eligibility.search_requires_creative_commons`). Never reintroduce a separate
  `creative_commons_search_only` switch — the two could contradict.
- **Rights**: Autopilot carries a persistent Source Rights Policy and records it
  with every processed source. It must never synthesise manual mode's
  acknowledgement checkbox.

Files: `db.py` (schema + repository), `models.py` (states + legal transitions),
`config.py` (validation of everything the dashboard sends), `youtube_client.py`,
`discovery.py`, `ranking.py`, `eligibility.py`, `scheduler.py` (slot maths),
`publishing.py`, `orchestrator.py` (the state machine), `service.py` (loop,
lease, dashboard view), `api.py`, `ports.py` (the seam to `app.py`).

Ops: `ops/macos/` for always-on macOS setup. `ops/healthcheck.py` and
`ops/benchmark.py` for the M1 deployment profile.


### SEO / AI-crawler surface

The dashboard is a client-rendered SPA with hash routing, so the HTML served for
`/` used to contain an empty `<div id="root">`. Googlebot renders JavaScript and
saw the real page; GPTBot, ClaudeBot and PerplexityBot do not and measured the
homepage as zero characters of text. `vite-plugin-seo.js` fixes that at build time:

- Injects the content of `seo/landing-fallback.js` into `#root`. React's
  `createRoot().render()` replaces it on mount, so users get the app and
  non-executing clients get the copy. **Keep it in sync with `Landing.jsx`.**
- Emits the standalone pages under `/alternatives`, `/free-ai-clip-generator`,
  `/self-hosted-video-clipper` and `/how-klippo-works` as flat `.html` files.
  nginx resolves the clean URL through `try_files $uri $uri.html`; serving them as
  directories instead makes nginx 301 to a trailing slash and every canonical
  would then point at a redirect.
- Generates `sitemap.xml` and `llms.txt` from the same page list, so they cannot
  drift. Do not add a static `public/sitemap.xml` back.

When editing pricing anywhere, edit `seo/data.js` too. Nothing on the site should
say "Klippo is free" without naming the Cloud price in the same breath: both
are true of different editions and quoting only the first one is what makes AI
answers describe the paid product as free.

### Dual-Mode Video Reframing
- **TRACK Mode** (single subject): MediaPipe face detection + YOLOv8 fallback with "Heavy Tripod" stabilization
- **GENERAL Mode** (groups/landscapes): Blurred background layout preserving full width

### Key Classes
- `SmoothedCameraman` - Stabilized camera movement with safe zone logic (prevents jitter)
- `SpeakerTracker` - Prevents rapid speaker switching, handles temporary occlusions

### API Endpoints
| Method | Route | Purpose |
|--------|-------|---------|
| POST | `/api/process` | Submit video for processing |
| GET | `/api/status/{job_id}` | Poll job status and logs |
| POST | `/api/edit` | Apply AI video effects |
| POST | `/api/subtitle` | Generate and apply subtitles (auto-transcribes dubbed videos) |
| POST | `/api/hook` | Add text hook overlays |
| POST | `/api/translate` | AI voice dubbing via ElevenLabs |
| GET | `/api/translate/languages` | List supported dubbing languages |
| POST | `/api/social/post` | Post to social media (async upload) |
| GET | `/health/detail` | Backend + Autopilot operational health (no credentials) |
| GET | `/api/autopilot/status` | Full Autopilot dashboard payload |
| GET/PUT | `/api/autopilot/settings` | Autopilot configuration (PUT accepts a partial patch) |
| POST | `/api/autopilot/{enable,disable,pause,resume,emergency-stop}` | Engine controls |
| POST | `/api/autopilot/{discover,process-next}` | Run a stage now |
| POST | `/api/autopilot/sources/{id}/{skip,retry}` | Candidate actions |
| POST | `/api/autopilot/publishes/{id}/{retry,force-retry,resolve}` | Publish-attempt actions |

### Concurrency Model
Async job queue with semaphore-based concurrency control. Configure via `MAX_CONCURRENT_JOBS` env var (default: 5). Jobs auto-cleanup after 1 hour.

## Environment Variables

**Server-side (.env):**
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `AWS_S3_BUCKET` - For S3 backup
- `MAX_CONCURRENT_JOBS` - Concurrent processing limit (default: 5; **1 on a laptop**)
- `VITE_API_URL` - Production API URL override
- `GEMINI_API_KEY`, `YOUTUBE_DATA_API_KEY`, `UPLOAD_POST_API_KEY`, `UPLOAD_POST_USER` -
  required by Autopilot, which cannot read the browser's localStorage
- `AUTOPILOT_ENABLED`, `AUTOPILOT_DB_PATH`, `AUTOPILOT_TICK_SECONDS`, `AUTOPILOT_LEASE_TTL`

**Client-side (localStorage, encrypted):**
- `GEMINI_API_KEY` - Google Gemini API key (required)
- `ELEVENLABS_API_KEY` - ElevenLabs API key for voice dubbing (optional)
- `UPLOAD_POST_API_KEY` - Upload-Post API key for social posting (optional)

> API keys are stored encrypted in the browser and sent via headers only when needed. Never stored server-side.

## Tech Stack
- **Backend:** Python 3.11, FastAPI, google-genai, faster-whisper, ultralytics (YOLOv8), mediapipe, opencv-python, yt-dlp, FFmpeg, httpx
- **Frontend:** React 18, Vite 4, Tailwind CSS 3.4
- **External APIs:** Google Gemini, ElevenLabs Dubbing, Upload-Post
- **Infrastructure:** Docker + Docker Compose, AWS S3
