"""
Constants for the video generation pipeline.
Toggle settings here to control video generation behavior.
"""

import glob
import json
import os

# -- GCP Configuration --
# Auto-detect service account key and project ID.
# Priority:
#   1. ENV var GCP_KEY_FILE → use that specific key file
#   2. Otherwise → pick the first video-gen-*-key.json in this directory
# The project ID is read directly from the key file's "project_id" field.

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

def _find_key_file() -> str:
    """Find the active service account key file."""
    # Check env var first (easy switching: export GCP_KEY_FILE=video-gen-XXXX-key.json)
    env_key = os.environ.get("GCP_KEY_FILE")
    if env_key:
        path = env_key if os.path.isabs(env_key) else os.path.join(_THIS_DIR, env_key)
        if os.path.isfile(path):
            return path
        raise FileNotFoundError(f"GCP_KEY_FILE env var set to '{env_key}' but file not found")

    # Auto-discover: find video-gen-*-key.json in project dir
    pattern = os.path.join(_THIS_DIR, "video-gen-92*-key.json")
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise FileNotFoundError(
            f"No service account key found matching {pattern}. "
            "Run setup_new_account.sh or set GCP_KEY_FILE env var."
        )
    # Use the most recent one (last alphabetically, which is highest timestamp)
    return matches[-1]

def _load_project_id(key_path: str) -> str:
    """Extract project_id from the service account key JSON."""
    with open(key_path) as f:
        data = json.load(f)
    project_id = data.get("project_id")
    if not project_id:
        raise ValueError(f"No 'project_id' field in {key_path}")
    return project_id

SERVICE_ACCOUNT_KEY_PATH = _find_key_file()
GCP_PROJECT_ID = _load_project_id(SERVICE_ACCOUNT_KEY_PATH)
GCP_LOCATION = "us-central1"

# -- Model Configuration --
# LLM model for generating video scripts/prompts
# Available (Gemini API, May 2026):
#   "gemini-3.5-flash"         — Stable, most intelligent flash (recommended)
#   "gemini-3-flash-preview"   — Preview, frontier-class
#   "gemini-2.5-flash"         — Stable, best price-performance
LLM_MODEL = "gemini-flash-latest"

# Fallback model if primary hits rate limits
LLM_MODEL_FALLBACK = "gemini-2.5-flash"

# Where to call the LLM. Options:
#   "gemini_api"  — Use generativelanguage.googleapis.com with GEMINI_API_KEYS (fast, recommended)
#   "vertex_ai"   — Use Vertex AI endpoint with service account (needs model enabled on project)
#   "auto"        — Try Vertex AI first, fall back to Gemini API (wastes time if Vertex fails)
LLM_BACKEND = "gemini_api"

# Thinking budget for Gemini (tokens the model can "think" before answering)
# Range: 0 (disabled) to 24576. Higher = deeper reasoning, slower + more expensive.
# 8192 is a good balance for creative script generation.
LLM_THINKING_BUDGET = 8192

# -- Secondary Veo Prompt Enhancer --
# After the first LLM generates the script, a second stronger LLM rewrites each
# veo_prompt for maximum Veo quality: cinematic action, clear Nepali audio, correct terminology.
# "gemini-3.1-pro-preview" — strongest available for creative prompt writing
# "gemini-3.5-flash" — fast alternative if pro hits rate limits
VEO_PROMPT_ENHANCER_MODEL = "gemini-3.5-flash"
VEO_PROMPT_ENHANCER_THINKING_BUDGET = 4096  # Enough thinking for creative rewrite
ENHANCE_VEO_PROMPTS = False  # Set True when API keys have quota; saves time when rate-limited

# Gemini API key for generativelanguage.googleapis.com (Google AI Studio key)
# Required when the model is not available on Vertex AI (e.g. Gemini 3 Flash Preview)
# Get yours at: https://aistudio.google.com/apikey
# Add multiple keys — pipeline will rotate to the next one if a key hits quota/credit limits.
GEMINI_API_KEYS = [
    "AIzaSyAmilIU2cC0gFdMo_AhGSs7w_VmKwhQdjw",  # new key 1
    "AIzaSyDA0uT0S2mRus5OhoRBX49S-NTB2pFMl64",  # new key 2
    "AIzaSyDLkJbwSfFlsQ_M7ee5ZIIQ5oOPU7mOAmE",  # random_sharma_acc
    "AIzaSyBWlc2OaQRalX6L3NTTc2vYjy6aBXYKk9Y",  # ioeexam_acc
]

