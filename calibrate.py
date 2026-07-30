"""Calibration run for the back-catalogue backfill dedup thresholds.

MEASUREMENT TOOL, NOT AN INGESTER. Strictly READ-ONLY over everything that
matters: it reads `saved_posts.csv` and the perceptual-hash index
(`archive_index.db`) and writes ONLY under `calibration/` (gitignored). It
never touches `manifest.json`, never writes under `ARCHIVE_DIR`, and never
builds/writes the index -- it only calls `hash_index.query_image` to look up.

Goal: take a slice of saved posts from a period with strong archive coverage
(May 2023), fetch each post's image, and measure the pHash Hamming distance
between the Reddit copy and its nearest archived original. The resulting
distribution is what sets the dedup thresholds for the bulk backfill.

Two subcommands:
    python calibrate.py run [--count 200] [--start-date 2023-05-01] [--keep] ...
    python calibrate.py ground-truth --folder <dir of known May-2023 files>

Fetch approach mirrors ingest.py's gallery fetching: Reddit's `.json`/API
endpoints 403 for this project's User-Agent, but the plain
`old.reddit.com/comments/<id>/` HTML permalink works (redirect-following,
`Cookie: over18=1`). That HTML carries BOTH the post's created timestamp and
its image URL, so no JSON is needed. Polite: sequential, sleep between requests.

Run with the interpreter that has `imagehash` installed (the one that built the
index) -- typically the global `python`, not the project venvs.
"""
import argparse
import csv
import html as html_mod
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# The lookup SEAM -- reused, never reimplemented. hash_index is standalone
# (only stdlib + dotenv at import), so importing it has no side effects and
# does NOT pull in manifest/tag.
from hash_index import (
    DEFAULT_DB,
    Match,
    _configure_pillow,
    compute_phash,
    query_image,
    read_hash_bits,
)
# Shared, not mirrored. `useragent` is stdlib-only by design, so importing it
# keeps this module independent of the pipeline modules (manifest/tag) -- which
# is what the old local copy of this function was actually protecting. The
# distinct product token below is the part worth keeping: it makes a measurement
# run attributable separately from ordinary ingest traffic.
from useragent import build_user_agent

CSV_PATH = Path("data/saved_posts.csv")
DEFAULT_OUT = Path("calibration")

# --- mirrored from ingest.py (kept local to avoid importing manifest/tag) --- #
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

USER_AGENT_PRODUCT = "oshiire-calibrate"


# Same structured gallery-tile matcher ingest.py uses: requires the tile's
# data-media-id to reappear in a nested preview.redd.it <img src>, so markup
# drift under-detects rather than mis-detects. See ingest.fetch_gallery_images.
GALLERY_TILE_RE = re.compile(
    r'<div\s+'
    r'(?=[^>]*\bclass="[^"]*\bgallery-tile\b[^"]*")'
    r'(?=[^>]*\bclass="[^"]*\bgallery-navigation\b[^"]*")'
    r'[^>]*?\bdata-media-id="([A-Za-z0-9]+)"[^>]*>'
    r'.{0,600}?'
    r'<img\b[^>]*\bsrc="https://preview\.redd\.it/\1(\.[A-Za-z0-9]+)(?:\?|")',
    re.DOTALL,
)

USER_AGENT = build_user_agent(USER_AGENT_PRODUCT)
FETCH_HEADERS = {"User-Agent": USER_AGENT, "Cookie": "over18=1"}
HTTP_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 30


# --------------------------------------------------------------------------- #
# Reddit HTML fetch + extraction (read-only network reads)
# --------------------------------------------------------------------------- #
def permalink_for(post_id: str) -> str:
    """The old.reddit HTML permalink for a post id. `.json`/api endpoints 403
    for our UA; this HTML page works and carries timestamp + image url."""
    return f"https://old.reddit.com/comments/{post_id}/"


