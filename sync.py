"""Sync engine: reconcile the manifest and the pHash index with an archive the
user has reorganised BY HAND, and report the layout problems routing cannot
report on itself.

Two things drift after archive.py has filed an image:

  1. The user moves the file. Promoting a character out of a catch-all folder
     (Genshin Impact/Fatui/ -> Genshin Impact/Sandrone/) is a normal, correct
     thing to do in Explorer, and nothing tells oshiire about it. The entry's
     `archive_path` and the index's `rel_path` then both name a path that no
     longer exists -- and a row keyed on a vanished path is compared against
     NOTHING, which is exactly how duplicate detection quietly stops working.
  2. layout.json accumulates entries that are individually valid and jointly
     wrong -- most sharply, a `character_aliases` key equal to the franchise's
     OWN name, which silently files every image tagged only with the franchise
     into one character's folder. Routing cannot flag this: it resolves
     cleanly, so nothing ever reaches resolve.py.

IDENTITY MODEL -- basename, and nothing else. A manual move changes the
folder, never the filename, so `basename(archive_path)` is a stable key that
is already recorded. Disk files are matched to manifest entries by basename
ALONE. Post-ids are deliberately never reparsed: a trailing "_1" may be a
gallery image index (t3_abc_1), a shortname code (t3_abc_BtR), or a collision
suffix _avoid_collision appended (t3_abc_2), and no rule distinguishes them.
Stripping it would conflate three unrelated files.

This module NEVER moves, copies, creates or deletes a file. Its only writes,
and only under --apply, are the manifest (one atomic save at the end) and the
index re-keying that mirrors it. Only the unambiguous bucket -- one disk
location, in a folder layout.json actually knows -- is applied; everything
else is reported for a human.

Usage:
    python sync.py                            # dry-run report (default), writes nothing
    python sync.py --apply                    # repair MOVED_OK entries
    python sync.py --scope "Genshin Impact"   # limit the report to a subtree
    python sync.py --apply --db data/archive_index.db
"""
import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

import archive
import hash_index
from hash_index import DEFAULT_DB, iter_image_files, move_indexed_file
from manifest import MANIFEST_PATH, load_manifest, save_manifest
from shortname import (
    _lookup_character,
    load_layout,
    lookup_ci,
    normalize_name_key,
)

# --------------------------------------------------------------------------- #
# Buckets
# --------------------------------------------------------------------------- #
UNCHANGED = "unchanged"
MOVED_OK = "moved_ok"
MOVED_UNKNOWN = "moved_unknown"
DUPLICATED = "duplicated"
VANISHED = "vanished"
MANIFEST_COLLISION = "manifest_collision"

# Report order: the quiet bucket first, then the one thing --apply repairs,
# then everything that needs a human, worst data problem last.
BUCKET_ORDER = (
    UNCHANGED,
    MOVED_OK,
    MOVED_UNKNOWN,
    DUPLICATED,
    VANISHED,
    MANIFEST_COLLISION,
)

# Buckets --apply deliberately refuses to touch. Each is ambiguous in a way no
# rule can settle: an unknown destination folder may be a typo or a layout the
# user has not written down yet; two files sharing one basename may be a real
# duplicate or a half-finished move; a vanished file is as likely an unmounted
# Drive folder as a deletion.
ATTENTION_BUCKETS = (MOVED_UNKNOWN, DUPLICATED, VANISHED, MANIFEST_COLLISION)

BUCKET_LABELS = {
    UNCHANGED: "unchanged",
    MOVED_OK: "moved (repairable)",
    MOVED_UNKNOWN: "moved into an unknown folder",
    DUPLICATED: "same filename in several folders",
    VANISHED: "recorded file not found on disk",
    MANIFEST_COLLISION: "filename recorded by several entries",
}

UNTRACKED_LABEL = "untracked files (not oshiire's)"


@dataclass
class SyncItem:
    """One archived manifest entry, classified against what is on disk."""
    key: str                       # manifest dict key (may be a gallery "t3_x_1")
    bucket: str
    basename: str
    old_rel: str                   # archive_path as recorded, POSIX
    new_rel: Optional[str] = None  # where the file actually is (single hit only)
    detail: str = ""


