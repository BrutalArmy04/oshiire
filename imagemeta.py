"""Per-image perceptual hash + dimension cache, and the duplicate lookup the
review UI shows.

Exists because two review-time features need the same thing -- one decode of
each staging image yielding both a pHash (duplicate detection) and its pixel
dimensions (wallpaper suitability hints). Computing them together and caching
them on the manifest entry means an image is opened once, ever.

Why the duplicate corpus is split in two:
  * `archived` entries no longer have a staging file -- it was moved into
    ARCHIVE_DIR. Those are ALREADY hashed by hash_index.py's archive index,
    and a manifest entry's `archive_path` is stored in exactly the form that
    index uses for `rel_path` (POSIX, relative to ARCHIVE_DIR), so the two
    join directly and the index is reused rather than recomputed.
  * `pending_review` / `approved` entries still live in staging/ and are
    hashed here, cached onto the entry.
  * `rejected` entries have no file at all (Reject deletes it), so they can
    only be compared if they were hashed BEFORE deletion -- which review.py
    now does. Entries rejected before that are permanently uncomparable.

Hashing/distance primitives come from hash_index.py; this module never
reimplements them. Strictly read-only over ARCHIVE_DIR.
"""
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hash_index import (
    DEFAULT_DB,
    DEFAULT_HASH_SIZE,
    _configure_pillow,
    compute_phash,
    read_hash_bits,
)

# Thresholds calibrated on THIS archive by calibrate.py: true matches sit at
# 0-8, 9-11 is an uncertain band, 12+ is the spurious-collision noise floor.
# Duplicated from backfill.py rather than imported -- importing that module
# pulls in its network/User-Agent setup, which the review UI must never touch
# (same "duplicated rather than imported" reasoning as review.py's OC regex).
DUPLICATE_MAX = 8   # <= 8  : same artwork
UNCERTAIN_MAX = 11  # 9..11 : possibly related


@dataclass
class DuplicateMatch:
    """One near-identical image found for the entry under review."""
    distance: int
    title: str
    where: str                      # human-readable location ("Fate/", "staging")
    post_id: Optional[str] = None
    image_path: Optional[str] = None  # absolute, for a thumbnail; None if gone
    # Which half of the corpus this came from: "archive" (a row in the pHash
    # index) or "staging" (another manifest entry). Callers need this to tell
    # an already-filed keeper from a still-under-review twin, and it can NOT
    # be inferred from post_id -- most archive rows predate the manifest and
    # have post_id=None.
    source: str = "staging"
    # This match's OWN hash, as an int. Not for display -- it exists so
    # _collapse_copies can tell "the same artwork filed several times" from
    # "several different near-matches" without re-reading any file.
    phash_int: Optional[int] = None

    @property
    def is_certain(self) -> bool:
        return self.distance <= DUPLICATE_MAX


# --------------------------------------------------------------------------- #
# Hash size
# --------------------------------------------------------------------------- #
def index_hash_size(db_path: Path = DEFAULT_DB) -> int:
    """Hash size (N of an NxN hash) the archive index was built with.

    Staging hashes MUST use this same size or Hamming distances against the
    index are meaningless. Falls back to the default when there's no index yet.
    """
    if not db_path.exists():
        return DEFAULT_HASH_SIZE
    conn = sqlite3.connect(db_path)
    try:
        return int(round(read_hash_bits(conn) ** 0.5))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Per-entry cache
# --------------------------------------------------------------------------- #
def ensure_image_meta(entry: dict, hash_size: int) -> bool:
    """Populate phash/phash_bits/width/height on `entry` if absent.

    Returns True when the entry ends up with usable metadata. Mutates the
    entry in place but does NOT save the manifest -- callers batch that, so a
    warm-up pass writes once instead of once per image. An entry already
    carrying a hash at a DIFFERENT bit depth is recomputed, so changing the
    index's hash size doesn't silently leave incomparable hashes behind.
    """
    hash_bits = hash_size * hash_size
    if entry.get("phash") and entry.get("phash_bits") == hash_bits:
        return True

    local_path = entry.get("local_path")
    if not local_path:
        return False
    path = Path(local_path)
    if not path.exists():
        return False

    try:
        phash, width, height = compute_phash(path, hash_size)
    except Exception:
        # Corrupt/unreadable image: skip it rather than breaking the review
        # loop. The entry simply won't take part in duplicate detection.
        return False

    entry["phash"] = phash
    entry["phash_bits"] = hash_bits
    entry["width"] = width
    entry["height"] = height
    return True


