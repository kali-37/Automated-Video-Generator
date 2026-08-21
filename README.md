# Automated Video Generator

> An end-to-end pipeline that turns a content brief into publish-ready short-form
> videos using Google's Gemini LLMs for scripting and Veo 3.1 for video
> generation — then optionally ships them to YouTube and TikTok.

Built for high-throughput, autonomous content production: define a "genera"
(vertical), drop in a master prompt, and let the pipeline generate, validate,
enhance, render, and upload videos with zero manual intervention.

---

## What it does

The pipeline automates the full content lifecycle for short-form video:

1. **Script generation** — A Gemini model reads a master prompt + constraints
   (language, format, segment count, de-duplication history) and returns a
   structured JSON script: per-segment characters, dialogue, and cinematic
   `veo_prompt`s.
2. **Prompt enhancement** — An optional second, stronger LLM pass rewrites each
   `veo_prompt` for maximum Veo fidelity (action density, native-script audio,
   visual continuity across segments).
3. **Validation & safety** — Dialogue word counts are trimmed to language
   limits, and an aggressive RAI softener retries blocked prompts with
   child-safe rephrasing before giving up.
4. **Video generation** — Veo 3.1 renders each segment. Supports text-to-video,
   image-to-video (reference frames), native extend chaining for frame
   continuity, and 4K upscaling.
5. **Audio polish** — Optional Chirp3-HD TTS replaces Veo's muddy native speech
   with clean, emotion-tagged dialogue, mixed over Veo's ambient SFX.
6. **Publishing** — Finished videos in `assets/` are scanned and uploaded to
   YouTube and/or TikTok, with upload state tracked to avoid re-posts.

---

## Architecture

```
┌─────────────────┐   ┌──────────────────┐   ┌─────────────────┐
│  Master Prompt  │──▶│   Gemini LLM     │──▶│  Script JSON    │
│  (genera/*.md)  │   │  (script + enh)  │   │  (validate/fix) │
└─────────────────┘   └──────────────────┘   └────────┬────────┘
                                                      │
                                                      ▼
┌─────────────────┐   ┌──────────────────┐   ┌─────────────────┐
│  YouTube /      │◀──│  ffmpeg + TTS    │◀──│  Veo 3.1 Render │
│  TikTok Upload  │   │  (mix / concat)  │   │  (extend chain) │
└─────────────────┘   └──────────────────┘   └─────────────────┘
```

| Component | File | Responsibility |
|-----------|------|----------------|
| Orchestrator | `video_pipeline_v31.py` | LLM calls, Veo rendering, RAI handling, history |
| Config | `constants.py` | All tunables: models, language, formats, quotas |
| YouTube | `youtube_uploader.py` | OAuth upload + dedup tracking |
| TikTok | `tiktok_uploader.py` | Session-cookie upload + dedup tracking |
| Account setup | `setup_new_account.sh` | Bootstraps a GCP service-account key |

---

## Repository layout

```
.
├── video_pipeline_v31.py   # main generation pipeline
├── constants.py            # configuration (models, language, quotas)
├── youtube_uploader.py     # YouTube publishing
├── tiktok_uploader.py      # TikTok publishing
├── setup_new_account.sh    # GCP service-account bootstrap
├── MASTER_PROMPT_*.md      # content briefs per vertical
├── assets/                 # generated videos (gitignored)
├── genera_*/               # per-vertical working folders (gitignored)
└── credentials.json        # secrets (gitignored)
```

> **Note:** `assets/`, `genera_*/`, `*.mp4`, `*.zip`, and all credential files
> are excluded from version control (see `.gitignore`). Generated media and
> keys never leave your machine via git.

---

## Getting started

### Prerequisites

- Python 3.14+
- `ffmpeg` + `ffprobe` on `PATH`
- A Google Cloud project with **Veo API** enabled
- A Gemini API key (Google AI Studio) and/or a GCP service-account key

### Install

```bash
# from the project root
uv sync            # installs deps from pyproject.toml / uv.lock
```

### Configure credentials

```bash
# bootstrap a service-account key (recommended)
./setup_new_account.sh

# or set the env var / drop a key file matching video-gen-*-key.json
export GCP_KEY_FILE=video-gen-XXXXXXXX-key.json
```

Add your Gemini API keys to `GEMINI_API_KEYS` in `constants.py` (the pipeline
rotates through them on rate limits).

---

## Usage

### Generate videos

```bash
# run the full pipeline for the default genera
python video_pipeline_v31.py

# target a specific vertical folder (episode or auto mode)
python video_pipeline_v31.py --folder genera_asmr

# force a language / format
python video_pipeline_v31.py --language nepali --format body_drama

# control segment count
python video_pipeline_v31.py --parts 5
```

