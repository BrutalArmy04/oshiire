"""One-off migration: re-file the r/Asuka posts that were tagged Sword Art Online.

r/Asuka is an Evangelion subreddit that subreddit_map.json briefly mapped to
Sword Art Online. The map is already corrected; this fixes the entries it
produced -- moving each archived file into Evangelion/ and retagging the
franchise so nothing re-files itself into the wrong folder on the next run.

Dry-run by default, --apply to execute (same convention as archive.py).
Data migration only -- no other module is modified.

SCOPE NOTE: the affected set is DISCOVERED from the manifest (every r/Asuka
entry whose franchise is not already exactly ["Evangelion"]), not hardcoded.
The hand-written list this migration started from was short by one archived
file and one pending_review entry; discovering the set from the manifest is
what caught that, so it stays discovered.

Two things deliberately NOT touched:
  * wallpaper_paths -- a wallpaper copy lives under Wallpaper/<target>/ and its
    path encodes no franchise, so re-filing the archive copy does not move it.
    Any such copy is verified still in place afterwards rather than assumed.
  * character_guess -- Evangelion is a `flat` franchise, so the character name
    never reaches the path. Retagging it would be an unrelated edit.
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# This script lives in scripts/migrations/; the modules it reuses live at the
# repo root, two levels up.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

from manifest import load_manifest, save_manifest
from shortname import franchise_folder_and_def, load_layout, load_series_aliases

SUBREDDIT = "Asuka"
WRONG_FRANCHISE_FOLDER = "Sword Art Online"
RIGHT_FRANCHISE_TAG = "Evangelion"

DB_PATH = Path("data/archive_index.db")


def discover(manifest: dict):
    """Returns (entries, renames).

    entries is [(key, entry)] for every r/Asuka post still mistagged.
    renames is [(src_rel, dst_rel, key)] for the subset that has an archived
    file under the wrong franchise folder.
    """
    entries = []
    for key in sorted(manifest):
        entry = manifest[key]
        if not isinstance(entry, dict):
            continue
        if (entry.get("subreddit") or "").casefold() != SUBREDDIT.casefold():
            continue
        if entry.get("franchise") == [RIGHT_FRANCHISE_TAG]:
            continue  # already correct
        entries.append((key, entry))

    renames = []
    for key, entry in entries:
        src = entry.get("archive_path") or ""
        if src.startswith(WRONG_FRANCHISE_FOLDER + "/"):
            # Keep the filename exactly as it is -- only the folder changes.
            # Evangelion is `flat`, so there is no shortname suffix to add.
            renames.append((src, f"{RIGHT_FRANCHISE_TAG}/{Path(src).name}", key))
    return entries, renames


def check_target_style(layout: dict, series_aliases: dict) -> str:
    """Confirms Evangelion resolves to a real `flat` folder before we build any
    path from it. A nested target would need a character subfolder, which this
    migration does not compute -- better to abort than to file into a folder
    that routing would not have chosen."""
    folder, definition = franchise_folder_and_def(RIGHT_FRANCHISE_TAG, layout, series_aliases)
    if not folder or definition is None:
        print(f"ABORT: {RIGHT_FRANCHISE_TAG!r} does not resolve to a folder in layout.json.",
              file=sys.stderr)
        sys.exit(1)
    style = (definition or {}).get("style")
    if style != "flat":
        print(f"ABORT: {folder!r} has style {style!r}, expected 'flat'. This migration "
              "builds flat paths only.", file=sys.stderr)
        sys.exit(1)
    return folder


def preflight(renames, archive_dir: Path):
    """Every source must exist and every destination must be free, checked for
    the WHOLE plan before a single file is touched. No partial application."""
    problems = []
    for src, dst, key in renames:
        if not (archive_dir / src).exists():
            problems.append(f"  missing source: {src}  ({key})")
        if (archive_dir / dst).exists():
            problems.append(f"  destination already exists: {dst}  ({key})")
    return problems


def report_scope(entries, renames, manifest: dict):
    """Say plainly what the discovered set covers, broken down by status -- the
    operator's chance to notice the scope is not what they expected before
    anything moves. A retag with no file to move is the easy one to miss, so it
    is counted separately rather than folded into the total."""
    keys = [k for k, _ in entries]
    move_keys = {k for _, _, k in renames}

    print("Scope")
    print("-----")
    print(f"  r/{SUBREDDIT} entries still mistagged: {len(keys)}")
    print(f"  of those, with an archived file to move: {len(move_keys)}")
    print(f"  retag only (no file on disk):            {len(keys) - len(move_keys)}")
    by_status = {}
    for key in keys:
        status = manifest[key].get("status")
        by_status[status] = by_status.get(status, 0) + 1
    for status, count in sorted(by_status.items(), key=lambda kv: str(kv[0])):
        print(f"    status={status}: {count}")
    print()


def print_plan(entries, renames, archive_dir: Path):
    print("Planned file moves")
    print("------------------")
    width = max((len(src) for src, _, _ in renames), default=0)
    for src, dst, key in renames:
        exists = "ok " if (archive_dir / src).exists() else "MISSING"
        clash = "  <- DEST EXISTS" if (archive_dir / dst).exists() else ""
        print(f"  [{exists}] {src:<{width}}  ->  {dst}   ({key}){clash}")
    print(f"  {len(renames)} file(s).")
    print()

    print("Planned franchise retags")
    print("------------------------")
    for key, entry in entries:
        moved = " (+ file move)" if any(k == key for _, _, k in renames) else " (no file to move)"
        print(f"  {key}  status={entry.get('status'):<14} "
              f"{entry.get('franchise')} -> [{RIGHT_FRANCHISE_TAG!r}]{moved}")
    print(f"  {len(entries)} entry(ies).")
    print()

    untouched = [(k, e.get("wallpaper_paths")) for k, e in entries if e.get("wallpaper_paths")]
    print("Wallpaper copies (NOT moved -- their paths encode no franchise)")
    print("--------------------------------------------------------------")
    if not untouched:
        print("  (none)")
    for key, paths in untouched:
        for path in paths:
            state = "ok" if (archive_dir / path).exists() else "MISSING"
            print(f"  [{state}] {path}   ({key}, unchanged)")
    print()


def apply_moves(renames, archive_dir: Path) -> int:
    for src, dst, _key in renames:
        (archive_dir / dst).parent.mkdir(parents=True, exist_ok=True)
        os.replace(archive_dir / src, archive_dir / dst)
    return len(renames)


def apply_manifest(entries, renames) -> tuple:
    """Rewrites archive_path for moved files and franchise for every mistagged
    entry. wallpaper_paths is deliberately left alone."""
    rename_map = {src: dst for src, dst, _ in renames}
    keys = {k for k, _ in entries}
    manifest = load_manifest()

    paths_changed = 0
    tags_changed = 0
    for key, entry in manifest.items():
        if not isinstance(entry, dict) or key not in keys:
            continue
        archive_path = entry.get("archive_path")
        if archive_path in rename_map:
            entry["archive_path"] = rename_map[archive_path]
            paths_changed += 1
        if entry.get("franchise") != [RIGHT_FRANCHISE_TAG]:
            entry["franchise"] = [RIGHT_FRANCHISE_TAG]
            tags_changed += 1

    save_manifest(manifest)
    return paths_changed, tags_changed


def apply_index(renames, db_path: Path = DB_PATH):
    """rel_path is stored POSIX-relative to ARCHIVE_DIR, exactly as the manifest
    holds it. A move changes no pixels, so phash/size/mtime stay put and nothing
    is rehashed. A row that doesn't match is a warning, not an error -- the index
    may simply predate that file."""
    if not db_path.exists():
        print(f"  {db_path} does not exist -- skipping index update.")
        return 0, []

    matched = 0
    unmatched = []
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            for src, dst, _key in renames:
                cur = conn.execute(
                    "UPDATE images SET rel_path = ? WHERE rel_path = ?", (dst, src)
                )
                if cur.rowcount:
                    matched += cur.rowcount
                else:
                    unmatched.append(src)
    finally:
        conn.close()
    return matched, unmatched


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def verify(renames, archive_dir: Path):
    print()
    print("=" * 72)
    print("VERIFICATION")
    print("=" * 72)
    manifest = load_manifest()

    print()
    print(f"1. archived r/{SUBREDDIT} entries still under {WRONG_FRANCHISE_FOLDER}/")
    stragglers = [
        k for k, e in sorted(manifest.items())
        if isinstance(e, dict)
        and (e.get("subreddit") or "").casefold() == SUBREDDIT.casefold()
        and (e.get("archive_path") or "").startswith(WRONG_FRANCHISE_FOLDER + "/")
    ]
    print(f"   -> {len(stragglers)} (expected 0){'  ' + ', '.join(stragglers) if stragglers else ''}")

    print()
    print(f"2. r/{SUBREDDIT} entries not tagged [{RIGHT_FRANCHISE_TAG!r}]")
    mistagged = [
        (k, e.get("franchise")) for k, e in sorted(manifest.items())
        if isinstance(e, dict)
        and (e.get("subreddit") or "").casefold() == SUBREDDIT.casefold()
        and e.get("franchise") != [RIGHT_FRANCHISE_TAG]
    ]
    print(f"   -> {len(mistagged)} (expected 0)")
    for key, franchise in mistagged:
        print(f"      {key}: {franchise}")

    print()
    print("3. every moved file present at its new path")
    all_ok = True
    for _src, dst, key in renames:
        ok = (archive_dir / dst).exists()
        all_ok = all_ok and ok
        print(f"   [{'ok' if ok else 'MISSING'}] {dst}   ({key})")
    print(f"   -> {len(renames)} path(s), all present: {all_ok}")

    print()
    print("4. wallpaper_paths on affected entries still resolve (unchanged)")
    any_wp = False
    for key, entry in sorted(manifest.items()):
        if not isinstance(entry, dict):
            continue
        if (entry.get("subreddit") or "").casefold() != SUBREDDIT.casefold():
            continue
        for path in entry.get("wallpaper_paths") or []:
            any_wp = True
            ok = (archive_dir / path).exists()
            print(f"   [{'ok' if ok else 'MISSING'}] {path}   ({key})")
    if not any_wp:
        print("   (none)")

    print()
    print("5. archive_index.db rows for the moved paths")
    if not DB_PATH.exists():
        print(f"   {DB_PATH} does not exist.")
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        for src, dst, key in renames:
            stale = conn.execute(
                "SELECT COUNT(*) FROM images WHERE rel_path = ?", (src,)
            ).fetchone()[0]
            fresh = conn.execute(
                "SELECT COUNT(*) FROM images WHERE rel_path = ?", (dst,)
            ).fetchone()[0]
            on_disk = (archive_dir / dst).exists()
            flag = "ok" if (stale == 0 and fresh and on_disk) else "CHECK"
            print(f"   [{flag}] {dst}   old_rows={stale} new_rows={fresh} "
                  f"file_exists={on_disk}   ({key})")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Re-file r/{SUBREDDIT} posts mistagged as "
                    f"{WRONG_FRANCHISE_FOLDER} (one-off data migration)."
    )
    parser.add_argument("--apply", "--execute", dest="apply", action="store_true",
                        help="Actually move files and write the manifest/index. Default is dry-run.")
    parser.add_argument("--verify-only", action="store_true",
                        help="Re-run the verification report against current state and exit.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    os.chdir(repo_root)  # every module path (manifest.json, layout.json) is repo-relative

    load_dotenv()
    archive_dir_value = os.environ.get("ARCHIVE_DIR")
    if not archive_dir_value:
        print("ARCHIVE_DIR is not set in .env.", file=sys.stderr)
        sys.exit(1)
    archive_dir = Path(archive_dir_value)
    if not archive_dir.exists():
        print(f"ARCHIVE_DIR does not exist: {archive_dir}", file=sys.stderr)
        sys.exit(1)

    manifest = load_manifest()
    layout = load_layout()
    series_aliases = load_series_aliases()

    if args.verify_only:
        # Recompute against ALREADY-migrated paths so the on-disk check still
        # names the moved files.
        done = [
            (e["archive_path"], e["archive_path"], k)
            for k, e in sorted(manifest.items())
            if isinstance(e, dict)
            and (e.get("subreddit") or "").casefold() == SUBREDDIT.casefold()
            and (e.get("archive_path") or "").startswith(RIGHT_FRANCHISE_TAG + "/")
        ]
        verify(done, archive_dir)
        return

    folder = check_target_style(layout, series_aliases)
    entries, renames = discover(manifest)

    print(f"ARCHIVE_DIR: {archive_dir}")
    print(f"Target folder: {folder}/ (style flat)")
    print()
    report_scope(entries, renames, manifest)

    if not entries:
        print(f"Nothing to migrate -- no r/{SUBREDDIT} entry is mistagged.")
        return

    print_plan(entries, renames, archive_dir)

    problems = preflight(renames, archive_dir)
    if problems:
        print("ABORT -- preflight failed, nothing was touched:", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        sys.exit(1)
    print("Preflight: all sources present, no destination collisions.")
    print()

    if not args.apply:
        print("Dry-run only -- nothing was moved or written. "
              "Re-run with --apply to perform this migration.")
        return

    # 1. files first: the only step that can fail on the filesystem, and the one
    #    that leaves the archive inconsistent if the manifest moves ahead of it.
    print(f"Moved {apply_moves(renames, archive_dir)} file(s).")

    # 2. manifest
    paths_changed, tags_changed = apply_manifest(entries, renames)
    print(f"Manifest: rewrote {paths_changed} archive_path(s), {tags_changed} franchise tag(s).")

    # 3. pHash index
    matched, unmatched = apply_index(renames)
    print(f"Index: {matched} row(s) updated in {DB_PATH}.")
    for src in unmatched:
        print(f"  warning: no index row for {src} (index may predate the file).")

    verify(renames, archive_dir)


if __name__ == "__main__":
    main()
