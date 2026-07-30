"""Build the safe-to-unsave whitelist for the browser bulk-unsave userscript.

Reddit's saved list has a ~1,000-item listing ceiling. Once it's full, new saves
are stored but never surface in the list or the RSS feed, so Oshiire's ingest
front door is wedged shut. The fix is to bulk-*unsave* only the posts Oshiire has
PROVABLY captured, freeing headroom. This script builds that whitelist; the
companion `oshiire_unsave.user.js` clicks the unsave links in the owner's own
browser.

READ-ONLY. Touches only `manifest.json` (via manifest.py) and
`data/backfill/backfill_log.jsonl`. No network, no `.env`, no secrets. Its only
output is `data/unsave_list.json` -- a flat array of BARE base-36 post ids (`t3_abc123` ->
`abc123`), the form the userscript matches against old.reddit's `data-fullname`.

Safety model (an id is emitted only if the post is provably captured):
  * Every manifest entry (any status) -- captured. Keyed on the `post_id` FIELD,
    deduped, since gallery posts contribute several entries sharing one parent id.
  * backfill log `owned` -- image already in the local archive (no manifest entry
    is created for these, so the log is the ONLY record).
  * backfill log `dead` with reason removed/no_image/gallery_no_images -- the post
    is gone; nothing to lose.
  * NOT emitted: log `failed` and `dead`/`fetch_failed` are transient (the post may
    be live and uncaptured) -- they TAINT their post_id and are subtracted from the
    final list even if the post is otherwise in the manifest (a gallery may have one
    image captured and a sibling image that failed; unsaving would lose the retry).
  * log `new`/`uncertain` create manifest entries, so they are authorized via
    manifest membership only -- never from the log alone.
"""
import json
import os
import sys
from pathlib import Path

from manifest import load_manifest

LOG_PATH = Path("data/backfill") / "backfill_log.jsonl"
OUTPUT_PATH = Path("data/unsave_list.json")

# `dead` reasons that mean the post is genuinely gone (safe to unsave). A `dead`
# with reason "fetch_failed" is NOT here: a failed HTTP fetch isn't proof of
# death (could be a transient 429/timeout on a live, uncaptured post).
SAFE_DEAD_REASONS = {"removed", "no_image", "gallery_no_images"}


def bare(post_id: str) -> str:
    """`t3_abc123` -> `abc123`. Leaves an already-bare id untouched."""
    return post_id[3:] if post_id.startswith("t3_") else post_id


def collect_manifest_ids(manifest: dict) -> set[str]:
    """Bare ids of every manifest entry, deduped on the `post_id` FIELD (gallery
    entries share one parent id across their suffixed dict keys)."""
    return {
        bare(entry["post_id"])
        for entry in manifest.values()
        if entry.get("post_id")
    }


def scan_backfill_log(path: Path) -> tuple[set[str], set[str], set[str], set[str]]:
    """Return (owned, dead_safe, tainted_failed, tainted_fetch_failed) as sets of
    BARE ids. `owned`/`dead_safe` are candidates to add; the two tainted sets are
    posts that must be kept saved (subtracted from the final list)."""
    owned: set[str] = set()
    dead_safe: set[str] = set()
    tainted_failed: set[str] = set()
    tainted_fetch_failed: set[str] = set()

    if not path.exists():
        print(f"note: no backfill log at {path} -- using manifest only.",
              file=sys.stderr)
        return owned, dead_safe, tainted_failed, tainted_fetch_failed

    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                print(f"warning: skipping unparseable log line {lineno}",
                      file=sys.stderr)
                continue
            pid = rec.get("post_id")
            if not pid:
                continue
            bid = bare(pid)
            outcome = rec.get("outcome")
            reason = rec.get("reason")

            if outcome == "failed":
                tainted_failed.add(bid)
            elif outcome == "dead" and reason == "fetch_failed":
                tainted_fetch_failed.add(bid)
            elif outcome == "owned":
                owned.add(bid)
            elif outcome == "dead" and reason in SAFE_DEAD_REASONS:
                dead_safe.add(bid)
            # new / uncertain / manifest_skip: authorized via the manifest, not here.

    return owned, dead_safe, tainted_failed, tainted_fetch_failed


def build_whitelist():
    manifest = load_manifest()
    manifest_ids = collect_manifest_ids(manifest)

    owned, dead_safe, tainted_failed, tainted_fetch_failed = scan_backfill_log(LOG_PATH)
    tainted = tainted_failed | tainted_fetch_failed

    whitelist = (manifest_ids | owned | dead_safe) - tainted

    # Non-overlapping attribution so the "from_*" counts sum to the total.
    # Priority: manifest > backfill-owned > backfill-dead.
    from_manifest = manifest_ids - tainted
    from_backfill_owned = owned - manifest_ids - tainted
    from_backfill_dead = dead_safe - manifest_ids - owned - tainted

    return {
        "whitelist": whitelist,
        "from_manifest": from_manifest,
        "from_backfill_owned": from_backfill_owned,
        "from_backfill_dead": from_backfill_dead,
        "tainted_failed": tainted_failed,
        "tainted_fetch_failed": tainted_fetch_failed,
    }


def load_previous_ids(path: Path) -> "set[str] | None":
    """The id set from a prior export, or None if there's no readable previous
    file. Read BEFORE overwriting so we can report how many ids are NEW this
    run -- that count is the number of posts to drain in the browser."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return set(data) if isinstance(data, list) else None


def print_summary(r: dict) -> None:
    total = len(r["whitelist"])
    n_manifest = len(r["from_manifest"])
    n_owned = len(r["from_backfill_owned"])
    n_dead = len(r["from_backfill_dead"])

    print("Oshiire unsave whitelist")
    print(f"  from manifest .............. {n_manifest:>7,}")
    print(f"  from backfill (owned) ...... {n_owned:>7,}")
    print(f"  from backfill (dead) ....... {n_dead:>7,}")
    print("  " + "-" * 37)
    print(f"  TOTAL safe to unsave ....... {total:>7,}")
    print(f"  excluded (failed) .......... {len(r['tainted_failed']):>7,}")
    print(f"  excluded (dead/fetch_failed) {len(r['tainted_fetch_failed']):>7,}"
          "   <- kept SAVED on purpose")

    # Attribution must partition the whitelist exactly.
    assert n_manifest + n_owned + n_dead == total, (
        f"count mismatch: {n_manifest}+{n_owned}+{n_dead} != {total}")


def main() -> None:
    # Manifest titles/paths may contain non-Latin (e.g. Japanese) text; the
    # Windows console defaults to cp1252 and would crash printing them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    r = build_whitelist()

    # Read the prior export BEFORE overwriting, so we can report how many ids
    # are newly safe-to-unsave since last time (the drain count).
    previous = load_previous_ids(OUTPUT_PATH)
    if previous is None:
        new_since_last, prev_note = len(r["whitelist"]), "(no previous export)"
    else:
        new_since_last = len(r["whitelist"] - previous)
        prev_note = f"(previous export had {len(previous):,})"

    ids = sorted(r["whitelist"])  # sorted -> stable diffs
    tmp = OUTPUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ids, indent=0), encoding="utf-8")
    os.replace(tmp, OUTPUT_PATH)

    print_summary(r)
    print(f"  NEW since last export ...... {new_since_last:>7,}   <- posts to drain")
    print(f"  {prev_note}")
    print(f"Wrote {OUTPUT_PATH} ({len(ids):,} ids)")


if __name__ == "__main__":
    main()
