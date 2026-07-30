"""Dead-image "tombstone" detection: shared matcher + staging sweep tool.

Some image hosts serve a fixed "this image was removed" placeholder card with
HTTP 200 and an image content-type when the real image is gone (imgur's
`removed.png` is the canonical case). A naive fetch saves that placeholder and
the backfill routes it as a brand-new archive candidate -- so deleted posts
sneak into the review queue as junk.

Because each host's placeholder is a *fixed* image, its perceptual hash (pHash)
is constant. This module maintains a small list of those signatures
(`data/tombstones.json`) and answers one question: does a fetched image match a
known placeholder? Two independent signals, either one is a match:

  1. pHash within `max_distance` Hamming of the signature (primary -- exact for
     a constant image, tolerant of host re-encodes).
  2. exact `width`x`height` AND file size <= `max_size_bytes` (secondary --
     catches a placeholder whose bytes drifted past the pHash threshold).

Reuses hash_index's hashing (never reimplement it) and manifest.py's atomic I/O.

CLI:
    python tombstones.py sweep [--reject]        # scan staging backfill queue
    python tombstones.py add <url|file> --label L [--host H] [--max-distance N]
    python tombstones.py list
"""
import argparse
import io
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Mandated reuse: hashing + Hamming from hash_index, atomic manifest I/O, and
# the one User-Agent definition. `useragent` is stdlib-only, so this is a
# top-level import rather than the deferred in-function one it used to be --
# that deferral existed only to avoid importing ingest.py for a single function.
from hash_index import compute_phash, hamming, _configure_pillow
from manifest import load_manifest, save_manifest
from useragent import build_user_agent

TOMBSTONES_PATH = Path("data/tombstones.json")
STAGING_DIR = Path("staging")

# Default Hamming tolerance if a signature omits max_distance. Small: a fixed
# placeholder hashes to distance 0 against itself; real art is far away.
DEFAULT_MAX_DISTANCE = 6


@dataclass
class Signature:
    """One known placeholder card, loaded from data/tombstones.json."""
    label: str
    phash: str
    max_distance: int
    host: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    max_size_bytes: Optional[int] = None
    note: str = ""


@dataclass
class Hit:
    """A fetched image matched against a signature."""
    label: str
    rule: str          # "phash" or "dims_size"
    distance: int      # Hamming distance to the signature (for logging)


def load_signatures(path: Path = TOMBSTONES_PATH) -> tuple[int, list[Signature]]:
    """Return (hash_bits, [Signature]) from the tombstone file. Missing file ->
    (64, []) so callers degrade gracefully to 'no tombstones known'."""
    if not path.exists():
        return 64, []
    data = json.loads(path.read_text(encoding="utf-8"))
    hash_bits = int(data.get("hash_bits", 64))
    sigs = [
        Signature(
            label=s["label"],
            phash=s["phash"],
            max_distance=int(s.get("max_distance", DEFAULT_MAX_DISTANCE)),
            host=s.get("host"),
            width=s.get("width"),
            height=s.get("height"),
            max_size_bytes=s.get("max_size_bytes"),
            note=s.get("note", ""),
        )
        for s in data.get("signatures", [])
    ]
    return hash_bits, sigs


def match_signature(
    phash: str, width: int, height: int, size: int, signatures: list[Signature]
) -> Optional[Hit]:
    """Return the first signature this (phash, dims, size) matches, or None.
    pHash within max_distance is primary; exact dims + tiny size is a fallback."""
    for sig in signatures:
        distance = hamming(phash, sig.phash)
        if distance <= sig.max_distance:
            return Hit(label=sig.label, rule="phash", distance=distance)
        if (
            sig.width is not None
            and sig.max_size_bytes is not None
            and width == sig.width
            and height == sig.height
            and size <= sig.max_size_bytes
        ):
            return Hit(label=sig.label, rule="dims_size", distance=distance)
    return None


def check_image(
    path: Path, hash_size: int, signatures: list[Signature]
) -> Optional[Hit]:
    """Hash `path` and test it against every signature. Returns a Hit or None.
    Raises on an unreadable image (caller decides how to treat that)."""
    if not signatures:
        return None
    phash, width, height = compute_phash(path, hash_size)
    size = path.stat().st_size
    return match_signature(phash, width, height, size, signatures)


