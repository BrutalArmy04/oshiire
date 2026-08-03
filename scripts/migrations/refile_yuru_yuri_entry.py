"""One-off migration: re-file the Yuru Yuri image misfiled under the YC code.

While data/series_aliases.json mapped "Yuru Yuri" onto "Yuru Camp", every
Yuru Yuri tag canonicalized to Yuru Camp and matched its YC shortname, so the
art filed as Others/Known Series/{id}_YC.* -- indistinguishable by path from a
genuine Yuru Camp image. fix_yuru_yuri_alias.py retracted the alias and gave
Yuru Yuri its own YY code; this moves the one file that alias misfiled.

SCOPE NOTE: which entries are wrong is a judgement about the ARTWORK, which a
path cannot answer -- but that judgement is already recorded, as the entry's
franchise TAG. So the set is discovered from the manifest on exactly that:
carries the wrong suffix AND is tagged with the misfiled series. Nothing is
hardcoded. An image of the sibling series is tagged with the sibling series and
is therefore never selected, which is the property that matters here and is
asserted explicitly at the end rather than left to omission.

Dry-run by default, --apply to execute (same convention as archive.py).
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

WRONG_SUFFIX = "_YC"
RIGHT_SUFFIX = "_YY"

# The franchise tag whose art was misfiled. This is the whole selector: an
# entry carrying the wrong suffix AND this tag is misfiled by construction.
MISFILED_TAG = "Yuru Yuri"

DB_PATH = Path("data/archive_index.db")


def swap_suffix(rel_path: str) -> str:
    """Anchored on the stem's tail, so a coincidental "_YC" earlier in a
    reddit-generated filename can never be rewritten."""
    p = Path(rel_path)
    if not p.stem.endswith(WRONG_SUFFIX):
        return rel_path
    return p.with_name(p.stem[: -len(WRONG_SUFFIX)] + RIGHT_SUFFIX + p.suffix).as_posix()


def build_plan(manifest: dict):
    """Returns renames for every entry that carries the wrong suffix AND is
    tagged with the misfiled series -- both conditions, so an entry of the
    sibling series that legitimately uses this code is never selected."""
    renames = []
    for key in sorted(manifest):
        entry = manifest[key]
        if not isinstance(entry, dict):
            continue
        src = entry.get("archive_path") or ""
        if not Path(src).stem.endswith(WRONG_SUFFIX):
            continue
        tags = entry.get("franchise") or []
        if not any(t.casefold() == MISFILED_TAG.casefold() for t in tags):
            continue
        renames.append((src, swap_suffix(src), key))
    return renames


def preflight(renames, archive_dir: Path):
    """Every source must exist and every destination must be free, checked for
    the WHOLE plan before a single file is touched."""
    problems = []
    for src, dst, key in renames:
        if not (archive_dir / src).exists():
            problems.append(f"  missing source: {src}  ({key})")
        if (archive_dir / dst).exists():
            problems.append(f"  destination already exists: {dst}  ({key})")
    return problems


def report_siblings(manifest: dict, renames) -> None:
    """Name every OTHER entry still on the wrong-suffix code and say plainly
    that it is staying -- confirmed explicitly, never by omission."""
    moving = {k for _, _, k in renames}
    staying = [
        (k, e) for k, e in sorted(manifest.items())
        if isinstance(e, dict)
        and Path(e.get("archive_path") or "").stem.endswith(WRONG_SUFFIX)
        and k not in moving
    ]
    print(f"Entries keeping the {WRONG_SUFFIX} code ({len(staying)}) -- NOT moved:")
    for key, entry in staying:
        print(f"  {key}  franchise={entry.get('franchise')}  {entry.get('archive_path')!r}")
        print(f"      {entry.get('title')!r}")
    if not staying:
        print("  (none)")
    print()


def apply_index(renames, db_path: Path = DB_PATH):
    """A rename changes no pixels, so phash/size/mtime stay put and nothing is
    rehashed. A row that doesn't match is a warning, not an error."""
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


