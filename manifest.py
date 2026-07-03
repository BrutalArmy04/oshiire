"""Manifest.json: the single source of truth for ingestion/review/archive state.

Schema: top-level dict keyed by the stable Reddit fullname (e.g. "t3_abc123"),
so dedup is an O(1) lookup. Each value is a dict with at least:
    post_id, title, subreddit, permalink, status, fetched_at
and, depending on status:
    image_url, local_path        (status == "pending_review")
    reason                       (status in {"skipped", "download_failed"})
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