# Veo model for video generation
# Available GA models (2026):
#   "veo-3.1-generate-001"       — Best quality, full features, audio+dialogue
#   "veo-3.1-fast-generate-001"  — Faster generation, same quality tier
#   "veo-3.1-lite-generate-001"  — Cheapest ($0.05/sec 720p), same speed as Fast
# All support: text-to-video, image-to-video, extend, first+last frame, prompt rewriting
VEO_MODEL = "veo-3.1-generate-001"

# -- Video Settings --
VIDEO_DURATION_SECONDS = 8  # 4, 6, or 8 seconds per clip
VIDEO_ASPECT_RATIO = "9:16"
VIDEO_PERSON_GENERATION = "allow_adult"

# Note: Veo 3.1 ALWAYS runs its prompt rewriter — the enhancePrompt parameter
# is ignored. Removed from API calls to avoid confusion.

# Number of videos per generation request (1 = cheapest, each video costs per-second)
NUMBER_OF_VIDEOS_PER_REQUEST = 1

# Upscale to 4K if available, otherwise 1080p/720p
# Set True to request upscale (4K), False for default resolution
UPSCALE_4K = False

# Seed for visual/voice consistency across parts (same seed = same style)
# Set to a fixed number for reproducible results, or None for random each time
VEO_SEED = None  # None = random each time (different visuals per generation)

# -- Image-to-Video (reference images) --
IMAGE_GENERATION_ENABLED = False  # Set True or use --image flag
IMAGEN_MODEL = "imagen-4.0-generate-001"

# =====================================================
# VIDEO EXTENSION (Veo Extend Feature)
# =====================================================
# After generating all parts, optionally extend each clip by 7 seconds
# using Veo's native extend API. This creates smoother, longer scenes.
#
# How it works:
#   1. Each 8s part is generated normally
#   2. If EXTEND_ENABLED, each part is extended by 7s (total: 15s per part)
#   3. Extension uses the same prompt for continuity
#   4. Can repeat up to EXTEND_ITERATIONS times (each adds 7s)
#
# Constraints:
#   - Input must be MP4, 24fps, 720p/1080p/4K, 9:16 or 16:9
#   - Each extension adds exactly 7 seconds
#   - Max input video length for extension: 30 seconds
#   - Works with all Veo 3.1 models including Lite

EXTEND_ENABLED = False  # Set True to extend each part after generation
EXTEND_ITERATIONS = 1   # How many times to extend (each adds 7s). Max practical: 3
EXTEND_PROMPT_SUFFIX = ""  # Optional: append to prompt for extension (e.g. "continue the scene smoothly")

# =====================================================
# FRAME CONTINUITY (Veo 3.1 Native Extend Chaining)
# =====================================================
# When enabled, instead of generating each part as a separate clip,
# the pipeline generates part 1 normally, then uses Veo's native EXTEND
# API to continue the video with each subsequent part's prompt.
#
# How it works:
#   1. Part 1 is generated normally (text-to-video, 8s)
#   2. Part 2: Veo EXTENDS part 1's video using part 2's prompt (+7s)
#   3. Part 3: Veo EXTENDS the result using part 3's prompt (+7s)
#   4. Each extension adds 7 seconds of continuation
#   5. Final video is one continuous piece (no concat needed)
#
# This uses the same API as the "Extend" button in the browser UI.
#
# Requirements:
#   - Input video must be MP4, 24fps, 720p/1080p/4K, 9:16 or 16:9
#   - Max input video length for extension: 30 seconds
#   - Each extension adds exactly 7 seconds
#   - Works with all Veo 3.1 models (generate-001, fast-001, lite-001)
#
# Note: If video exceeds 30s (after ~3 extensions), remaining parts
# are generated as separate clips and concatenated at the end.

FRAME_CONTINUITY = True  # Use native Veo extend to chain part 1 → part 2