@dataclass
class AuditFinding:
    """One layout.json health problem. Advisory only -- never applied."""
    franchise: str
    kind: str
    alias: str
    target: str
    detail: str


@dataclass
class SyncPlan:
    items: list = field(default_factory=list)
    untracked: list = field(default_factory=list)
    audit: list = field(default_factory=list)
    scope: Optional[str] = None
    disk_files: int = 0

    def by_bucket(self, bucket: str) -> list:
        return [item for item in self.items if item.bucket == bucket]

    def counts(self) -> dict:
        counts = {bucket: 0 for bucket in BUCKET_ORDER}
        for item in self.items:
            counts[item.bucket] = counts.get(item.bucket, 0) + 1
        return counts


@dataclass
class ApplyResult:
    moved: int = 0
    index_updated: int = 0
    index_missed: int = 0
    manifest_written: bool = False


# --------------------------------------------------------------------------- #
# Path helpers (POSIX, relative to ARCHIVE_DIR -- the one form both the
# manifest's archive_path and the index's rel_path are stored in)
# --------------------------------------------------------------------------- #
def posix_dirname(rel_path: str) -> str:
    """Directory part of a POSIX rel path; "" for a file at the archive root."""
    parent = str(PurePosixPath(rel_path).parent)
    return "" if parent == "." else parent


def posix_basename(rel_path: str) -> str:
    return PurePosixPath(rel_path).name


def _under(rel_path: str, root: str) -> bool:
    """Is `rel_path` the folder `root`, or inside it? Case-insensitive, because
    the archive lives on Windows/Drive where a folder's case is not identity."""
    if not root or not rel_path:
        return False
    lowered = rel_path.casefold()
    lowered_root = root.casefold().strip("/")
    return lowered == lowered_root or lowered.startswith(lowered_root + "/")


# --------------------------------------------------------------------------- #
# Which destination folders the layout actually knows about
# --------------------------------------------------------------------------- #
def wallpaper_root(layout: dict) -> str:
    return ((layout.get("special_folders") or {}).get("wallpaper_root") or "").strip("/")


def special_dirs(layout: dict) -> set:
    """Every non-franchise destination route_entry can produce, as a POSIX dir
    relative to ARCHIVE_DIR: Crossover, the three Others/ folders, and the
    wallpaper root plus its two composed leaves."""
    special = layout.get("special_folders") or {}
    dirs = set()
    for key in ("crossover", "others_oc", "others_unknown_source", "others_known_series"):
        value = (special.get(key) or "").strip("/")
        if value:
            dirs.add(value)
    root = wallpaper_root(layout)
    if root:
        dirs.add(root)
        for key in ("wallpaper_pc", "wallpaper_phone"):
            leaf = (special.get(key) or "").strip("/")
            if leaf:
                dirs.add(root + "/" + leaf)
    return dirs


def nested_special_leaves(layout: dict) -> set:
    """Second-segment folder names that are valid under a NESTED franchise but
    are not roster characters: the group subfolder archive.py routes group
    shots into, and a per-franchise wallpaper folder."""
    leaves = {(layout.get("group_subfolder") or "Others_Group").strip("/")}
    root = wallpaper_root(layout)
    if root:
        leaves.add(root)
    return {leaf for leaf in leaves if leaf}


