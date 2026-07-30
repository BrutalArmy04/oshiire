"""Shared layout.json + shortname-file I/O, matching, and proposal helpers.

Used by archive.py (Slice 3a routing), resolve.py (Slice 3b flag resolution),
and review.py (Slice 2 review UI). Generic read/parse/write/match/propose over
layout.json and the shortname file, so all three call sites share one
implementation instead of drifting apart.

Name RESOLUTION (resolve_franchise / resolve_character / franchise_folder_and_def)
lives here too, rather than in archive.py, because all three UIs need it: the
review UI must know whether a typed character resolves to a folder in order to
offer to learn an alias, and it must not import Slice 3a's routing engine to
find out. The routing DECISIONS built on top of these (route_entry, the
precedence ladder, Others_Group/Crossover) stay in archive.py.
"""
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

LAYOUT_PATH = Path("layout.json")
SERIES_ALIASES_PATH = Path("data/series_aliases.json")


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


def load_series_aliases(path: Path = SERIES_ALIASES_PATH) -> dict:
    """Returns the {variant_name: canonical_name} map, or {} when the file
    doesn't exist yet. Unlike load_layout this never exits -- the alias store
    is optional, and every caller must work fine without one."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f).get("aliases", {})


def save_series_aliases(aliases: dict, path: Path = SERIES_ALIASES_PATH) -> None:
    """Atomic write, same tmp+os.replace discipline as save_layout. Sorted,
    since this file has no hand-curated order to preserve."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump({"aliases": aliases}, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp_path, path)


def canonicalize_series(name: str, aliases: Optional[dict]) -> str:
    """Maps a variant series name to its canonical name, case-insensitively.
    Returns `name` unchanged when there's no alias for it. Single-hop by
    design: save_series_alias resolves the target before storing, so an alias
    never points at another alias and no chain-following is needed here."""
    if not name or not aliases:
        return name
    target = _normalize_series_name(name)
    for variant, canonical in aliases.items():
        if _normalize_series_name(variant) == target:
            return canonical
    return name


def save_series_alias(variant: str, canonical: str, path: Path = SERIES_ALIASES_PATH) -> dict:
    """Records variant -> canonical and returns the updated map. Resolves
    `canonical` through the existing aliases first, so pointing a new variant
    at something that is itself an alias stores the real canonical instead of
    building a chain."""
    aliases = load_series_aliases(path)
    resolved = canonicalize_series(canonical, aliases)
    aliases[variant.strip()] = resolved.strip()
    save_series_aliases(aliases, path)
    return aliases


def save_character_alias(folder_name: str, variant: str, canonical: str,
                         layout: dict, path: Path = LAYOUT_PATH) -> dict:
    """Records a per-franchise character alias (variant -> canonical folder) and
    returns the updated layout. The character-level twin of save_series_alias.

    Scoped by franchise FOLDER because two franchises can have different
    characters sharing one short name -- a global store would make "Marine"
    ambiguous. Lives in layout.json's existing `character_aliases` rather than a
    data/ file of its own: that block is already {folder: {variant: canonical}}
    and is already the only thing character matching consults, so there is
    exactly one source of truth and hand-written entries keep working.

    Resolves `canonical` through the existing aliases first, so pointing a new
    variant at something that is itself an alias stores the real target instead
    of building a chain (same rule as save_series_alias). Writing goes through
    save_layout, so it is atomic (tmp file + os.replace)."""
    table = layout.setdefault("character_aliases", {}).setdefault(folder_name, {})
    _, existing = _lookup_character(table, canonical)
    resolved = (existing if existing is not None else canonical).strip()
    table[variant.strip()] = resolved
    save_layout(layout, path)
    return layout


def resolve_franchise(tag_name: str, layout: dict, series_aliases: Optional[dict] = None):
    """Returns (folder_name_or_None, status) where status is one of
    "aliased", "identity", "unmapped". A None folder_name with status
    "aliased" means an explicit null alias (no folder exists yet).

    The tag is first canonicalized through the series-alias store (a recorded
    variant name resolves to its canonical series), then matched against
    layout.json case-insensitively -- so a casing difference in a tag can
    never flag an entry whose franchise is already configured."""
    tag_name = canonicalize_series(tag_name, series_aliases)
    matched, folder = lookup_ci(layout.get("franchise_aliases", {}), tag_name)
    if matched is not None:
        return folder, "aliased"
    matched, _ = lookup_ci(layout.get("franchises", {}), tag_name)
    if matched is not None:
        return matched, "identity"
    return None, "unmapped"