# -- Language / Content Style --
# Supported: "hindi", "nepali", "comedy"
# "hindi" = Hindi/Hinglish health education for Indian audience
# "nepali" = Nepali health education for Nepali audience
# "comedy" = Universal English comedy shorts (fake docs, NPCs, conspiracies)
VIDEO_LANGUAGE = "asmr"

# -- Video Format --
# Options: "body_drama" (organs arguing inside body), "beauty_inside" (hair/skin transformation)
# Set to None to let LLM pick randomly
VIDEO_FORMAT = None

# -- Dialogue Word Limits --
# Veo 3.1 has improved lip-sync and speech clarity.
# Natural Nepali/Hindi speech pace: ~3 words/sec → 24 words in 8 seconds comfortably.
# Keeping a small buffer for pauses and emotion.
MAX_WORDS_HINDI = 25
MAX_WORDS_NEPALI = 25

# =====================================================
# TTS AUDIO REPLACEMENT (fixes Veo's unclear Nepali)
# =====================================================
# Veo 3.1 generates emotional but unintelligible Nepali speech.
# Solution: Generate clear TTS audio (Google Cloud Chirp3-HD) with SSML emotion
# prosody, then mix it over Veo's audio (kept at low volume for ambience/SFX).
#
# How it works:
#   1. Each video part is generated by Veo (has visuals + muddy audio)
#   2. TTS generates clear dialogue with emotion (pitch/rate/emphasis via SSML)
#   3. ffmpeg mixes: Veo audio × VEO_BG_VOLUME + TTS audio × 1.0
#   Result: Clear Nepali dialogue with Veo's background emotion/SFX preserved.

TTS_ENABLED = False  # Set False to keep Veo's native audio unchanged

# Voice assignments per character type
# Your picks: Achernar (female, warm) for Nepali chars, Fenrir (male, deep) for men
TTS_VOICES = {
    # character_type -> (voice_name, language_code)
    "food_hero":  ("hi-IN-Chirp3-HD-Achernar", "hi-IN"),   # warm female
    "organ":      ("hi-IN-Chirp3-HD-Achernar", "hi-IN"),   # warm female
    "chemical":   ("hi-IN-Chirp3-HD-Achernar", "hi-IN"),   # warm female
    "food_villain":("hi-IN-Chirp3-HD-Fenrir",  "hi-IN"),   # deep male (angry)
    "villain":    ("hi-IN-Chirp3-HD-Fenrir",   "hi-IN"),   # deep male (angry)
    "default":    ("hi-IN-Chirp3-HD-Achernar", "hi-IN"),   # fallback
}

# Emotion -> SSML prosody settings to add feeling to the TTS voice
# (rate, pitch, volume) — these shape the voice to match the scene emotion
TTS_EMOTION_PROFILES = {
    # Positive / heroic emotions
    "Confident":    {"rate": "medium",  "pitch": "+2st",  "volume": "loud"},
    "Heroic":       {"rate": "medium",  "pitch": "+3st",  "volume": "x-loud"},
    "Powerful":     {"rate": "slow",    "pitch": "+1st",  "volume": "x-loud"},
    "Proud":        {"rate": "slow",    "pitch": "+2st",  "volume": "loud"},
    "Relieved":     {"rate": "slow",    "pitch": "+1st",  "volume": "medium"},
    "Happy":        {"rate": "fast",    "pitch": "+4st",  "volume": "loud"},
    "Excited":      {"rate": "fast",    "pitch": "+3st",  "volume": "x-loud"},
    "Grateful":     {"rate": "slow",    "pitch": "+2st",  "volume": "medium"},
    "Protective":   {"rate": "medium",  "pitch": "+1st",  "volume": "loud"},
    # Negative / scared emotions
    "Distressed":   {"rate": "fast",    "pitch": "+5st",  "volume": "x-loud"},
    "Scared":       {"rate": "fast",    "pitch": "+4st",  "volume": "loud"},
    "Panicking":    {"rate": "x-fast",  "pitch": "+6st",  "volume": "x-loud"},
    "Worried":      {"rate": "medium",  "pitch": "+2st",  "volume": "medium"},
    "Exhausted":    {"rate": "x-slow",  "pitch": "-2st",  "volume": "soft"},
    "Weak":         {"rate": "x-slow",  "pitch": "-3st",  "volume": "soft"},
    "Sick":         {"rate": "slow",    "pitch": "-2st",  "volume": "soft"},
    # Angry / villain emotions
    "Arrogant":     {"rate": "slow",    "pitch": "-4st",  "volume": "x-loud"},
    "Angry":        {"rate": "fast",    "pitch": "-3st",  "volume": "x-loud"},
    "Destructive":  {"rate": "fast",    "pitch": "-5st",  "volume": "x-loud"},
    "Mocking":      {"rate": "medium",  "pitch": "-2st",  "volume": "loud"},
    "Sneaky":       {"rate": "slow",    "pitch": "-3st",  "volume": "soft"},
    # Neutral / explanatory
    "Determined":   {"rate": "medium",  "pitch": "+0st",  "volume": "loud"},
    "Calm":         {"rate": "slow",    "pitch": "+0st",  "volume": "medium"},
    "Wise":         {"rate": "slow",    "pitch": "-1st",  "volume": "medium"},
    "default":      {"rate": "medium",  "pitch": "+0st",  "volume": "loud"},
}

