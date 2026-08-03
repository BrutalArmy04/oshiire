"""One-off migration: retract the wrong "Yuru Yuri" -> "Yuru Camp" series alias.

data/series_aliases.json mapped Yuru Yuri (a comedy) onto Yuru Camp
(Laid-Back Camp) -- two unrelated series. The alias was recorded by answering
the review UI's "remember this?" prompt with the wrong target, the same origin
as the r/Asuka mismapping.

It leaves no trace in the manifest: both names canonicalize to "Yuru Camp" and
match the YC shortname, so a misfiled entry lands at
Others/Known Series/{id}_YC.* and is indistinguishable from a correct one by
path alone. Identification needs the titles.

Order matters. The shortname code is added FIRST and the alias retracted
SECOND, so "Yuru Yuri" never resolves to nothing in between -- the same rule
the MM2 migration followed. Adding the code first is not enough on its own:
match_shortname canonicalizes BEFORE matching, so while the alias stands it
still wins and returns YC. Retracting is what flips it to YY.

Config only -- this moves NO files. Re-filing the misfiled entry is a separate
decision, made from the titles, and is deliberately not automated here.

Dry-run by default, --apply to execute (same convention as archive.py).
"""
import argparse
import os
import sys
from pathlib import Path

# This script lives in scripts/migrations/; the modules it reuses live at the
# repo root, two levels up.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from manifest import load_manifest
from shortname import (
    canonicalize_series,
    find_shortname_collision,
    load_layout,
    load_series_aliases,
    load_shortname_map,
    match_shortname,
    propose_shortname_code,
    remove_series_alias,
    save_shortname_entry,
    verify_shortname_entry,
)

VARIANT = "Yuru Yuri"
WRONG_TARGET = "Yuru Camp"
NEW_CODE = "YY"

# Guards on series we deliberately did NOT merge, plus the earlier MM2 work.
REGRESSION_GUARDS = [
    ("Yuru Camp", "YC"),
    ("Madoka Magica", "MM"),
    ("Magia Record", "MM"),
    ("Sound Euphonium", "SE"),
    ("Soul Eater", "SE2"),
]


def report_state(label: str) -> None:
    layout = load_layout()
    aliases = load_series_aliases()
    shortnames = load_shortname_map(layout)
    print(f"  [{label}] canonicalize_series({VARIANT!r}) -> "
          f"{canonicalize_series(VARIANT, aliases)!r}")
    print(f"  [{label}] match_shortname({VARIANT!r})      -> "
          f"{match_shortname(VARIANT, shortnames, aliases)!r}")


def report_affected() -> None:
    """Every archived entry carrying the _YC suffix, so the misfiled ones can be
    picked out by title. This script does not act on them."""
    manifest = load_manifest()
    rows = [
        (k, e) for k, e in sorted(manifest.items())
        if isinstance(e, dict) and Path(e.get("archive_path") or "").stem.endswith("_YC")
    ]
    print(f"Entries filed under the YC shortname: {len(rows)}")
    for key, entry in rows:
        print(f"  {key}  r/{entry.get('subreddit')}  franchise={entry.get('franchise')}")
        print(f"      {entry.get('title')!r}")
        print(f"      {entry.get('archive_path')!r}")
    print("  (no files are moved by this script)")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Retract the {VARIANT!r} -> {WRONG_TARGET!r} series alias "
                    "and give Yuru Yuri its own shortname code."
    )
    parser.add_argument("--apply", "--execute", dest="apply", action="store_true",
                        help="Actually write the config. Default is dry-run.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    os.chdir(repo_root)  # every module path (layout.json, data/) is repo-relative

    layout = load_layout()
    shortname_path = Path(layout["shortname_file"])
    shortnames = load_shortname_map(layout)
    aliases = load_series_aliases()

    print(f"Shortname file: {shortname_path}")
    print(f"Alias store   : data/series_aliases.json ({len(aliases)} aliases)")
    print()

    report_affected()

    print("Before:")
    report_state("now")
    print()

    if canonicalize_series(VARIANT, aliases).casefold() != WRONG_TARGET.casefold():
        print(f"Nothing to do -- {VARIANT!r} no longer canonicalizes to {WRONG_TARGET!r}.")
        return

    proposed = propose_shortname_code(VARIANT, shortnames)
    if proposed != NEW_CODE:
        print(f"  note: propose_shortname_code suggests {proposed!r}, using {NEW_CODE!r}.")
    collision = find_shortname_collision(shortname_path, NEW_CODE, VARIANT)
    if collision:
        print(f"ABORT: code {NEW_CODE!r} is already used by {collision!r}.", file=sys.stderr)
        sys.exit(1)
    print(f"Plan:")
    print(f"  1. append `{NEW_CODE} = {VARIANT}` to {shortname_path} (code is free)")
    print(f"  2. remove_series_alias({VARIANT!r})")
    print(f"  (in that order, so {VARIANT!r} never resolves to nothing)")
    print()

    if not args.apply:
        print("Dry-run only -- nothing was written. Re-run with --apply.")
        return

    # 1. code first.
    before_bytes = shortname_path.read_bytes()
    save_shortname_entry(shortname_path, NEW_CODE, VARIANT)
    after_bytes = shortname_path.read_bytes()
    if not after_bytes.startswith(before_bytes):
        print("ABORT: the shortname file's existing lines are no longer byte-identical.",
              file=sys.stderr)
        sys.exit(1)
    print(f"Appended `{NEW_CODE} = {VARIANT}`; "
          f"{len(before_bytes)} existing bytes byte-identical, "
          f"added {after_bytes[len(before_bytes):]!r}")
    if not verify_shortname_entry(shortname_path, NEW_CODE, VARIANT):
        print("ABORT: the shortname entry did not persist.", file=sys.stderr)
        sys.exit(1)

    # 2. then retract the alias.
    remaining = remove_series_alias(VARIANT)
    print(f"Retracted the alias; {len(remaining)} alias(es) remain.")
    print()

    print("After:")
    report_state("now")
    print()

    print("Regression guards:")
    layout = load_layout()
    shortnames = load_shortname_map(layout)
    aliases = load_series_aliases()
    ok = True
    print(f"  match_shortname({VARIANT!r}) -> "
          f"{match_shortname(VARIANT, shortnames, aliases)!r}  (expected {NEW_CODE!r})")
    ok &= match_shortname(VARIANT, shortnames, aliases) == NEW_CODE
    for name, want in REGRESSION_GUARDS:
        got = match_shortname(name, shortnames, aliases)
        flag = "ok" if got == want else "FAIL"
        print(f"  [{flag}] match_shortname({name!r}) -> {got!r}  (expected {want!r})")
        ok &= got == want
    print()
    print("All guards passed." if ok else "GUARD FAILURE -- review the above.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