def _lookup_character(table, name: str):
    """lookup_ci for CHARACTER names: compares on normalize_name_key.

    lookup_ci uses the SERIES normalizer, which only casefolds and strips
    trailing punctuation -- so "Hutao" never matched a "Hu Tao" folder and every
    spacing variant had to be recorded as its own alias. Character names are
    routinely typed both ways (family-name-only, no spaces, hyphenated), so they
    get the stricter key that drops all non-alphanumerics. Returns the
    CONFIGURED spelling, never the caller's casing, so the path built from it
    points at the real folder on disk.

    Works over a dict (returns (key, value)) or a list (returns (key, key)),
    matching lookup_ci's shape."""
    if not table or not name:
        return None, None
    target = normalize_name_key(name)
    if not target:
        return None, None
    items = table.items() if isinstance(table, dict) else ((item, item) for item in table)
    for key, value in items:
        if normalize_name_key(key) == target:
            return key, value
    return None, None


def resolve_character(folder_name: str, franchise_def: dict, character_name: str, layout: dict):
    """Returns the matching character SUBFOLDER name, or None. Matching is
    case-insensitive and ignores spacing/punctuation, and always returns the
    name as configured in layout.json -- never the tag's casing -- so the path
    built from it points at the real folder on disk.

    If the name doesn't resolve as given, a two-token name is retried in
    reversed token order (see reversed_name_variant) before giving up, so
    "Kuki Shinobu" finds a "Shinobu Kuki" folder and vice versa. The retry runs
    the full alias-then-roster path, since the alias table may be keyed on
    either order. Only the LOOKUP is reversed -- the tag stored in the manifest
    is never rewritten."""
    if not character_name:
        return None
    aliases = layout.get("character_aliases", {}).get(folder_name, {})
    characters = franchise_def.get("characters", [])

    for candidate_name in (character_name, reversed_name_variant(character_name)):
        if not candidate_name:
            continue
        _, aliased = _lookup_character(aliases, candidate_name)
        candidate = aliased if aliased is not None else candidate_name
        matched, _ = _lookup_character(characters, candidate)
        if matched:
            return matched
    return None


def franchise_folder_and_def(franchise_tag, layout: dict, series_aliases: Optional[dict] = None):
    """(folder_name_or_None, franchise_def_or_None) for a franchise TAG.

    resolve_franchise alone isn't enough: its "identity" path returns the tag's
    own casing, and an exact-case dict lookup on that yields an empty roster (or
    a KeyError on write). The lookup_ci re-key step is what guarantees the
    caller holds the real layout.json key."""
    folder_name, _ = resolve_franchise(franchise_tag, layout, series_aliases)
    if not folder_name:
        return None, None
    matched_folder, franchise_def = lookup_ci(layout.get("franchises", {}), folder_name)
    return matched_folder, franchise_def


# Sensible defaults used when layout.json has no "wallpaper_rules" block, or
# fills in only some of it. Aspect is width/height, so the PC band is >1 and
# the phone band <1 -- disjoint by construction, which is why "both" is
# effectively never auto-suggested (see imagemeta.suggest_wallpaper).
DEFAULT_WALLPAPER_RULES = {
    "pc": {"min_width": 1920, "min_height": 1080, "aspect_min": 1.30, "aspect_max": 2.40},
    "phone": {"min_width": 1080, "min_height": 1920, "aspect_min": 0.40, "aspect_max": 0.75},
}


def load_wallpaper_rules(layout: dict) -> dict:
    """Merge layout.json's optional "wallpaper_rules" over the defaults.

    Merged per-key rather than replaced wholesale so a layout can override
    just one threshold (e.g. only pc.min_width) without having to restate the
    whole block. Unknown targets are ignored -- only pc/phone are meaningful."""
    configured = (layout or {}).get("wallpaper_rules") or {}
    rules = {}
    for target, defaults in DEFAULT_WALLPAPER_RULES.items():
        rule = dict(defaults)
        override = configured.get(target)
        if isinstance(override, dict):
            rule.update(override)
        rules[target] = rule
    return rules


