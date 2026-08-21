#!/usr/bin/env python3
"""
Video generation pipeline using Vertex AI (Gemini LLM + Veo 3.1).
Authenticates via service account key, generates scripts, creates videos, combines with ffmpeg.
"""

import os
import re
import json
import time
import base64
import logging
import subprocess
import argparse
import shutil
import random
from pathlib import Path
from datetime import datetime

from google.oauth2 import service_account
from google.auth.transport.requests import Request
import requests

from constants import (
    GCP_PROJECT_ID,
    GCP_LOCATION,
    SERVICE_ACCOUNT_KEY_PATH,
    LLM_MODEL,
    LLM_MODEL_FALLBACK,
    LLM_BACKEND,
    LLM_THINKING_BUDGET,
    VEO_PROMPT_ENHANCER_MODEL,
    VEO_PROMPT_ENHANCER_THINKING_BUDGET,
    ENHANCE_VEO_PROMPTS,
    GEMINI_API_KEYS,
    VEO_MODEL,
    VIDEO_DURATION_SECONDS,
    VIDEO_ASPECT_RATIO,
    VIDEO_PERSON_GENERATION,
    NUMBER_OF_VIDEOS_PER_REQUEST,
    UPSCALE_4K,
    VEO_SEED,
    EXTEND_ENABLED,
    EXTEND_ITERATIONS,
    EXTEND_PROMPT_SUFFIX,
    FRAME_CONTINUITY,
    IMAGE_GENERATION_ENABLED,
    IMAGEN_MODEL,
    VIDEO_LANGUAGE,
    MAX_WORDS_HINDI,
    MAX_WORDS_NEPALI,
    POLL_INTERVAL_SECONDS,
    MAX_WAIT_SECONDS,
    HISTORY_FILE,
    VIDEO_FORMAT,
    MASTER_PROMPT_FILES,
    TTS_ENABLED,
    TTS_VOICES,
    TTS_EMOTION_PROFILES,
    VEO_BG_VOLUME,
)

# -- Logging setup --
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
]

# -- Custom exceptions --

class RAIFilterException(Exception):
    """Raised when Veo RAI filter blocks content after all retries."""
    pass


def get_key_path():
    """Resolve absolute path to the service account key file."""
    # SERVICE_ACCOUNT_KEY_PATH is already absolute (resolved by constants.py)
    p = Path(SERVICE_ACCOUNT_KEY_PATH)
    if not p.exists():
        raise FileNotFoundError(f"Service account key not found: {p}")
    return str(p)


def get_credentials():
    """Load service account credentials and refresh the token."""
    key_path = get_key_path()
    creds = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)
    creds.refresh(Request())
    return creds


# -- Topic history management --

# Active genera folder (set by --folder CLI arg or defaults to project root)
_GENERA_DIR = Path(__file__).parent
_IS_EPISODE_MODE = False


def set_genera_folder(folder_path):
    """Set the active genera folder and detect mode (auto vs episode)."""
    global _GENERA_DIR, _IS_EPISODE_MODE
    _GENERA_DIR = Path(folder_path) if os.path.isabs(folder_path) else Path(__file__).parent / folder_path
    _IS_EPISODE_MODE = "_ep" in _GENERA_DIR.name
    if not _GENERA_DIR.exists():
        raise FileNotFoundError(f"Genera folder not found: {_GENERA_DIR}")
    log.info("Genera folder: %s (mode: %s)", _GENERA_DIR, "episode" if _IS_EPISODE_MODE else "auto")


def get_assets_dir():
    """Get the assets directory for the active genera folder."""
    return str(_GENERA_DIR / "assets")


def get_history_file():
    """Get the history/episodes file path for the active genera folder."""
    if _IS_EPISODE_MODE:
        return str(_GENERA_DIR / "episodes_made.json")
    return str(_GENERA_DIR / "generation_history.json")


def get_master_prompt_path():
    """Get the master prompt file for the active genera folder."""
    return _GENERA_DIR / "master_prompt.md"


def get_next_episode():
    """For episode mode: find the next episode that hasn't been made yet.
    Returns (episode_key, markdown_path) or (None, None) if all done.
    """
    if not _IS_EPISODE_MODE:
        return None, None

    need_file = _GENERA_DIR / "episodes_need.json"
    if not need_file.exists():
        return None, None

    with open(need_file) as f:
        episodes_need = json.load(f)

    history = load_history()
    done_keys = [ep.get("episode_key", "") for ep in history.get("episodes_done", [])]

    for ep_key, md_file in episodes_need.items():
        if ep_key not in done_keys:
            md_path = _GENERA_DIR / md_file
            if md_path.exists():
                return ep_key, md_path
            else:
                log.warning("Episode %s markdown not found: %s", ep_key, md_path)

    return None, None


def load_history():
    """Load previously generated topics from history file."""
    p = Path(get_history_file())
    if p.exists():
        with open(p) as f:
            return json.load(f)
    if _IS_EPISODE_MODE:
        return {"episodes_done": []}
    return {"generated_topics": []}


