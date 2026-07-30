"""Back-catalogue backfill ingester (one-time, resumable, batched).

Sweeps the full `saved_posts.csv` export of the owner's Reddit saves, skips any
post already owned -- first for free via manifest membership, then via the
perceptual-hash index of the local archive -- and routes only the genuine gaps
into the normal `pending_review` pipeline for later review.

This is NOT `ingest.py`. `ingest.py` reads the recent RSS feed; this reads the
whole historical CSV (id + permalink only) and must therefore fetch each post's
`old.reddit.com` HTML permalink to discover its image(s), timestamp being
irrelevant here.

Dedup thresholds come from the real calibration run (`calibrate.py`) on THIS
archive (64-bit pHash): true matches sit at 0-8, 9-11 is an uncertain band, and
12+ is a hard noise floor of spurious near-collisions -- so:
    top-1 distance <= 8   -> ALREADY OWNED   (discard, no manifest entry)
    top-1 distance  9-11  -> UNCERTAIN       (keep, flag possible-duplicate)
    top-1 distance >= 12  -> GENUINELY NEW   (keep, normal entry)
Dead links are the COMMON path here, not an edge case, so dead-link handling
must never crash the sweep. Link rot rises with the age of a save: the oldest
rows are the most decayed, and since the sweep walks the CSV oldest-first, the
observed dead-link rate starts high and falls as it advances. Any single
headline percentage is therefore wrong by the time you read it -- early-sweep
and late-sweep rates differ substantially and both are "true" -- so no figure
is quoted. Read the running rate off `state.json`'s `totals` if you want to
know where a given sweep currently sits.

Some hosts (imgur) serve a fixed "image removed" placeholder card with HTTP 200
+ an image content-type for a deleted image, so those don't fail the download --
they're caught by perceptual-hash match against `tombstones.py`'s signature list
and bucketed as `tombstone` (dead, no manifest entry, source link logged for
manual rescue).

Invariants:
  * READ-ONLY over ARCHIVE_DIR and the index -- only calls hash_index.query_image.
  * Writes only manifest.json (atomically, via manifest.py), staging/ (kept
    images), and the gitignored data/backfill/ dir (cursor + log + temp downloads).
  * Never reimplements hashing -- pHash/NN come from hash_index.
  * Post-level dedup keyed on the `post_id` FIELD across all entries, per the
    manifest schema (galleries share one parent id across suffixed keys).
  * No AI tagging (too slow at 40k scale) -- metadata tagging only.

Usage:
    python backfill.py [--limit 500] [--offset N] [--sleep 2.0] [--db data/archive_index.db]

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
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# Mandated reuse -- hashing + nearest-neighbour, atomic manifest I/O, and the
# Slice 1 metadata tagger. We also borrow ingest.py's download/UA/gallery-tile
# primitives so this sweep is polite in exactly the same way as normal ingest.
from hash_index import DEFAULT_DB, query_image, read_hash_bits, _configure_pillow
from manifest import load_manifest, save_manifest
from tag import tag_entry
from tombstones import check_image, load_signatures
from useragent import build_user_agent
from ingest import (
    GALLERY_TILE_RE,
    IMAGE_EXTENSIONS,
    STAGING_DIR,
    download_image,
)

CSV_PATH = Path("data/saved_posts.csv")
BACKFILL_DIR = Path("data/backfill")
STATE_PATH = BACKFILL_DIR / "state.json"
LOG_PATH = BACKFILL_DIR / "backfill_log.jsonl"
TMP_DIR = BACKFILL_DIR / "tmp"

DEFAULT_LIMIT = 500
DEFAULT_SLEEP = 2.0
CHECKPOINT_EVERY = 50  # save manifest + advance cursor this often (crash-safety)

# Calibrated on THIS archive (see module docstring / calibrate.py).
OWNED_MAX = 8       # <= 8   : already owned
UNCERTAIN_MAX = 11  # 9..11  : uncertain band; 12+ : genuinely new

USER_AGENT = build_user_agent()
FETCH_HEADERS = {"User-Agent": USER_AGENT, "Cookie": "over18=1"}
HTTP_TIMEOUT = 15

# Rate-limit handling. Most old saves ARE genuine dead links (see the module
# docstring on why no fixed rate is quoted), but a NON-200 / network error is NOT
# how reddit signals deletion (a removed post still renders 200 with no `thing`
# div -> "removed"). A non-200 here is almost always throttling, so treat 429
# (and transient network errors) as retryable rather than dead. Backoff schedule
# mirrors the unsave userscript's BACKOFF_STEPS.
RATE_LIMIT_BACKOFF = [30, 60, 120, 240]  # seconds; exponential, up to 4 retries
# A run of this many consecutive fetch_failed outcomes means we're being soft-
# throttled (the log shows blocks of 11/34/55). Abort the batch instead of
# burning through hundreds of live rows marking them dead. Recover them later
# with `--retry-failed`.
#
# FORWARD-SWEEP ONLY. `--retry-failed` deliberately does not use this: its work
# list is *composed of* previously-failed ids and drops the ones that succeed,
# so deterministic per-post failures necessarily cluster at the head and would
# trip the streak on row 15 of every run, forever, without ever reaching the
# untried ids behind them. There, the only honest throttle signal is a
# persistent 429 (RateLimited); per-post failures are expected and get skipped.
FETCH_FAIL_ABORT_THRESHOLD = 15
FETCH_FAILED_LABEL = "dead (fetch_failed)"
MAX_FAIL_DETAIL = 200  # truncate requests' very long connection-error messages

# HTTP statuses that mean "this post will never be fetchable", as opposed to
# "not right now". 403 is reddit refusing the permalink outright -- a
# restricted or quarantined subreddit -- and it does not change with time,
# backoff, or a different run. Logged under a TERMINAL reason so
# load_retry_ids() stops re-queueing it, instead of the retryable
# "fetch_failed" that had these ids circling forever. 404/5xx stay retryable:
# those do flip back, and a 5xx in particular is often throttling in disguise.
TERMINAL_HTTP_STATUSES = frozenset({403})
FORBIDDEN_REASON = "forbidden"
FORBIDDEN_LABEL = f"dead ({FORBIDDEN_REASON})"
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})$")


def is_terminal_fetch_error(detail: Optional[str]) -> bool:
    """True when a fetch-failure detail names a permanently-dead status.

    Reads the SAME `fetch_error` string the log already stores, so it doubles
    as a read-side reclassifier: log lines written before this existed carry
    reason="fetch_failed" with fetch_error="HTTP 403", and load_retry_ids()
    can retire them without rewriting a single logged line or re-fetching the
    post to find out what it already knows."""
    if not detail:
        return False
    match = _HTTP_STATUS_RE.match(detail.strip())
    return match is not None and int(match.group(1)) in TERMINAL_HTTP_STATUSES

OUTCOME_KEYS = ["manifest_skip", "owned", "uncertain", "new", "tombstone", "dead", "failed"]


class RateLimited(Exception):
    """Reddit returned HTTP 429 and the backoff schedule was exhausted. Signals
    the sweep to stop cleanly and resume later, NOT to mark the row dead."""


# --------------------------------------------------------------------------- #
# Reddit HTML fetch + extraction (read-only network reads)
#
# CSV rows carry only (bare id, permalink), so -- unlike ingest.py, which gets
# the image URL straight from the RSS entry -- we fetch the post's HTML to find
# its image(s). These helpers mirror the proven approach in calibrate.py; the
# `.json`/api endpoints 403 for our UA, but the plain old.reddit HTML works.
# --------------------------------------------------------------------------- #
def permalink_for(csv_id: str) -> str:
    """old.reddit HTML permalink for a BARE post id (not the t3_ fullname)."""
    return f"https://old.reddit.com/comments/{csv_id}/"


def _fail_detail(exc: BaseException) -> str:
    """`ExceptionType: message` on one line, capped -- requests' connection
    errors carry multi-line urllib3 tracebacks that would bloat the log."""
    msg = " ".join(str(exc).split())
    if len(msg) > MAX_FAIL_DETAIL:
        msg = msg[:MAX_FAIL_DETAIL - 3] + "..."
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def fail_kind(detail: Optional[str]) -> str:
    """Coarse grouping key for a fetch-failure detail -- the exception class or
    the HTTP status, with the per-URL message tail dropped, so a run's failures
    can be tallied by cause ("ReadTimeout", "HTTP 404", ...)."""
    if not detail:
        return "unrecorded"  # pre-fix log entries carry no detail
    return detail.split(":", 1)[0].strip()


def fetch_post_html(csv_id: str) -> tuple[Optional[str], Optional[str]]:
    """GET the old.reddit permalink, following redirects. Returns
    `(page_text, None)` on 200, or `(None, detail)` when no page came back --
    a genuine dead link (403/404/5xx) or a network error that outlived the
    backoff schedule. `detail` names the specific cause ("HTTP 404",
    "ReadTimeout: ...") so persistent failures are diagnosable from the log
    rather than guessed at. A 429 or a transient network error is throttling,
    not death: back off through RATE_LIMIT_BACKOFF and retry. If a 429 persists
    past the schedule, raise RateLimited so the sweep stops cleanly instead of
    mislabelling live rows dead."""
    url = permalink_for(csv_id)
    last_exc: Optional[Exception] = None
    for attempt in range(len(RATE_LIMIT_BACKOFF) + 1):
        resp = None
        try:
            resp = requests.get(url, headers=FETCH_HEADERS, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            last_exc = exc  # timeout / connection reset -> often a throttle symptom
        else:
            if resp.status_code == 200:
                return resp.text, None
            if resp.status_code != 429:
                # Genuine dead link (403/404/5xx/redirect-to-removed). Keep the
                # status: it is what distinguishes a permanently gone post from
                # a transient failure worth retrying.
                return None, f"HTTP {resp.status_code}"

        # Here: HTTP 429, or a network exception. Both are retryable.
        if attempt == len(RATE_LIMIT_BACKOFF):
            break  # retries exhausted
        wait = RATE_LIMIT_BACKOFF[attempt]
        if resp is not None:  # 429 -- honor a saner Retry-After if given
            retry_after = (resp.headers.get("Retry-After") or "").strip()
            if retry_after.isdigit():
                wait = max(wait, int(retry_after))
            reason = "429 rate limited"
        else:
            reason = f"network error ({type(last_exc).__name__})"
        print(f"  ! {reason} on {csv_id}; backing off {wait}s "
              f"(attempt {attempt + 1}/{len(RATE_LIMIT_BACKOFF)})", file=sys.stderr)
        time.sleep(wait)

    # Retries exhausted. A lingering 429 is a hard throttle -> abort the sweep;
    # a lingering network error settles as a (retryable) fetch_failed.
    if resp is not None:
        raise RateLimited(csv_id)
    detail = _fail_detail(last_exc) if last_exc is not None else "unknown"
    print(f"  ! fetch error {csv_id}: {detail}", file=sys.stderr)
    return None, detail


def _thing_tag(html: str, csv_id: str) -> Optional[str]:
    """The opening <div ...> of the post's own 'thing' element (scoped by
    data-fullname="t3_<id>"), or None -- absence means the post is
    deleted/removed (no thing rendered)."""
    m = re.search(r'<div\b[^>]*\bdata-fullname="t3_' + re.escape(csv_id) + r'"[^>]*>', html)
    return m.group(0) if m else None


def _looks_like_image_url(url: str) -> bool:
    ext = Path(url.split("?", 1)[0]).suffix.lower()
    host = url.split("/", 3)[2] if "://" in url else ""
    return ext in IMAGE_EXTENSIONS or host in {"i.redd.it", "i.imgur.com"}


def extract_images(html: str, csv_id: str, permalink: str) -> tuple[str, list[str], Optional[str]]:
    """Classify the post and return (kind, image_urls, reason).

    kind is one of:
      "single"  -- image_urls == [one direct image url]
      "gallery" -- image_urls == [url_1, url_2, ...] in gallery order (ALL tiles)
      "dead"    -- nothing importable; image_urls == [], reason explains why
                   ("removed", "no_image", or "gallery_no_images").
    """
    tag = _thing_tag(html, csv_id)
    if not tag:
        return "dead", [], "removed"

    data_url = None
    m = re.search(r'data-url="([^"]*)"', tag)
    if m:
        data_url = html_mod.unescape(m.group(1))

    is_gallery = (data_url is not None and "/gallery/" in data_url) or ("gallery-tile" in html)
    if is_gallery:
        # Same extraction ingest.fetch_gallery_images performs, run on the HTML
        # we already hold (avoids a second GET). De-dup while preserving order.
        seen: dict[str, str] = {}
        for media_id, ext in GALLERY_TILE_RE.findall(html):
            seen.setdefault(media_id, ext.lower())
        if not seen:
            return "dead", [], "gallery_no_images"
        return "gallery", [f"https://i.redd.it/{mid}{ext}" for mid, ext in seen.items()], None

    # Single post: data-url is the linked target. Dead/self/text posts point it
    # back at the permalink (or omit it) -> no importable image.
    if not data_url or data_url.rstrip("/") == permalink.rstrip("/"):
        return "dead", [], "no_image"
    if _looks_like_image_url(data_url):
        return "single", [data_url], None
    return "dead", [], "no_image"


def extract_title(html: str, permalink: str) -> str:
    """Best-effort real post title from the HTML, falling back to a de-slugged
    permalink. old.reddit renders `<title>Post title : subreddit</title>` (the
    subreddit token has no spaces), so drop that trailing ` : ...` segment."""
    m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    if m:
        title = html_mod.unescape(m.group(1)).strip()
        title = re.sub(r"\s*:\s*[^:]+$", "", title).strip()  # strip " : subreddit"
        if title:
            return title
    return _deslug(permalink)


def _deslug(permalink: str) -> str:
    """Rough title from the permalink slug: .../comments/<id>/<slug>/ -> 'slug words'."""
    m = re.search(r"/comments/[^/]+/([^/]+)", permalink)
    return m.group(1).replace("_", " ").strip() if m else ""


def extract_subreddit(permalink: str) -> str:
    m = re.search(r"/r/([^/]+)/", permalink)
    return m.group(1) if m else "unknown"


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def load_rows() -> list[tuple[str, str]]:
    """Return [(bare_id, permalink)] for every data row (header excluded)."""
    if not CSV_PATH.exists():
        print(f"CSV not found: {CSV_PATH.resolve()}", file=sys.stderr)
        sys.exit(1)
    rows = []
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # header: id,permalink
        for rec in reader:
            if len(rec) >= 2 and rec[0].strip():
                rows.append((rec[0].strip(), rec[1].strip()))
    return rows


# --------------------------------------------------------------------------- #
# State / cursor + log
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"warning: unreadable {STATE_PATH}, starting cursor from 0", file=sys.stderr)
    return {"next_row": 0, "totals": {k: 0 for k in OUTCOME_KEYS + ["processed"]}}


def save_state(state: dict) -> None:
    """Atomic write (tmp + os.replace), same discipline as the manifest."""
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def log_outcome(record: dict) -> None:
    """Append one JSONL line. Append-only so prior batches survive verbatim."""
    record["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Per-image routing
# --------------------------------------------------------------------------- #
def route_image(tmp_path: Path, db_path: Path) -> tuple[str, Optional[int], Optional[str]]:
    """Hash + nearest-neighbour lookup via hash_index, then bucket by distance.
    Returns (outcome, distance, matched_path). An empty index -> 'new'."""
    matches = query_image(tmp_path, top_n=1, db_path=db_path)
    top = matches[0] if matches else None
    if top is None:
        return "new", None, None
    if top.distance <= OWNED_MAX:
        return "owned", top.distance, top.rel_path
    if top.distance <= UNCERTAIN_MAX:
        return "uncertain", top.distance, top.rel_path
    return "new", top.distance, top.rel_path


def build_entry(fullname, image_index, title, subreddit, permalink, image_url,
                local_path, fetched_at, outcome, distance, matched_path) -> dict:
    """A pending_review manifest entry mirroring ingest.py's shape, plus backfill
    provenance. image_index is None for single-image posts."""
    entry = {
        "post_id": fullname,
        "title": title,
        "subreddit": subreddit,
        "permalink": permalink,
        "image_url": image_url,
        "local_path": str(local_path),
        "status": "pending_review",
        "fetched_at": fetched_at,
        "backfill": True,
    }
    if image_index is not None:
        entry["image_index"] = image_index
    if outcome == "uncertain":
        entry["backfill_uncertain"] = True
        entry["backfill_match_path"] = matched_path
        entry["backfill_match_distance"] = distance
    return entry


def process_image(csv_id, fullname, image_index, title, subreddit, permalink,
                  image_url, manifest, db_path, sleep, signatures, hash_size) -> str:
    """Download one image to tmp, route it, and (for keeps) move into staging +
    create a tagged manifest entry. Returns the outcome bucket. Raises nothing
    the caller can't survive -- download/hash errors surface as 'failed'."""
    ext = Path(image_url.split("?", 1)[0]).suffix or ".jpg"
    suffix = f"_{image_index}" if image_index is not None else ""
    tmp_path = TMP_DIR / f"{fullname}{suffix}{ext}"
    try:
        download_image(image_url, tmp_path)
        time.sleep(sleep)
    except requests.RequestException as exc:
        log_outcome({"post_id": fullname, "image_index": image_index,
                     "outcome": "failed", "reason": f"download: {exc}"})
        return "failed"

    # Tombstone gate: a deleted image whose host served a placeholder card (200 +
    # image content-type) is a DEAD link, not new art. Catch it before routing so
    # it never becomes a manifest entry. Log the source link for manual rescue.
    try:
        hit = check_image(tmp_path, hash_size, signatures)
    except Exception as exc:  # unreadable/corrupt download -> hash failure
        tmp_path.unlink(missing_ok=True)
        log_outcome({"post_id": fullname, "image_index": image_index,
                     "outcome": "failed", "reason": f"hash: {type(exc).__name__}: {exc}"})
        return "failed"
    if hit is not None:
        tmp_path.unlink(missing_ok=True)  # dead placeholder -- never keep it
        log_outcome({"post_id": fullname, "image_index": image_index,
                     "outcome": "tombstone", "reason": hit.label, "match_rule": hit.rule,
                     "distance": hit.distance, "source_link": image_url,
                     "permalink": permalink})
        return "tombstone"

    try:
        outcome, distance, matched_path = route_image(tmp_path, db_path)
    except Exception as exc:  # unreadable/corrupt download -> hash failure
        tmp_path.unlink(missing_ok=True)
        log_outcome({"post_id": fullname, "image_index": image_index,
                     "outcome": "failed", "reason": f"hash: {type(exc).__name__}: {exc}"})
        return "failed"

    if outcome == "owned":
        tmp_path.unlink(missing_ok=True)  # never re-import art we already have
        log_outcome({"post_id": fullname, "image_index": image_index, "outcome": "owned",
                     "distance": distance, "matched_path": matched_path})
        return "owned"

    # Keep: move tmp -> staging, build + tag the entry.
    key = f"{fullname}{suffix}"
    local_path = STAGING_DIR / f"{fullname}{suffix}{ext}"
    shutil.move(str(tmp_path), str(local_path))
    entry = build_entry(fullname, image_index, title, subreddit, permalink,
                        image_url, local_path, datetime.now(timezone.utc).isoformat(),
                        outcome, distance, matched_path)
    tag_entry(entry)  # metadata tagging only; no AI
    manifest[key] = entry
    log_outcome({"post_id": fullname, "image_index": image_index, "outcome": outcome,
                 "distance": distance, "matched_path": matched_path})
    return outcome