def fetch_post_html(post_id: str) -> Optional[str]:
    """GET the old.reddit permalink, following redirects. Returns page text, or
    None on a non-2xx / network error (caller treats that as dead/failed)."""
    try:
        resp = requests.get(
            permalink_for(post_id), headers=FETCH_HEADERS, timeout=HTTP_TIMEOUT
        )
    except requests.RequestException as exc:
        print(f"  ! fetch error {post_id}: {exc}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        return None
    return resp.text


def _thing_tag(html: str, post_id: str) -> Optional[str]:
    """Return the opening <div ...> tag of the post's own 'thing' element
    (scoped by data-fullname="t3_<id>"), or None if absent -- absence means the
    post is deleted/removed (no thing rendered)."""
    m = re.search(
        r'<div\b[^>]*\bdata-fullname="t3_' + re.escape(post_id) + r'"[^>]*>', html
    )
    return m.group(0) if m else None


def extract_created_utc(html: str, post_id: str) -> Optional[datetime]:
    """Parse the post's created time from data-timestamp (epoch ms) on its
    thing tag. None if the post isn't present (deleted)."""
    tag = _thing_tag(html, post_id)
    if not tag:
        return None
    m = re.search(r'data-timestamp="(\d+)"', tag)
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)) / 1000.0, tz=timezone.utc)


def _looks_like_image_url(url: str) -> bool:
    ext = Path(url.split("?", 1)[0]).suffix.lower()
    host = url.split("/", 3)[2] if "://" in url else ""
    return ext in IMAGE_EXTENSIONS or host in {"i.redd.it", "i.imgur.com"}


def extract_image_url(html: str, post_id: str, permalink: str) -> tuple[Optional[str], bool]:
    """Return (image_url, is_gallery). image_url is None when the post is
    dead/removed or carries no usable direct image.

    Gallery handling: FIRST image only (per calibration decision). A gallery is
    detected either by a `/gallery/` data-url or by gallery-tile markup; its
    first (media_id, ext) tile becomes an i.redd.it url."""
    tag = _thing_tag(html, post_id)
    if not tag:
        return None, False  # deleted/removed

    data_url = None
    m = re.search(r'data-url="([^"]*)"', tag)
    if m:
        data_url = html_mod.unescape(m.group(1))

    is_gallery = (data_url is not None and "/gallery/" in data_url) or (
        "gallery-tile" in html
    )
    if is_gallery:
        tiles = GALLERY_TILE_RE.findall(html)
        if tiles:
            media_id, ext = tiles[0][0], tiles[0][1].lower()
            return f"https://i.redd.it/{media_id}{ext}", True
        return None, True  # gallery we couldn't parse -> treated as failed

    # Single post: data-url is the linked target. Dead/self posts point the
    # data-url back at the permalink (or omit it) -> no image.
    if not data_url or data_url.rstrip("/") == permalink.rstrip("/"):
        return None, False
    if _looks_like_image_url(data_url):
        return data_url, False
    return None, False


def download_image(url: str, dest_path: Path) -> None:
    """Stream a URL to disk. Mirrors ingest.download_image. Raises on error."""
    resp = requests.get(
        url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=DOWNLOAD_TIMEOUT
    )
    resp.raise_for_status()
    with dest_path.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


# --------------------------------------------------------------------------- #
# CSV + May-2023 locate
# --------------------------------------------------------------------------- #
def load_rows() -> list[tuple[int, str, str]]:
    """Return [(row_index, post_id, permalink)] for every data row (0-based
    row_index over data rows, excluding the header)."""
    if not CSV_PATH.exists():
        print(f"CSV not found: {CSV_PATH.resolve()}", file=sys.stderr)
        sys.exit(1)
    rows = []
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)  # id,permalink
        for i, rec in enumerate(reader):
            if len(rec) >= 2 and rec[0]:
                rows.append((i, rec[0].strip(), rec[1].strip()))
    return rows


def monotone_block_end(rows: list[tuple[int, str, str]]) -> int:
    """Index (exclusive) where the first ascending-by-id block ends. The CSV is
    two concatenated ascending blocks; base36-decoding the ids is a free,
    no-network way to find the discontinuity so the binary search never crosses
    it. Returns len(rows) if no big drop is found."""
    prev = None
    for idx, (_ri, post_id, _pl) in enumerate(rows):
        try:
            dec = int(post_id, 36)
        except ValueError:
            continue
        if prev is not None and dec < prev * 0.5:  # sharp drop = new (older) block
            return idx
        prev = dec
    return len(rows)