# --------------------------------------------------------------------------- #
# CLI: sweep the existing staging backfill queue
# --------------------------------------------------------------------------- #
def _sweep(reject: bool) -> None:
    """Scan pending_review backfill entries whose staging file matches a
    tombstone signature. Lists them; with --reject, bulk-rejects (status
    'rejected' + delete the staging file, manifest record kept for dedup --
    identical to the review UI's Reject)."""
    _configure_pillow()
    hash_bits, signatures = load_signatures()
    if not signatures:
        print(f"No signatures in {TOMBSTONES_PATH}. Nothing to match against.")
        return
    hash_size = int(round(hash_bits ** 0.5))

    manifest = load_manifest()
    candidates = [
        (key, entry)
        for key, entry in manifest.items()
        if entry.get("status") == "pending_review"
        and entry.get("backfill")
        and entry.get("local_path")
    ]
    print(
        f"Scanning {len(candidates):,} pending_review backfill entries against "
        f"{len(signatures)} signature(s) ...\n"
    )

    hits: list[tuple[str, dict, Hit]] = []
    missing = 0
    for key, entry in candidates:
        local_path = Path(entry["local_path"])
        if not local_path.exists():
            missing += 1
            continue
        try:
            hit = check_image(local_path, hash_size, signatures)
        except Exception as exc:  # unreadable staging file -> not a tombstone
            print(f"  ! could not hash {local_path}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue
        if hit is not None:
            hits.append((key, entry, hit))

    if not hits:
        print("No tombstone matches in the staging queue.")
        if missing:
            print(f"({missing} entr{'y' if missing == 1 else 'ies'} had no staging file on disk.)")
        return

    print(f"{len(hits)} tombstone match(es):\n")
    for key, entry, hit in hits:
        src = entry.get("image_url", "")
        print(f"  {key}")
        print(f"      match     : {hit.label} (via {hit.rule}, dist {hit.distance})")
        print(f"      title     : {entry.get('title', '')}")
        print(f"      source    : {src}")
        print(f"      permalink : {entry.get('permalink', '')}")
        print(f"      staging   : {entry.get('local_path', '')}")

    if not reject:
        print(f"\nListing only. Re-run with --reject to bulk-reject these "
              f"{len(hits)} entr{'y' if len(hits) == 1 else 'ies'}.")
        return

    # Reject: mirror review.py -- status 'rejected', save manifest, delete file.
    for key, _entry, _hit in hits:
        manifest[key]["status"] = "rejected"
        manifest[key]["reject_reason"] = "tombstone"
    save_manifest(manifest)  # persist status BEFORE deleting files (crash-safe)

    deleted = 0
    for key, entry, _hit in hits:
        p = Path(entry["local_path"])
        if p.exists():
            try:
                p.unlink()
                deleted += 1
            except OSError as exc:
                print(f"  ! could not delete {p}: {exc}", file=sys.stderr)
    print(f"\nRejected {len(hits)} entr{'y' if len(hits) == 1 else 'ies'}; "
          f"deleted {deleted} staging file(s). Manifest records kept for dedup.")


# --------------------------------------------------------------------------- #
# CLI: add a new signature from an image URL or local file
# --------------------------------------------------------------------------- #
def _load_image_bytes(source: str) -> bytes:
    """Bytes for a URL (http/https) or a local file path."""
    if source.startswith(("http://", "https://")):
        # `requests` stays deferred: it is the only network dependency here, and
        # the check_image path (which every backfill run uses) needs none of it.
        import requests

        resp = requests.get(source, headers={"User-Agent": build_user_agent()}, timeout=30)
        resp.raise_for_status()
        return resp.content
    return Path(source).read_bytes()


def _add(source: str, label: str, host: Optional[str], max_distance: int) -> None:
    """Compute a signature (pHash + dims + size) for `source` and append it to
    the tombstone file (atomic write)."""
    from PIL import Image

    _configure_pillow()
    hash_bits, signatures = load_signatures()
    hash_size = int(round(hash_bits ** 0.5))

    raw = _load_image_bytes(source)
    with Image.open(io.BytesIO(raw)) as img:
        width, height = img.size
        import imagehash
        phash = str(imagehash.phash(img, hash_size=hash_size))

    if any(s.label == label for s in signatures):
        print(f"A signature labelled '{label}' already exists in {TOMBSTONES_PATH}.",
              file=sys.stderr)
        sys.exit(1)

    data = json.loads(TOMBSTONES_PATH.read_text(encoding="utf-8")) if TOMBSTONES_PATH.exists() \
        else {"hash_bits": hash_bits, "signatures": []}
    entry = {
        "label": label,
        "host": host,
        "phash": phash,
        "max_distance": max_distance,
        "width": width,
        "height": height,
        "max_size_bytes": max(2000, len(raw) + 512),
        "source_url": source if source.startswith(("http://", "https://")) else None,
        "note": f"Added via `tombstones.py add` from {source}.",
    }
    data.setdefault("signatures", []).append(entry)

    tmp = TOMBSTONES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, TOMBSTONES_PATH)
    print(f"Added signature '{label}': phash={phash} dims={width}x{height} "
          f"size={len(raw)}B -> {TOMBSTONES_PATH}")


def _list() -> None:
    hash_bits, signatures = load_signatures()
    if not signatures:
        print(f"No signatures in {TOMBSTONES_PATH}.")
        return
    print(f"{len(signatures)} signature(s) in {TOMBSTONES_PATH} (hash_bits={hash_bits}):\n")
    for s in signatures:
        dims = f"{s.width}x{s.height}" if s.width else "?"
        print(f"  {s.label:<16} phash={s.phash}  <=dist {s.max_distance}  "
              f"dims={dims}  host={s.host or '-'}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")

    p_sweep = sub.add_parser(
        "sweep", help="Scan the staging backfill queue for tombstone matches.")
    p_sweep.add_argument(
        "--reject", action="store_true",
        help="Bulk-reject matches (delete staging file, keep manifest record). "
             "Default is to list only.")

    p_add = sub.add_parser("add", help="Add a signature from an image URL or file.")
    p_add.add_argument("source", help="Image URL (http/https) or local file path.")
    p_add.add_argument("--label", required=True, help="Unique short name for the signature.")
    p_add.add_argument("--host", default=None, help="Host this placeholder comes from (metadata).")
    p_add.add_argument("--max-distance", type=int, default=DEFAULT_MAX_DISTANCE,
                       help=f"Hamming tolerance (default {DEFAULT_MAX_DISTANCE}).")

    sub.add_parser("list", help="Print the current signatures.")

    args = parser.parse_args()
    command = args.command or "sweep"
    if command == "sweep":
        _sweep(reject=getattr(args, "reject", False))
    elif command == "add":
        _add(args.source, args.label, args.host, args.max_distance)
    elif command == "list":
        _list()


if __name__ == "__main__":
    main()