def dest_dir_is_known(rel_dir: str, layout: dict) -> bool:
    """Is this POSIX directory somewhere layout.json can legitimately file to?

    This is the whole difference between MOVED_OK (auto-repaired) and
    MOVED_UNKNOWN (reported): a file that turns up somewhere routing itself
    could have put it is a deliberate reorganisation, whereas one in a folder
    the layout has never heard of is just as likely a typo'd folder name or a
    staging area mid-cleanup, and rewriting archive_path to point at it would
    bless the mistake.

    Matching mirrors routing exactly -- lookup_ci (the series normalizer) for
    the franchise segment, _lookup_character (drops spacing and punctuation)
    for the character segment -- so a folder routing would accept is never
    called unknown here.
    """
    normalized = (rel_dir or "").strip("/")
    if not normalized:
        return False

    folded = normalized.casefold()
    if any(known.casefold() == folded for known in special_dirs(layout)):
        return True

    segments = [segment for segment in normalized.split("/") if segment]
    folder, franchise_def = lookup_ci(layout.get("franchises", {}), segments[0])
    if folder is None or not isinstance(franchise_def, dict):
        return False

    # The franchise's own folder is a valid leaf for a flat style, and for a
    # nested one with "fallback": "root" -- and it is a real folder either way,
    # so a file sitting directly in it is where the user put it.
    if len(segments) == 1:
        return True

    if len(segments) != 2 or franchise_def.get("style") != "nested":
        return False

    leaf = segments[1]
    if _lookup_character(franchise_def.get("characters") or [], leaf)[0] is not None:
        return True
    leaf_key = normalize_name_key(leaf)
    return any(normalize_name_key(special) == leaf_key for special in nested_special_leaves(layout))


# --------------------------------------------------------------------------- #
# Indexes over disk and manifest
# --------------------------------------------------------------------------- #
def build_disk_index(archive_dir: Path) -> dict:
    """{basename: [rel_path, ...]} for every image under ARCHIVE_DIR. Read-only,
    and reuses hash_index's walk so "what counts as an image" has one
    definition."""
    by_base = {}
    for path in iter_image_files(archive_dir):
        try:
            rel = path.relative_to(archive_dir).as_posix()
        except ValueError:  # pragma: no cover -- os.walk cannot leave the root
            continue
        by_base.setdefault(path.name, []).append(rel)
    for rels in by_base.values():
        rels.sort()
    return by_base


def archived_entries(manifest: dict):
    """(key, entry, archive_path) for the ground-truth set: every entry that
    claims to be filed. Yielded in sorted key order so reports are stable."""
    for key in sorted(manifest):
        entry = manifest[key]
        if not isinstance(entry, dict) or entry.get("status") != "archived":
            continue
        rel = entry.get("archive_path")
        if rel:
            yield key, entry, rel


def build_manifest_index(manifest: dict) -> dict:
    """{basename: [manifest key, ...]} over archived entries. A list with two
    keys in it is the MANIFEST_COLLISION case -- basename is this module's
    identity, so a shared one means no disk file can be attributed."""
    by_base = {}
    for key, _entry, rel in archived_entries(manifest):
        by_base.setdefault(posix_basename(rel), []).append(key)
    return by_base


def recorded_wallpaper_paths(manifest: dict) -> set:
    """Every wallpaper copy the manifest knows it made, as POSIX rel paths."""
    paths = set()
    for entry in manifest.values():
        if not isinstance(entry, dict):
            continue
        for rel in entry.get("wallpaper_paths") or []:
            if rel:
                paths.add(rel)
    return paths


def known_basenames(manifest: dict) -> set:
    """Basenames oshiire is responsible for: every archive_path AND every
    wallpaper_paths entry, across all statuses.

    Wallpaper copies are included so a moved one is not reported as untracked
    -- it is a file this pipeline created, just not the primary copy of its
    entry. Everything else under ARCHIVE_DIR predates oshiire and is left
    alone.
    """
    names = set()
    for entry in manifest.values():
        if not isinstance(entry, dict):
            continue
        rel = entry.get("archive_path")
        if rel:
            names.add(posix_basename(rel))
        for wallpaper_rel in entry.get("wallpaper_paths") or []:
            if wallpaper_rel:
                names.add(posix_basename(wallpaper_rel))
    return names