def verify(renames, archive_dir: Path, manifest_before: dict):
    print()
    print("=" * 72)
    print("VERIFICATION")
    print("=" * 72)
    manifest = load_manifest()
    ok = True

    print()
    print("1. moved files present, old paths gone")
    for src, dst, key in renames:
        here = (archive_dir / dst).exists()
        gone = not (archive_dir / src).exists()
        ok &= here and gone
        print(f"   [{'ok' if here and gone else 'FAIL'}] {dst} exists={here}, "
              f"old path gone={gone}   ({key})")

    print()
    print("2. manifest archive_path rewritten")
    for _src, dst, key in renames:
        got = manifest[key].get("archive_path")
        ok &= got == dst
        print(f"   [{'ok' if got == dst else 'FAIL'}] {key} -> {got!r}")

    print()
    print(f"3. no {WRONG_SUFFIX} path remains for the moved entries")
    for _src, _dst, key in renames:
        stem = Path(manifest[key].get("archive_path") or "").stem
        clean = not stem.endswith(WRONG_SUFFIX)
        ok &= clean
        print(f"   [{'ok' if clean else 'FAIL'}] {key} stem={stem!r}")

    print()
    print("4. index rows updated, none pointing at a missing file")
    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        try:
            for src, dst, key in renames:
                stale = conn.execute("SELECT COUNT(*) FROM images WHERE rel_path = ?",
                                     (src,)).fetchone()[0]
                fresh = conn.execute("SELECT COUNT(*) FROM images WHERE rel_path = ?",
                                     (dst,)).fetchone()[0]
                on_disk = (archive_dir / dst).exists()
                good = stale == 0 and fresh == 1 and on_disk
                ok &= good
                print(f"   [{'ok' if good else 'FAIL'}] {dst}  old_rows={stale} "
                      f"new_rows={fresh} file_exists={on_disk}   ({key})")
        finally:
            conn.close()

    print()
    print("5. every entry NOT in the plan is byte-identical in the manifest")
    moving = {k for _, _, k in renames}
    changed = [
        k for k in manifest_before
        if k not in moving and manifest_before[k] != manifest.get(k)
    ]
    ok &= not changed
    print(f"   [{'ok' if not changed else 'FAIL'}] {len(changed)} unrelated entry(ies) changed "
          f"(expected 0){'  ' + ', '.join(changed) if changed else ''}")

    print()
    print("All checks passed." if ok else "FAILURES above.")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Re-file {MISFILED_TAG} art misfiled under the {WRONG_SUFFIX} "
                    "shortname code (one-off data migration)."
    )
    parser.add_argument("--apply", "--execute", dest="apply", action="store_true",
                        help="Actually rename files and write the manifest/index. Default is dry-run.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    os.chdir(repo_root)  # every module path (manifest.json) is repo-relative

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
    manifest_before = {k: dict(v) for k, v in manifest.items() if isinstance(v, dict)}

    renames = build_plan(manifest)

    print(f"ARCHIVE_DIR: {archive_dir}")
    print()
    report_siblings(manifest, renames)

    if not renames:
        print(f"Nothing to migrate -- no {WRONG_SUFFIX} entry is tagged {MISFILED_TAG!r}.")
        return

    print("Planned renames")
    print("---------------")
    for src, dst, key in renames:
        entry = manifest[key]
        exists = "ok " if (archive_dir / src).exists() else "MISSING"
        clash = "  <- DEST EXISTS" if (archive_dir / dst).exists() else ""
        print(f"  [{exists}] {src}")
        print(f"        -> {dst}   ({key}, franchise={entry.get('franchise')}){clash}")
        print(f"        {entry.get('title')!r}")
    print(f"  {len(renames)} file(s).")
    print()

    preflight_problems = preflight(renames, archive_dir)
    if preflight_problems:
        print("ABORT -- preflight failed, nothing was touched:", file=sys.stderr)
        for problem in preflight_problems:
            print(problem, file=sys.stderr)
        sys.exit(1)
    print("Preflight: all sources present, no destination collisions.")
    print()

    if not args.apply:
        print("Dry-run only -- nothing was renamed or written. Re-run with --apply.")
        return

    # 1. files first: the only step that can fail on the filesystem.
    for src, dst, _key in renames:
        os.replace(archive_dir / src, archive_dir / dst)
    print(f"Renamed {len(renames)} file(s).")

    # 2. manifest
    for _src, dst, key in renames:
        manifest[key]["archive_path"] = dst
    save_manifest(manifest)
    print(f"Manifest: rewrote {len(renames)} archive_path(s).")

    # 3. pHash index
    matched, unmatched = apply_index(renames)
    print(f"Index: {matched} row(s) updated in {DB_PATH}.")
    for src in unmatched:
        print(f"  warning: no index row for {src} (index may predate the file).")

    sys.exit(0 if verify(renames, archive_dir, manifest_before) else 1)


if __name__ == "__main__":
    main()