def save_history(history):
    """Save topic history to file."""
    p = Path(get_history_file())
    with open(p, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def add_to_history(data):
    """Add a completed video's topic info to history."""
    history = load_history()
    m = data.get("metadata", {})

    if _IS_EPISODE_MODE:
        entry = {
            "episode_title": m.get("episode_title", ""),
            "episode_key": m.get("_episode_key", ""),
            "characters": m.get("characters", ""),
            "theme": m.get("theme", ""),
            "timestamp": datetime.now().isoformat(),
        }
        history.setdefault("episodes_done", []).append(entry)
    else:
        entry = {
            "episode_title": m.get("episode_title", ""),
            "theme": m.get("theme", m.get("format", "")),
            "language": m.get("language", ""),
            "video_format": m.get("video_format", m.get("format", "")),
            "timestamp": datetime.now().isoformat(),
            "characters": [c.get("name", "") for c in data.get("characters", [])],
            "caption": data.get("caption", "")[:100],
        }
        history["generated_topics"].append(entry)

    save_history(history)


# -- Scan assets for already-generated topics --

def scan_existing_topics():
    """Scan the assets folder for already-generated topics to avoid duplicates."""
    assets_dir = Path(get_assets_dir())
    topics = []
    if not assets_dir.exists():
        return topics
    for folder in assets_dir.iterdir():
        if not folder.is_dir():
            continue
        meta_file = folder / "metadata.json"
        if meta_file.exists():
            try:
                with open(meta_file) as f:
                    data = json.load(f)
                m = data.get("metadata", {})
                topics.append({
                    "episode_title": m.get("episode_title", ""),
                    "theme": m.get("theme", ""),
                    "video_format": m.get("video_format", ""),
                    "language": m.get("language", ""),
                    "folder": folder.name,
                })
            except (json.JSONDecodeError, KeyError):
                continue
    return topics


# -- Master prompt --

def load_master_prompt(language=None):
    """Load the master prompt — episode markdown if in ep mode, otherwise genera folder prompt."""
    # Episode mode: use the specific episode markdown as the LLM prompt
    if _IS_EPISODE_MODE:
        ep_key, ep_path = get_next_episode()
        if ep_path and ep_path.exists():
            log.info("Episode mode: using %s as LLM guidance", ep_key)
            content = ep_path.read_text()
            # Try to extract from backticks first
            m = re.search(r"```(?:json)?\n(.*?)```", content, re.DOTALL)
            if m:
                episode_prompt = m.group(1)
            else:
                # Use full file content minus header
                lines = content.split('\n')
                start = 1 if lines and lines[0].startswith('#') else 0
                episode_prompt = '\n'.join(lines[start:])

            # Also load master_prompt.md rules and append them
            master_path = get_master_prompt_path()
            if master_path.exists():
                master_content = master_path.read_text()
                mm = re.search(r"```\n(.*?)```", master_content, re.DOTALL)
                if mm:
                    episode_prompt = mm.group(1) + "\n\n--- EPISODE GUIDANCE ---\n\n" + episode_prompt

            return episode_prompt
        elif ep_key is None:
            raise RuntimeError("All episodes in episodes_need.json are already made.")
        else:
            raise FileNotFoundError(f"Episode markdown not found for next episode")

    # Auto mode: use genera folder's master_prompt.md
    genera_prompt = get_master_prompt_path()
    if genera_prompt.exists():
        content = genera_prompt.read_text()
        m = re.search(r"```\n(.*?)```", content, re.DOTALL)
        if m:
            return m.group(1)
        raise ValueError(f"Could not extract prompt block from {genera_prompt}")

    # Fallback to MASTER_PROMPT_FILES mapping
    lang = language or VIDEO_LANGUAGE
    filename = MASTER_PROMPT_FILES.get(lang, "MASTER_PROMPT_ASMR.md")
    p = Path(__file__).parent / filename
    if not p.exists():
        raise FileNotFoundError(f"{filename} not found")
    content = p.read_text()
    m = re.search(r"```\n(.*?)```", content, re.DOTALL)
    if m:
        return m.group(1)
    raise ValueError(f"Could not extract prompt block from {filename}")


def build_scenario_seed():
    """Code-side randomization for folders that ship a seed.json.

    Keeps the folder's master_prompt.md short: the variable parts of a scenario
    (one random element per category + a random duration/segment-count) are
    chosen here and injected into the LLM prompt as a SCENARIO SEED block.

    seed.json shape:
        {
          "language": "english",          # optional
          "video_format": "kids_play",    # optional
          "durations": [{"seconds": 32, "parts": 4}, ...],
          "categories": {"environment": [...], "companion": [...], ...}
        }

    Returns a dict {seed_text, language, video_format, parts, seconds} or
    None when the active folder has no seed.json (unchanged behaviour).
    """
    seed_path = _GENERA_DIR / "seed.json"
    if not seed_path.exists():
        return None

    with open(seed_path) as f:
        cfg = json.load(f)

    # Pick a duration -> segment-count pairing
    durations = cfg.get("durations") or [{"seconds": VIDEO_DURATION_SECONDS, "parts": 1}]
    choice = random.choice(durations)
    seconds = choice.get("seconds")
    parts = choice.get("parts")

    # Pick one element per category
    picks = {}
    for name, options in (cfg.get("categories") or {}).items():
        if options:
            picks[name] = random.choice(options)

    lines = [
        "SCENARIO SEED (build the entire story around THESE EXACT elements — "
        "do not substitute or add others):"
    ]
    for name, val in picks.items():
        lines.append(f"- {name.replace('_', ' ').title()}: {val}")
    lines.append(
        f"- Target duration: {seconds} seconds across EXACTLY {parts} segments "
        f"(prompt_1 ... prompt_{parts})."
    )

    log.info("Scenario seed: %ds/%d parts | %s",
             seconds, parts, ", ".join(f"{k}={v}" for k, v in picks.items()))

    return {
        "seed_text": "\n".join(lines),
        "language": cfg.get("language"),
        "video_format": cfg.get("video_format"),
        "parts": parts,
        "seconds": seconds,
    }


def build_prompt(category=None, topic=None, parts=None):
    """Build the full LLM prompt with constraints and history context."""
    base = load_master_prompt(VIDEO_LANGUAGE)
    constraints = []

    # Code-seeded scenario: if the active folder has a seed.json, pick one random
    # element per category + a random duration and inject them. This keeps the
    # master_prompt.md short — the variable "steps" are generated here in code.
    seed = build_scenario_seed()

    # Set language (a seed may override the folder-agnostic default)
    language = (seed.get("language") if seed else None) or VIDEO_LANGUAGE
    constraints.append(f'Language: "{language}"')

    # Set video format (explicit constant wins, else the seed's)
    video_format = VIDEO_FORMAT or (seed.get("video_format") if seed else None)
    if video_format:
        constraints.append(f'Video format: "{video_format}"')

    if category:
        constraints.append(f'Category: "{category}"')
    if topic:
        constraints.append(f'Topic: "{topic}"')

    # Segment count: explicit --parts wins, else the seed's duration choice
    seg_parts = parts or (seed.get("parts") if seed else None)
    if seg_parts:
        constraints.append(f"Exactly {seg_parts} segments (prompt_1 ... prompt_{seg_parts})")

    # Inject the chosen scenario elements
    if seed and seed.get("seed_text"):
        constraints.append(seed["seed_text"])

    # Gather ALL previously covered topics from both history and assets folder
    covered = []

    history = load_history()

    if _IS_EPISODE_MODE:
        # Episode mode: show which episodes are already made
        for ep in history.get("episodes", []):
            title = ep.get('episode_title', '')
            theme = ep.get('theme', '')
            chars = ep.get('characters', '')
            line = f"- {title}"
            if chars:
                line += f" ({chars})"
            if theme:
                line += f" [{theme}]"
            covered.append(line)
    else:
        # Auto mode: show past topics for dedup
        for h in history.get("generated_topics", [])[-50:]:
            title = h.get('episode_title', '')
            theme = h.get('theme', '')
            caption = h.get('caption', '')
            line = f"- {title}"
            if theme:
                line += f" ({theme})"
            if caption:
                line += f" — {caption[:60]}"
            covered.append(line)

    # From assets folder scan (catches anything not in history)
    existing = scan_existing_topics()
    existing_titles = {c.split("(")[0].strip("- ") for c in covered}
    for e in existing:
        if e["episode_title"] not in existing_titles:
            covered.append(f"- {e['episode_title']} ({e.get('theme', '')})")

    if covered:
        constraints.append(
            "ALREADY MADE (do NOT repeat these, create something completely new and different):\n"
            + "\n".join(covered)
        )

    constraints.append(
        "UNIQUENESS RULE: Generate something COMPLETELY DIFFERENT from all listed above. "
        "Different story, different characters, different theme. NEVER repeat."
    )

    if constraints:
        base += "\n\nCONSTRAINTS:\n- " + "\n- ".join(constraints)

    return base


# -- LLM call (Gemini via Vertex AI) --

def _extract_text(result):
    """Extract the real text response from a Gemini API result.
    When thinking is enabled the response has multiple parts:
      parts[0] = thinking/reasoning (thought=True) — SKIP
      parts[1] = the actual text output — USE THIS
    Falls back to the first part with actual text if no thinking part found.
    """
    parts = result["candidates"][0]["content"]["parts"]
    
    # If only one part, use it
    if len(parts) == 1:
        return parts[0].get("text", "").strip()
    
    # Multiple parts: skip thinking parts, find the JSON/text part
    for part in parts:
        if part.get("thought", False):
            continue
        text = part.get("text", "").strip()
        if text:
            return text
    
    # Fallback: return last part's text
    return parts[-1].get("text", "").strip()


def _parse_json_response(result):
    """Extract text from result and parse as JSON, stripping any markdown fences."""
    raw = _extract_text(result)
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    try:
        parsed = json.loads(raw)
        # If LLM returned an array with one element, unwrap it
        if isinstance(parsed, list) and len(parsed) == 1:
            log.info("Unwrapping single-element JSON array")
            return parsed[0]
        return parsed
    except json.JSONDecodeError as e:
        # "Extra data" means the first JSON object is valid but there's trailing text.
        # Use JSONDecoder.raw_decode to extract just the first complete JSON value.
        if "Extra data" in str(e):
            log.warning("JSON has trailing extra data — extracting first JSON object")
            try:
                decoder = json.JSONDecoder()
                parsed, _ = decoder.raw_decode(raw)
                if isinstance(parsed, list) and len(parsed) == 1:
                    log.info("Unwrapping single-element JSON array")
                    return parsed[0]
                return parsed
            except json.JSONDecodeError:
                pass  # fall through to original error
        # "Unterminated string" means the output was truncated mid-response.
        # Try to recover by removing the incomplete last prompt and closing the JSON.
        if "Unterminated string" in str(e) or "Expecting" in str(e):
            log.warning("JSON appears truncated — attempting recovery by trimming last incomplete prompt")
            try:
                # Find the last complete prompt_N block by walking back from the cut point
                # Strategy: find the last ',' before a '"prompt_' key and cut there,
                # then close the prompts object and the root object.
                cut = raw[:e.pos] if hasattr(e, 'pos') else raw
                # Walk back to find the last complete top-level comma separator
                # between two prompt entries — pattern: }, "prompt_N"
                import re as _re
                last_complete = _re.search(r'(.*\})\s*,\s*"prompt_\d+"', cut, _re.DOTALL)
                if last_complete:
                    repaired = last_complete.group(1) + "}}}"
                    parsed = json.loads(repaired)
                    total = parsed.get("metadata", {}).get("total_parts", "?")
                    have = len(parsed.get("prompts", {}))
                    log.warning("Truncation recovery: kept %d/%s prompts", have, total)
                    return parsed
            except Exception:
                pass  # fall through to original error
        log.error("JSON parse failed. Raw text (first 500 chars): %s", raw[:500])
        raise


def call_llm(prompt):
    """Call Gemini model via REST API to generate video script JSON.
    Uses LLM_BACKEND setting to determine endpoint."""
    creds = get_credentials()

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 1.0,
            "maxOutputTokens": 32768,
            "responseMimeType": "application/json",
            "thinkingConfig": {
                "thinkingBudget": LLM_THINKING_BUDGET,
            },
        },
    }

    last_error = None

    # 1. Try Vertex AI (only if backend is "vertex_ai" or "auto")
    if LLM_BACKEND in ("vertex_ai", "auto"):
        vertex_url = (
            f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
            f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
            f"publishers/google/models/{LLM_MODEL}:generateContent"
        )
        try:
            log.info("Trying Vertex AI endpoint...")
            headers = {
                "Authorization": f"Bearer {creds.token}",
                "Content-Type": "application/json",
            }
            resp = requests.post(vertex_url, headers=headers, json=body, timeout=300)
            resp.raise_for_status()
            result = resp.json()
            return _parse_json_response(result)
        except requests.exceptions.HTTPError as e:
            last_error = e
            log.warning("Vertex AI endpoint failed: %s", e)
            if LLM_BACKEND == "vertex_ai":
                raise RuntimeError(f"Vertex AI LLM call failed: {e}") from e

    # 2. Fallback: cycle through all GEMINI_API_KEYS
    if not GEMINI_API_KEYS:
        raise RuntimeError(
            "Vertex AI endpoint failed and GEMINI_API_KEYS is empty in constants.py. "
            "Get an API key at https://aistudio.google.com/apikey and add it to GEMINI_API_KEYS."
        )
    genai_base = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{LLM_MODEL}:generateContent"
    )
    for idx, api_key in enumerate(GEMINI_API_KEYS):
        try:
            log.info(
                "Trying generativelanguage.googleapis.com with API key %d/%d...",
                idx + 1, len(GEMINI_API_KEYS),
            )
            url_with_key = f"{genai_base}?key={api_key}"
            headers = {"Content-Type": "application/json"}
            resp = requests.post(url_with_key, headers=headers, json=body, timeout=300)
            resp.raise_for_status()
            result = resp.json()
            return _parse_json_response(result)
        except requests.exceptions.HTTPError as e:
            last_error = e
            status = e.response.status_code if e.response is not None else "?"
            log.warning(
                "API key %d/%d failed (HTTP %s) — %s",
                idx + 1, len(GEMINI_API_KEYS), status, e,
            )
            if status not in (429, 403, 401, 503, 500, 502):
                # Non-transient error (e.g. bad request) — no point trying other keys
                break
            continue

    # All keys failed — try fallback model before waiting
    if LLM_MODEL_FALLBACK and LLM_MODEL_FALLBACK != LLM_MODEL:
        log.warning("All keys failed on %s. Trying fallback model: %s", LLM_MODEL, LLM_MODEL_FALLBACK)
        fallback_base = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/{LLM_MODEL_FALLBACK}:generateContent"
        )
        for idx, api_key in enumerate(GEMINI_API_KEYS):
            try:
                log.info("Fallback model — API key %d/%d...", idx + 1, len(GEMINI_API_KEYS))
                url_with_key = f"{fallback_base}?key={api_key}"
                headers = {"Content-Type": "application/json"}
                resp = requests.post(url_with_key, headers=headers, json=body, timeout=300)
                resp.raise_for_status()
                result = resp.json()
                return _parse_json_response(result)
            except requests.exceptions.HTTPError:
                continue

    # Still failed — wait and retry once more
    log.warning("All API keys rate-limited. Waiting 60s before retry...")
    time.sleep(60)
    for idx, api_key in enumerate(GEMINI_API_KEYS):
        try:
            log.info("Retry after wait — API key %d/%d...", idx + 1, len(GEMINI_API_KEYS))
            url_with_key = f"{genai_base}?key={api_key}"
            headers = {"Content-Type": "application/json"}
            resp = requests.post(url_with_key, headers=headers, json=body, timeout=300)
            resp.raise_for_status()
            result = resp.json()
            return _parse_json_response(result)
        except requests.exceptions.HTTPError:
            continue

    raise RuntimeError(f"All LLM endpoints and API keys failed after retry. Last error: {last_error}")


