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

MANIFEST_PATH = Path("manifest.json")


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