def _entry_candidates(rel: str, disk_hits: list, wp_root: str, wp_paths: set) -> list:
    """Narrow a basename's disk hits to the ones that could be THIS entry's
    primary archive file.

    archive.py copies a wallpaper under the SAME filename into
    <wallpaper_root>/<PC|phone>/, so every entry with a wallpaper copy has its
    basename in two or more places on disk. Without this, each of those would
    be reported as DUPLICATED -- a permanent false positive on entries that are
    perfectly in sync.

    Two exclusions, both only when the entry's OWN recorded path is outside the
    wallpaper tree: a hit recorded as some entry's wallpaper copy, and any hit
    inside the wallpaper root (which also covers a wallpaper copy the user has
    since moved within that tree). If excluding would leave nothing, the
    exclusion is dropped -- reporting a real move beats inventing a VANISHED.
    """
    if _under(rel, wp_root):
        return list(disk_hits)
    filtered = [
        hit for hit in disk_hits
        if hit not in wp_paths and not _under(hit, wp_root)
    ]
    return filtered or list(disk_hits)


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
def _in_scope(rel_paths, scope: Optional[str]) -> bool:
    if not scope:
        return True
    return any(rel.startswith(scope) for rel in rel_paths if rel)


def build_plan(manifest: dict, layout: dict, archive_dir: Path,
               scope: Optional[str] = None) -> SyncPlan:
    """Classify every archived entry against the archive on disk. Pure: it reads
    file NAMES only, writes nothing, and returns everything the report and
    --apply need."""
    scope = (scope or "").strip("/") or None

    disk_by_base = build_disk_index(archive_dir)
    mani_by_base = build_manifest_index(manifest)
    known = known_basenames(manifest)
    wp_root = wallpaper_root(layout)
    wp_paths = recorded_wallpaper_paths(manifest)

    items = []
    for key, _entry, rel in archived_entries(manifest):
        base = posix_basename(rel)

        siblings = mani_by_base.get(base, [])
        if len(siblings) > 1:
            others = ", ".join(other for other in siblings if other != key)
            items.append(SyncItem(
                key, MANIFEST_COLLISION, base, rel,
                detail="basename also recorded by: " + others,
            ))
            continue

        candidates = _entry_candidates(rel, disk_by_base.get(base, []), wp_root, wp_paths)

        if not candidates:
            items.append(SyncItem(
                key, VANISHED, base, rel,
                detail="no file with this name under ARCHIVE_DIR",
            ))
        elif len(candidates) > 1:
            items.append(SyncItem(
                key, DUPLICATED, base, rel,
                detail="found at: " + ", ".join(candidates),
            ))
        else:
            found = candidates[0]
            found_dir = posix_dirname(found)
            if found_dir == posix_dirname(rel):
                items.append(SyncItem(key, UNCHANGED, base, rel))
            elif dest_dir_is_known(found_dir, layout):
                items.append(SyncItem(key, MOVED_OK, base, rel, new_rel=found))
            else:
                items.append(SyncItem(
                    key, MOVED_UNKNOWN, base, rel, new_rel=found,
                    detail="'" + found_dir + "/' is not a folder layout.json can file to",
                ))

    untracked = sorted(
        rel
        for base, rels in disk_by_base.items() if base not in known
        for rel in rels
    )
    disk_files = sum(len(rels) for rels in disk_by_base.values())

    if scope:
        items = [item for item in items if _in_scope((item.old_rel, item.new_rel), scope)]
        untracked = [rel for rel in untracked if rel.startswith(scope)]

    return SyncPlan(
        items=items,
        untracked=untracked,
        audit=audit_layout(layout),
        scope=scope,
        disk_files=disk_files,
    )


# --------------------------------------------------------------------------- #
# Layout health audit (advisory; iterates layout only, never disk)
# --------------------------------------------------------------------------- #
ALIAS_KEY_IS_FRANCHISE = "alias_key_is_franchise"
ALIAS_VALUE_NOT_A_FOLDER = "alias_value_not_a_folder"
ALIAS_KEY_SHADOWS_ROSTER = "alias_key_shadows_roster"