The pipeline loops automatically until `RUN_UNTIL` directories exist in
`assets/` (set `RUN_UNTIL = 0` for single-shot mode).

### Publish

```bash
# YouTube (requires YOUTUBE_ENABLED = True + client_secrets.json)
python youtube_uploader.py --dry-run     # preview
python youtube_uploader.py                # upload

# TikTok (requires TIKTOK_ENABLED = True + TIKTOK_SESSION_ID)
python tiktok_uploader.py --dry-run
python tiktok_uploader.py
```

---

## Configuration reference (`constants.py`)

| Setting | Default | Notes |
|---------|---------|-------|
| `LLM_MODEL` | `gemini-flash-latest` | Script-generation model |
| `LLM_BACKEND` | `gemini_api` | `gemini_api` / `vertex_ai` / `auto` |
| `VEO_MODEL` | `veo-3.1-generate-001` | Rendering model |
| `VIDEO_LANGUAGE` | `asmr` | `asmr` / `comedy` / `nepali` |
| `VIDEO_FORMAT` | `None` | `body_drama` / `beauty_inside` / `None` |
| `VIDEO_DURATION_SECONDS` | `8` | 4 / 6 / 8 per segment |
| `VIDEO_ASPECT_RATIO` | `9:16` | vertical shorts |
| `ENHANCE_VEO_PROMPTS` | `False` | second LLM pass for cinematic prompts |
| `FRAME_CONTINUITY` | `True` | native Veo extend chaining |
| `TTS_ENABLED` | `False` | Chirp3-HD dialogue replacement |
| `EXTEND_ENABLED` | `False` | +7s per segment via extend API |
| `RUN_UNTIL` | `50` | auto-loop target (0 = off) |

---

## Notable design choices

- **De-duplication** — Past topics are loaded from both `generation_history.json`
  and a live scan of `assets/`, then injected into the LLM prompt so the model
  produces genuinely new content each run.
- **RAI resilience** — On a safety-block, prompts are progressively softened
  (word swaps → child-friendly framing → G-rated prefix) before the pipeline
  regenerates entirely new content, bounded by `MAX_REGENERATION_ATTEMPTS`.
- **Truncation recovery** — The JSON parser recovers partial LLM responses
  (trailing data, mid-string cuts) so a flaky generation doesn't abort a run.
- **Backend fallback** — LLM calls try Vertex AI, then rotate Gemini API keys,
  then a fallback model, then a timed retry — minimizing silent failures.

---

## Credentials & access

The pipeline is credential-driven, and **all secret material is excluded from
version control** — `credentials.json`, `video-gen-*-key.json`, `*.key.json`,
and the entire `assets/` / `genera_*/` trees are gitignored. You can clone this
repo anywhere and drop in your own keys without risk of leaking them.

### 1. Provision a GCP project

1. Create a project in the [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Vertex AI API** (powers Veo 3.1 video generation).
3. Create a **Service Account** with the `Cloud Vertex AI User` role and export
   its JSON key as `video-gen-<id>-key.json` in the project root (auto-discovered
   by `constants.py`), or point `GCP_KEY_FILE` at it.
4. _Optional:_ for the `gemini_api` backend, grab a key from
   [Google AI Studio](https://aistudio.google.com/apikey) and add it to
   `GEMINI_API_KEYS`.

> New to GCP? Google offers **$300 in free credits** for the first 90 days —
> plenty to validate the full pipeline end to end before spending real money.

### 2. Wire up authentication for CLI invocation

The toolchain authenticates at runtime, so once the key file is in place there
is nothing to export per-command:

```bash
# place your service-account key (auto-detected) or be explicit
export GCP_KEY_FILE=video-gen-1234567-key.json

# verify the project is reachable from the CLI
python video_pipeline_v31.py --help
```

For managed publishers (YouTube/TikTok), drop `client_secrets.json` (YouTube
OAuth) into the root and set `TIKTOK_SESSION_ID` for TikTok — both are
gitignored as well.

### 3. Operational guardrails

- **Least privilege:** scope the service account to Vertex AI only; no
  project-owner or billing-admin roles.
- **Rotation:** regenerate keys on a schedule and the moment a key is suspected
  exposed — the pipeline supports multiple `GEMINI_API_KEYS` for seamless swaps.
- **Local-only secrets:** keys are read from disk/`ENV`, never embedded in code
  or committed. CI should inject them as masked secrets, not artifacts.
