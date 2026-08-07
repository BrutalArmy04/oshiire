"""Manifest.json: the single source of truth for ingestion/review/archive state.

Schema: top-level dict keyed by the stable Reddit fullname (e.g. "t3_abc123")
for ordinary entries, so dedup is an O(1) lookup. Gallery posts instead get
one entry per image, keyed "t3_abc123_1", "t3_abc123_2", ... (matching an
`image_index` field) -- for those, the dict key and the `post_id` field
diverge: `post_id` always holds the shared PARENT fullname, never the
suffixed key. Post-level dedup is therefore done by collecting every entry's
`post_id` field, not by dict-key membership (see ingest.py's known_post_ids).

Each value is a dict with at least:
    post_id, title, subreddit, permalink, status, fetched_at
and, depending on status:
    image_url, local_path, image_index   (status == "pending_review", gallery
                                           entries only have image_index)
    skip_reason                          (status == "skipped")
    reason                               (status in {"download_failed", "error"})
skip_reason and reason are distinct fields -- never interchangeable.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MANIFEST_PATH = Path("manifest.json")

DISPLAY_REDDIT_HOST = "www.reddit.com"


def display_permalink(url: str) -> str:
    """Rewrite a reddit permalink's host to www.reddit.com, FOR DISPLAY ONLY.

    Entries reach the manifest from two pipelines that spell the same URL
    differently: ingest.py takes the RSS feed's `old.reddit.com` links, while
    backfill.py builds `www.reddit.com` ones from the CSV export. Only the host
    differs -- the path shape is identical -- so this normalizes what the review
    and resolve screens render, and nothing else.

    The stored value is deliberately left alone. ingest.retry_skipped_galleries
    feeds `entry["permalink"]` straight to fetch_gallery_images, which parses
    gallery-tile markup only old.reddit's HTML emits; a www URL there yields an
    empty image list, which the caller reads as "confirmed non-gallery" and
    settles into a non-retryable skip. That is silent data loss, and the host
    check in fetch_gallery_images is only a stderr warning, so nothing would
    surface it. The stored host is also provenance -- it says which pipeline
    wrote the entry.

    Only reddit.com and its subdomains are touched. redd.it image hosts
    (i.redd.it, preview.redd.it) are a different domain and are left as-is, as
    are non-reddit hosts, empty strings and anything unparseable.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return url
    if not host:
        return url
    host = host.lower()
    if host != "reddit.com" and not host.endswith(".reddit.com"):
        return url
    netloc = DISPLAY_REDDIT_HOST
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_manifest(data: dict, path: Path = MANIFEST_PATH) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)
