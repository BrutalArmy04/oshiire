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
from tag import run_tagging

STAGING_DIR = Path("staging")
USER_AGENT = "oshiire:v0.1 (personal saved-feed archiver by /u/BrutalArmy)"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
LINK_ANCHOR_RE = re.compile(r'<a href="([^"]+)">\[link\]</a>')

# Scopes to a gallery-tile div (class order-agnostic via lookaheads, bounded
# to one tag), captures its data-media-id, then requires the SAME id to
# reappear in a nested <img src="https://preview.redd.it/<id>..."> within a
# bounded span -- a tile only counts if both halves agree, so markup drift
# makes this under-detect rather than mis-detect. See fetch_gallery_images.
GALLERY_TILE_RE = re.compile(
    r'<div\s+'
    r'(?=[^>]*\bclass="[^"]*\bgallery-tile\b[^"]*")'
    r'(?=[^>]*\bclass="[^"]*\bgallery-navigation\b[^"]*")'
    r'[^>]*?\bdata-media-id="([A-Za-z0-9]+)"[^>]*>'
    r'.{0,600}?'
    r'<img\b[^>]*\bsrc="https://preview\.redd\.it/\1(\.[A-Za-z0-9]+)(?:\?|")',
    re.DOTALL,
)

# "gallery_post"/"gallery_fetch_failed"/"gallery_parse_error" are retried on
# every ingest run (see retry_skipped_galleries); "gallery_no_images" is a
# settled, confirmed-non-gallery outcome and is never retried.
RETRYABLE_SKIP_REASONS = {"gallery_post", "gallery_fetch_failed", "gallery_parse_error"}


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
    """Returns (kind, reason, image_url). kind is "image", "gallery", or
    "skip". "gallery" carries no reason/image_url -- the caller must fetch
    the permalink to find out what's actually in it (see fetch_gallery_images)."""
    submission_url = extract_submission_url(entry)
    if not submission_url or submission_url == permalink:
        return "skip", "text_or_link_post", None

    parsed_url = urlparse(submission_url)
    if parsed_url.netloc == "v.redd.it":
        return "skip", "video_post", None
    if "/gallery/" in submission_url:
        return "gallery", None, None

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


class GalleryParseError(Exception):
    """Raised when the permalink fetched fine but gallery-tile markup looks
    present-but-broken -- distinct from a confirmed-empty (non-gallery) page,
    so the caller can keep this retryable instead of settling on it forever."""


def fetch_gallery_images(permalink):
    """GETs the plain HTML permalink (not `.json` -- Reddit's json/api
    endpoints return 403 for this User-Agent regardless of post) and
    extracts ordered, de-duplicated (media_id, ext) pairs for each real
    gallery image. `Cookie: over18=1` is the same non-authenticated,
    client-side self-attestation any anonymous browser visitor sets --
    harmless to always send, and required for NSFW posts to return real
    content instead of Reddit's age-gate interstitial.

    Returns:
        list[(media_id, ext)] -- possibly empty, meaning the fetch
        succeeded and the page genuinely has no gallery (removed/video/etc).

    Raises:
        requests.RequestException -- network/timeout/non-2xx.
        GalleryParseError -- fetch succeeded and "gallery-tile" markup is
            present in the page, but no tile matched the structured regex --
            likely markup drift, not a genuine non-gallery page.
    """
    if "old.reddit.com" not in permalink:
        print(f"warning: unexpected permalink host for gallery fetch: {permalink}", file=sys.stderr)

    resp = requests.get(
        permalink, headers={"User-Agent": USER_AGENT, "Cookie": "over18=1"}, timeout=15
    )
    resp.raise_for_status()
    html = resp.text

    seen = {}
    for match in GALLERY_TILE_RE.finditer(html):
        media_id, ext = match.group(1), match.group(2).lower()
        seen.setdefault(media_id, ext)

    if not seen and "gallery-tile" in html:
        raise GalleryParseError("gallery-tile markup present but no tile matched")

    return list(seen.items())


def _build_gallery_entries(post_id, title, subreddit, permalink, fetched_at, images):
    """Downloads each (media_id, ext) in `images` (1-based gallery order) and
    returns {gallery_key: entry} to merge into the manifest. Shared by the
    fresh-RSS path and retry_skipped_galleries so per-image download/entry
    construction isn't duplicated. A per-image download failure just yields a
    download_failed entry for that image -- siblings are unaffected."""
    entries = {}
    for index, (media_id, ext) in enumerate(images, start=1):
        key = f"{post_id}_{index}"
        image_url = f"https://i.redd.it/{media_id}{ext}"
        local_path = STAGING_DIR / f"{post_id}_{index}{ext}"
        base = {
            "post_id": post_id,
            "title": title,
            "subreddit": subreddit,
            "permalink": permalink,
            "image_index": index,
            "fetched_at": fetched_at,
        }
        try:
            download_image(image_url, local_path)
        except requests.RequestException as exc:
            entries[key] = {**base, "status": "download_failed", "reason": str(exc)}
            continue
        entries[key] = {
            **base,
            "image_url": image_url,
            "local_path": str(local_path),
            "status": "pending_review",
        }
    return entries


