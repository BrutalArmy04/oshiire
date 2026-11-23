"""Slice 0: walk the owner's logged-in Reddit saved listing, download images to staging/,
and record pending_review entries in manifest.json. No AI, no UI, no archiving.

Also owns triage for the `skipped` entries it writes -- posts with no
importable image. Those never reach the review UI (it queues `pending_review`
only) or resolve.py (flagged entries only), so without a way to see and close
them they accumulate unseen:

    python src/ingest.py --list-skipped            # what got skipped, and why
    python src/ingest.py --retry-skipped [KEY ...] # re-attempt the gallery fetch
    python src/ingest.py --reject-skipped KEY ...  # close one out for good

These live here rather than in review.py because a skipped entry has NO
downloaded file: putting one in the review queue would hand the UI an entry
with no image to show, judge, or delete.
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

import redditclient
import savedlisting
from manifest import load_manifest, save_manifest
from tag import run_tagging
from useragent import (
    RedditAuthWall,
    build_headers,
    build_user_agent,
    is_login_wall,
)

STAGING_DIR = Path("staging")
IMAGE_EXTENSIONS = savedlisting.IMAGE_EXTENSIONS

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
# "auth_walled" joins them: it means the fetch hit Reddit's login page, which
# a valid REDDIT_SESSION_COOKIE turns back into a real page -- a condition that
# clears from outside the entry, so it must stay retryable.
RETRYABLE_SKIP_REASONS = {
    "gallery_post", "gallery_fetch_failed", "gallery_parse_error", "auth_walled",
}


def download_image(url, dest_path):
    resp = requests.get(
        url, headers={"User-Agent": build_user_agent()}, stream=True, timeout=30
    )
    resp.raise_for_status()
    with dest_path.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


class GalleryParseError(Exception):
    """Raised when the permalink fetched fine but gallery-tile markup looks
    present-but-broken -- distinct from a confirmed-empty (non-gallery) page,
    so the caller can keep this retryable instead of settling on it forever."""


def _force_old_reddit_host(permalink):
    """Rewrite a permalink onto old.reddit.com, whatever host it arrived on.

    Not the mirror image of _normalize_feed_host: the gallery-tile markup
    GALLERY_TILE_RE matches exists ONLY in old.reddit's server-rendered HTML.
    www.reddit.com returns a JS shell with no tiles, which would parse as a
    confirmed-empty gallery -- a wrong answer that looks like a right one."""
    parts = urlsplit(permalink)
    if not parts.netloc:
        return permalink
    netloc = "old.reddit.com" + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))


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
        redditclient.RedditFetchError -- network/timeout/non-2xx.
        GalleryParseError -- fetch succeeded and "gallery-tile" markup is
            present in the page, but no tile matched the structured regex --
            likely markup drift, not a genuine non-gallery page.
        RedditAuthWall -- fetch returned 200 but the body is the login page.
    """
    resp = redditclient.get(
        _force_old_reddit_host(permalink),
        headers=build_headers(over18=True),
        timeout=15,
    )
    redditclient.raise_for_status(resp)
    if is_login_wall(resp.url, resp.text):
        raise RedditAuthWall(
            f"permalink fetch returned Reddit's login page: {permalink}"
        )
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


def retry_skipped_galleries(manifest, keys=None):
    """Re-attempts every status=="skipped" entry whose reason is retryable
    (see RETRYABLE_SKIP_REASONS). Runs automatically on every ingest call --
    self-limiting, since each candidate settles into either expanded per-
    image entries or a new, non-retryable skip_reason after one attempt.
    Returns counts for the summary print.

    `keys` narrows the attempt to specific manifest keys (--retry-skipped);
    the retryable-reason rule still applies, so naming a settled entry never
    forces a pointless fetch."""
    counts = {"retried": 0, "expanded": 0, "still_failed": 0, "confirmed_non_gallery": 0}

    candidate_keys = [
        key for key, entry in manifest.items()
        if entry.get("status") == "skipped" and _skip_reason(entry) in RETRYABLE_SKIP_REASONS
        and (keys is None or key in keys)
    ]

    for key in candidate_keys:
        entry = manifest[key]
        counts["retried"] += 1
        fetched_at = datetime.now(timezone.utc).isoformat()
        permalink = entry.get("permalink", "")

        try:
            images = fetch_gallery_images(permalink)
        except redditclient.RedditFetchError as exc:
            print(f"retry: gallery fetch failed for {key}: {exc}", file=sys.stderr)
            entry["skip_reason"] = "gallery_fetch_failed"
            entry.pop("reason", None)
            entry["fetched_at"] = fetched_at
            counts["still_failed"] += 1
            continue
        except RedditAuthWall as exc:
            print(f"retry: auth wall for {key}: {exc}", file=sys.stderr)
            entry["skip_reason"] = "auth_walled"
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


