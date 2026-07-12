"""Shared layout.json + shortname-file I/O, matching, and proposal helpers.

Used by archive.py (Slice 3a routing), resolve.py (Slice 3b flag resolution),
and review.py (Slice 2 review UI). Holds no routing/business logic (that
stays in archive.py's resolve_franchise/resolve_character/route_entry) --
just generic read/parse/write/match/propose over layout.json and the
shortname file, so all three call sites share one implementation instead of
drifting apart.
"""
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

LAYOUT_PATH = Path("layout.json")


def load_layout(path: Path = LAYOUT_PATH) -> dict:
    if not path.exists():
        print(
            f"{path} not found. Copy layout.example.json to {path} and fill in "
            "your archive layout, then retry.",
            file=sys.stderr,
        )
        sys.exit(1)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_layout(layout: dict, path: Path = LAYOUT_PATH) -> None:
    """Atomic write, preserving layout.json's hand-curated key order -- no
    sort_keys, since the file is loaded, mutated in place, and re-dumped."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(layout, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def _parse_shortname_lines(text: str):
    """Yields (code, full_name) for each real mapping line in a shortname
    file's text -- skips blank lines and comments. Shared by every
    reader/writer below so the line format is parsed exactly one way."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        code, full_name = stripped.split("=", 1)
        yield code.strip(), full_name.strip()


def load_shortname_map(layout: dict) -> list:
    """Returns [(code, full_name), ...] in file order. A list, not a dict,
    since match_shortname needs to check both the code and full_name columns
    (a dict keyed by lowercased full_name alone can't represent that)."""
    shortname_path = Path(layout["shortname_file"])
    if not shortname_path.exists():
        return []
    return list(_parse_shortname_lines(shortname_path.read_text(encoding="utf-8")))


def match_shortname(tag_name: str, shortname_entries: list) -> Optional[str]:
    """Matches a franchise tag against the shortname file's code AND full_name
    columns, case-insensitively, including a tag that's a leading token of a
    longer full_name (e.g. "NIKKE" vs "NIKKE The Goddess of Victory"). Only
    reached in route_entry after layout.json resolution has already failed,
    so a false positive here can only affect shortname-fallback routing --
    never a real layout.json mapping. Returns the matched code, or None."""
    if not tag_name:
        return None
    tag = tag_name.strip().lower()
    for code, full_name in shortname_entries:
        code_l = code.lower()
        full_l = full_name.lower()
        if tag == code_l or tag == full_l:
            return code
        if full_l.startswith(tag) and (
            len(full_l) == len(tag) or not full_l[len(tag)].isalnum()
        ):
            return code
    return None


def propose_shortname_code(tag_name: str, shortname_entries: list) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", tag_name or "") if w]
    if not words:
        return ""
    base = "".join(w[0].upper() for w in words)

    used_codes = {code for code, _ in shortname_entries}
    if base not in used_codes:
        return base
    n = 2
    while f"{base}{n}" in used_codes:
        n += 1
    return f"{base}{n}"


def find_shortname_collision(shortname_path: Path, code: str, full_name: str) -> Optional[str]:
    """Returns the existing full_name already using `code`, if any, when that
    full_name differs from the one being saved -- else None. Guards against
    two different series silently sharing one undecodable code."""
    if not shortname_path.exists():
        return None
    target_code = code.strip()
    target_full = full_name.strip().lower()
    for existing_code, existing_full in _parse_shortname_lines(shortname_path.read_text(encoding="utf-8")):
        if existing_code == target_code and existing_full.lower() != target_full:
            return existing_full
    return None


def verify_shortname_entry(shortname_path: Path, code: str, full_name: str) -> bool:
    """Re-reads the file after a save and confirms `code = full_name` is
    actually present on disk -- guards against a save that silently didn't
    persist (e.g. a stale path, a swallowed write error)."""
    if not shortname_path.exists():
        return False
    target_code = code.strip()
    target_full = full_name.strip().lower()
    return any(
        existing_code == target_code and existing_full.lower() == target_full
        for existing_code, existing_full in _parse_shortname_lines(shortname_path.read_text(encoding="utf-8"))
    )


def save_shortname_entry(shortname_path: Path, code: str, full_name: str) -> None:
    """Atomic, line-based write that preserves comments/blank lines/other
    entries -- does not round-trip through load_shortname_map's list, which
    discards exactly that formatting. Replaces an existing entry for the same
    full_name (case-insensitive) in place, else appends a new line."""
    lines = shortname_path.read_text(encoding="utf-8").splitlines() if shortname_path.exists() else []
    target = full_name.strip().lower()
    new_line = f"{code.strip()} = {full_name.strip()}"

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        _, existing_full = stripped.split("=", 1)
        if existing_full.strip().lower() == target:
            lines[i] = new_line
            break
    else:
        lines.append(new_line)

    tmp_path = shortname_path.with_suffix(shortname_path.suffix + ".tmp")
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, shortname_path)


def undo_shortname_write(path: Path, existed_before: bool, snapshot: Optional[str]) -> None:
    """Reverts a save_shortname_entry call: restores the pre-save file text if
    it existed, else deletes the file that save_shortname_entry created."""
    if existed_before:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(snapshot, encoding="utf-8")
        os.replace(tmp_path, path)
    elif path.exists():
        path.unlink()
