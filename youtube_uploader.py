#!/usr/bin/env python3
"""
YouTube Video Uploader.
Scans assets/ for completed videos, uploads them to YouTube,
and tracks uploads in history.json to avoid re-uploading.
"""

import os
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

from constants import (
    YOUTUBE_ENABLED,
    YOUTUBE_CLIENT_SECRETS_FILE,
    YOUTUBE_TOKEN_FILE,
    YOUTUBE_DEFAULT_PRIVACY,
    YOUTUBE_DEFAULT_CATEGORY_ID,
    YOUTUBE_DEFAULT_TAGS,
    OUTPUT_BASE_DIR,
    HISTORY_FILE,
)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


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


def get_uploaded_folders():
    """Get set of folder names that have already been uploaded to YouTube."""
    history = load_history()
    uploaded = set()
    for entry in history.get("generated_topics", []):
        yt = entry.get("youtube_upload")
        if yt and yt.get("uploaded"):
            uploaded.add(yt.get("folder", ""))
    return uploaded


def get_youtube_service():
    """Authenticate and return YouTube API service object."""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request as AuthRequest
        from googleapiclient.discovery import build
    except ImportError:
        log.error(
            "Missing dependencies. Install with:\n"
            "  uv pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )
        raise

    creds = None
    token_path = Path(__file__).parent / YOUTUBE_TOKEN_FILE
    secrets_path = Path(__file__).parent / YOUTUBE_CLIENT_SECRETS_FILE

    if not secrets_path.exists():
        raise FileNotFoundError(
            f"YouTube client secrets file not found: {secrets_path}\n"
            "Download it from Google Cloud Console -> APIs & Services -> Credentials"
        )

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(AuthRequest())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def upload_video(youtube, video_path, title, description, tags, category_id, privacy):
    """Upload a single video to YouTube."""
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": title[:100],  # YouTube title max 100 chars
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)

    log.info("Uploading: %s", title[:60])
    log.info("File: %s", video_path)

    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("Upload progress: %d%%", int(status.progress() * 100))

    video_id = response["id"]
    log.info("Upload complete! Video ID: %s", video_id)
    log.info("URL: https://www.youtube.com/watch?v=%s", video_id)
    return video_id


def find_uploadable_videos():
    """Find all video folders in assets that have a completed video.mp4."""
    assets_dir = Path(__file__).parent / OUTPUT_BASE_DIR
    uploadable = []
    if not assets_dir.exists():
        return uploadable

    uploaded = get_uploaded_folders()

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


def mark_as_uploaded(folder_name, video_id):
    """Mark a video folder as uploaded in history.json."""
    history = load_history()
    for entry in history.get("generated_topics", []):
        # Match by folder name or episode title
        if entry.get("episode_title", "") in folder_name.replace("_", " "):
            entry["youtube_upload"] = {
                "uploaded": True,
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
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
        "youtube_upload": {
            "uploaded": True,
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "folder": folder_name,
            "uploaded_at": datetime.now().isoformat(),
        },
    })
    save_history(history)


def run_uploader(dry_run=False):
    """Main upload logic: find videos, upload to YouTube, update history."""
    if not YOUTUBE_ENABLED:
        log.warning("YouTube upload is DISABLED. Set YOUTUBE_ENABLED = True in constants.py")
        return []

    videos = find_uploadable_videos()
    if not videos:
        log.info("No new videos to upload.")
        return []

    log.info("Found %d video(s) to upload", len(videos))

    if not dry_run:
        youtube = get_youtube_service()

    uploaded_ids = []

    for v in videos:
        # Load metadata for title/description
        with open(v["metadata_path"]) as f:
            meta = json.load(f)

        m = meta.get("metadata", {})
        title = m.get("episode_title", v["folder"])
        series = m.get("series_title", "")
        ep_num = m.get("episode_number", "")
        ep_total = m.get("episode_total", "")

        # Build YouTube title
        yt_title = title
        if series and ep_num:
            yt_title = f"{series} Ep {ep_num}/{ep_total}: {title}"

        # Build description
        caption = ""
        if v["caption_path"]:
            with open(v["caption_path"]) as f:
                caption = f.read()

        description = caption
        next_hint = m.get("next_episode_hint", "")
        if next_hint:
            description += f"\n\n🔜 Next: {next_hint}"
        description += "\n\n#Shorts"

        # Tags
        tags = list(set(YOUTUBE_DEFAULT_TAGS + m.get("hashtags", [])))
        tags = [t.lstrip("#") for t in tags]

        if dry_run:
            log.info("[DRY RUN] Would upload: %s", yt_title[:80])
            log.info("  File: %s", v["video_path"])
            log.info("  Tags: %s", ", ".join(tags[:10]))
            continue

        try:
            video_id = upload_video(
                youtube,
                v["video_path"],
                yt_title,
                description,
                tags,
                YOUTUBE_DEFAULT_CATEGORY_ID,
                YOUTUBE_DEFAULT_PRIVACY,
            )
            mark_as_uploaded(v["folder"], video_id)
            uploaded_ids.append(video_id)
        except Exception as e:
            log.error("Failed to upload %s: %s", v["folder"], e)

    log.info("Upload session complete. Uploaded: %d", len(uploaded_ids))
    return uploaded_ids


def main():
    parser = argparse.ArgumentParser(description="YouTube Video Uploader")
    parser.add_argument("--dry-run", "-d", action="store_true", help="List videos to upload without uploading")
    args = parser.parse_args()

    run_uploader(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