def _skip_reason(entry):
    """skip_reason if present, else the legacy `reason` field -- back-compat
    for skip records written before the skip_reason rename."""
    return entry.get("skip_reason") or entry.get("reason")


def retry_skipped_galleries(manifest):
    """Re-attempts every status=="skipped" entry whose reason is retryable
    (see RETRYABLE_SKIP_REASONS). Runs automatically on every ingest call --
    self-limiting, since each candidate settles into either expanded per-
    image entries or a new, non-retryable skip_reason after one attempt.
    Returns counts for the summary print."""
    counts = {"retried": 0, "expanded": 0, "still_failed": 0, "confirmed_non_gallery": 0}

    candidate_keys = [
        key for key, entry in manifest.items()
        if entry.get("status") == "skipped" and _skip_reason(entry) in RETRYABLE_SKIP_REASONS
    ]

    for key in candidate_keys:
        entry = manifest[key]
        counts["retried"] += 1
        fetched_at = datetime.now(timezone.utc).isoformat()
        permalink = entry.get("permalink", "")

        try:
            images = fetch_gallery_images(permalink)
        except requests.RequestException as exc:
            print(f"retry: gallery fetch failed for {key}: {exc}", file=sys.stderr)
            entry["skip_reason"] = "gallery_fetch_failed"
            entry.pop("reason", None)
            entry["fetched_at"] = fetched_at
            counts["still_failed"] += 1
            continue
        except GalleryParseError as exc:
            print(f"retry: gallery parse error for {key}: {exc}", file=sys.stderr)
            entry["skip_reason"] = "gallery_parse_error"
            entry.pop("reason", None)
            entry["fetched_at"] = fetched_at
            counts["still_failed"] += 1
            continue

        if not images:
            entry["skip_reason"] = "gallery_no_images"
            entry.pop("reason", None)
            entry["fetched_at"] = fetched_at
            counts["confirmed_non_gallery"] += 1
            continue

        post_id = entry.get("post_id", key)
        gallery_entries = _build_gallery_entries(
            post_id, entry.get("title", ""), entry.get("subreddit", ""),
            permalink, fetched_at, images,
        )
        del manifest[key]
        manifest.update(gallery_entries)
        counts["expanded"] += 1

    return counts


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

    # Post-level dedup keyed on the post_id FIELD (not the dict key), since a
    # gallery post now stores its bare parent id in every one of its N
    # per-image entries' post_id field while its dict keys are suffixed
    # "<post_id>_1", "<post_id>_2", ... -- this is a strict generalization of
    # today's behavior, where key == post_id always.
    known_post_ids = {entry.get("post_id") for entry in manifest.values()}

    downloaded = skipped = failed = 0

    for entry in parsed.entries:
        post_id = entry.get("id")
        if not post_id or post_id in known_post_ids:
            continue
        known_post_ids.add(post_id)

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
                    "skip_reason": reason,
                    "fetched_at": fetched_at,
                }
                skipped += 1
                continue

            if kind == "gallery":
                try:
                    images = fetch_gallery_images(permalink)
                except requests.RequestException as exc:
                    print(f"gallery fetch failed for {post_id}: {exc}", file=sys.stderr)
                    manifest[post_id] = {
                        "post_id": post_id,
                        "title": title,
                        "subreddit": subreddit,
                        "permalink": permalink,
                        "status": "skipped",
                        "skip_reason": "gallery_fetch_failed",
                        "fetched_at": fetched_at,
                    }
                    failed += 1
                    continue
                except GalleryParseError as exc:
                    print(f"gallery parse error for {post_id}: {exc}", file=sys.stderr)
                    manifest[post_id] = {
                        "post_id": post_id,
                        "title": title,
                        "subreddit": subreddit,
                        "permalink": permalink,
                        "status": "skipped",
                        "skip_reason": "gallery_parse_error",
                        "fetched_at": fetched_at,
                    }
                    failed += 1
                    continue

                if not images:
                    manifest[post_id] = {
                        "post_id": post_id,
                        "title": title,
                        "subreddit": subreddit,
                        "permalink": permalink,
                        "status": "skipped",
                        "skip_reason": "gallery_no_images",
                        "fetched_at": fetched_at,
                    }
                    skipped += 1
                    continue

                gallery_entries = _build_gallery_entries(
                    post_id, title, subreddit, permalink, fetched_at, images
                )
                manifest.update(gallery_entries)
                downloaded += sum(
                    1 for e in gallery_entries.values() if e["status"] == "pending_review"
                )
                failed += sum(
                    1 for e in gallery_entries.values() if e["status"] == "download_failed"
                )
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

    retry_counts = retry_skipped_galleries(manifest)

    tagged = run_tagging(manifest)

    save_manifest(manifest)
    print(
        f"downloaded={downloaded} skipped={skipped} failed={failed} tagged={len(tagged)} "
        f"gallery_retried={retry_counts['retried']} gallery_expanded={retry_counts['expanded']}"
    )


if __name__ == "__main__":
    main()