# -- Secondary LLM call (stronger model for Veo prompt enhancement) --

VEO_ENHANCE_SYSTEM = """\
You are an expert Veo 3.1 cinematic prompt writer specialising in Pixar-quality animated videos.
You will receive a raw veo_prompt and the character dialogue. Your job is to REWRITE the veo_prompt
to be more cinematic, more physically action-driven, and generate cleaner audio.

RULES:
1. Keep the same story/scene — do not change what happens, only make it richer.
2. SEQUENCE structure: always use "SEQUENCE: First, ... Then ..."
3. Maximum 2 KEY ACTIONS per SEQUENCE. Not 3, not 5. Just 2. Veo renders 2 actions cleanly.
4. TOTAL veo_prompt length: 80-120 words MAXIMUM. Shorter = higher quality. NEVER exceed 130 words.
   If the input prompt is longer, you MUST condense it while keeping the core action and emotion.
5. Every action must be PHYSICAL — characters run, crash, dive, scrub, build, explode, transform.
6. Audio line MUST follow this EXACT format (critical for clear speech):
   Audio: ONE single [VOICE DESCRIPTION - gender + tone, e.g. "warm female", "deep male"] [LANGUAGE]
   voice speaking clearly and slowly with dramatic emotion as [CHARACTER NAME]:
   '[DIALOGUE — max 15 words IN NATIVE SCRIPT]'. Background: [SFX]. Quiet [MUSIC].
   IMPORTANT: Only ONE voice speaks in this clip. No overlapping voices.
   
   CRITICAL — NATIVE SCRIPT PRESERVATION:
   - If input dialogue is in Devanagari (Hindi/Nepali native script), output MUST be in Devanagari.
   - If input dialogue is in romanized text, output MUST be in romanized text.
   - NEVER convert between scripts. Preserve the exact script used in the input dialogue.
   - Detect the language from the input (Hindi or Nepali) and mention it in [LANGUAGE] field.
   
7. For Nepali: use ONLY Nepali body terms in dialogue (ragat nali, swaas, maas banaune samagri, etc.)
   For Hindi: use Hindi/Hinglish body terms naturally.
8. If dialogue > 15 words, SHORTEN it to 12-15 words while keeping meaning AND script. Shorter = clearer audio.
9. ONE camera movement per shot (zoom, follow, or close-up — pick ONE).
10. Lighting description is MANDATORY. Describe mood + any transition.
11. ONE character in visual focus per shot. Others can be in blurred background.
12. Background music must be described as QUIET or SUBTLE — loud music drowns out speech.
13. Output ONLY the rewritten veo_prompt text. No explanation, no JSON, no markdown.

BANNED in veo_prompts: text on objects, split-screen, labels, gauges with text, "8 seconds", "AI generated"

VISUAL CONTINUITY (CRITICAL — each video has 5 parts generated separately by Veo):
14. NEVER remove character visual descriptions (shape, color, texture, accessories) — keep them ALL.
15. If the prompt starts with "Continuing from the previous scene" — KEEP that phrase and the environment anchor.
16. Re-describe the character with IDENTICAL visual details every time — same shape, color, texture, accessories.
17. Re-describe the environment with the SAME landmark every time so Veo generates consistent-looking parts.
18. The goal is that all 5 parts look like they come from ONE continuous video, not 5 separate clips.
"""


def call_llm_pro(prompt_text):
    """Call the stronger enhancer model via Gemini API keys (plain text response)."""
    creds = get_credentials()

    vertex_url = (
        f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
        f"publishers/google/models/{VEO_PROMPT_ENHANCER_MODEL}:generateContent"
    )
    body = {
        "contents": [
            {"role": "user", "parts": [{"text": VEO_ENHANCE_SYSTEM + "\n\n" + prompt_text}]}
        ],
        "generationConfig": {
            "temperature": 0.9,
            "maxOutputTokens": 2048,
            "thinkingConfig": {
                "thinkingBudget": VEO_PROMPT_ENHANCER_THINKING_BUDGET,
            },
        },
    }

    last_error = None

    # 1. Try Vertex AI
    try:
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
        }
        resp = requests.post(vertex_url, headers=headers, json=body, timeout=300)
        resp.raise_for_status()
        result = resp.json()
        return _extract_text(result)
    except requests.exceptions.HTTPError as e:
        last_error = e
        log.warning("Enhancer Vertex AI failed: %s", e)

    # 2. Cycle through API keys
    genai_base = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{VEO_PROMPT_ENHANCER_MODEL}:generateContent"
    )
    for idx, api_key in enumerate(GEMINI_API_KEYS):
        try:
            log.info("Enhancer API key %d/%d...", idx + 1, len(GEMINI_API_KEYS))
            resp = requests.post(
                f"{genai_base}?key={api_key}",
                headers={"Content-Type": "application/json"},
                json=body,
                timeout=300,
            )
            resp.raise_for_status()
            result = resp.json()
            return _extract_text(result)
        except requests.exceptions.HTTPError as e:
            last_error = e
            status = e.response.status_code if e.response is not None else "?"
            log.warning("Enhancer API key %d/%d failed (HTTP %s)", idx + 1, len(GEMINI_API_KEYS), status)
            if status not in (429, 403, 401):
                break

    log.warning("Veo prompt enhancer failed, keeping original. Last error: %s", last_error)
    return None  # Caller will keep the original veo_prompt on failure


def enhance_veo_prompts(data):
    """Second LLM pass: rewrite each veo_prompt using the stronger enhancer model."""
    prompts = data.get("prompts", {})
    total = len(prompts)
    for idx, (key, p) in enumerate(sorted(prompts.items())):
        log.info("Enhancing veo_prompt %d/%d (part %s)...", idx + 1, total, key)
        enhance_input = (
            f"CHARACTER: {p.get('character_name', 'unknown')}\n"
            f"EMOTION: {p.get('emotion', '')}\n"
            f"DIALOGUE: {p.get('dialogue', '')}\n"
            f"ORIGINAL VEO PROMPT:\n{p['veo_prompt']}"
        )
        enhanced = call_llm_pro(enhance_input)
        if enhanced:
            p["veo_prompt"] = enhanced
            log.info("  -> Enhanced successfully")
        else:
            log.warning("  -> Kept original (enhancer unavailable)")
    return data


# -- Validation --

def count_words(text):
    return len(text.split())


def trim_dialogue(text, max_words):
    """Trim dialogue to fit within word limit."""
    words = text.split()
    if len(words) <= max_words:
        return text
    log.warning("Trimming dialogue from %d to %d words", len(words), max_words)
    trimmed = " ".join(words[:max_words])
    for end in [". ", "! ", "? "]:
        pos = trimmed.rfind(end)
        if pos > len(trimmed) * 0.5:
            return trimmed[: pos + 1]
    return trimmed.rstrip(".,!? ") + "."


def sync_veo_dialogue(veo_prompt, new_dialogue):
    """Update the dialogue inside a veo_prompt to match the trimmed dialogue."""
    # Try new format: "as CHARACTER_NAME: 'dialogue'"
    pattern_new = r"(as\s+\w[\w\s]*?:\s*')[^']*(')"
    result = re.sub(pattern_new, rf"\g<1>{new_dialogue}\g<2>", veo_prompt, count=1)
    if result != veo_prompt:
        return result
    # Try old format: "speaking with lip-sync: 'dialogue'"
    pattern = r"(speaking with lip-sync:\s*')[^']*(')"
    result = re.sub(pattern, rf"\g<1>{new_dialogue}\g<2>", veo_prompt, count=1)
    if result != veo_prompt:
        return result
    # Try double-quote variants
    pattern2 = r'(as\s+\w[\w\s]*?:\s*")[^"]*(")'
    result = re.sub(pattern2, rf'\g<1>{new_dialogue}\g<2>', veo_prompt, count=1)
    if result != veo_prompt:
        return result
    pattern3 = r'(speaking with lip-sync:\s*")[^"]*(")'
    result = re.sub(pattern3, rf'\g<1>{new_dialogue}\g<2>', veo_prompt, count=1)
    return result


def get_max_words(lang):
    """Get max word limit for the given language."""
    return {"hindi": MAX_WORDS_HINDI, "nepali": MAX_WORDS_NEPALI}.get(lang, MAX_WORDS_HINDI)


def validate(data):
    """Validate and fix dialogue word counts in the generated script."""
    log.info("Validating script...")
    lang = data.get("metadata", {}).get("language", VIDEO_LANGUAGE)
    max_w = get_max_words(lang)
    fixes = 0

    for key in sorted(data["prompts"]):
        p = data["prompts"][key]
        wc = count_words(p["dialogue"])
        name = p.get("character_name", key)

        if wc > max_w:
            fixes += 1
            log.warning("%s: %d words exceeds max %d, trimming", name, wc, max_w)
            p["dialogue"] = trim_dialogue(p["dialogue"], max_w)
            p["veo_prompt"] = sync_veo_dialogue(p["veo_prompt"], p["dialogue"])
        else:
            log.info("%s: %d words - ok", name, wc)

        p["word_count"] = count_words(p["dialogue"])
        p.pop("subtitle_keywords", None)

    data["full_script"] = " ".join(
        data["prompts"][k]["dialogue"] for k in sorted(data["prompts"])
    )

    if fixes == 0:
        log.info("All dialogues within word limits")
    else:
        log.info("Fixed %d dialogue(s)", fixes)

    return data


# -- Content generation (Step 1) --

def generate_content(category=None, topic=None, parts=None):
    """Generate video script via LLM. Episode mode sends episode markdown as prompt guidance."""

    prompt = build_prompt(category, topic, parts)
    log.info("Calling LLM (model=%s)...", LLM_MODEL)
    data = call_llm(prompt)

    # In episode mode, inject the episode key for tracking
    if _IS_EPISODE_MODE:
        ep_key, _ = get_next_episode()
        if ep_key:
            data.setdefault("metadata", {})["_episode_key"] = ep_key

    m = data["metadata"]
    log.info("Generated: %s", m.get("episode_title", "untitled"))
    log.info(
        "  theme=%s format=%s language=%s parts=%d",
        m.get("theme", m.get("format", "?")),
        m.get("video_format", m.get("format", "?")),
        m.get("language", "?"),
        m.get("total_parts", len(data.get("prompts", {}))),
    )
    for ch in data.get("characters", []):
        log.info(
            "  character %d: %s (%s) [%s]",
            ch["character_id"],
            ch["name"],
            ch.get("character_type", "?"),
            ch.get("dominant_emotion", "?"),
        )

    data = validate(data)

    if ENHANCE_VEO_PROMPTS:
        log.info("Running Veo prompt enhancer (model=%s)...", VEO_PROMPT_ENHANCER_MODEL)
        data = enhance_veo_prompts(data)

    return data