def warm_image_meta(manifest: dict, hash_size: int, progress=None) -> int:
    """Hash every manifest entry whose staging file still exists and that
    lacks a current hash. Returns how many were newly computed.

    Done as one up-front pass rather than purely on demand because duplicate
    detection is only as good as its corpus -- hashing lazily would mean an
    entry could only be compared against images already viewed this session.
    Cached on the manifest, so this is a real cost once and ~free thereafter.
    """
    _configure_pillow()
    hash_bits = hash_size * hash_size
    todo = [
        entry for entry in manifest.values()
        if entry.get("local_path")
        and not (entry.get("phash") and entry.get("phash_bits") == hash_bits)
        and Path(entry["local_path"]).exists()
    ]
    computed = 0
    for done, entry in enumerate(todo, start=1):
        if ensure_image_meta(entry, hash_size):
            computed += 1
        if progress is not None:
            progress(done, len(todo))
    return computed


# --------------------------------------------------------------------------- #
# Duplicate search
# --------------------------------------------------------------------------- #
def load_archive_hashes(db_path: Path = DEFAULT_DB) -> list[tuple[str, int]]:
    """Load [(rel_path, phash_as_int)] from the archive index, once.

    Read into memory because the review UI queries it per image and a linear
    XOR scan over even tens of thousands of ints is microseconds -- far
    cheaper than re-reading SQLite each time, and no index structure needed.
    """
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT rel_path, phash FROM images").fetchall()
    finally:
        conn.close()
    hashes = []
    for rel_path, phash in rows:
        try:
            hashes.append((rel_path, int(phash, 16)))
        except (TypeError, ValueError):
            continue
    return hashes


def build_archive_path_map(manifest: dict) -> dict:
    """{archive_path: post_id} so an index hit resolves back to its manifest
    entry (for a title, rather than just a filename)."""
    return {
        entry["archive_path"]: post_id
        for post_id, entry in manifest.items()
        if entry.get("archive_path")
    }


def _collapse_copies(matches: list[DuplicateMatch], prefer=None) -> list[DuplicateMatch]:
    """One entry per DUPLICATE, not per corpus row. `matches` must already be
    sorted nearest-first.

    Two collapses, because one artwork reaches this list several ways:
      * identity -- the same post_id, or the same file path, seen twice.
      * copies -- one artwork filed at several archive paths (the franchise
        folder plus its Wallpaper/PC and Wallpaper/<phone> copies, or plain
        duplicate files inside the archive). Those rows have distinct paths
        and usually no post_id at all, so identity can't catch them: a
        candidate within DUPLICATE_MAX of an ALREADY-KEPT one is the same
        artwork again and is folded away. That test is safe precisely because
        every candidate here is already near the query -- two genuinely
        different duplicates of one image are necessarily near each other
        too, so collapsing them loses nothing the reviewer can act on
        separately.

    `prefer` is an optional `(match) -> bool` predicate naming the copies a
    caller can actually ACT on. Distance still decides the order of the groups;
    within a group it decides only the tiebreak. Without it, a group's
    representative was simply its nearest member -- so a nearer copy the
    caller can't act on (a rejected twin, whose file is gone) silently deleted
    the farther one it could (the archived keeper), and the banner ended up
    showing a certain duplicate with no reject button. Picking the nearest
    PREFERRED member instead makes "every certain match shown is actionable"
    true by construction rather than by luck of which copy happened to be
    nearest. Falls back to the nearest member when no member qualifies -- a
    duplicate with no keeper still has to be reported, just without a button.
    """
    groups: list[list[DuplicateMatch]] = []
    seen_ids: set = set()
    for match in matches:
        identity = match.post_id or match.image_path
        if identity is not None and identity in seen_ids:
            continue
        if identity is not None:
            seen_ids.add(identity)
        for group in groups:
            if match.phash_int is not None and any(
                other.phash_int is not None
                and (match.phash_int ^ other.phash_int).bit_count() <= DUPLICATE_MAX
                for other in group
            ):
                group.append(match)
                break
        else:
            groups.append([match])

    # `matches` arrives sorted, so each group is too: min() on a boolean key is
    # the FIRST preferred member, i.e. the nearest one, and falls back to the
    # nearest overall when none is preferred.
    if prefer is None:
        return [group[0] for group in groups]
    return [min(group, key=lambda m: not prefer(m)) for group in groups]