# --------------------------------------------------------------------------- #
# Sweep
# --------------------------------------------------------------------------- #
def run(args) -> None:
    db_path = args.db
    if not db_path.exists():
        print(f"No index at {db_path}. Run `python hash_index.py build` first.", file=sys.stderr)
        sys.exit(1)
    _configure_pillow()

    BACKFILL_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    rows = load_rows()
    manifest = load_manifest()
    # Post-level dedup: the FIELD across all entries, not dict keys (galleries
    # share one parent id across suffixed keys). See manifest schema.
    known_post_ids = {e.get("post_id") for e in manifest.values()}

    state = load_state()
    start = args.offset if args.offset is not None else state.get("next_row", 0)
    end = min(start + args.limit, len(rows))
    if start >= len(rows):
        print(f"Nothing to do: start row {start} >= {len(rows)} CSV rows. Backfill complete.")
        return

    with sqlite3.connect(db_path) as conn:
        state.setdefault("hash_bits", read_hash_bits(conn))
    hash_size = int(round(state["hash_bits"] ** 0.5))

    # Load tombstone signatures once (dead-image placeholders that return 200 +
    # image content-type). Their hash must be computed at the same size as the
    # index, or Hamming distances are meaningless -- so warn on a mismatch.
    tomb_bits, signatures = load_signatures()
    if signatures and tomb_bits != state["hash_bits"]:
        print(f"warning: tombstones.json hash_bits ({tomb_bits}) != index hash_bits "
              f"({state['hash_bits']}); tombstone matching disabled.", file=sys.stderr)
        signatures = []

    print(f"Backfill: rows [{start}, {end}) of {len(rows):,}  "
          f"(manifest has {len(known_post_ids):,} known post ids; "
          f"{len(signatures)} tombstone signature(s))\n")

    batch = {k: 0 for k in OUTCOME_KEYS + ["processed"]}

    def checkpoint(next_row: int) -> None:
        save_manifest(manifest)  # manifest FIRST, then advance cursor (crash-safe)
        state["next_row"] = max(state.get("next_row", 0), next_row)
        for k in OUTCOME_KEYS + ["processed"]:
            state["totals"][k] = state["totals"].get(k, 0) + batch[k]
            batch[k] = 0  # folded into state; avoid double counting on next checkpoint
        save_state(state)

    consecutive_fetch_fail = 0
    for i in range(start, end):
        csv_id, permalink = rows[i]
        fullname = "t3_" + csv_id  # CSV ids are bare; manifest is t3_ fullnames
        batch["processed"] += 1

        # 1. Free dedup -- already in the manifest, in any status. No fetch.
        if fullname in known_post_ids:
            batch["manifest_skip"] += 1
            log_outcome({"post_id": fullname, "outcome": "manifest_skip"})
        else:
            known_post_ids.add(fullname)
            try:
                outcome = process_row(csv_id, fullname, permalink, manifest, db_path,
                                      args.sleep, batch, signatures, hash_size)
            except RateLimited:
                # Hard 429: do NOT mark this row dead. Persist progress up to the
                # PREVIOUS row so this one is retried next run, then stop cleanly.
                checkpoint(i)
                _cleanup_tmp()
                print(f"\n! Reddit is rate-limiting (HTTP 429 persisted through backoff).\n"
                      f"  Stopped cleanly at row {i}; it will be retried on the next run.\n"
                      f"  Wait a while for the throttle to clear, then re-run backfill.",
                      file=sys.stderr)
                print_summary(state, len(rows))
                return
            print(f"  [{i}] {fullname} {outcome}")

            # Circuit breaker: a long run of fetch_failed is soft-throttling, not a
            # cluster of dead links -- abort before burning through live rows.
            if outcome.startswith(FETCH_FAILED_LABEL):
                consecutive_fetch_fail += 1
                if consecutive_fetch_fail >= FETCH_FAIL_ABORT_THRESHOLD:
                    checkpoint(i + 1)
                    _cleanup_tmp()
                    print(f"\n! {consecutive_fetch_fail} consecutive fetch failures -- almost\n"
                          f"  certainly soft rate-limiting, not dead links. Aborting at row {i}\n"
                          f"  rather than marking hundreds of live rows dead.\n"
                          f"  Wait for the throttle to clear, then recover these with:\n"
                          f"      python backfill.py --retry-failed", file=sys.stderr)
                    print_summary(state, len(rows))
                    return
            else:
                consecutive_fetch_fail = 0

        if batch["processed"] % CHECKPOINT_EVERY == 0:
            checkpoint(i + 1)

    checkpoint(end)
    _cleanup_tmp()
    print_summary(state, len(rows))