# -- Video generation via Veo (Step 2) --

RAI_MAX_RETRIES = 2  # max retries with softened prompt (reduced — unfixable blocks skip this)
MAX_REGENERATION_ATTEMPTS = 5  # max times to generate completely new content on RAI block


def _soften_prompt(veo_prompt, attempt):
    """Aggressively rephrase the prompt to bypass RAI safety filter on retry.

    Strategy: remove potentially triggering words, add 'safe for children'
    framing, simplify action verbs, remove problematic phrases.
    """
    softened = veo_prompt

    # Comprehensive list of trigger words - applied on ALL attempts
    trigger_words = [
        # Violence/aggression
        ("crashes", "arrives"), ("CRASHES", "ARRIVES"),
        ("slams", "places"), ("SLAMS", "PLACES"),
        ("explod", "expand"), ("EXPLOD", "EXPAND"),
        ("attack", "approach"), ("ATTACK", "APPROACH"),
        ("destroy", "transform"), ("DESTROY", "TRANSFORM"),
        ("kills", "stops"), ("KILLS", "STOPS"),
        ("die", "rest"), ("DIE", "REST"),
        ("punches", "taps"), ("punch", "tap"), ("Punches", "Taps"), ("PUNCH", "TAP"),
        ("kicks", "nudges"), ("kick", "nudge"), ("Kicks", "Nudges"), ("KICK", "NUDGE"),
        ("hits", "touches"), ("hit", "touch"), ("Hits", "Touches"), ("HIT", "TOUCH"),
        ("strikes", "touches"), ("strike", "touch"), ("Strikes", "Touches"), ("STRIKE", "TOUCH"),
        ("smashes", "presses"), ("smash", "press"), ("SMASH", "PRESS"),
        ("crushes", "squeezes"), ("crush", "squeeze"), ("CRUSH", "SQUEEZE"),
        ("breaks", "bends"), ("break", "bend"), ("BREAK", "BEND"),
        ("cracks", "opens"), ("crack", "open"), ("CRACK", "OPEN"),
        # Body/medical
        ("bloodstream", "red stream"), ("BLOODSTREAM", "RED STREAM"),
        ("blood", "red fluid"), ("BLOOD", "RED FLUID"),
        ("wound", "mark"), ("WOUND", "MARK"),
        ("injury", "scratch"), ("INJURY", "SCRATCH"),
        ("bleed", "flow"), ("BLEED", "FLOW"),
        # Violent sounds/actions
        ("clanging", "tinging"), ("CLANGING", "TINGING"),
        ("clangs", "tings"), ("clang", "ting"), ("CLANG", "TING"),
        ("bang", "tap sound"), ("BANG", "TAP SOUND"),
        ("boom", "thud"), ("BOOM", "THUD"),
        ("crash", "sound"), ("CRASH", "SOUND"),
        # Intensity/speed
        ("rapidly", "smoothly"), ("RAPIDLY", "SMOOTHLY"),
        ("fast", "swift"), ("FAST", "SWIFT"),
        ("quick", "smooth"), ("QUICK", "SMOOTH"), ("quickly", "smoothly"),
        ("sudden", "gentle"), ("SUDDEN", "GENTLE"), ("suddenly", "gently"),
        ("sharp", "clear"), ("SHARP", "CLEAR"), ("sharply", "clearly"),
        ("violent", "active"), ("VIOLENT", "ACTIVE"),
        ("intense", "bright"), ("INTENSE", "BRIGHT"),
        ("dramatic", "expressive"), ("DRAMATIC", "EXPRESSIVE"),
        # Actions
        ("floating", "moving slowly"), ("diving", "floating"), ("dive", "float"), ("DIVE", "FLOAT"), ("dives", "floats"),
        ("zooms", "moves"), ("ZOOMS", "MOVES"), ("zoom", "move"), ("ZOOM", "MOVE"),
        ("rushing", "gliding"), ("rushes", "glides"), ("rush", "glide"), ("RUSH", "GLIDE"),
        ("dashes", "moves"), ("dash", "move"), ("DASH", "MOVE"),
        ("charges", "goes"), ("charge", "go"), ("CHARGE", "GO"),
        ("leaps", "hops"), ("leap", "hop"), ("LEAP", "HOP"),
        ("jumps", "bounces"), ("jump", "bounce"), ("JUMP", "BOUNCE"),
        ("flexing", "showing"), ("flexes", "shows"), ("flex", "show"), ("FLEX", "SHOW"),
        ("swings", "waves"), ("swing", "wave"), ("SWING", "WAVE"),
        # Tools/weapons
        ("weapon", "tool"), ("WEAPON", "TOOL"),
        ("knife", "utensil"), ("KNIFE", "UTENSIL"),
        ("blade", "edge"), ("BLADE", "EDGE"),
        ("sword", "stick"), ("SWORD", "STICK"),
        ("gun", "pointer"), ("GUN", "POINTER"),
        ("nail", "pin"), ("NAIL", "PIN"),
        ("spike", "point"), ("SPIKE", "POINT"),
        # Fire/heat
        ("forge", "workshop"), ("FORGE", "WORKSHOP"),
        ("fire", "glow"), ("FIRE", "GLOW"),
        ("burning", "warming"), ("burn", "warm"), ("BURN", "WARM"),
        ("flame", "light"), ("FLAME", "LIGHT"),
        ("glowing red", "warm orange"),
        # Emotions
        ("terror", "wonder"), ("TERROR", "WONDER"),
        ("horror", "surprise"), ("HORROR", "SURPRISE"),
        ("panic", "excitement"), ("PANIC", "EXCITEMENT"),
        ("fear", "curiosity"), ("FEAR", "CURIOSITY"),
        ("angry", "determined"), ("ANGRY", "DETERMINED"),
        ("rage", "energy"), ("RAGE", "ENERGY"),
        # Negative states
        ("choke", "breathe"), ("CHOKE", "BREATHE"),
        ("suffocate", "exhale"), ("SUFFOCATE", "EXHALE"),
        ("strangle", "hold"), ("STRANGLE", "HOLD"),
        ("drown", "float"), ("DROWN", "FLOAT"),
        # Combat/conflict
        ("fight", "play"), ("FIGHT", "PLAY"),
        ("battle", "activity"), ("BATTLE", "ACTIVITY"),
        ("combat", "game"), ("COMBAT", "GAME"),
        ("war", "challenge"), ("WAR", "CHALLENGE"),
        # Physical force
        ("squeeze", "hold"), ("SQUEEZE", "HOLD"),
        ("grip", "hold"), ("GRIP", "HOLD"),
        ("grabs", "takes"), ("grab", "take"), ("GRAB", "TAKE"),
        ("snatch", "pick"), ("SNATCH", "PICK"),
        ("throws", "tosses"), ("throw", "toss"), ("THROW", "TOSS"),
        ("hurl", "send"), ("HURL", "SEND"),
    ]

    for old, new in trigger_words:
        softened = softened.replace(old, new)

    # Attempt 1: Just word replacements above
    
    # Attempt 2: Add child-friendly prefix
    if attempt >= 2:
        safe_prefix = "Child-friendly educational animation suitable for all ages. "
        if not softened.startswith("Child-friendly"):
            softened = safe_prefix + softened

    # Attempt 3: Add G-rated prefix + simplify adverbs
    if attempt >= 3:
        extra_safe = "G-rated peaceful content. "
        if not softened.startswith("G-rated"):
            softened = extra_safe + softened
        
        # Remove intensity adverbs
        softened = softened.replace(" forcefully", " gently")
        softened = softened.replace(" aggressively", " actively")
        softened = softened.replace(" violently", " energetically")
        softened = softened.replace(" powerfully", " strongly")
        softened = softened.replace(" hard", " firmly")

    # Attempt 4: Simplify camera movements and remove problematic phrases
    if attempt >= 4:
        # Simplify camera work
        softened = softened.replace("CAMERA ZOOMS RAPIDLY", "camera moves slowly")
        softened = softened.replace("CAMERA ZOOMS", "camera moves")
        softened = softened.replace("ZOOMS RAPIDLY", "moves smoothly")
        softened = softened.replace("close-up", "view")
        
        # Remove phrases about physical contact with screen
        softened = softened.replace("punches the screen playfully", "waves at the viewer")
        softened = softened.replace("taps the screen", "gestures")
        softened = softened.replace("hits the screen", "points")
        
        # Simplify metallic descriptions
        softened = softened.replace("clangs like heavy metal", "sounds like a bell")
        softened = softened.replace("taps his chest, which tings like heavy metal", "taps his chest gently")
        softened = softened.replace("metallic ting", "gentle sound")
        softened = softened.replace("metallic clang", "gentle sound")

    # Attempt 5: Ultimate safety + remove all complex action sequences
    if attempt >= 5:
        ultimate_safe = "Wholesome educational scene for young children. Calm and peaceful. "
        if not softened.startswith("Wholesome"):
            softened = ultimate_safe + softened
        
        # Replace all sparkle/particle effects that might look like impacts
        softened = softened.replace("sending out red metallic sparkles", "with gentle sparkles appearing")
        softened = softened.replace("sparkles", "gentle lights")
        
        # Simplify all transformation language
        softened = softened.replace("transforms", "changes")
        softened = softened.replace("morphs", "shifts")
        
        # Remove blacksmith/forge references entirely
        softened = softened.replace("blacksmith's glowing red forge", "warm cozy workshop")
        softened = softened.replace("Nepali blacksmith's glowing red forge (aaran)", "warm craft workshop")
        
        # Simplify "showing off" actions
        softened = softened.replace("showing off silver metallic biceps", "displaying arms")
        softened = softened.replace("shows both arms", "displays arms")

    log.info("Softened prompt for attempt %d", attempt)
    return softened


