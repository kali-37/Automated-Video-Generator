#!/usr/bin/env python3
"""
TikTok Video Uploader.
Scans assets/ for completed videos, uploads them to TikTok,
and tracks uploads in history.json to avoid re-uploading.

Uses TikTok's Content Posting API (requires TikTok Developer App).
Alternatively uses session cookie approach for simpler setup.
"""

import os
import json
import logging
import argparse
import time
from pathlib import Path
from datetime import datetime

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

from constants import (
    TIKTOK_ENABLED,
    TIKTOK_SESSION_ID,
    OUTPUT_BASE_DIR,
    HISTORY_FILE,
)


def load_history():
    """Load upload history."""
    p = Path(__file__).parent / HISTORY_FILE
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {"generated_topics": []}


def save_history(history):
    """Save upload history."""
    p = Path(__file__).parent / HISTORY_FILE
    with open(p, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def get_uploaded_folders_tiktok():
    """Get set of folder names that have already been uploaded to TikTok."""
    history = load_history()
    uploaded = set()
    for entry in history.get("generated_topics", []):
        tt = entry.get("tiktok_upload")
        if tt and tt.get("uploaded"):
            uploaded.add(tt.get("folder", ""))
    return uploaded


def find_uploadable_videos():
    """Find all video folders in assets that have a completed video.mp4."""
    assets_dir = Path(__file__).parent / OUTPUT_BASE_DIR
    uploadable = []
    if not assets_dir.exists():
        return uploadable

    uploaded = get_uploaded_folders_tiktok()

    for folder in sorted(assets_dir.iterdir()):
        if not folder.is_dir():
            continue
        if folder.name in uploaded:
            continue
        video_file = folder / "video.mp4"
        meta_file = folder / "metadata.json"
        caption_file = folder / "caption.txt"
        if video_file.exists() and meta_file.exists():
            uploadable.append({
                "folder": folder.name,
                "video_path": str(video_file),
                "metadata_path": str(meta_file),
                "caption_path": str(caption_file) if caption_file.exists() else None,
            })

    return uploadable


def upload_to_tiktok(video_path, caption, session_id):
    """
    Upload video to TikTok using the unofficial API with session cookie.

    NOTE: This is a basic implementation. TikTok's upload API is complex
    and may require browser automation (e.g., playwright/selenium) for
    reliable uploads. This provides the structure for integration.

    For production use, consider:
    1. TikTok's official Content Posting API (requires approved developer app)
    2. Browser automation with playwright
    3. Third-party services like tikapi.io
    """
    if not session_id:
        raise ValueError("TIKTOK_SESSION_ID not set in constants.py")

    # Check if tiktok-uploader package is available
    try:
        from tiktok_uploader.upload import upload_video as ttu_upload
        from tiktok_uploader.auth import AuthBackend

        # Use the tiktok-uploader library
        log.info("Uploading to TikTok via tiktok-uploader library...")

        # Create cookies list for auth
        cookies = [{"name": "sessionid", "value": session_id, "domain": ".tiktok.com", "path": "/"}]

        auth = AuthBackend(cookies=cookies)
        result = ttu_upload(
            filename=video_path,
            description=caption[:2200],  # TikTok caption limit
            cookies=cookies,
        )
        log.info("TikTok upload result: %s", result)
        return {"success": True, "result": str(result)}

    except ImportError:
        log.warning(
            "tiktok-uploader library not installed. Install with:\n"
            "  uv pip install tiktok-uploader\n"
            "Falling back to manual upload tracking."
        )

        # Fallback: just mark for manual upload
        log.info("TikTok upload skipped (library not available).")
        log.info("Video ready for manual upload: %s", video_path)
        log.info("Caption: %s", caption[:100])
        return {"success": False, "reason": "tiktok-uploader not installed"}


def mark_as_uploaded_tiktok(folder_name, result):
    """Mark a video folder as uploaded to TikTok in history.json."""
    history = load_history()
    for entry in history.get("generated_topics", []):
        if entry.get("episode_title", "") in folder_name.replace("_", " "):
            entry["tiktok_upload"] = {
                "uploaded": result.get("success", False),
                "result": result.get("result", result.get("reason", "")),
                "folder": folder_name,
                "uploaded_at": datetime.now().isoformat(),
            }
            save_history(history)
            return

    # If not found in history, create a new entry
    history["generated_topics"].append({
        "episode_title": folder_name,
        "theme": "",
        "language": "",
        "video_format": "",
        "timestamp": datetime.now().isoformat(),
        "characters": [],
        "tiktok_upload": {
            "uploaded": result.get("success", False),
            "result": result.get("result", result.get("reason", "")),
            "folder": folder_name,
            "uploaded_at": datetime.now().isoformat(),
        },
    })
    save_history(history)


def run_uploader(dry_run=False):
    """Main upload logic: find videos, upload to TikTok, update history."""
    if not TIKTOK_ENABLED:
        log.warning("TikTok upload is DISABLED. Set TIKTOK_ENABLED = True in constants.py")
        return []

    if not TIKTOK_SESSION_ID:
        log.error("TIKTOK_SESSION_ID is empty. Set it in constants.py")
        return []

    videos = find_uploadable_videos()
    if not videos:
        log.info("No new videos to upload to TikTok.")
        return []

    log.info("Found %d video(s) to upload to TikTok", len(videos))

    results = []

    for v in videos:
        # Load metadata
        with open(v["metadata_path"]) as f:
            meta = json.load(f)

        m = meta.get("metadata", {})

        # Build caption
        caption = ""
        if v["caption_path"]:
            with open(v["caption_path"]) as f:
                caption = f.read()
        if not caption:
            caption = m.get("episode_title", v["folder"])

        if dry_run:
            log.info("[DRY RUN] Would upload to TikTok: %s", m.get("episode_title", v["folder"])[:80])
            log.info("  File: %s", v["video_path"])
            log.info("  Caption: %s", caption[:100])
            continue

        try:
            result = upload_to_tiktok(v["video_path"], caption, TIKTOK_SESSION_ID)
            mark_as_uploaded_tiktok(v["folder"], result)
            results.append(result)

            # Rate limit between uploads
            time.sleep(5)
        except Exception as e:
            log.error("Failed to upload %s to TikTok: %s", v["folder"], e)

    log.info("TikTok upload session complete. Processed: %d", len(results))
    return results


def main():
    parser = argparse.ArgumentParser(description="TikTok Video Uploader")
    parser.add_argument("--dry-run", "-d", action="store_true", help="List videos to upload without uploading")
    args = parser.parse_args()

    run_uploader(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