# --------------------------------------------------------------------------- #
# Retry mode -- re-process rows the throttling bug recorded as dead/fetch_failed
# --------------------------------------------------------------------------- #
def load_retry_ids() -> list[str]:
    """Scan the append-only log for post ids recorded dead/fetch_failed that have
    NOT since resolved to any terminal outcome, and return their BARE ids (t3_
    stripped) in first-seen order.

    "Resolved" = the id also appears with a real fetch result: owned/new/
    uncertain/tombstone, a terminal `dead` reason (removed/no_image/
    gallery_no_images/forbidden), a download/hash `failed` (the FETCH reached the
    post), or a manifest_skip. Excluding those makes repeated --retry-failed runs
    idempotent -- an id recovered on a prior run is never re-fetched.

    A pre-existing `fetch_failed` line whose fetch_error is a terminal status
    (see is_terminal_fetch_error) is retired here on READ. The log itself is
    append-only and stays byte-for-byte as written; this only stops re-fetching
    a post whose recorded answer already says it can never succeed."""
    if not LOG_PATH.exists():
        return []
    failed: dict[str, None] = {}  # ordered set of fullnames ever seen fetch_failed
    resolved: set[str] = set()    # fullnames seen with any real fetch result
    with LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = rec.get("post_id")
            if not pid:
                continue
            outcome = rec.get("outcome")
            if outcome == "dead" and rec.get("reason") == "fetch_failed":
                if is_terminal_fetch_error(rec.get("fetch_error")):
                    resolved.add(pid)  # legacy line for a permanently dead post
                else:
                    failed.setdefault(pid, None)
            else:
                # Anything else means the fetch actually reached the post.
                resolved.add(pid)
    return [pid[3:] if pid.startswith("t3_") else pid
            for pid in failed if pid not in resolved]