# --------------------------------------------------------------------------- #
# Skipped-entry triage (see module docstring)
# --------------------------------------------------------------------------- #
def _skipped_entries(manifest):
    """Every skipped entry, oldest first -- the order they were encountered."""
    return sorted(
        ((key, entry) for key, entry in manifest.items() if entry.get("status") == "skipped"),
        key=lambda item: item[1].get("fetched_at", ""),
    )


def list_skipped(manifest) -> None:
    """Print what got skipped and why, marking which reasons ingest is already
    retrying by itself -- otherwise a self-healing entry looks like one needing
    a decision."""
    entries = _skipped_entries(manifest)
    if not entries:
        print("No skipped entries -- nothing was passed over at ingest.")
        return

    print(f"{len(entries)} skipped entr(ies). None has a downloaded image:\n")
    for key, entry in entries:
        reason = _skip_reason(entry) or "(no reason recorded)"
        state = ("retried automatically on every ingest run"
                 if reason in RETRYABLE_SKIP_REASONS else "settled -- never retried")
        print(f"  {key}  [{reason}]  -- {state}")
        print(f"      {entry.get('title', '')}")
        print(f"      r/{entry.get('subreddit', '')}  {entry.get('permalink', '')}")
    print(
        "\nTo act on one:\n"
        "  python src/ingest.py --retry-skipped <KEY>    re-attempt the gallery fetch now\n"
        "  python src/ingest.py --reject-skipped <KEY>   close it out (status -> rejected)\n"
        "A rejected entry keeps its manifest record, so dedup never re-downloads it."
    )


def reject_skipped(manifest, keys) -> None:
    """Mark named skipped entries `rejected` -- the only way to close one out.

    Status is the only field written: there is no staging file to delete (a
    skipped entry never had one), and `skip_reason` is left in place so the
    record still explains itself. Dedup keys on the post_id field regardless
    of status, so this can't cause a re-download; it just stops the entry
    being retried and reported forever.
    """
    changed = []
    for key in keys:
        entry = manifest.get(key)
        if entry is None:
            print(f"  no such manifest entry: {key}", file=sys.stderr)
            continue
        status = entry.get("status")
        if status != "skipped":
            print(f"  {key}: status is '{status}', not 'skipped' -- left alone", file=sys.stderr)
            continue
        entry["status"] = "rejected"
        changed.append(key)

    if changed:
        save_manifest(manifest)
        print(f"Rejected {len(changed)} skipped entr(ies): {', '.join(changed)}")
    else:
        print("Nothing changed.")


def retry_skipped(manifest, keys) -> None:
    """Force the gallery re-fetch now instead of waiting for the next ingest.
    An empty `keys` means every retryable entry."""
    targets = set(keys) if keys else None
    if targets:
        unknown = [key for key in targets if key not in manifest]
        for key in unknown:
            print(f"  no such manifest entry: {key}", file=sys.stderr)

    counts = retry_skipped_galleries(manifest, keys=targets)
    if counts["retried"]:
        save_manifest(manifest)
    print(
        f"retried={counts['retried']} expanded={counts['expanded']} "
        f"still_failed={counts['still_failed']} "
        f"confirmed_non_gallery={counts['confirmed_non_gallery']}"
    )
    if not counts["retried"]:
        print(
            "Nothing was retryable. Only "
            f"{', '.join(sorted(RETRYABLE_SKIP_REASONS))} are re-fetchable; "
            "anything else is a settled non-image post -- use --reject-skipped "
            "to close it out."
        )


# --------------------------------------------------------------------------- #
# The saved-listing walk
# --------------------------------------------------------------------------- #
PAGE_DELAY_S = 3
MAX_PAGES = 45
RATE_LIMIT_BACKOFF = [30, 60, 120, 240]  # seconds, as backfill's

COOKIE_FIX = (
    "Copy a fresh login cookie into REDDIT_SESSION_COOKIE (Settings -> App -> "
    "setup window), then run ingest again."
)


def known_post_ids(manifest):
    """Every post_id FIELD in the manifest -- not the dict keys, since a gallery
    post's per-image entries are keyed `<post_id>_N` under one shared post_id."""
    return {entry.get("post_id") for entry in manifest.values()} - {None}