def extract_last_frame(video_path):
    """Extract the last frame from a video file as a PNG image using ffmpeg.

    Returns the path to the extracted frame image, or None on failure.
    Used as fallback when extend API fails.
    """
    frame_path = video_path.replace(".mp4", "_lastframe.png")
    try:
        # Get video duration first
        dur_result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, text=True,
        )
        duration = float(dur_result.stdout.strip())
        # Seek to 0.1s before end to get the last frame
        seek_time = max(0, duration - 0.1)

        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(seek_time),
                "-i", video_path,
                "-frames:v", "1",
                "-q:v", "2",
                frame_path,
            ],
            capture_output=True, check=True,
        )

        if os.path.exists(frame_path) and os.path.getsize(frame_path) > 100:
            log.info("Extracted last frame: %s", frame_path)
            return frame_path
        else:
            log.warning("Last frame extraction produced empty file")
            return None
    except Exception as e:
        log.warning("Failed to extract last frame from %s: %s", video_path, e)
        return None


def get_video_duration(video_path):
    """Get video duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, text=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def extend_video_with_prompt(video_path, prompt, part_num):
    """Extend an existing video using Veo's native extend API with a new prompt.

    This is the same functionality as the "Extend" button in the browser UI.
    Takes the existing video and generates a 7-second continuation guided by the prompt.

    Returns the path to the extended video, or None on failure.
    """
    creds = get_credentials()

    endpoint = (
        f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
        f"publishers/google/models/{VEO_MODEL}:predictLongRunning"
    )

    # Read the video and encode as base64
    with open(video_path, "rb") as f:
        video_bytes = base64.b64encode(f.read()).decode("utf-8")

    body = {
        "instances": [
            {
                "prompt": prompt,
                "video": {
                    "bytesBase64Encoded": video_bytes,
                    "mimeType": "video/mp4",
                },
            }
        ],
        "parameters": {
            "sampleCount": 1,
        },
    }

    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }

    log.info("Extending video with part %d prompt (native Veo extend)...", part_num)
    resp = requests.post(endpoint, headers=headers, json=body)
    resp.raise_for_status()
    resp_json = resp.json()
    op_name = resp_json.get("name")
    if not op_name:
        raise RuntimeError(f"No operation name returned for extend: {resp_json}")

    log.info("Extend operation: %s", op_name)

    # Poll for completion
    fetch_url = (
        f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
        f"publishers/google/models/{VEO_MODEL}:fetchPredictOperation"
    )
    elapsed = 0

    while elapsed < MAX_WAIT_SECONDS:
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

        if creds.expired:
            creds.refresh(Request())
            headers["Authorization"] = f"Bearer {creds.token}"

        poll_resp = requests.post(
            fetch_url,
            headers=headers,
            json={"operationName": op_name},
        )
        if poll_resp.status_code != 200:
            log.error("Extend poll failed (%d): %s", poll_resp.status_code, poll_resp.text[:300])
            poll_resp.raise_for_status()
        poll_data = poll_resp.json()

        if poll_data.get("done"):
            log.info("Part %d extend complete (%ds)", part_num, elapsed)
            break

        progress = poll_data.get("metadata", {}).get("progressPercent", "?")
        log.info("Part %d extend: %s%% (%ds elapsed)", part_num, progress, elapsed)
    else:
        raise TimeoutError(f"Part {part_num} extend timed out after {MAX_WAIT_SECONDS}s")

    # Check for errors
    if "error" in poll_data:
        error_info = poll_data["error"]
        log.error("Part %d extend failed: %s", part_num, error_info)
        return None

    # Check for RAI filter
    metadata = poll_data.get("metadata", {})
    if metadata.get("raiMediaFilteredCount", 0) or metadata.get("raiMediaFilteredReasons", []):
        log.warning("Part %d extend blocked by RAI filter", part_num)
        return None

    # Extract extended video
    response_data = poll_data.get("response", {})
    videos = response_data.get("videos", response_data.get("predictions", []))
    if not videos:
        if "generateVideoResponse" in response_data:
            videos = response_data["generateVideoResponse"].get("generatedSamples", [])
        if not videos:
            rai_count = response_data.get("raiMediaFilteredCount", 0)
            if rai_count:
                log.warning("Part %d extend RAI filtered", part_num)
                return None
            log.error("No extended video returned for part %d", part_num)
            return None

    video_b64 = videos[0].get(
        "bytesBase64Encoded",
        videos[0].get("video", {}).get("bytesBase64Encoded", ""),
    )
    if not video_b64:
        log.error("Empty extended video data for part %d", part_num)
        return None

    # Save the extended video — overwrite the input
    with open(video_path, "wb") as f:
        f.write(base64.b64decode(video_b64))

    new_duration = get_video_duration(video_path)
    log.info("Extended video saved: %s (now %.1fs)", video_path, new_duration)
    return video_path


def generate_video(veo_prompt, part_num, out_dir, first_frame_path=None):
    """Submit a Veo generation request, poll until done, save the video file.

    If first_frame_path is provided, uses image-to-video mode (fallback for frame continuity).
    Retries up to RAI_MAX_RETRIES times if the RAI safety filter blocks the video.
    On child safety (58061214) or celebrity (29310472) blocks, fails immediately
    without retrying — these can't be fixed by softening, need fresh content.
    """
    current_prompt = veo_prompt

    for attempt in range(1, RAI_MAX_RETRIES + 1):
        if attempt > 1:
            log.warning("RAI retry %d/%d for part %d — softening prompt...",
                        attempt, RAI_MAX_RETRIES, part_num)
            current_prompt = _soften_prompt(veo_prompt, attempt)

        result = _submit_and_poll_veo(current_prompt, part_num, out_dir, first_frame_path=first_frame_path)
        if result is not None:
            return result

        # _submit_and_poll_veo returned None → RAI filtered
        if attempt < RAI_MAX_RETRIES:
            log.warning("Part %d blocked by RAI filter, will retry with softened prompt...", part_num)
            time.sleep(5)  # brief delay before retry

    raise RAIFilterException(
        f"Part {part_num} blocked by Veo RAI safety filter after {RAI_MAX_RETRIES} attempts."
    )


def _submit_and_poll_veo(veo_prompt, part_num, out_dir, first_frame_path=None):
    """Submit prompt to Veo, poll until done, save video. Returns file path or None if RAI-filtered.

    If first_frame_path is provided, sends it as the first frame image for image-to-video generation.
    """
    creds = get_credentials()

    endpoint = (
        f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
        f"publishers/google/models/{VEO_MODEL}:predictLongRunning"
    )

    params = {
        "aspectRatio": VIDEO_ASPECT_RATIO,
        "durationSeconds": VIDEO_DURATION_SECONDS,
        "personGeneration": VIDEO_PERSON_GENERATION,
        "numberOfVideos": NUMBER_OF_VIDEOS_PER_REQUEST,
        "resolution": "1080p",
    }

    if VEO_SEED is not None:
        params["seed"] = VEO_SEED

    if UPSCALE_4K:
        params["upscale"] = True

    # Build instance — text-to-video or image-to-video
    instance = {"prompt": veo_prompt}

    if first_frame_path and os.path.isfile(first_frame_path):
        # Image-to-video mode: provide first frame
        with open(first_frame_path, "rb") as f:
            img_bytes = base64.b64encode(f.read()).decode("utf-8")
        instance["image"] = {
            "bytesBase64Encoded": img_bytes,
            "mimeType": "image/png",
        }
        log.info("Using first-frame continuity (image-to-video) for part %d", part_num)

    body = {
        "instances": [instance],
        "parameters": params,
    }

    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }

    log.info("Submitting part %d to Veo...", part_num)
    resp = requests.post(endpoint, headers=headers, json=body)
    if not resp.ok:
        log.error("Veo submit HTTP %d — body: %s", resp.status_code, resp.text[:800])
    resp.raise_for_status()
    resp_json = resp.json()
    op_name = resp_json.get("name")
    if not op_name:
        raise RuntimeError(f"No operation name returned: {resp_json}")

    log.info("Operation: %s", op_name)

    # Veo uses fetchPredictOperation to poll long-running operations
    fetch_url = (
        f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
        f"publishers/google/models/{VEO_MODEL}:fetchPredictOperation"
    )
    elapsed = 0

    while elapsed < MAX_WAIT_SECONDS:
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

        if creds.expired:
            creds.refresh(Request())
            headers["Authorization"] = f"Bearer {creds.token}"

        poll_resp = requests.post(
            fetch_url,
            headers=headers,
            json={"operationName": op_name},
        )
        if poll_resp.status_code != 200:
            log.error("Poll failed (%d): %s", poll_resp.status_code, poll_resp.text[:300])
            poll_resp.raise_for_status()
        poll_data = poll_resp.json()

        if poll_data.get("done"):
            log.info("Part %d generation complete (%ds)", part_num, elapsed)
            break

        progress = poll_data.get("metadata", {}).get("progressPercent", "?")
        log.info("Part %d: %s%% (%ds elapsed)", part_num, progress, elapsed)
    else:
        raise TimeoutError(f"Part {part_num} timed out after {MAX_WAIT_SECONDS}s")

    # Extract video bytes
    # Debug: log poll_data keys to understand response structure
    log.info("Part %d poll_data keys: %s", part_num, list(poll_data.keys()))
    
    # Check for errors in the response
    if "error" in poll_data:
        error_info = poll_data["error"]
        error_code = error_info.get("code", -1) if isinstance(error_info, dict) else -1
        error_msg = error_info.get("message", "") if isinstance(error_info, dict) else str(error_info)
        log.error("Part %d failed with error: %s", part_num, error_info)

        # Check for unfixable blocks — these need completely new content, not softening
        # 58061214 = child safety, 29310472/15236754 = celebrity
        unfixable_codes = ["58061214", "29310472", "15236754", "89371032", "49114662"]
        is_unfixable = any(code in error_msg for code in unfixable_codes)

        if is_unfixable:
            log.error("Part %d: UNFIXABLE safety block (support code in message). Need fresh content.", part_num)
            # Raise immediately — softening won't help, need completely new script
            raise RAIFilterException(
                f"Part {part_num} blocked by unfixable safety filter. Need new content. Error: {error_msg[:200]}"
            )

        # Code 3 = content policy block — allow retry with softened prompt
        if error_code == 3 or "third-party content" in error_msg.lower():
            log.warning("Part %d blocked by content policy (code %s), will retry with softened prompt...", part_num, error_code)
            return None  # signal retry like RAI filter
        raise RuntimeError(f"Veo generation failed for part {part_num}: {error_info}")
    
    response_data = poll_data.get("response", {})
    log.info("Part %d response_data keys: %s", part_num, list(response_data.keys()))
    
    # Check for RAI filter at the top level first (might be in metadata or root)
    metadata = poll_data.get("metadata", {})
    top_level_rai_count = metadata.get("raiMediaFilteredCount", 0)
    top_level_rai_reasons = metadata.get("raiMediaFilteredReasons", [])
    if top_level_rai_count or top_level_rai_reasons:
        log.warning(
            "Part %d RAI filtered (found in metadata: count=%s, reasons=%s)",
            part_num, top_level_rai_count, top_level_rai_reasons,
        )
        return None  # signal retry
    
    videos = response_data.get("videos", response_data.get("predictions", []))
    if not videos:
        if "generateVideoResponse" in response_data:
            videos = response_data["generateVideoResponse"].get("generatedSamples", [])
        if not videos:
            # Check if RAI filtered
            rai_count = response_data.get("raiMediaFilteredCount", 0)
            rai_reasons = response_data.get("raiMediaFilteredReasons", [])
            if rai_count or rai_reasons:
                log.warning(
                    "Part %d RAI filtered (count=%s, reasons=%s)",
                    part_num, rai_count, rai_reasons,
                )
                return None  # signal retry
            
            # Better error reporting
            log.error("No videos found in response. Full poll_data structure:")
            log.error("poll_data keys: %s", list(poll_data.keys()))
            log.error("response_data keys: %s", list(response_data.keys()))
            if response_data:
                log.error("response_data content: %s", str(response_data)[:500])
            
            raise RuntimeError(
                f"No video returned for part {part_num}. "
                f"Response keys: {list(response_data.keys())}. "
                f"Poll data keys: {list(poll_data.keys())}"
            )

    video_b64 = videos[0].get(
        "bytesBase64Encoded",
        videos[0].get("video", {}).get("bytesBase64Encoded", ""),
    )
    if not video_b64:
        raise RuntimeError(f"Empty video data for part {part_num}")

    file_name = f"part_{part_num}.mp4"
    file_path = os.path.join(out_dir, file_name)
    with open(file_path, "wb") as f:
        f.write(base64.b64decode(video_b64))

    log.info("Saved %s", file_path)
    return file_path


# -- Video Extension via Veo Extend API --

def extend_video(video_path, prompt, part_num, out_dir, iteration=1):
    """Extend an existing video by 7 seconds using Veo's extend API.

    The video must be MP4, 24fps, 720p/1080p/4K, 9:16 or 16:9.
    Returns the path to the extended video file.
    """
    creds = get_credentials()

    endpoint = (
        f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
        f"publishers/google/models/{VEO_MODEL}:predictLongRunning"
    )

    # Read the video file and encode as base64
    with open(video_path, "rb") as f:
        video_bytes = base64.b64encode(f.read()).decode("utf-8")

    # Build the extension prompt
    extend_prompt = prompt
    if EXTEND_PROMPT_SUFFIX:
        extend_prompt = f"{prompt} {EXTEND_PROMPT_SUFFIX}"

    body = {
        "instances": [
            {
                "prompt": extend_prompt,
                "video": {
                    "bytesBase64Encoded": video_bytes,
                    "mimeType": "video/mp4",
                },
            }
        ],
        "parameters": {
            "sampleCount": 1,
        },
    }

    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }

    log.info("Extending part %d (iteration %d) — adding 7s...", part_num, iteration)
    resp = requests.post(endpoint, headers=headers, json=body)
    resp.raise_for_status()
    resp_json = resp.json()
    op_name = resp_json.get("name")
    if not op_name:
        raise RuntimeError(f"No operation name returned for extend: {resp_json}")

    log.info("Extend operation: %s", op_name)

    # Poll for completion
    fetch_url = (
        f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
        f"publishers/google/models/{VEO_MODEL}:fetchPredictOperation"
    )
    elapsed = 0

    while elapsed < MAX_WAIT_SECONDS:
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

        if creds.expired:
            creds.refresh(Request())
            headers["Authorization"] = f"Bearer {creds.token}"

        poll_resp = requests.post(
            fetch_url,
            headers=headers,
            json={"operationName": op_name},
        )
        if poll_resp.status_code != 200:
            log.error("Extend poll failed (%d): %s", poll_resp.status_code, poll_resp.text[:300])
            poll_resp.raise_for_status()
        poll_data = poll_resp.json()

        if poll_data.get("done"):
            log.info("Part %d extension complete (%ds)", part_num, elapsed)
            break

        progress = poll_data.get("metadata", {}).get("progressPercent", "?")
        log.info("Part %d extend: %s%% (%ds elapsed)", part_num, progress, elapsed)
    else:
        raise TimeoutError(f"Part {part_num} extend timed out after {MAX_WAIT_SECONDS}s")

    # Check for errors
    if "error" in poll_data:
        error_info = poll_data["error"]
        log.error("Part %d extend failed: %s", part_num, error_info)
        raise RuntimeError(f"Veo extend failed for part {part_num}: {error_info}")

    # Extract extended video
    response_data = poll_data.get("response", {})
    videos = response_data.get("videos", response_data.get("predictions", []))
    if not videos:
        if "generateVideoResponse" in response_data:
            videos = response_data["generateVideoResponse"].get("generatedSamples", [])
        if not videos:
            raise RuntimeError(f"No extended video returned for part {part_num}")

    video_b64 = videos[0].get(
        "bytesBase64Encoded",
        videos[0].get("video", {}).get("bytesBase64Encoded", ""),
    )
    if not video_b64:
        raise RuntimeError(f"Empty extended video data for part {part_num}")

    # Save extended video (overwrite the original part file)
    extended_path = video_path  # overwrite in place
    with open(extended_path, "wb") as f:
        f.write(base64.b64decode(video_b64))

    log.info("Extended part %d saved: %s (iteration %d)", part_num, extended_path, iteration)
    return extended_path


def extend_video_iterative(video_path, prompt, part_num, out_dir):
    """Extend a video multiple times based on EXTEND_ITERATIONS setting.

    Each iteration adds 7 seconds. Returns the final extended video path.
    """
    current_path = video_path
    for i in range(1, EXTEND_ITERATIONS + 1):
        try:
            current_path = extend_video(current_path, prompt, part_num, out_dir, iteration=i)
        except Exception as e:
            log.error("Extension iteration %d failed for part %d: %s", i, part_num, e)
            log.warning("Keeping video at current length (iteration %d failed)", i)
            break
    return current_path


# -- TTS audio generation + replacement (Step 2b) --

def _build_ssml(dialogue, emotion, character_type):
    """Build SSML with emotion prosody tags for Google Cloud TTS.

    Adds pitch/rate/volume adjustments based on the character's emotion,
    plus emphasis on exclamatory words and natural pauses at punctuation.
    """
    profile = TTS_EMOTION_PROFILES.get(emotion, TTS_EMOTION_PROFILES.get("default", {}))
    rate = profile.get("rate", "medium")
    pitch = profile.get("pitch", "+0st")
    volume = profile.get("volume", "loud")

    # Add pauses at sentence boundaries for natural flow
    text = dialogue.strip()
    text = text.replace("! ", '! <break time="250ms"/> ')
    text = text.replace("? ", '? <break time="250ms"/> ')
    text = text.replace(". ", '. <break time="200ms"/> ')

    # Wrap exclamatory sentences in emphasis
    # (words before ! get strong emphasis for emotion)
    parts = text.split("!")
    if len(parts) > 1:
        rebuilt = []
        for i, part in enumerate(parts):
            part = part.strip()
            if not part:
                continue
            if i < len(parts) - 1:
                rebuilt.append(f'<emphasis level="strong">{part}!</emphasis>')
            else:
                rebuilt.append(part)
        text = " ".join(rebuilt)

    ssml = (
        f'<speak>'
        f'<prosody rate="{rate}" pitch="{pitch}" volume="{volume}">'
        f'{text}'
        f'</prosody>'
        f'</speak>'
    )
    return ssml


def generate_tts_audio(dialogue, emotion, character_type, out_path):
    """Generate TTS audio using Google Cloud TTS Chirp3-HD with SSML emotion.

    Args:
        dialogue: The text to speak
        emotion: Character emotion (e.g. "Heroic", "Distressed", "Arrogant")
        character_type: Character type key for voice selection (e.g. "food_hero", "villain")
        out_path: Where to save the audio file (.mp3)

    Returns:
        Path to the generated audio file, or None on failure.
    """
    creds = get_credentials()

    voice_name, lang_code = TTS_VOICES.get(
        character_type,
        TTS_VOICES.get("default", ("hi-IN-Chirp3-HD-Achernar", "hi-IN")),
    )

    ssml = _build_ssml(dialogue, emotion, character_type)

    body = {
        "input": {"ssml": ssml},
        "voice": {
            "languageCode": lang_code,
            "name": voice_name,
        },
        "audioConfig": {
            "audioEncoding": "MP3",
            "sampleRateHertz": 24000,
        },
    }

    url = "https://texttospeech.googleapis.com/v1/text:synthesize"
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, headers=headers, json=body)
        resp.raise_for_status()
        audio_b64 = resp.json().get("audioContent", "")
        if not audio_b64:
            log.warning("TTS returned empty audio for: %s...", dialogue[:50])
            return None
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(audio_b64))
        log.info("TTS audio saved: %s (voice=%s, emotion=%s)", out_path, voice_name, emotion)
        return out_path
    except Exception as e:
        log.warning("TTS generation failed: %s", e)
        return None


def replace_audio_with_tts(video_path, tts_path, out_path):
    """Mix TTS audio over video, keeping Veo's original audio as low background.

    Approach:
      - Veo audio is lowered to VEO_BG_VOLUME (keeps ambient SFX/music/emotion)
      - TTS audio is padded/trimmed to match video duration
      - Both are mixed together → clear dialogue + emotional background
    """
    try:
        # Get video duration
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True,
        )
        vid_dur = float(probe.stdout.strip())
    except Exception:
        vid_dur = float(VIDEO_DURATION_SECONDS)

    bg_vol = VEO_BG_VOLUME

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", tts_path,
                "-filter_complex",
                (
                    f"[0:a]volume={bg_vol}[bg];"
                    f"[1:a]apad,atrim=0:{vid_dur}[tts];"
                    f"[bg][tts]amix=inputs=2:duration=first:dropout_transition=2[out]"
                ),
                "-map", "0:v",
                "-map", "[out]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                out_path,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning("ffmpeg mix failed: %s", result.stderr[-300:] if result.stderr else "unknown")
            return None
        log.info("Audio replaced: %s (Veo bg=%.0f%% + TTS)", out_path, bg_vol * 100)
        return out_path
    except Exception as e:
        log.warning("Audio replacement failed: %s", e)
        return None


def apply_tts_to_part(video_path, part_num, prompt_data, out_dir):
    """Full TTS pipeline for a single video part:
    1. Generate TTS from dialogue + emotion
    2. Mix over video
    3. Replace original file with mixed version

    Returns the final video path (mixed or original if TTS failed).
    """
    dialogue = prompt_data.get("dialogue", "")
    emotion = prompt_data.get("emotion", "default")
    char_type = prompt_data.get("content_type", "default")  # Try content_type first

    # Determine character_type from the characters list if available
    char_name = prompt_data.get("character_name", "")
    # character_type is set during pipeline from the characters array
    char_type = prompt_data.get("_character_type", char_type)

    if not dialogue.strip():
        log.info("Part %d: no dialogue, skipping TTS", part_num)
        return video_path

    tts_path = os.path.join(out_dir, f"tts_{part_num}.mp3")
    mixed_path = os.path.join(out_dir, f"part_{part_num}_mixed.mp4")

    # Step 1: Generate TTS
    tts_file = generate_tts_audio(dialogue, emotion, char_type, tts_path)
    if not tts_file:
        log.warning("Part %d: TTS failed, keeping Veo audio", part_num)
        return video_path

    # Step 2: Mix audio
    mixed = replace_audio_with_tts(video_path, tts_file, mixed_path)
    if not mixed:
        log.warning("Part %d: audio mix failed, keeping Veo audio", part_num)
        return video_path

    # Step 3: Replace original with mixed version
    os.replace(mixed_path, video_path)
    log.info("Part %d: Veo audio replaced with TTS+bg mix", part_num)
    return video_path


# -- Combine videos with ffmpeg (Step 3) --

def combine_videos(out_dir, part_count):
    """Concatenate all parts into a single video using ffmpeg."""
    log.info("Combining %d parts with ffmpeg...", part_count)
    concat_list = os.path.join(out_dir, "_concat.txt")

    with open(concat_list, "w") as f:
        for i in range(1, part_count + 1):
            p = os.path.join(out_dir, f"part_{i}.mp4")
            if os.path.exists(p):
                f.write(f"file '{os.path.abspath(p)}'\n")

    final_path = os.path.join(out_dir, "video.mp4")

    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", final_path],
        capture_output=True,
    )

    if result.returncode != 0:
        log.warning("Stream copy failed, re-encoding...")
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "128k", final_path,
            ],
            check=True,
            capture_output=True,
        )

    os.remove(concat_list)

    try:
        dur_result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                final_path,
            ],
            capture_output=True,
            text=True,
        )
        duration = f"{float(dur_result.stdout.strip()):.1f}s"
    except Exception:
        duration = "unknown"

    log.info("Combined video: %s (duration: %s)", final_path, duration)
    return final_path


# -- Main pipeline --

def run_pipeline_with_retry(category=None, topic=None, parts=None, dry_run=False, from_file=None, resume=None):
    """Run pipeline. On RAI filter, keep the folder and try fresh content (new LLM generation)."""
    if resume:
        return run_pipeline(category, topic, parts, dry_run, from_file, resume)

    for attempt in range(1, 4):
        try:
            if attempt > 1:
                log.info("Retry attempt %d/3 — generating fresh content...", attempt)
            return run_pipeline(category, topic, parts, dry_run, from_file, resume)
        except RAIFilterException as e:
            log.error("RAI filter blocked (attempt %d): %s", attempt, e)
            if attempt < 3:
                log.info("Keeping folder. Trying again with new content...")
                time.sleep(5)
            else:
                log.error("Failed after 3 attempts. All folders preserved.")
                raise RuntimeError(f"RAI filter blocked after 3 fresh attempts. Error: {e}") from e


def run_pipeline(category=None, topic=None, parts=None, dry_run=False, from_file=None, resume=None):
    """Run the full pipeline: generate script -> create videos -> combine.

    If resume is set to an existing output directory, skip parts that already have
    part_N.mp4 files and only generate missing ones, then re-combine.
    """

    log.info("=" * 50)
    log.info("VIDEO PIPELINE START")
    log.info("=" * 50)
    log.info("Veo model: %s", VEO_MODEL)
    log.info("Language: %s", VIDEO_LANGUAGE)
    log.info("Upscale 4K: %s", UPSCALE_4K)
    if EXTEND_ENABLED:
        log.info("Extend: ENABLED (%d iterations = +%ds per part)", EXTEND_ITERATIONS, EXTEND_ITERATIONS * 7)
    else:
        log.info("Extend: disabled")
    log.info("Frame continuity: %s", "ENABLED (last frame → first frame)" if FRAME_CONTINUITY else "disabled")
    log.info("TTS audio: %s", "ENABLED (Chirp3-HD)" if TTS_ENABLED else "DISABLED (Veo native)")

    # Step 1: Get script data
    if resume:
        # Resume mode: load from the existing output directory
        out_dir = resume.rstrip("/")
        meta_path = os.path.join(out_dir, "metadata.json")
        if not os.path.exists(meta_path):
            raise FileNotFoundError(f"No metadata.json found in {out_dir}")
        log.info("RESUME MODE: loading from %s", out_dir)
        with open(meta_path) as f:
            data = json.load(f)
        data = validate(data)
    elif from_file:
        log.info("Loading script from file: %s", from_file)
        with open(from_file) as f:
            data = json.load(f)
        data = validate(data)
    else:
        data = generate_content(category, topic, parts)

    # Build output directory
    episode = data["metadata"]["episode_title"]
    slug = re.sub(r"[^\w\s-]", "", episode)
    slug = re.sub(r"\s+", "_", slug).strip("_")[:60]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not resume:
        out_dir = os.path.join(get_assets_dir(), f"{slug}_{ts}")
    os.makedirs(out_dir, exist_ok=True)

    # Save metadata, caption, script, prompts
    with open(os.path.join(out_dir, "metadata.json"), "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    with open(os.path.join(out_dir, "caption.txt"), "w") as f:
        f.write(data["caption"])

    with open(os.path.join(out_dir, "full_script.txt"), "w") as f:
        f.write(data["full_script"])

    with open(os.path.join(out_dir, "prompts.txt"), "w") as f:
        for k in sorted(data["prompts"]):
            p = data["prompts"][k]
            f.write(f"{'=' * 40}\n")
            f.write(f"{k} - {p['character_name']} [{p['emotion']}] ({p.get('segment_type', '?')})\n")
            f.write(f"Words: {p.get('word_count', '?')}\n")
            f.write(f"Dialogue: {p['dialogue']}\n")
            f.write(f"Action: {p.get('action', 'N/A')}\n")
            f.write(f"Veo prompt:\n{p['veo_prompt']}\n\n")

    log.info("Output directory: %s", out_dir)

    if dry_run:
        log.info("DRY RUN - skipping video generation")
        log.info("Caption: %s", data["caption"][:200])
        lang = data["metadata"].get("language", VIDEO_LANGUAGE)
        max_w = get_max_words(lang)
        for k in sorted(data["prompts"]):
            p = data["prompts"][k]
            wc = p.get("word_count", count_words(p["dialogue"]))
            status = "OK" if wc <= max_w else "OVER LIMIT"
            log.info(
                "  %s [%s] (%s) - %dw - %s",
                p["character_name"],
                p["emotion"],
                p.get("segment_type", "?"),
                wc,
                status,
            )
            log.info("    \"%s\"", p["dialogue"][:100])
            myth = p.get("myth_busted", "none")
            if myth and myth != "none":
                log.info("    MYTH BUSTED: %s", myth[:120])
        # Don't mark episodes as done in dry-run (no video was actually made)
        if not _IS_EPISODE_MODE:
            add_to_history(data)
        return out_dir

    # Step 2: Generate each video part
    keys = sorted(data["prompts"])
    total = len(keys)

    # Build character_id -> character_type lookup for TTS voice selection
    char_type_map = {}
    for ch in data.get("characters", []):
        cid = ch.get("character_id")
        if cid is not None:
            char_type_map[cid] = ch.get("character_type", "default")

    for idx, k in enumerate(keys, 1):
        p = data["prompts"][k]
        log.info("-" * 40)
        log.info(
            "Part %d/%d: %s [%s] (%dw)",
            idx, total,
            p["character_name"],
            p["emotion"],
            p.get("word_count", 0),
        )
        log.info("Dialogue: \"%s\"", p["dialogue"][:80])

        # Skip already-generated parts in resume mode
        existing_file = os.path.join(out_dir, f"part_{idx}.mp4")
        if resume and os.path.exists(existing_file) and os.path.getsize(existing_file) > 1000:
            log.info("Part %d already exists (%s), skipping", idx, existing_file)
            continue

        # Frame continuity: Part 2+ ALWAYS extends from previous part (never independent)
        if FRAME_CONTINUITY and idx > 1:
            chain_file = os.path.join(out_dir, "_chain.mp4")
            if os.path.exists(chain_file):
                chain_duration = get_video_duration(chain_file)
                if chain_duration <= 30.0:
                    log.info("Extending chain (%.1fs) with part %d prompt...", chain_duration, idx)
                    extended = extend_video_with_prompt(chain_file, p["veo_prompt"], idx)
                    if extended:
                        # Save intermediate chain state
                        chain_part_file = os.path.join(out_dir, f"chain_part{idx}.mp4")
                        import shutil as _shutil
                        _shutil.copy2(chain_file, chain_part_file)
                        log.info("Part %d chained (chain now %.1fs), saved %s",
                                 idx, get_video_duration(chain_file), chain_part_file)
                        continue
                    else:
                        # Extend failed — fallback: extract last frame and do image-to-video
                        log.warning("Extend failed for part %d. Falling back to last-frame image-to-video...", idx)
                        last_frame = extract_last_frame(chain_file)
                        if last_frame:
                            generate_video(p["veo_prompt"], idx, out_dir, first_frame_path=last_frame)
                            # Update chain with the new part
                            new_part = os.path.join(out_dir, f"part_{idx}.mp4")
                            if os.path.exists(new_part):
                                chain_part_file = os.path.join(out_dir, f"chain_part{idx}.mp4")
                                import shutil as _shutil
                                _shutil.copy2(new_part, chain_part_file)
                                log.info("Part %d generated via last-frame fallback, saved %s", idx, chain_part_file)
                            # Clean up extracted frame
                            if os.path.exists(last_frame):
                                os.remove(last_frame)
                            continue
                        else:
                            log.error("Could not extract last frame. Stopping.")
                            raise RAIFilterException(f"Part {idx} extend failed and last-frame extraction failed.")
                else:
                    # Chain too long — use last frame of chain as image-to-video
                    log.warning("Chain too long (%.1fs > 30s). Using last frame for part %d...", chain_duration, idx)
                    last_frame = extract_last_frame(chain_file)
                    if last_frame:
                        generate_video(p["veo_prompt"], idx, out_dir, first_frame_path=last_frame)
                        new_part = os.path.join(out_dir, f"part_{idx}.mp4")
                        if os.path.exists(new_part):
                            chain_part_file = os.path.join(out_dir, f"chain_part{idx}.mp4")
                            import shutil as _shutil
                            _shutil.copy2(new_part, chain_part_file)
                        if os.path.exists(last_frame):
                            os.remove(last_frame)
                        continue
                    else:
                        log.error("Could not extract last frame. Stopping.")
                        raise RAIFilterException(f"Part {idx} last-frame extraction failed.")

        # Normal generation (text-to-video or image-to-video with reference)
        first_frame = None

        # Auto-detect reference image for Part 1 only
        if idx == 1:
            # Check for reference_style.png/jpeg in genera folder
            for ext in (".png", ".jpeg", ".jpg"):
                ref_style = _GENERA_DIR / f"reference_style{ext}"
                if ref_style.exists():
                    first_frame = str(ref_style)
                    log.info("Using reference_style image for Part 1: %s", ref_style.name)
                    break
            # Also check episode-specific references (episode_X_rN format)
            if not first_frame and _IS_EPISODE_MODE:
                ep_key, _ = get_next_episode()
                if ep_key:
                    for ext in (".jpeg", ".jpg", ".png"):
                        ref_path = _GENERA_DIR / f"{ep_key}_r{idx}{ext}"
                        if ref_path.exists():
                            first_frame = str(ref_path)
                            log.info("Using episode reference image: %s", ref_path.name)
                            break

        generate_video(p["veo_prompt"], idx, out_dir, first_frame_path=first_frame)

        # If this is part 1 and frame continuity is on, start the chain file
        if FRAME_CONTINUITY and idx == 1:
            chain_file = os.path.join(out_dir, "_chain.mp4")
            import shutil as _shutil
            _shutil.copy2(existing_file, chain_file)
            # Also save as chain_part1
            _shutil.copy2(existing_file, os.path.join(out_dir, "chain_part1.mp4"))

        # Step 2a: Extend video duration if enabled (separate from frame continuity)
        if EXTEND_ENABLED:
            video_file = os.path.join(out_dir, f"part_{idx}.mp4")
            extend_video_iterative(video_file, p["veo_prompt"], idx, out_dir)

        # Step 2b: Replace audio with clear TTS
        if TTS_ENABLED:
            # Inject character_type from the characters array
            cid = p.get("character_id")
            p["_character_type"] = char_type_map.get(cid, "default")
            video_file = os.path.join(out_dir, f"part_{idx}.mp4")
            apply_tts_to_part(video_file, idx, p, out_dir)

    # Step 3: Combine / Finalize
    if FRAME_CONTINUITY:
        chain_file = os.path.join(out_dir, "_chain.mp4")
        if os.path.exists(chain_file):
            chain_duration = get_video_duration(chain_file)
            final_path = os.path.join(out_dir, "video.mp4")
            # Check if any parts were generated independently (after chain limit)
            independent_parts = []
            for i in range(1, total + 1):
                pf = os.path.join(out_dir, f"part_{i}.mp4")
                # part_1 is always independent (it started the chain), skip it
                if i == 1:
                    continue
                if os.path.exists(pf) and os.path.getsize(pf) > 1000:
                    independent_parts.append(pf)

            if independent_parts:
                # Concat chain + independent parts
                concat_list = os.path.join(out_dir, "_concat.txt")
                with open(concat_list, "w") as f:
                    f.write(f"file '{os.path.abspath(chain_file)}'\n")
                    for ip in independent_parts:
                        f.write(f"file '{os.path.abspath(ip)}'\n")
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", final_path],
                    capture_output=True,
                )
                os.remove(concat_list)
                log.info("Frame continuity: chain (%.1fs) + %d independent parts combined",
                         chain_duration, len(independent_parts))
            else:
                # Chain file IS the final video
                import shutil as _shutil
                _shutil.copy2(chain_file, final_path)
                log.info("Frame continuity: chain is final video (%.1fs)", chain_duration)

            # Clean up chain file
            os.remove(chain_file)
            final = final_path
        else:
            # No chain was created — fall back to normal combine
            final = combine_videos(out_dir, total)
    else:
        final = combine_videos(out_dir, total)

    add_to_history(data)

    log.info("=" * 50)
    log.info("PIPELINE COMPLETE")
    log.info("=" * 50)
    log.info("Output: %s", out_dir)
    log.info("Parts: %d | Combined: %s", total, final)
    log.info("Caption: %s", data["caption"][:120])

    return out_dir


def cleanup_incomplete_directories():
    """Remove all directories in get_assets_dir() that don't contain video.mp4.
    
    This ensures we only count successfully completed videos towards RUN_UNTIL target.
    """
    if not os.path.exists(get_assets_dir()):
        return
    
    removed_count = 0
    for dirname in os.listdir(get_assets_dir()):
        dirpath = os.path.join(get_assets_dir(), dirname)
        
        # Skip if not a directory
        if not os.path.isdir(dirpath):
            continue
        
        # Check if video.mp4 exists
        video_path = os.path.join(dirpath, "video.mp4")
        if not os.path.exists(video_path):
            log.warning("Removing incomplete directory (no video.mp4): %s", dirname)
            try:
                shutil.rmtree(dirpath)
                removed_count += 1
            except Exception as e:
                log.error("Failed to remove directory %s: %s", dirname, e)
    
    if removed_count > 0:
        log.info("Cleaned up %d incomplete directories", removed_count)
    else:
        log.info("All directories contain video.mp4 - no cleanup needed")




def main():
    parser = argparse.ArgumentParser(description="Video generation pipeline (Veo 3.1)")
    parser.add_argument("--category", "-c", help="Video category")
    parser.add_argument("--topic", "-t", help="Video topic")
    parser.add_argument("--parts", "-p", type=int, help="Number of segments (4-6)")
    parser.add_argument("--dry-run", "-d", action="store_true", help="Generate script only, skip video generation")
    parser.add_argument("--from-file", "-f", help="Load script from JSON file instead of generating")
    parser.add_argument("--resume", "-r", help="Resume from an existing output directory (re-generates missing parts)")
    parser.add_argument("--batch", "-b", type=int, default=1, help="Number of videos to generate in batch")
    parser.add_argument("--model", "-m", choices=[
        "veo-3.1-generate-001",
        "veo-3.1-fast-generate-001",
        "veo-3.1-lite-generate-001",
    ], help="Override Veo model (default: from constants.py)")
    parser.add_argument("--extend", action="store_true", help="Enable video extension (+7s per part)")
    parser.add_argument("--extend-iterations", type=int, help="Number of extend iterations (each +7s)")
    parser.add_argument("--continue", dest="continue_count", type=int, metavar="N",
                        help="Generate until N total videos exist in assets/ (one by one, then stop)")
    parser.add_argument("--folder", help="Genera folder to use (e.g. genera_asmr, genera_tiny_luv_ep)")
    parser.add_argument("--image", action="store_true",
                        help="Use reference images (episode_X_rN.jpeg) for image-to-video generation")
    args = parser.parse_args()

    # Set genera folder if specified
    if args.folder:
        set_genera_folder(args.folder)
    else:
        # Default to genera_asmr if it exists, otherwise project root
        default_genera = Path(__file__).parent / "genera_asmr"
        if default_genera.exists():
            set_genera_folder(str(default_genera))

    # Apply CLI overrides to module-level constants
    import constants
    if args.model:
        constants.VEO_MODEL = args.model
        globals()["VEO_MODEL"] = args.model
        log.info("Model override: %s", args.model)
    if args.extend:
        constants.EXTEND_ENABLED = True
        globals()["EXTEND_ENABLED"] = True
    if args.extend_iterations:
        constants.EXTEND_ITERATIONS = args.extend_iterations
        globals()["EXTEND_ITERATIONS"] = args.extend_iterations
    if args.image:
        globals()["IMAGE_GENERATION_ENABLED"] = True

    # Clean up incomplete directories before starting
    log.info("Checking for incomplete directories...")
    cleanup_incomplete_directories()

    if args.continue_count:
        # --continue N: generate one by one until N total videos exist
        target = args.continue_count
        log.info("Continue mode: target %d videos in %s", target, get_assets_dir())

        while True:
            current_count = 0
            if os.path.exists(get_assets_dir()):
                current_count = len([d for d in os.listdir(get_assets_dir())
                                    if os.path.isdir(os.path.join(get_assets_dir(), d))])

            log.info("Videos: %d/%d", current_count, target)

            if current_count >= target:
                log.info("Target reached! %d videos exist. Done.", current_count)
                break

            log.info("Generating video %d/%d...", current_count + 1, target)
            try:
                run_pipeline_with_retry(
                    category=args.category,
                    topic=args.topic,
                    parts=args.parts,
                    dry_run=args.dry_run,
                    from_file=args.from_file,
                    resume=args.resume,
                )
            except Exception as e:
                log.error("Video generation failed: %s", e)
                log.warning("Continuing to next video...")

            time.sleep(5)
    else:
        # Default: generate batch count (default 1) then stop
        for i in range(args.batch):
            if args.batch > 1:
                log.info("BATCH %d/%d", i + 1, args.batch)
            run_pipeline_with_retry(
                category=args.category,
                topic=args.topic,
                parts=args.parts,
                dry_run=args.dry_run,
                from_file=args.from_file,
                resume=args.resume,
            )
            if args.batch > 1 and i < args.batch - 1:
                time.sleep(10)


if __name__ == "__main__":
    main()