def run_retry_failed(args) -> None:
    """Orthogonal pass over the log's unresolved dead/fetch_failed rows. Re-fetches
    each with the same 429 backoff and routing as the forward sweep -- but WITHOUT
    its consecutive-failure breaker (see FETCH_FAIL_ABORT_THRESHOLD), and never
    touching the positional `next_row` cursor (this is not a resume)."""
    db_path = args.db
    if not db_path.exists():
        print(f"No index at {db_path}. Run `python hash_index.py build` first.", file=sys.stderr)
        sys.exit(1)
    _configure_pillow()

    BACKFILL_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    retry_ids = load_retry_ids()
    if not retry_ids:
        print("No unresolved fetch_failed rows in the log -- nothing to retry.")
        return

    rows = load_rows()
    permalink_by_id = {csv_id: permalink for csv_id, permalink in rows}
    manifest = load_manifest()
    known_post_ids = {e.get("post_id") for e in manifest.values()}

    state = load_state()
    with sqlite3.connect(db_path) as conn:
        state.setdefault("hash_bits", read_hash_bits(conn))
    hash_size = int(round(state["hash_bits"] ** 0.5))

    tomb_bits, signatures = load_signatures()
    if signatures and tomb_bits != state["hash_bits"]:
        print(f"warning: tombstones.json hash_bits ({tomb_bits}) != index hash_bits "
              f"({state['hash_bits']}); tombstone matching disabled.", file=sys.stderr)
        signatures = []

    # Preserve CSV order; drop ids no longer in the CSV or already in the manifest.
    work = [(cid, permalink_by_id[cid]) for cid in retry_ids
            if cid in permalink_by_id and ("t3_" + cid) not in known_post_ids]
    limited = work[:args.limit]

    print(f"Retry-failed: {len(retry_ids):,} unresolved fetch_failed id(s); "
          f"{len(work):,} still fetchable, processing {len(limited):,} this run "
          f"(limit {args.limit}).\n")

    batch = {k: 0 for k in OUTCOME_KEYS + ["processed"]}

    def checkpoint() -> None:
        # Manifest + cumulative totals only. The positional next_row cursor is
        # orthogonal to this pass and must NOT move.
        save_manifest(manifest)
        for k in OUTCOME_KEYS + ["processed"]:
            state["totals"][k] = state["totals"].get(k, 0) + batch[k]
            batch[k] = 0
        save_state(state)

    refetched = still_failed = forbidden = 0
    fail_causes: dict[str, int] = {}
    stopped_early = False
    for n, (csv_id, permalink) in enumerate(limited):
        fullname = "t3_" + csv_id
        batch["processed"] += 1
        known_post_ids.add(fullname)
        try:
            outcome = process_row(csv_id, fullname, permalink, manifest, db_path,
                                  args.sleep, batch, signatures, hash_size,
                                  fail_causes=fail_causes)
        except RateLimited:
            checkpoint()
            _cleanup_tmp()
            print(f"\n! Reddit is rate-limiting (HTTP 429 persisted through backoff).\n"
                  f"  Stopped after {n} row(s); the rest stay queued. Wait for the\n"
                  f"  throttle to clear, then re-run `python backfill.py --retry-failed`.",
                  file=sys.stderr)
            stopped_early = True
            break
        print(f"  [{n}] {fullname} {outcome}")

        # NO consecutive-failure breaker here -- see FETCH_FAIL_ABORT_THRESHOLD.
        # This work list is made of ids that already failed once, minus the ones
        # that since succeeded, so deterministic per-post failures pile up at the
        # head and a streak test would abort every run in the same first few rows
        # and never reach the untried tail. A persistent 429 (RateLimited, above)
        # is the only real throttle signal in this mode; everything else is a
        # per-post failure to skip over.
        if outcome.startswith(FETCH_FAILED_LABEL):
            still_failed += 1
        elif outcome == FORBIDDEN_LABEL:
            # Neither recovered nor still-queued: retired for good.
            forbidden += 1
        else:
            refetched += 1

        if batch["processed"] % CHECKPOINT_EVERY == 0:
            checkpoint()

    if not stopped_early:
        checkpoint()
        _cleanup_tmp()

    remaining = len(work) - (refetched + still_failed + forbidden)
    print(f"\nRetry-failed: {refetched:,} re-fetched OK, {forbidden:,} retired as "
          f"{FORBIDDEN_LABEL}, {still_failed:,} still fetch_failed this run; "
          f"~{max(0, remaining):,} unresolved id(s) remain.")
    if fail_causes:
        print("\n  Still failing, by cause:")
        for cause, count in sorted(fail_causes.items(), key=lambda kv: -kv[1]):
            print(f"    {cause:<24} {count:>6,}")
        print("    (an HTTP 4xx here is a permanently gone post, not throttling. 403 is\n"
              "     retired automatically; the rest keep re-queueing on every run.)")
    if remaining > 0:
        print("  Re-run `python backfill.py --retry-failed` to continue.")


