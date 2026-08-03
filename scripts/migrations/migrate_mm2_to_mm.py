"""One-off migration: collapse the duplicate MM2 shortname onto MM.

000___Known_Series_Names.txt carried two codes for one series:
    MM  = Mahou Shoujo Madoka Magica
    MM2 = Madoka Magica              <- the duplicate, removed here

Renames every archived `_MM2` file to `_MM`, rewrites the manifest paths and
the pHash index rows, drops the MM2 line, and records "Madoka Magica" as a
series alias of "Mahou Shoujo Madoka Magica" so the tag still resolves.

Dry-run by default, --apply to execute (same convention as archive.py).
Data migration only -- no other module is modified.

SCOPE NOTE: the set of files to rename is DISCOVERED from the manifest (every
entry whose archive_path/wallpaper_paths carry the _MM2 suffix), not hardcoded.
Deleting the MM2 code orphans the suffix on every file still carrying it, so a
partial rename would leave undecodable filenames behind -- the exact breakage
this migration exists to prevent.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

# This script lives in scripts/migrations/; the modules it reuses live at the
# repo root, two levels up.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

from manifest import load_manifest, save_manifest
from shortname import (
    load_layout,
    load_series_aliases,
    load_shortname_map,
    match_shortname,
    save_series_alias,
)

OLD_SUFFIX = "_MM2"
NEW_SUFFIX = "_MM"

OLD_CODE = "MM2"
NEW_CODE = "MM"
VARIANT_NAME = "Madoka Magica"
CANONICAL_NAME = "Mahou Shoujo Madoka Magica"

SHORTNAME_LINE_NO = 103  # expected position of "MM2 = Madoka Magica"

DB_PATH = Path("data/archive_index.db")


def swap_suffix(rel_path: str) -> str:
    """Others/Known Series/<id>_MM2.jpg -> Others/Known Series/<id>_MM.jpg.

    Anchored on the stem's tail so a coincidental "_MM2" earlier in a
    reddit-generated filename can never be rewritten."""
    p = Path(rel_path)
    if not p.stem.endswith(OLD_SUFFIX):
        return rel_path
    return p.with_name(p.stem[: -len(OLD_SUFFIX)] + NEW_SUFFIX + p.suffix).as_posix()


def has_old_suffix(rel_path: str) -> bool:
    return bool(rel_path) and Path(rel_path).stem.endswith(OLD_SUFFIX)


def build_plan(manifest: dict):
    """Returns (renames, entry_ids) where renames is an ordered, de-duplicated
    list of (src_rel, dst_rel, entry_id, field) tuples."""
    renames = []
    seen_src = set()
    entry_ids = []

    for key in sorted(manifest):
        entry = manifest[key]
        if not isinstance(entry, dict):
            continue

        hits = []
        archive_path = entry.get("archive_path") or ""
        if has_old_suffix(archive_path):
            hits.append((archive_path, "archive_path"))
        for wallpaper_path in entry.get("wallpaper_paths") or []:
            if has_old_suffix(wallpaper_path):
                hits.append((wallpaper_path, "wallpaper_paths"))

        if hits:
            entry_ids.append(key)
        for src, field in hits:
            if src in seen_src:
                continue
            seen_src.add(src)
            renames.append((src, swap_suffix(src), key, field))

    return renames, entry_ids


def preflight(renames, archive_dir: Path):
    """Every source must exist and every destination must be free, checked for
    the WHOLE plan before a single file is touched. No partial application."""
    problems = []
    for src, dst, entry_id, _field in renames:
        src_abs = archive_dir / src
        dst_abs = archive_dir / dst
        if not src_abs.exists():
            problems.append(f"  missing source: {src}  ({entry_id})")
        if dst_abs.exists():
            problems.append(f"  destination already exists: {dst}  ({entry_id})")
    return problems


def report_scope(manifest: dict, entry_ids):
    """Say plainly what the discovered set covers, broken down by status -- the
    operator's chance to notice the scope is not what they expected before
    anything moves."""
    print("Scope")
    print("-----")
    print(f"  entries carrying the {OLD_SUFFIX} suffix: {len(entry_ids)}")
    by_status = {}
    for key in entry_ids:
        status = manifest.get(key, {}).get("status")
        by_status[status] = by_status.get(status, 0) + 1
    for status, count in sorted(by_status.items(), key=lambda kv: str(kv[0])):
        print(f"    status={status}: {count}")
    print()


def print_plan(renames, archive_dir: Path):
    print("Planned renames")
    print("---------------")
    width = max((len(src) for src, _, _, _ in renames), default=0)
    for src, dst, entry_id, field in renames:
        exists = "ok " if (archive_dir / src).exists() else "MISSING"
        clash = "  <- DEST EXISTS" if (archive_dir / dst).exists() else ""
        print(f"  [{exists}] {src:<{width}}  ->  {Path(dst).name}   ({entry_id}, {field}){clash}")
    print(f"  {len(renames)} file(s).")
    print()


def apply_renames(renames, archive_dir: Path) -> int:
    done = 0
    for src, dst, _entry_id, _field in renames:
        os.replace(archive_dir / src, archive_dir / dst)
        done += 1
    return done


def apply_manifest(renames) -> int:
    """Rewrites archive_path and the matching wallpaper_paths element in place."""
    rename_map = {src: dst for src, dst, _, _ in renames}
    manifest = load_manifest()
    changed = 0

    for entry in manifest.values():
        if not isinstance(entry, dict):
            continue
        archive_path = entry.get("archive_path")
        if archive_path in rename_map:
            entry["archive_path"] = rename_map[archive_path]
            changed += 1
        wallpaper_paths = entry.get("wallpaper_paths")
        if isinstance(wallpaper_paths, list):
            for i, wallpaper_path in enumerate(wallpaper_paths):
                if wallpaper_path in rename_map:
                    wallpaper_paths[i] = rename_map[wallpaper_path]
                    changed += 1

    save_manifest(manifest)
    return changed


def apply_index(renames, db_path: Path = DB_PATH):
    """rel_path is stored POSIX-relative to ARCHIVE_DIR, exactly as the manifest
    holds it. A rename changes no pixels, so phash/size/mtime stay put and
    nothing is rehashed. A row that doesn't match is a warning, not an error --
    the index may simply predate that file."""
    if not db_path.exists():
        print(f"  {db_path} does not exist -- skipping index update.")
        return 0, []

    matched = 0
    unmatched = []
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            for src, dst, _entry_id, _field in renames:
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


def delete_shortname_line(shortname_path: Path, apply: bool):
    """Drops the `MM2 = Madoka Magica` line. Reads and rejoins BYTES with
    keepends, so CRLF is preserved and every other line stays byte-identical."""
    raw = shortname_path.read_bytes()
    lines = raw.splitlines(keepends=True)

    targets = [
        i for i, line in enumerate(lines)
        if line.strip() and not line.strip().startswith(b"#") and b"=" in line
        and line.split(b"=", 1)[0].strip() == OLD_CODE.encode()
    ]

    if not targets:
        print(f"  no `{OLD_CODE} = ...` line found in {shortname_path} -- nothing to delete.")
        return False
    if len(targets) > 1:
        print(f"  ABORT: {len(targets)} lines define `{OLD_CODE}` "
              f"(lines {', '.join(str(i + 1) for i in targets)}).", file=sys.stderr)
        return False

    idx = targets[0]
    print(f"  line {idx + 1}: {lines[idx].rstrip().decode('utf-8')!r}")
    if idx + 1 != SHORTNAME_LINE_NO:
        print(f"  note: expected line {SHORTNAME_LINE_NO}, found it at {idx + 1}.")

    if not apply:
        return True

    del lines[idx]
    tmp_path = shortname_path.with_suffix(shortname_path.suffix + ".tmp")
    tmp_path.write_bytes(b"".join(lines))
    os.replace(tmp_path, shortname_path)
    return True


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def repo_grep(pattern: str, repo_root: Path):
    """`grep -rn` over git-TRACKED text files. Uses the git file list rather
    than walking the tree so gitignored staging//archive/ and the binary pHash
    index are out of scope -- walking them takes minutes and reports coincidental
    base64 filename hits that have nothing to do with the shortname."""
    import subprocess
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"], cwd=repo_root,
            capture_output=True, check=True,
        ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError) as exc:
        return None, f"git ls-files failed: {exc}"

    hits = []
    for rel in listing.split("\0"):
        if not rel:
            continue
        path = repo_root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern in line:
                hits.append((rel, lineno, line.strip()))
    return hits, None


def verify(renames, archive_dir: Path, repo_root: Path):
    print()
    print("=" * 72)
    print("VERIFICATION")
    print("=" * 72)

    layout = load_layout()
    shortname_entries = load_shortname_map(layout)
    aliases = load_series_aliases()
    manifest = load_manifest()

    # 1. repo-wide grep
    print()
    print(f'1. grep -rn "{OLD_CODE}" over tracked repo files')
    hits, err = repo_grep(OLD_CODE, repo_root)
    if err:
        print(f"   {err}")
    else:
        this_file = Path(__file__).resolve()
        external = [
            h for h in hits
            if (repo_root / h[0]).resolve() != this_file
        ]
        for rel, lineno, line in hits:
            marker = "   (this migration script itself)" if (repo_root / rel).resolve() == this_file else ""
            print(f"   {rel}:{lineno}: {line[:100]}{marker}")
        print(f"   -> {len(hits)} hit(s); {len(external)} outside this script "
              f"(expected 0 outside this script).")

    # 2/3. manifest counts
    print()
    print("2. manifest entries whose archive_path contains \"_MM2\"")
    mm2 = [k for k, e in sorted(manifest.items())
           if isinstance(e, dict) and "_MM2" in (e.get("archive_path") or "")]
    print(f"   -> {len(mm2)} (expected 0){'  ' + ', '.join(mm2) if mm2 else ''}")

    print()
    print("3. manifest entries whose archive_path contains \"_MM\"")
    mm_sub = [k for k, e in manifest.items()
              if isinstance(e, dict) and "_MM" in (e.get("archive_path") or "")]
    mm_exact = [k for k, e in manifest.items()
                if isinstance(e, dict) and Path(e.get("archive_path") or "").stem.endswith("_MM")]
    print(f"   -> {len(mm_sub)} contain the substring \"_MM\"")
    print(f"   -> {len(mm_exact)} actually end in the _MM suffix")
    print(f"      ({len(renames) and len([r for r in renames if 'Known Series' in r[0]])} migrated "
          f"+ {len(mm_exact) - len([r for r in renames if 'Known Series' in r[0]])} pre-existing)")

    # 4. renamed files on disk
    print()
    print("4. renamed files present under ARCHIVE_DIR")
    all_ok = True
    for _src, dst, entry_id, _field in renames:
        ok = os.path.exists(archive_dir / dst)
        all_ok = all_ok and ok
        print(f"   [{'ok' if ok else 'MISSING'}] {dst}   ({entry_id})")
    print(f"   -> {len(renames)} path(s), all present: {all_ok}")

    # 5/6. shortname resolution
    print()
    print("5. match_shortname resolution")
    for name in (VARIANT_NAME, "Magia Record", CANONICAL_NAME):
        code = match_shortname(name, shortname_entries, aliases)
        print(f"   {name!r:<32} -> {code!r}   (expected {NEW_CODE!r})")

    # 7. entries still carrying the tag but not yet archived
    print()
    print("6. resolved shortname for remaining entries tagged 'Madoka Magica'")
    pending = [
        (k, e) for k, e in sorted(manifest.items())
        if isinstance(e, dict)
        and any(VARIANT_NAME.casefold() == (f or "").casefold() for f in (e.get("franchise") or []))
        and not e.get("archive_path")
    ]
    if not pending:
        print("   (none unarchived)")
    for key, entry in pending:
        codes = [match_shortname(f, shortname_entries, aliases) for f in entry.get("franchise") or []]
        print(f"   {key}  status={entry.get('status'):<14} franchise={entry.get('franchise')} -> {codes}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collapse the duplicate MM2 shortname onto MM (one-off data migration)."
    )
    parser.add_argument("--apply", "--execute", dest="apply", action="store_true",
                        help="Actually rename files and write the manifest/index/config. Default is dry-run.")
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
    shortname_path = Path(layout["shortname_file"])

    if args.verify_only:
        # Recompute the plan against the ALREADY-migrated paths so the on-disk
        # check still names every migrated file.
        renames, _ = build_plan(manifest)
        if not renames:
            done = [
                (swap_suffix(src) if has_old_suffix(src) else src)
                for src in _already_migrated_paths(manifest)
            ]
            renames = [(p, p, _owner_of(manifest, p), "archive_path") for p in done]
        verify(renames, archive_dir, repo_root)
        return

    renames, entry_ids = build_plan(manifest)

    print(f"ARCHIVE_DIR: {archive_dir}")
    print()
    report_scope(manifest, entry_ids)

    if not renames:
        print("Nothing to migrate -- no entry carries the _MM2 suffix.")
        return

    print_plan(renames, archive_dir)

    problems = preflight(renames, archive_dir)
    if problems:
        print("ABORT -- preflight failed, nothing was touched:", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        sys.exit(1)
    print("Preflight: all sources present, no destination collisions.")
    print()

    print(f"Shortname file: {shortname_path}")
    delete_shortname_line(shortname_path, apply=False)
    print(f"Series alias to record: {VARIANT_NAME!r} -> {CANONICAL_NAME!r}")
    print()

    if not args.apply:
        print("Dry-run only -- nothing was renamed or written. "
              "Re-run with --apply to perform this migration.")
        return

    # 1. files first: the only step that can fail on the filesystem, and the one
    #    that leaves the archive inconsistent if the config moves ahead of it.
    renamed = apply_renames(renames, archive_dir)
    print(f"Renamed {renamed} file(s).")

    # 2. manifest
    changed = apply_manifest(renames)
    print(f"Manifest: rewrote {changed} path(s).")

    # 3. pHash index
    matched, unmatched = apply_index(renames)
    print(f"Index: {matched} row(s) updated in {DB_PATH}.")
    for src in unmatched:
        print(f"  warning: no index row for {src} (index may predate the file).")

    # 4/5. alias BEFORE the line delete, so "Madoka Magica" never resolves to
    #      nothing in between.
    save_series_alias(VARIANT_NAME, CANONICAL_NAME)
    print(f"Series alias recorded: {VARIANT_NAME!r} -> {CANONICAL_NAME!r}")
    if delete_shortname_line(shortname_path, apply=True):
        print(f"Deleted the {OLD_CODE} line from {shortname_path}.")

    verify(renames, archive_dir, repo_root)


def _already_migrated_paths(manifest: dict):
    out = []
    for key in sorted(manifest):
        entry = manifest[key]
        if not isinstance(entry, dict):
            continue
        archive_path = entry.get("archive_path") or ""
        if Path(archive_path).stem.endswith(NEW_SUFFIX) and archive_path.startswith("Others/Known Series/"):
            out.append(archive_path)
        for wallpaper_path in entry.get("wallpaper_paths") or []:
            if Path(wallpaper_path).stem.endswith(NEW_SUFFIX):
                out.append(wallpaper_path)
    return out


def _owner_of(manifest: dict, rel_path: str) -> str:
    for key, entry in manifest.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("archive_path") == rel_path or rel_path in (entry.get("wallpaper_paths") or []):
            return key
    return "?"


if __name__ == "__main__":
    main()