def reversed_name_variant(name: str) -> Optional[str]:
    """For an exactly-two-token name, return the token-swapped form ("Kuki
    Shinobu" -> "Shinobu Kuki"); None for anything else.

    Western/Japanese name order varies between how a post tags a character and
    how the folder on disk is named, so a match-time swap catches folders that
    the alias table would otherwise have to enumerate by hand. Deliberately
    capped at two tokens: with 3+ the permutations stop being a name-order
    question and start being guesses, and this must never invent a match.
    Match-time only -- callers use the result to LOOK UP a folder, never to
    rename a stored tag."""
    if not name:
        return None
    tokens = name.split()
    if len(tokens) != 2:
        return None
    return f"{tokens[1]} {tokens[0]}"


def normalize_name_key(name: str) -> str:
    """Comparison key for a CHARACTER name: casefolded, with every separator
    and punctuation mark dropped, so "Hu Tao", "Hutao" and "hu-tao" collapse
    to one key.

    Deliberately more aggressive than _normalize_series_name, because the two
    answer different questions. Series names are compared to decide which
    FOLDER or shortname a tag means, where dropping spaces would let genuinely
    different series collide. Character names arrive from two independent
    sources for the same entry -- the subreddit map's canonical spelling and
    the title parser's -- which differ mostly in spacing, and a character must
    appear in the list exactly once.

    A key-only helper: callers use it to compare, never to store. The name
    written to the manifest is always a real spelling, never this."""
    return re.sub(r"[^0-9a-z]+", "", (name or "").casefold())


def dedupe_names(names) -> list:
    """Collapse names that differ only by spacing/punctuation/casing, keeping
    the FIRST spelling of each (callers order their canonical spelling first).
    A name that normalizes to nothing at all falls back to comparing itself,
    so it is neither dropped nor merged into another."""
    seen = set()
    result = []
    for name in names:
        key = normalize_name_key(name) or name
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
    return result


def lookup_ci(mapping, key: str):
    """Case-insensitive lookup over a dict (or membership test over a list).
    Returns (matched_key, value) for a dict, or (matched_key, matched_key) for
    a list; (None, None) on no match. Always returns the CONFIGURED key, never
    the caller's casing, so folder names on disk are never renamed by a typo."""
    if not key:
        return None, None
    if isinstance(mapping, dict):
        if key in mapping:
            return key, mapping[key]
        target = _normalize_series_name(key)
        for existing_key, value in mapping.items():
            if _normalize_series_name(existing_key) == target:
                return existing_key, value
        return None, None

    if key in mapping:
        return key, key
    target = _normalize_series_name(key)
    for existing_key in mapping:
        if _normalize_series_name(existing_key) == target:
            return existing_key, existing_key
    return None, None