def audit_layout(layout: dict) -> list:
    """Report character_aliases entries that resolve cleanly but mean something
    the user did not intend. None of these can surface through archive.py:
    routing either succeeds (and files the image somewhere wrong, silently) or
    flags a reason that names the character rather than the alias table.

      - a key equal to the franchise's OWN name -- every image tagged with just
        the franchise and no character then files into that one character's
        folder. This is the case that motivated the audit.
      - a value that is not a roster folder -- the alias resolves to a name
        matching no subfolder, so routing flags needs_folder against a
        character the user believes they already mapped.
      - a key that is itself a roster entry AND points at a different folder --
        the alias shadows the folder of the same name, so that character's own
        art files elsewhere. (An alias that merely respells the folder it
        points at is a no-op and is not reported.)

    Independent of --apply and of the reconcile plan; nothing here is ever
    written back.
    """
    findings = []
    franchises = layout.get("franchises") or {}
    alias_tables = layout.get("character_aliases") or {}

    for table_key in sorted(alias_tables):
        table = alias_tables[table_key]
        if not isinstance(table, dict):
            continue
        folder, franchise_def = lookup_ci(franchises, table_key)
        roster = (franchise_def or {}).get("characters") or []
        franchise_name = folder or table_key
        franchise_key = normalize_name_key(franchise_name)

        for alias in sorted(table):
            target = table[alias]
            target_text = str(target)

            if normalize_name_key(alias) == franchise_key:
                findings.append(AuditFinding(
                    franchise_name, ALIAS_KEY_IS_FRANCHISE, alias, target_text,
                    detail=(
                        "alias key is the franchise's own name -- every image tagged "
                        "only '" + franchise_name + "' files into '" + target_text + "'"
                    ),
                ))

            # A franchise with no roster (flat style, or one not defined at all)
            # has no folders to check a value or a shadow against, so those two
            # checks would fire on every alias while saying nothing.
            if not roster:
                continue

            if not target or _lookup_character(roster, target_text)[0] is None:
                findings.append(AuditFinding(
                    franchise_name, ALIAS_VALUE_NOT_A_FOLDER, alias, target_text,
                    detail=(
                        "'" + target_text + "' is not in " + franchise_name + "'s roster -- '"
                        + alias + "' will still flag needs_folder"
                    ),
                ))

            # Only harmful when the alias sends the name somewhere ELSE. Both
            # sides are resolved through the roster first, so an alias that is
            # merely a spelling of the folder it points at ("WIz" -> "Wiz",
            # "Megumin'" -> "Megumin") is recognised as the redundant no-op it
            # is. Reporting those would bury the real finding under
            # typo-shaped noise nobody needs to act on, and an audit whose
            # output is mostly noise stops being read.
            shadowed, _ = _lookup_character(roster, alias)
            redirected_to, _ = _lookup_character(roster, target_text)
            if shadowed is not None and shadowed != redirected_to:
                findings.append(AuditFinding(
                    franchise_name, ALIAS_KEY_SHADOWS_ROSTER, alias, target_text,
                    detail=(
                        "'" + alias + "' is also a " + franchise_name + " subfolder -- the "
                        "alias wins, so its own art files into '" + target_text + "'"
                    ),
                ))

    return findings


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def apply_plan(plan: SyncPlan, manifest: dict, db_path: Path = DEFAULT_DB,
               manifest_path: Path = MANIFEST_PATH) -> ApplyResult:
    """Repair the MOVED_OK items, and nothing else.

    Every manifest edit happens in memory and is committed by ONE
    save_manifest at the end (its atomic tmp + os.replace), so an interrupted
    run leaves either the old manifest or the new one, never a half-written
    file. The index is re-keyed best-effort alongside: it is a rebuildable
    cache and `hash_index.py build` is its source of truth, so a failure there
    must never cost the manifest write that records where the files actually
    are.

    Never moves, copies, creates or deletes a file.
    """
    result = ApplyResult()
    repairable = plan.by_bucket(MOVED_OK)
    if not repairable:
        return result

    for item in repairable:
        entry = manifest.get(item.key)
        if not isinstance(entry, dict):  # pragma: no cover -- the plan came from it
            continue
        entry["archive_path"] = item.new_rel
        result.moved += 1
        try:
            rekeyed = move_indexed_file(db_path, item.old_rel, item.new_rel)
        except (OSError, sqlite3.Error) as exc:
            print(f"  warning: index re-key failed for {item.old_rel}: {exc}", file=sys.stderr)
            rekeyed = False
        if rekeyed:
            result.index_updated += 1
        else:
            result.index_missed += 1
            print(
                f"  warning: index had no row for {item.old_rel} -- not re-keyed",
                file=sys.stderr,
            )

    save_manifest(manifest, manifest_path)
    result.manifest_written = True
    return result


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def print_report(plan: SyncPlan, archive_dir: Path, db_path: Path = DEFAULT_DB) -> None:
    counts = plan.counts()
    index_note = "" if db_path.exists() else "  (not built)"

    print(f"Archive:  {archive_dir}")
    print(f"Index:    {db_path}{index_note}")
    if plan.scope:
        print(f"Scope:    {plan.scope}/")
    print(f"Images on disk: {plan.disk_files:,}")
    print()

    print("Archived manifest entries:")
    for bucket in BUCKET_ORDER:
        print(f"  {BUCKET_LABELS[bucket]:<38} {counts.get(bucket, 0):>6,}")
    print(f"  {UNTRACKED_LABEL:<38} {len(plan.untracked):>6,}")

    moved_ok = plan.by_bucket(MOVED_OK)
    if moved_ok:
        print()
        print(f"Repairable moves ({len(moved_ok)}):")
        for item in moved_ok:
            print(f"  {item.key}")
            print(f"      {item.old_rel}")
            print(f"   -> {item.new_rel}")

    attention = [item for bucket in ATTENTION_BUCKETS for item in plan.by_bucket(bucket)]
    if attention:
        print()
        print(f"Needs your decision ({len(attention)}) -- never applied automatically:")
        for item in attention:
            print(f"  [{item.bucket}] {item.key}  ({item.old_rel})")
            if item.detail:
                print(f"      {item.detail}")

    if plan.audit:
        print()
        print(f"layout.json health ({len(plan.audit)} finding(s), advisory only):")
        for finding in plan.audit:
            print(f"  [{finding.kind}] {finding.franchise}: '{finding.alias}' -> '{finding.target}'")
            print(f"      {finding.detail}")
    else:
        print()
        print("layout.json health: no findings.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def resolve_archive_dir() -> Path:
    """The archive root, cross-checked between the pipeline's reader and the
    index's.

    archive.py and hash_index.py each read ARCHIVE_DIR themselves (the latter
    deliberately, to stay standalone). This module is the one place that joins
    the manifest's archive_path to the index's rel_path, and both are relative
    to "the archive" -- so if the two readers ever disagreed, every path here
    would be silently relative to the wrong root. Cheap to check, and close to
    undebuggable if left unchecked.
    """
    archive_dir = archive.get_archive_dir()
    index_dir = hash_index.get_archive_dir()
    if archive_dir.resolve() != index_dir.resolve():
        print(
            "ARCHIVE_DIR disagrees between archive.py and hash_index.py:\n"
            f"  archive.py:    {archive_dir.resolve()}\n"
            f"  hash_index.py: {index_dir.resolve()}\n"
            "Both must name the same archive root before sync can join manifest "
            "paths to index paths.",
            file=sys.stderr,
        )
        sys.exit(1)
    return archive_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile the manifest and pHash index with hand-moved archive files."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Rewrite archive_path for repairable moves and re-key the index. Default is dry-run.",
    )
    parser.add_argument(
        "--scope", metavar="PREFIX", default=None,
        help="Only report archive paths under this POSIX prefix (e.g. 'Genshin Impact').",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"Index path (default {DEFAULT_DB}).",
    )
    args = parser.parse_args()

    archive_dir = resolve_archive_dir()
    layout = load_layout()
    manifest = load_manifest()

    plan = build_plan(manifest, layout, archive_dir, scope=args.scope)
    print_report(plan, archive_dir, args.db)

    if not args.apply:
        print()
        print("Dry-run only -- nothing was written. Re-run with --apply to repair the moves above.")
        return

    result = apply_plan(plan, manifest, args.db)
    print()
    if not result.moved:
        print("Nothing to apply -- no repairable moves.")
        return
    print(
        f"Applied: {result.moved} archive_path(s) rewritten; "
        f"index re-keyed for {result.index_updated}, missed {result.index_missed}."
    )
    if result.index_missed:
        print("run `python hash_index.py build` to fully resync the index.")


if __name__ == "__main__":
    main()