# Volume of Veo's original audio in the final mix (0.0 = muted, 1.0 = full)
# Keep low so Veo's emotional background SFX/music is audible but dialogue is clear TTS
VEO_BG_VOLUME = 0.15

# -- Polling --
POLL_INTERVAL_SECONDS = 15
MAX_WAIT_SECONDS = 600

# -- Output --
OUTPUT_BASE_DIR = "assets"

# -- Automatic Generation Control --
# Set RUN_UNTIL to a number to keep generating videos until
# the number of directories in OUTPUT_BASE_DIR equals RUN_UNTIL.
# Set to 0 or None to disable automatic looping (manual batch mode).
RUN_UNTIL = 50  # 0 = disabled, N = generate until N directories exist

# -- Batch --
DEFAULT_BATCH_SIZE = 1

# -- History file to track previously generated topics --
HISTORY_FILE = "generation_history.json"

# -- Master prompt files --
MASTER_PROMPT_FILES = {
    "asmr": "MASTER_PROMPT_ASMR.md",
    "comedy": "MASTER_PROMPT_COMEDY.md",
    "nepali": "MASTER_PROMPT_NEPALI.md",
}

# =====================================================
# YOUTUBE UPLOAD CONFIGURATION
# =====================================================
# To enable YouTube uploads:
#   1. Create OAuth 2.0 credentials at https://console.cloud.google.com/apis/credentials
#   2. Enable "YouTube Data API v3" in your GCP project
#   3. Download the client_secrets.json file and place it in this directory
#   4. Set YOUTUBE_ENABLED = True
#   5. First run will open a browser for OAuth consent — token is saved to YOUTUBE_TOKEN_FILE

YOUTUBE_ENABLED = False
YOUTUBE_CLIENT_SECRETS_FILE = "client_secrets.json"
YOUTUBE_TOKEN_FILE = "youtube_token.json"
YOUTUBE_DEFAULT_PRIVACY = "public"  # "public", "unlisted", "private"
YOUTUBE_DEFAULT_CATEGORY_ID = "27"  # 27 = Education
YOUTUBE_DEFAULT_TAGS = ["health", "science", "myth busted", "animated", "education"]

# =====================================================
# TIKTOK UPLOAD CONFIGURATION
# =====================================================
# To enable TikTok uploads:
#   1. Create a TikTok Developer app at https://developers.tiktok.com/
#   2. Get your session cookie (sessionid) by logging into TikTok in browser
#      and extracting the 'sessionid' cookie value
#   3. Set TIKTOK_ENABLED = True

TIKTOK_ENABLED = False
TIKTOK_SESSION_ID = ""  # Your TikTok sessionid cookie value

# =====================================================
# MASTER.PY — DAILY CYCLE CONFIGURATION
# =====================================================
DAILY_CYCLE_HOURS = 24  # Generate one video every N hours
DAILY_HISTORY_FILE = "daily_history.json"  # Tracks daily generation+upload cycles