def find_duplicates(
    post_id: str,
    entry: dict,
    manifest: dict,
    archive_hashes: list[tuple[str, int]],
    archive_path_map: dict,
    archive_dir: Optional[Path],
    max_distance: int = UNCERTAIN_MAX,
    limit: int = 3,
    exclude=None,
    indexed_paths: Optional[set] = None,
    prefer=None,
) -> list[DuplicateMatch]:
    """Nearest DISTINCT duplicates of `entry` across the archive index and
    other manifest entries, within `max_distance`, nearest first.

    Skips the entry itself, its own archived copy (an archived entry's index
    row IS this image), and its gallery siblings -- a sibling is a different
    image of the same post, never a duplicate. Sibling detection has to go
    through the `post_id` FIELD: gallery entries are keyed `t3_..._1`,
    `t3_..._2`, ... while sharing one parent post_id, so a dict-key comparison
    silently lets every sibling through.

    `exclude` is an optional `(post_id) -> bool` predicate letting a caller
    drop matches on rules this module can't know (the review UI hides twins it
    hasn't reached yet). It is applied BEFORE `limit`, so a filtered-out match
    can never crowd out a real one -- which is what `limit` truncation used to
    do when a gallery's siblings filled all three slots.

    `indexed_paths` is the set of `rel_path`s the archive index actually holds.
    Pass it and an archived entry MISSING from the index is compared from its
    own cached manifest hash instead of being skipped as "the index has this
    one". That closes a silent hole: the index is a snapshot, everything filed
    since it was built is absent from it, and an archived entry is skipped in
    this loop on the assumption the archive half already covered it -- so
    between two `build` runs, freshly archived art was compared against
    nothing at all. Omit the argument for the old behaviour.

    `prefer` is an optional `(match) -> bool` predicate marking the copies the
    caller can act on; it decides which copy REPRESENTS an artwork once the
    duplicates of it are collapsed. See _collapse_copies.
    """
    query = entry.get("phash")
    if not query:
        return []
    hash_bits = entry.get("phash_bits")
    q_int = int(query, 16)
    own_archive_path = entry.get("archive_path")
    own_post_id = entry.get("post_id") or post_id

    def is_sibling(other: Optional[dict]) -> bool:
        other_post_id = (other or {}).get("post_id")
        return bool(other_post_id) and other_post_id == own_post_id

    matches: list[DuplicateMatch] = []

    for rel_path, other_int in archive_hashes:
        if rel_path == own_archive_path:
            continue
        distance = (q_int ^ other_int).bit_count()
        if distance > max_distance:
            continue
        other_id = archive_path_map.get(rel_path)
        other = manifest.get(other_id) if other_id else None
        if other_id == post_id or is_sibling(other):
            continue
        if other_id and exclude is not None and exclude(other_id):
            continue
        folder = str(Path(rel_path).parent).replace("\\", "/")
        matches.append(DuplicateMatch(
            distance=distance,
            title=(other or {}).get("title") or Path(rel_path).name,
            where=f"archived — {folder}/" if folder not in (".", "") else "archived",
            post_id=other_id,
            image_path=str(archive_dir / rel_path) if archive_dir else None,
            source="archive",
            phash_int=other_int,
        ))

    for other_id, other in manifest.items():
        if other_id == post_id or is_sibling(other):
            continue
        other_hash = other.get("phash")
        # Only compare like-for-like hash depths.
        if not other_hash or other.get("phash_bits") != hash_bits:
            continue
        archive_path = other.get("archive_path")
        # Skip what the archive half above already covered -- but only what it
        # ACTUALLY covers, not everything that has been filed.
        if archive_path and (indexed_paths is None or archive_path in indexed_paths):
            continue
        distance = (q_int ^ int(other_hash, 16)).bit_count()
        if distance > max_distance:
            continue
        if exclude is not None and exclude(other_id):
            continue
        if archive_path:
            # Filed, just not indexed yet. Present it exactly as an index hit:
            # same wording, same thumbnail location, and source="archive" so
            # callers treat it as the already-filed keeper it is.
            folder = str(Path(archive_path).parent).replace("\\", "/")
            where = f"archived — {folder}/" if folder not in (".", "") else "archived"
            image_path = str(archive_dir / archive_path) if archive_dir else None
            source = "archive"
        else:
            local_path = other.get("local_path")
            exists = bool(local_path) and Path(local_path).exists()
            status = other.get("status", "?")
            where = f"{status} — staging" if exists else f"{status} (file gone)"
            image_path = str(Path(local_path).resolve()) if exists else None
            source = "staging"
        matches.append(DuplicateMatch(
            distance=distance,
            title=other.get("title") or other_id,
            where=where,
            post_id=other_id,
            image_path=image_path,
            source=source,
            phash_int=int(other_hash, 16),
        ))

    # Distance decides the order; the tiebreak decides which copy REPRESENTS an
    # artwork once _collapse_copies folds the rest away. Prefer a copy that
    # still exists -- an archived row, then a staging file, then an entry whose
    # image is gone (a rejected twin keeps its hash but nothing to look at or
    # defer to). Without this, two copies at the same distance collapse in
    # arbitrary order and the surviving one may be the one with no file, which
    # costs the reviewer both the preview and the one-click reject.
    matches.sort(key=lambda m: (m.distance, 0 if m.source == "archive" else (1 if m.image_path else 2)))
    return _collapse_copies(matches, prefer=prefer)[:limit]