def process_row(csv_id, fullname, permalink, manifest, db_path, sleep, batch,
                signatures, hash_size, fail_causes=None) -> str:
    """Fetch + classify one post and route its image(s). Updates `batch` counts
    and returns a short outcome label for the progress line. Never raises.

    A fetch failure returns a label PREFIXED with FETCH_FAILED_LABEL and
    suffixed with the cause, so callers must test it with `.startswith`, not
    `==`. Pass a dict as `fail_causes` to also collect a cause -> count tally
    for the run summary."""
    try:
        html, fetch_error = fetch_post_html(csv_id)
        time.sleep(sleep)
        if html is None:
            batch["dead"] += 1
            if is_terminal_fetch_error(fetch_error):
                # Terminal: a restricted/quarantined sub, not a transient
                # failure. Recorded under its own reason so load_retry_ids()
                # treats it as resolved and never queues it again. Not counted
                # in fail_causes -- that tally is "still worth retrying".
                log_outcome({"post_id": fullname, "outcome": "dead",
                             "reason": FORBIDDEN_REASON, "fetch_error": fetch_error})
                return FORBIDDEN_LABEL
            # `reason` stays the bare "fetch_failed" -- load_retry_ids() keys off
            # it, and pre-fix log lines must keep matching. The cause goes in its
            # own field.
            log_outcome({"post_id": fullname, "outcome": "dead",
                         "reason": "fetch_failed", "fetch_error": fetch_error})
            kind = fail_kind(fetch_error)
            if fail_causes is not None:
                fail_causes[kind] = fail_causes.get(kind, 0) + 1
            return f"{FETCH_FAILED_LABEL} [{kind}]"

        kind, image_urls, reason = extract_images(html, csv_id, permalink)
        if kind == "dead":
            batch["dead"] += 1
            log_outcome({"post_id": fullname, "outcome": "dead", "reason": reason})
            return f"dead ({reason})"

        title = extract_title(html, permalink)
        subreddit = extract_subreddit(permalink)

        if kind == "single":
            oc = process_image(csv_id, fullname, None, title, subreddit, permalink,
                               image_urls[0], manifest, db_path, sleep, signatures, hash_size)
            batch[oc] += 1
            return oc

        # gallery -- one entry per image, hashed/routed independently.
        per = []
        for index, url in enumerate(image_urls, start=1):
            oc = process_image(csv_id, fullname, index, title, subreddit, permalink,
                               url, manifest, db_path, sleep, signatures, hash_size)
            batch[oc] += 1
            per.append(oc)
        return "gallery[" + ",".join(per) + "]"
    except RateLimited:
        raise  # hard throttle -> propagate so run() can stop the sweep cleanly
    except Exception as exc:  # last-resort guard: one bad row never stops the sweep
        batch["failed"] += 1
        log_outcome({"post_id": fullname, "outcome": "failed",
                     "reason": f"{type(exc).__name__}: {exc}"})
        return f"failed ({type(exc).__name__})"


