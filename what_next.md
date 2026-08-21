# What's Next — ASMR Video Pipeline

## Current State (Working)
- Pipeline generates realistic ASMR videos (2 parts × 8s = 16s)
- Uses Veo 3.1 Lite (cheapest) with native extend for seamless flow
- LLM: gemini-flash-latest for script generation
- Auto-deduplication via generation_history.json
- RAI-safe prompts (no children, no faces, hands-only)

## Next Steps

### 1. Auto Upload to Platforms
- **Facebook/Instagram Reels**: Use Graph API (need Page access token)
- **TikTok**: Use TikTok Developer API (tiktok_uploader.py exists)
- **YouTube Shorts**: Use YouTube Data API v3 (youtube_uploader.py exists)
- Flow: generate video → upload to all platforms → log in history

### 2. Scheduling / Cron
- Run pipeline on schedule (e.g., 3 videos/day)
- Spread uploads across day for better reach
- `--continue N` flag already supports batch generation

### 3. Analytics Feedback Loop
- Track which videos perform best (views, shares, saves)
- Feed top-performing categories back into prompt selection
- Double down on what works

### 4. A/B Testing Captions
- Generate 2-3 caption variants per video
- Post with different captions on different platforms
- Track which caption style gets more engagement

## File Structure
```
constants.py              — All config (model, keys, settings)
video_pipeline_v31.py     — Main pipeline (generate → Veo → combine)
MASTER_PROMPT_ASMR.md     — LLM prompt for ASMR content
generation_history.json   — Tracks generated topics (dedup)
youtube_uploader.py       — YouTube Shorts upload
tiktok_uploader.py        — TikTok upload
setup_new_account.sh      — GCP project setup script
assets/                   — Generated videos (each in own folder)
```

## Commands
```bash
# Generate 1 video
uv run python video_pipeline_v31.py -m veo-3.1-lite-generate-001

# Generate until 20 total videos exist
uv run python video_pipeline_v31.py -m veo-3.1-lite-generate-001 --continue 20

# Dry run (script only, no Veo)
uv run python video_pipeline_v31.py -m veo-3.1-lite-generate-001 --dry-run
```