def _stub(row, fetched_at, **fields):
    entry = {
        "post_id": row["post_id"],
        "title": row["title"],
        "subreddit": row["subreddit"],
        "permalink": row["permalink"],
        **fields,
        "fetched_at": fetched_at,
    }
    if row["crosspost_root_subreddit"]:
        entry["crosspost_root_subreddit"] = row["crosspost_root_subreddit"]
    return entry


def _ingest_gallery(manifest, row, fetched_at, counts, state):
    post_id = row["post_id"]
    try:
        images = fetch_gallery_images(row["permalink"])
    except redditclient.RedditFetchError as exc:
        print(f"gallery fetch failed for {post_id}: {exc}", file=sys.stderr)
        manifest[post_id] = _stub(row, fetched_at, status="skipped",
                                  skip_reason="gallery_fetch_failed")
        counts["failed"] += 1
        return
    except RedditAuthWall as exc:
        print(f"gallery fetch hit the login wall for {post_id}: {exc}", file=sys.stderr)
        if not state["auth_wall_warned"]:
            print(
                "warning: gallery posts need a logged-in session. " + COOKIE_FIX
                + " Then run `python src/ingest.py --retry-skipped` to pick these up. "
                "Single-image posts are unaffected and keep ingesting.",
                file=sys.stderr,
            )
            state["auth_wall_warned"] = True
        manifest[post_id] = _stub(row, fetched_at, status="skipped",
                                  skip_reason="auth_walled")
        counts["failed"] += 1
        return
    except GalleryParseError as exc:
        print(f"gallery parse error for {post_id}: {exc}", file=sys.stderr)
        manifest[post_id] = _stub(row, fetched_at, status="skipped",
                                  skip_reason="gallery_parse_error")
        counts["failed"] += 1
        return

    if not images:
        manifest[post_id] = _stub(row, fetched_at, status="skipped",
                                  skip_reason="gallery_no_images")
        counts["skipped"] += 1
        return

    gallery_entries = _build_gallery_entries(
        post_id, row["title"], row["subreddit"], row["permalink"], fetched_at, images
    )
    if row["crosspost_root_subreddit"]:
        for entry in gallery_entries.values():
            entry["crosspost_root_subreddit"] = row["crosspost_root_subreddit"]
    manifest.update(gallery_entries)
    counts["downloaded"] += sum(
        1 for e in gallery_entries.values() if e["status"] == "pending_review"
    )
    counts["failed"] += sum(
        1 for e in gallery_entries.values() if e["status"] == "download_failed"
    )


def ingest_row(manifest, row, counts, state):
    """File one listing row into the manifest: download it, expand it, or record
    why it was skipped. Anything unexpected becomes an `error` entry rather than
    stopping the walk."""
    post_id = row["post_id"]
    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        kind, reason, image_url = savedlisting.classify_row(row)

        if kind == "skip":
            manifest[post_id] = _stub(row, fetched_at, status="skipped", skip_reason=reason)
            counts["skipped"] += 1
        elif kind == "gallery":
            _ingest_gallery(manifest, row, fetched_at, counts, state)
        else:
            ext = Path(urlparse(image_url).path).suffix or ".jpg"
            local_path = STAGING_DIR / f"{post_id}{ext}"
            try:
                download_image(image_url, local_path)
            except requests.RequestException as exc:
                manifest[post_id] = _stub(row, fetched_at, status="download_failed",
                                          reason=str(exc))
                counts["failed"] += 1
            else:
                manifest[post_id] = _stub(row, fetched_at, image_url=image_url,
                                          local_path=str(local_path),
                                          status="pending_review")
                counts["downloaded"] += 1
    except Exception as exc:
        manifest[post_id] = {
            "post_id": post_id,
            "status": "error",
            "reason": str(exc),
            "fetched_at": fetched_at,
        }
        counts["failed"] += 1


def _fetch_with_backoff(url):
    """One listing page, retrying a 429 or a network error on the same 30/60/120/
    240s schedule backfill uses. Auth failures are never retried."""
    for attempt in range(len(RATE_LIMIT_BACKOFF) + 1):
        try:
            return savedlisting.fetch_page(url)
        except (savedlisting.SavedRateLimited, redditclient.RedditFetchError) as exc:
            if attempt == len(RATE_LIMIT_BACKOFF):
                raise savedlisting.SavedRateLimited(str(exc)) from exc
            wait = RATE_LIMIT_BACKOFF[attempt]
            print(f"listing fetch failed ({exc}); waiting {wait}s "
                  f"(attempt {attempt + 1}/{len(RATE_LIMIT_BACKOFF)})", file=sys.stderr)
            time.sleep(wait)


