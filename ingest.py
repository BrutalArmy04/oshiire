"""Slice 0: fetch the owner's Reddit saved feed, download images to staging/,
and record pending_review entries in manifest.json. No AI, no UI, no archiving.
"""
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
from dotenv import load_dotenv

from manifest import load_manifest, save_manifest

STAGING_DIR = Path("staging")
USER_AGENT = "oshiire:v0.1 (personal saved-feed archiver by /u/BrutalArmy)"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
LINK_ANCHOR_RE = re.compile(r'<a href="([^"]+)">\[link\]</a>')


def extract_subreddit(entry, permalink):
    tags = entry.get("tags")
    if tags and tags[0].get("term"):
        return tags[0]["term"].strip()
    match = re.search(r"/r/([^/]+)/", permalink)
    return match.group(1) if match else "unknown"


def extract_submission_url(entry):
    content = entry.get("content")
    html = content[0].get("value", "") if content else entry.get("summary", "")
    match = LINK_ANCHOR_RE.search(html)
    return match.group(1) if match else None


def classify_entry(entry, permalink):
    """Returns (kind, reason, image_url). kind is "image" or "skip"."""
    submission_url = extract_submission_url(entry)
    if not submission_url or submission_url == permalink:
        return "skip", "text_or_link_post", None

    parsed_url = urlparse(submission_url)
    if parsed_url.netloc == "v.redd.it":
        return "skip", "video_post", None
    if "/gallery/" in submission_url:
        return "skip", "gallery_post", None

    ext = Path(parsed_url.path).suffix.lower()
    if parsed_url.netloc == "i.redd.it" or ext in IMAGE_EXTENSIONS:
        return "image", None, submission_url

    return "skip", "unsupported_link_type", None


def fetch_feed(feed_url):
    resp = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def download_image(url, dest_path):
    resp = requests.get(
        url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=30
    )
    resp.raise_for_status()
    with dest_path.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def main():
    load_dotenv()
    feed_url = os.environ["REDDIT_SAVED_FEED_URL"]

    manifest = load_manifest()
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    try:
        parsed = fetch_feed(feed_url)
    except requests.RequestException as exc:
        print(f"Failed to fetch saved feed: {exc}", file=sys.stderr)
        sys.exit(1)

    downloaded = skipped = failed = 0

    for entry in parsed.entries:
        post_id = entry.get("id")
        if not post_id or post_id in manifest:
            continue

        fetched_at = datetime.now(timezone.utc).isoformat()
        try:
            permalink = entry.get("link", "")
            title = entry.get("title", "")
            subreddit = extract_subreddit(entry, permalink)
            kind, reason, image_url = classify_entry(entry, permalink)

            if kind == "skip":
                manifest[post_id] = {
                    "post_id": post_id,
                    "title": title,
                    "subreddit": subreddit,
                    "permalink": permalink,
                    "status": "skipped",
                    "reason": reason,
                    "fetched_at": fetched_at,
                }
                skipped += 1
                continue

            ext = Path(urlparse(image_url).path).suffix or ".jpg"
            local_path = STAGING_DIR / f"{post_id}{ext}"
            try:
                download_image(image_url, local_path)
            except requests.RequestException as exc:
                manifest[post_id] = {
                    "post_id": post_id,
                    "title": title,
                    "subreddit": subreddit,
                    "permalink": permalink,
                    "status": "download_failed",
                    "reason": str(exc),
                    "fetched_at": fetched_at,
                }
                failed += 1
                continue

            manifest[post_id] = {
                "post_id": post_id,
                "title": title,
                "subreddit": subreddit,
                "permalink": permalink,
                "image_url": image_url,
                "local_path": str(local_path),
                "status": "pending_review",
                "fetched_at": fetched_at,
            }
            downloaded += 1

        except Exception as exc:
            manifest[post_id] = {
                "post_id": post_id,
                "status": "error",
                "reason": str(exc),
                "fetched_at": fetched_at,
            }
            failed += 1

    save_manifest(manifest)
    print(f"downloaded={downloaded} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