class ProbeCache:
    """Caches created_utc probes by list-index so a binary search never fetches
    the same row twice."""

    def __init__(self) -> None:
        self._cache: dict[int, Optional[datetime]] = {}
        self.probes = 0

    def created_utc(self, rows, idx: int, sleep: float) -> Optional[datetime]:
        if idx in self._cache:
            return self._cache[idx]
        _ri, post_id, _pl = rows[idx]
        html = fetch_post_html(post_id)
        self.probes += 1
        time.sleep(sleep)
        dt = extract_created_utc(html, post_id) if html else None
        self._cache[idx] = dt
        return dt


def _probe_nearest(rows, idx, lo, hi, cache, sleep):
    """created_utc at idx, nudging to an adjacent live row if idx is dead."""
    for cand in _spiral(idx, lo, hi):
        dt = cache.created_utc(rows, cand, sleep)
        if dt is not None:
            return cand, dt
    return idx, None


def _spiral(idx, lo, hi):
    """Yield idx, then idx-1, idx+1, idx-2, ... staying within [lo, hi]."""
    yield idx
    for step in range(1, hi - lo + 1):
        for cand in (idx - step, idx + step):
            if lo <= cand <= hi:
                yield cand


def locate_start(rows, start_date: datetime, sleep: float) -> tuple[int, ProbeCache]:
    """Binary-search the first row whose post created_utc >= start_date, within
    the monotone first block. Returns (list_index, cache)."""
    cache = ProbeCache()
    lo, hi = 0, monotone_block_end(rows) - 1
    print(
        f"Locating first row with created_utc >= {start_date.date()} "
        f"in rows 0..{hi} (monotone block) ..."
    )
    answer = hi
    while lo <= hi:
        mid = (lo + hi) // 2
        cand, dt = _probe_nearest(rows, mid, lo, hi, cache, sleep)
        if dt is None:
            # whole neighbourhood dead; treat as "too early", move right
            lo = mid + 1
            continue
        label = rows[cand][1]
        print(f"  probe row {rows[cand][0]} ({label}) -> {dt.date()}")
        if dt >= start_date:
            answer = cand
            hi = cand - 1
        else:
            lo = cand + 1
    return answer, cache