def walk_saved(manifest, username, counts):
    """Walk the saved listing newest-first, filing every unknown post.

    Stops at the first page with NO unknown post_id (everything past it was seen
    on an earlier run), at the last page, or at MAX_PAGES. The manifest is saved
    after each page that added something, so a crash or a refusal mid-walk keeps
    the pages already done. Auth/rate-limit/listing errors propagate to the
    caller AFTER those saves; `counts["pages"]` is the pages fetched so far."""
    known = known_post_ids(manifest)
    state = {"auth_wall_warned": False}
    url = savedlisting.listing_url(username)
    while url and counts["pages"] < MAX_PAGES:
        if counts["pages"]:
            time.sleep(PAGE_DELAY_S)
        rows, next_url = _fetch_with_backoff(url)
        counts["pages"] += 1

        fresh = [row for row in rows if row["post_id"] not in known]
        if not fresh:
            return
        for row in fresh:
            if row["post_id"] in known:
                continue
            known.add(row["post_id"])
            ingest_row(manifest, row, counts, state)
        save_manifest(manifest)
        url = next_url
    if url:
        print(f"stopped at the {MAX_PAGES}-page limit; run ingest again to continue "
              "further back.", file=sys.stderr)


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Walk your logged-in saved listing and download new images. "
                    "Also triages the `skipped` entries ingest records."
    )
    parser.add_argument("--list-skipped", action="store_true",
                        help="List skipped entries (no image was importable) and exit.")
    parser.add_argument("--retry-skipped", nargs="*", metavar="KEY", default=None,
                        help="Re-attempt the gallery fetch for the given manifest key(s), "
                             "or every retryable skipped entry when given none.")
    parser.add_argument("--reject-skipped", nargs="+", metavar="KEY",
                        help="Mark the given skipped entr(ies) rejected to close them out.")
    return parser.parse_args()


def main():
    args = _parse_args()

    # Loaded before the triage dispatch, not after it: --retry-skipped fetches
    # permalinks, and that fetch now needs REDDIT_SESSION_COOKIE out of .env.
    load_dotenv()

    # Triage modes read/write only the manifest -- no listing walk, so they work
    # even when the login cookie is missing or expired (--retry-skipped makes its
    # own gallery fetches).
    if args.list_skipped:
        list_skipped(load_manifest())
        return
    if args.reject_skipped:
        reject_skipped(load_manifest(), args.reject_skipped)
        return
    if args.retry_skipped is not None:
        retry_skipped(load_manifest(), args.retry_skipped)
        return

    username = os.environ.get("REDDIT_USERNAME", "").strip()
    cookie = os.environ.get("REDDIT_SESSION_COOKIE", "").strip()
    if not username or not cookie:
        missing = " and ".join(
            name for name, value in (("REDDIT_USERNAME", username),
                                     ("REDDIT_SESSION_COOKIE", cookie)) if not value
        )
        print(
            f"{missing} not set. Ingest reads your logged-in saved listing, so it needs "
            "your Reddit username and login cookie (Settings -> App -> setup window).",
            file=sys.stderr,
        )
        sys.exit(1)

    manifest = load_manifest()
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    counts = {"downloaded": 0, "skipped": 0, "failed": 0, "pages": 0}
    failure = None
    try:
        walk_saved(manifest, username, counts)
    except savedlisting.SavedAuthError as exc:
        failure = f"{savedlisting.auth_message(exc, username)} {COOKIE_FIX}"
    except savedlisting.SavedRateLimited as exc:
        failure = (f"Rate limited, try later: Reddit would not serve the saved listing "
                   f"({exc}). What was fetched so far is saved.")
    except savedlisting.SavedListingError as exc:
        failure = f"Stopped: {exc}. What was fetched so far is saved."

    if failure:
        save_manifest(manifest)
        print(f"downloaded={counts['downloaded']} skipped={counts['skipped']} "
              f"failed={counts['failed']} pages={counts['pages']}")
        sys.stdout.flush()
        # Last, so the job page's "Ingest failed: ..." shows the reason.
        print(failure, file=sys.stderr)
        sys.exit(1)

    retry_counts = retry_skipped_galleries(manifest)

    tagged = run_tagging(manifest)

    save_manifest(manifest)
    print(
        f"downloaded={counts['downloaded']} skipped={counts['skipped']} "
        f"failed={counts['failed']} tagged={len(tagged)} pages={counts['pages']} "
        f"gallery_retried={retry_counts['retried']} gallery_expanded={retry_counts['expanded']}"
    )


if __name__ == "__main__":
    main()