# --------------------------------------------------------------------------- #
# Wallpaper suitability
# --------------------------------------------------------------------------- #
def suggest_wallpaper(width: Optional[int], height: Optional[int], rules: dict) -> list[str]:
    """Which wallpaper targets this image's dimensions satisfy: [], ["pc"],
    ["phone"], or both. A SUGGESTION only -- callers must never auto-select.

    "both" is reported only when the image independently satisfies each rule
    set. With the default disjoint aspect bands that essentially never
    happens, which is intended: "both" stays a deliberate manual choice.
    """
    if not width or not height:
        return []
    aspect = width / height
    suitable = []
    for target in ("pc", "phone"):
        rule = rules.get(target) or {}
        if width < rule.get("min_width", 0):
            continue
        if height < rule.get("min_height", 0):
            continue
        if not (rule.get("aspect_min", 0.0) <= aspect <= rule.get("aspect_max", float("inf"))):
            continue
        suitable.append(target)
    return suitable


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    """`python imagemeta.py warm` -- hash staging images ahead of time.

    Exists so the cost lands in the maintenance cycle (right after ingest
    brings new images in) rather than in front of the review UI. Hashing a
    high-res image costs ~0.2s, so a few hundred fresh downloads would
    otherwise stall the review launch for over a minute.
    """
    import argparse
    import time

    from manifest import load_manifest, save_manifest

    parser = argparse.ArgumentParser(
        description="Pre-compute pHash/dimensions for staging images (duplicate detection)."
    )
    parser.add_argument("command", nargs="?", default="warm", choices=["warm"])
    parser.parse_args()

    manifest = load_manifest()
    hash_size = index_hash_size()
    started = time.monotonic()

    def progress(done, total):
        if done % 10 == 0 or done == total:
            print(f"\r  {done} of {total} ({100.0 * done / total:.0f}%)", end="", flush=True)

    computed = warm_image_meta(manifest, hash_size, progress=progress)
    if computed:
        save_manifest(manifest)
        print(f"\r  hashed {computed} image(s) in {time.monotonic() - started:.0f}s.{' ' * 20}")
    else:
        print("  nothing to hash -- every staging image already has a current hash.")


if __name__ == "__main__":
    main()