# --------------------------------------------------------------------------- #
# ASCII histogram
# --------------------------------------------------------------------------- #
def ascii_histogram(distances: list[int], hash_bits: int, bin_width: int = 2, width: int = 50) -> str:
    """Text histogram of Hamming distances over 0..hash_bits."""
    if not distances:
        return "(no distances to plot)\n"
    n_bins = hash_bits // bin_width + 1
    counts = [0] * n_bins
    for d in distances:
        counts[min(d, hash_bits) // bin_width] += 1
    peak = max(counts) or 1
    lines = [f"Distance histogram (bin width {bin_width}, out of {hash_bits}):"]
    for b, c in enumerate(counts):
        if c == 0:
            continue
        lo = b * bin_width
        hi = min(lo + bin_width - 1, hash_bits)
        bar = "#" * max(1, round(c / peak * width))
        lines.append(f"  {lo:>2}-{hi:<2} | {bar} {c}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def cmd_run(args) -> None:
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    db_path = args.db
    if not db_path.exists():
        print(f"No index at {db_path}. Run `python hash_index.py build` first.", file=sys.stderr)
        sys.exit(1)

    out_dir = args.out
    tmp_dir = out_dir / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    _configure_pillow()

    rows = load_rows()
    start_idx, cache = locate_start(rows, start_date, args.sleep)
    slice_rows = rows[start_idx : start_idx + args.count]
    print(
        f"\nSelected {len(slice_rows)} rows starting at row {slice_rows[0][0]} "
        f"(post {slice_rows[0][1]}).\n"
    )

    results = []
    fetched = dead = failed = galleries = 0
    for n, (row_index, post_id, permalink) in enumerate(slice_rows, start=1):
        # Reuse the locate-phase HTML if this row was already probed? The cache
        # holds datetimes, not html, so we re-fetch here -- but slice rows are
        # rarely probe rows, so the overlap is negligible.
        html = fetch_post_html(post_id)
        time.sleep(args.sleep)
        if not html:
            dead += 1
            print(f"  [{n}/{len(slice_rows)}] {post_id} DEAD (no page)")
            continue

        created = extract_created_utc(html, post_id)
        image_url, is_gallery = extract_image_url(html, post_id, permalink)
        if is_gallery:
            galleries += 1
        if not image_url:
            dead += 1
            print(f"  [{n}/{len(slice_rows)}] {post_id} DEAD/no-image")
            continue

        ext = Path(image_url.split("?", 1)[0]).suffix or ".jpg"
        dest = tmp_dir / f"{post_id}{ext}"
        try:
            download_image(image_url, dest)
            time.sleep(args.sleep)
        except requests.RequestException as exc:
            failed += 1
            print(f"  [{n}/{len(slice_rows)}] {post_id} FAILED download: {exc}")
            continue

        try:
            phash, _w, _h = compute_phash(dest, int(round(read_hash_bits_for(db_path) ** 0.5)))
            matches = query_image(dest, top_n=1, db_path=db_path)
        except Exception as exc:  # unreadable image / hash failure
            failed += 1
            print(f"  [{n}/{len(slice_rows)}] {post_id} FAILED hash/match: {exc}")
            continue

        top = matches[0] if matches else None
        results.append(
            {
                "post_id": post_id,
                "permalink": permalink,
                "created_utc": created.isoformat() if created else None,
                "image_url": image_url,
                "is_gallery": is_gallery,
                "phash": phash,
                "top1_distance": top.distance if top else None,
                "top1_path": top.rel_path if top else None,
            }
        )
        fetched += 1
        d = top.distance if top else "?"
        print(f"  [{n}/{len(slice_rows)}] {post_id} dist={d} -> {top.rel_path if top else '(empty index)'}")

    hash_bits = read_hash_bits_for(db_path)
    report = build_report(results, fetched, dead, failed, len(slice_rows), galleries, hash_bits)
    print("\n" + report)

    (out_dir / "report.txt").write_text(report, encoding="utf-8")
    (out_dir / "results.json").write_text(
        json.dumps(
            {"hash_bits": hash_bits, "start_date": args.start_date, "results": results},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Report written to {(out_dir / 'report.txt').resolve()}")
    print(f"Machine-readable results at {(out_dir / 'results.json').resolve()}")

    if args.keep:
        print(f"Kept temp downloads in {tmp_dir.resolve()} (--keep).")
    else:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print("Temp downloads deleted.")


_HASH_BITS_CACHE: dict[str, int] = {}


def read_hash_bits_for(db_path: Path) -> int:
    """hash_bits for the index, cached (read-only open)."""
    key = str(db_path)
    if key not in _HASH_BITS_CACHE:
        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            _HASH_BITS_CACHE[key] = read_hash_bits(conn)
        finally:
            conn.close()
    return _HASH_BITS_CACHE[key]


def build_report(results, fetched, dead, failed, total, galleries, hash_bits) -> str:
    lines = []
    lines.append("=" * 72)
    lines.append("Oshiire calibration run -- pHash distance distribution")
    lines.append("=" * 72)
    lines.append(
        f"slice rows: {total}   fetched(matched): {fetched}   dead: {dead}   "
        f"failed: {failed}"
    )
    lines.append(
        f"galleries encountered: {galleries} (first image only)   "
        f"hash bits: {hash_bits}"
    )
    lines.append("")
    dists = [r["top1_distance"] for r in results if r["top1_distance"] is not None]
    if dists:
        dists_sorted = sorted(dists)
        lines.append(
            f"distance min/median/max: {dists_sorted[0]} / "
            f"{dists_sorted[len(dists_sorted)//2]} / {dists_sorted[-1]}"
        )
    lines.append("")
    lines.append(ascii_histogram(dists, hash_bits))
    lines.append("Matches (ascending by top-1 distance):")
    lines.append(f"  {'dist':>4}  {'post_id':<9}  {'matched archive path':<45}  permalink")
    lines.append(f"  {'-'*4}  {'-'*9}  {'-'*45}  {'-'*9}")
    for r in sorted(results, key=lambda x: (x["top1_distance"] is None, x["top1_distance"])):
        d = r["top1_distance"]
        d_str = f"{d:>2}/{hash_bits}" if d is not None else "  n/a"
        path = (r["top1_path"] or "")[:45]
        lines.append(f"  {d_str:>4}  {r['post_id']:<9}  {path:<45}  {r['permalink']}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# ground-truth
# --------------------------------------------------------------------------- #
def cmd_ground_truth(args) -> None:
    results_path = args.results
    if not results_path.exists():
        print(f"No results file at {results_path}. Run `calibrate.py run` first.", file=sys.stderr)
        sys.exit(1)
    data = json.loads(results_path.read_text(encoding="utf-8"))
    hash_bits = data.get("hash_bits", 64)
    hash_size = int(round(hash_bits ** 0.5))
    slice_results = [r for r in data["results"] if r.get("phash")]
    if not slice_results:
        print("results.json has no phashes to compare against.", file=sys.stderr)
        sys.exit(1)

    folder = args.folder
    if not folder.exists() or not folder.is_dir():
        print(f"Folder not found: {folder}", file=sys.stderr)
        sys.exit(1)

    _configure_pillow()
    known_files = sorted(
        p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in
        {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
    )
    if not known_files:
        print(f"No image files under {folder}.", file=sys.stderr)
        sys.exit(1)

    print(f"Cross-checking {len(known_files)} known files against "
          f"{len(slice_results)} fetched posts (hash bits {hash_bits}).\n")
    print(f"  {'dist':>5}  {'known file':<32}  {'best post_id':<11}  permalink")
    print(f"  {'-'*5}  {'-'*32}  {'-'*11}  {'-'*9}")
    for kf in known_files:
        try:
            kf_hash, _w, _h = compute_phash(kf, hash_size)
        except Exception as exc:
            print(f"  {'err':>5}  {kf.name[:32]:<32}  (unreadable: {exc})")
            continue
        k_int = int(kf_hash, 16)
        best = min(
            slice_results,
            key=lambda r: (k_int ^ int(r["phash"], 16)).bit_count(),
        )
        dist = (k_int ^ int(best["phash"], 16)).bit_count()
        print(f"  {dist:>2}/{hash_bits}  {kf.name[:32]:<32}  {best['post_id']:<11}  {best['permalink']}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    # Archive paths can contain non-Latin (e.g. Japanese) characters; the
    # Windows console defaults to cp1252 and would crash on printing them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Fetch a May-2023 slice, match, report the distance distribution.")
    p_run.add_argument("--count", type=int, default=200, help="Consecutive rows to sample (default 200).")
    p_run.add_argument("--start-date", default="2023-05-01", help="First date of the slice (YYYY-MM-DD).")
    p_run.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"Index path (default {DEFAULT_DB}).")
    p_run.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"Output dir (default {DEFAULT_OUT}).")
    p_run.add_argument("--sleep", type=float, default=2.0, help="Polite delay between requests (s).")
    p_run.add_argument("--keep", action="store_true", help="Keep temp downloads instead of deleting them.")
    p_run.set_defaults(func=cmd_run)

    p_gt = sub.add_parser("ground-truth", help="Cross-check known files against a completed run.")
    p_gt.add_argument("--folder", type=Path, required=True, help="Folder of known May-2023 files.")
    p_gt.add_argument("--results", type=Path, default=DEFAULT_OUT / "results.json", help="results.json from a run.")
    p_gt.add_argument("--db", type=Path, default=DEFAULT_DB, help="Index path (for hash-bits fallback).")
    p_gt.set_defaults(func=cmd_ground_truth)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