def _cleanup_tmp() -> None:
    """Drop any stray temp downloads (kept images were moved out already)."""
    for p in TMP_DIR.glob("*"):
        try:
            p.unlink()
        except OSError:
            pass


def print_summary(state, total_rows) -> None:
    t = state["totals"]
    next_row = state.get("next_row", 0)  # the true persisted cursor the next run resumes from
    print("\n" + "=" * 60)
    print("Backfill summary")
    print("=" * 60)
    header = f"  {'bucket':<14}  {'cumulative':>10}"
    print(header)
    print(f"  {'-'*14}  {'-'*10}")
    for k in ["processed"] + OUTCOME_KEYS:
        print(f"  {k:<14}  {t.get(k, 0):>10,}")
    remaining = max(0, total_rows - next_row)
    print(f"\n  next_row = {next_row:,} of {total_rows:,}  ({remaining:,} rows remaining)")
    if remaining:
        print(f"  Resume with: python backfill.py --limit {DEFAULT_LIMIT}")
    else:
        print("  Backfill complete -- all CSV rows processed.")
    print(f"  Log: {LOG_PATH.resolve()}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    # Archive/match paths can contain non-Latin (e.g. Japanese) characters; the
    # Windows console defaults to cp1252 and would crash printing them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Rows to process this run (default {DEFAULT_LIMIT}).")
    parser.add_argument("--offset", type=int, default=None,
                        help="Explicit start row; overrides the saved cursor. Re-runs are idempotent.")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                        help=f"Polite delay between requests, seconds (default {DEFAULT_SLEEP}).")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"pHash index path (default {DEFAULT_DB}).")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Re-process rows the log recorded dead/fetch_failed "
                             "(likely rate-limit false negatives), independent of "
                             "the resume cursor. Honors --limit.")
    args = parser.parse_args()
    if args.retry_failed:
        run_retry_failed(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