def _parse_shortname_line(line: str) -> Optional[tuple]:
    """Parses ONE line into (code, full_name), or None for a blank/comment/
    non-mapping line. The single definition of the line format -- both the
    whole-file reader below and save_shortname_entry's index-preserving
    rewrite go through it, so neither can drift from the other."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    code, full_name = stripped.split("=", 1)
    return code.strip(), full_name.strip()


def _parse_shortname_lines(text: str):
    """Yields (code, full_name) for each real mapping line in a shortname
    file's text -- skips blank lines and comments. Shared by every
    reader/writer below so the line format is parsed exactly one way."""
    for line in text.splitlines():
        parsed = _parse_shortname_line(line)
        if parsed is not None:
            yield parsed


def load_shortname_map(layout: dict) -> list:
    """Returns [(code, full_name), ...] in file order. A list, not a dict,
    since match_shortname needs to check both the code and full_name columns
    (a dict keyed by lowercased full_name alone can't represent that)."""
    shortname_path = Path(layout["shortname_file"])
    if not shortname_path.exists():
        return []
    return list(_parse_shortname_lines(shortname_path.read_text(encoding="utf-8")))


def _normalize_series_name(name: str) -> str:
    """Casefold and strip trailing !/./whitespace so trivially-equivalent
    series names ("Bocchi the rock!" vs "Bocchi the Rock") compare equal.
    Used by shortname matching and collision detection so such names reuse
    one code instead of demanding a near-duplicate entry."""
    return re.sub(r"[!.\s]+$", "", (name or "").strip()).casefold()


def same_series_name(a: str, b: str) -> bool:
    """True when two series names differ only in casing/trailing punctuation.
    The public form of the module's comparison rule, so callers never have to
    reach for _normalize_series_name or roll their own .lower()."""
    return _normalize_series_name(a) == _normalize_series_name(b)


def match_shortname(tag_name: str, shortname_entries: list, aliases: Optional[dict] = None) -> Optional[str]:
    """Matches a franchise tag against the shortname file's code AND full_name
    columns, case-insensitively, including a tag that's a leading token of a
    longer full_name (e.g. "NIKKE" vs "NIKKE The Goddess of Victory"). Both
    columns are compared via _normalize_series_name, so casing and trailing
    punctuation never decide a match ("bocchi the rock!" matches "Bocchi the
    Rock"). When `aliases` is given, the tag is canonicalized through the
    series-alias store first, so a recorded variant name resolves to its
    canonical series. Only reached in route_entry after layout.json resolution
    has already failed, so a false positive here can only affect shortname-
    fallback routing -- never a real layout.json mapping. Returns the matched
    code, or None."""
    if not tag_name:
        return None
    tag_name = canonicalize_series(tag_name, aliases)
    tag_norm = _normalize_series_name(tag_name)
    for code, full_name in shortname_entries:
        full_norm = _normalize_series_name(full_name)
        if tag_norm == _normalize_series_name(code) or tag_norm == full_norm:
            return code
        if tag_norm and full_norm.startswith(tag_norm) and (
            len(full_norm) == len(tag_norm) or not full_norm[len(tag_norm)].isalnum()
        ):
            return code
    return None


def propose_shortname_code(tag_name: str, shortname_entries: list) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", tag_name or "") if w]
    if not words:
        return ""
    base = "".join(w[0].upper() for w in words)

    # Normalized, so a proposal can't collide case-insensitively with an
    # existing code -- match_shortname would treat "mm" and "MM" as the same
    # code, so proposing the second one would create an undecodable pair.
    used_codes = {_normalize_series_name(code) for code, _ in shortname_entries}
    if _normalize_series_name(base) not in used_codes:
        return base
    n = 2
    while _normalize_series_name(f"{base}{n}") in used_codes:
        n += 1
    return f"{base}{n}"


def find_shortname_collision(shortname_path: Path, code: str, full_name: str) -> Optional[str]:
    """Returns the existing full_name already using `code`, if any, when that
    full_name differs from the one being saved -- else None. Guards against
    two different series silently sharing one undecodable code."""
    if not shortname_path.exists():
        return None
    target_code = _normalize_series_name(code)
    target_full = _normalize_series_name(full_name)
    for existing_code, existing_full in _parse_shortname_lines(shortname_path.read_text(encoding="utf-8")):
        if _normalize_series_name(existing_code) == target_code and _normalize_series_name(existing_full) != target_full:
            return existing_full
    return None


def verify_shortname_entry(shortname_path: Path, code: str, full_name: str) -> bool:
    """Re-reads the file after a save and confirms `code = full_name` is
    actually present on disk -- guards against a save that silently didn't
    persist (e.g. a stale path, a swallowed write error)."""
    if not shortname_path.exists():
        return False
    target_code = _normalize_series_name(code)
    target_full = _normalize_series_name(full_name)
    return any(
        _normalize_series_name(existing_code) == target_code
        and _normalize_series_name(existing_full) == target_full
        for existing_code, existing_full in _parse_shortname_lines(shortname_path.read_text(encoding="utf-8"))
    )


def save_shortname_entry(shortname_path: Path, code: str, full_name: str) -> None:
    """Atomic, line-based write that preserves comments/blank lines/other
    entries -- does not round-trip through load_shortname_map's list, which
    discards exactly that formatting. Replaces an existing entry for the same
    full_name in place, else appends a new line. Name comparison goes through
    _normalize_series_name -- the same rule the readers use -- so a name
    differing only in casing or trailing punctuation updates the existing row
    instead of appending a near-duplicate under a second code."""
    lines = shortname_path.read_text(encoding="utf-8").splitlines() if shortname_path.exists() else []
    target = _normalize_series_name(full_name)
    new_line = f"{code.strip()} = {full_name.strip()}"

    for i, line in enumerate(lines):
        parsed = _parse_shortname_line(line)
        if parsed is None:
            continue
        if _normalize_series_name(parsed[1]) == target:
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
