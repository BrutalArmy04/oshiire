"""Slice 1: metadata-based character tagging.

Stable seam: guess_character(image_path, post_metadata) -> Guess. Until Slice 4
this only tries the metadata path (subreddit_map.json + title parsing); no
torch/imgutils import here or anywhere else.
"""
import json
import os
import re
import sys
from collections import namedtuple
from pathlib import Path

from shortname import dedupe_names

Guess = namedtuple("Guess", ["name", "confidence", "source"])

SUBREDDIT_MAP_PATH = Path("subreddit_map.json")
LAYOUT_PATH = Path("layout.json")

STOPWORDS = {
    "the", "a", "an", "this", "that", "these", "those", "when", "while",
    "what", "who", "why", "how", "too", "so", "very", "all", "here", "there",
    "you", "your", "its", "their", "them", "for", "but", "or", "not", "is",
    "are", "was", "were", "with", "without", "from", "of", "in", "on", "at",
    "by",
}

PAREN_RE = re.compile(r"\s*\([^()]*\)")
PAREN_CONTENT_RE = re.compile(r"\(([^()]*)\)")
BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
ID_MARKER_RE = re.compile(r"^\s*(i\s*:\s*\d+|pixiv\s*\d+)\s*$", re.IGNORECASE)
META_TAGS = {"media", "discussion"}
BY_CREDIT_RE = re.compile(r"^by\b", re.IGNORECASE)
# OC bracket phrasings that should all be treated as an "original" signal
# (tag Unknown, no franchise), not mis-read as a franchise/character list.
OC_BRACKET_RE = re.compile(
    r"^(?:original(?:\s+character)?|oc|artist(?:['’]s|s)?\s+original)$",
    re.IGNORECASE,
)

TOKEN_RE = re.compile(
    r"&|,|\band\b|×|[A-Za-z][A-Za-z0-9'\-]*|\d+[A-Za-z][A-Za-z0-9'\-]*|[-:|—]",
    re.IGNORECASE,
)
SEPARATOR_TOKENS = {"&", ",", "and"}
X_SEPARATOR_TOKENS = {"x", "×"}
DIVIDER_TOKENS = {"-", ":", "|", "—"}


def read_subreddit_map(path: Path = SUBREDDIT_MAP_PATH) -> dict:
    """Raw reader for EDITORS of the map (the review UI's Settings tab).

    Three deliberate differences from _load_subreddit_map, which is the reader
    for CONSUMERS of the map:
      - raises FileNotFoundError instead of calling sys.exit. A Gradio event
        handler runs inside the server process, so an exit there kills the UI
        for every tab, not just the failing action.
      - keeps underscore-prefixed keys (`_comment`), because an editor has to
        write the whole file back and silently dropping them would delete
        them.
      - does not lowercase keys, so what is shown and what is written match
        the file byte for byte.

    Nothing here is cached: the file is re-read on every call, so an edit takes
    effect for the next tagged post with no invalidation step.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy subreddit_map.example.json to {path} "
            "(and edit it to match your subs)."
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_subreddit_map(path: Path = SUBREDDIT_MAP_PATH) -> dict:
    """Reader for CONSUMERS (ingest/tagger CLI). Exits on a missing file, drops
    underscore-prefixed keys and lowercases the rest -- all three are relied on
    by the callers below, so this behaviour is unchanged. Only the file read
    itself is shared with read_subreddit_map."""
    try:
        data = read_subreddit_map(path)
    except FileNotFoundError:
        print(
            f"{path} not found. Copy subreddit_map.example.json to {path} "
            "(and edit it to match your subs), then retry.",
            file=sys.stderr,
        )
        sys.exit(1)
    return {k.lower(): v for k, v in data.items() if not k.startswith("_")}


def normalize_subreddit(subreddit: str) -> str:
    return subreddit.strip().lower()


def subreddit_is_mapped(subreddit: str, path: Path = SUBREDDIT_MAP_PATH) -> bool:
    return normalize_subreddit(subreddit) in _load_subreddit_map(path)


def _atomic_write_json(data: dict, path: Path) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


def save_subreddit_map_entry(subreddit: str, franchise: str, character: str = None,
                              path: Path = SUBREDDIT_MAP_PATH) -> None:
    """Adds/overwrites one subreddit_map.json entry, preserving every other
    key/value/order in the file. Called from the review UI when the user opts
    to remember a subreddit -> franchise/character mapping, and from its
    Settings tab.

    MERGES onto an existing entry rather than replacing it. Rebuilding the
    entry from scratch used to discard every key this function doesn't know
    about -- and 8 entries carry a hand-written `_note` explaining why they are
    shaped the way they are (one of them alongside a character). Saving any of
    those from the UI destroyed the note silently, with nothing in the diff to
    suggest the save had done more than it was asked to.

    `franchise` of None is written as JSON null and is MEANINGFUL: it marks a
    sub whose franchise comes from the title instead. It is not the same as an
    absent or empty franchise, so it is written through as-is.

    A falsy `character` removes the key, so clearing the field in the Settings
    tab clears the mapping. Insertion order keeps franchise/character first;
    unrecognized keys trail them exactly as they did in the file."""
    key = normalize_subreddit(subreddit)
    data = json.loads(path.read_text(encoding="utf-8"))
    existing = data.get(key)
    entry = dict(existing) if isinstance(existing, dict) else {}
    entry["franchise"] = franchise
    if character:
        entry["character"] = character
    else:
        entry.pop("character", None)
    data[key] = entry
    _atomic_write_json(data, path)


def remove_subreddit_map_entry(subreddit: str, path: Path = SUBREDDIT_MAP_PATH) -> None:
    """Undo counterpart to save_subreddit_map_entry -- removes the key if present."""
    key = normalize_subreddit(subreddit)
    data = json.loads(path.read_text(encoding="utf-8"))
    if key in data:
        del data[key]
        _atomic_write_json(data, path)


def lookup_subreddit(subreddit: str, sub_map: dict):
    """Returns (franchise, character) or (None, None)/(None, name) for fallbacks."""
    key = normalize_subreddit(subreddit)
    if key in sub_map:
        entry = sub_map[key]
        return entry.get("franchise"), entry.get("character")

    if key.endswith("mains"):
        return None, _clean_fallback_name(key[: -len("mains")])
    if key.startswith("churchof"):
        return None, _clean_fallback_name(key[len("churchof"):])
    if key.startswith("onetrue"):
        return None, _clean_fallback_name(key[len("onetrue"):])

    return None, None


def _clean_fallback_name(raw: str) -> str:
    """Subreddit-slug fallback names (Mains/ChurchOf/OneTrue stripping) can
    leave stray separator characters behind, e.g. "hutao_mains" ->
    "hutao_" -- strip those before capitalizing. Does not attempt to split
    camelCase slugs into multi-word names (e.g. "hutao" -> "Hu Tao"); that
    needs an explicit subreddit_map.json entry."""
    return raw.strip("_-. ").capitalize()


def _load_layout(path: Path = LAYOUT_PATH) -> dict:
    if not path.exists():
        return {}          # layout.json is personal/gitignored; may not exist yet
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _build_known_franchise_index(sub_map: dict, layout: dict) -> dict:
    """Lowercased franchise/alias string -> canonical spelling. Sourced from
    subreddit_map.json's franchise values plus layout.json's `franchises`
    keys and `franchise_aliases` keys. Used only to recognize a parenthetical
    as a franchise candidate (see extract_parenthetical_franchise) -- never a
    fuzzy/substring match."""
    index = {}
    for entry in sub_map.values():
        franchise = entry.get("franchise")
        if franchise:
            index.setdefault(franchise.lower(), franchise)
    for name in layout.get("franchises", {}):
        index.setdefault(name.lower(), name)
    for name in layout.get("franchise_aliases", {}):
        index.setdefault(name.lower(), name)
    return index


def extract_parenthetical_franchise(title: str, known_franchises: dict):
    """Scans every non-nested (...) group. "by ..." content is always
    artist credit. Otherwise, if content exactly matches a known franchise
    (case-insensitive), it's taken as a franchise candidate. Anything else
    (handles, pixiv ids, plain artist names) is left alone -- same silent-
    strip behavior as before. All parens are removed from the returned title
    regardless of which case applied."""
    franchise = None
    for match in PAREN_CONTENT_RE.finditer(title):
        content = match.group(1).strip()
        if BY_CREDIT_RE.match(content):
            continue
        if franchise is None:
            canonical = known_franchises.get(content.lower())
            if canonical:
                franchise = canonical
    remaining = PAREN_RE.sub("", title).strip()
    return remaining, franchise


def extract_bracket(title: str):
    """Returns (remaining_title, bracket_kind, bracket_content) using only the
    first bracket group found. bracket_kind is one of:
    "meta", "original", "id", "artist", "data", or None (no usable bracket)."""
    match = BRACKET_RE.search(title)
    if not match:
        return title, None, None

    content = match.group(1).strip()
    remaining = (title[: match.start()] + " " + title[match.end():]).strip()

    lowered = content.lower()
    if lowered in META_TAGS:
        return remaining, "meta", content
    if OC_BRACKET_RE.match(content):
        return remaining, "original", content
    if ID_MARKER_RE.match(content):
        return remaining, "id", content
    if BY_CREDIT_RE.match(content):
        return remaining, "artist", content
    return remaining, "data", content


def _clean_segment(text: str) -> str:
    return text.strip()


def extract_leading_names(text: str):
    """Scans from the start of text, accumulating consecutive capitalized
    words into name segments; &/,/and start a new segment. A standalone x/×
    also starts a new segment, but only when a name is already accumulating
    AND the next token is itself name-like (capitalized/digit-leading) --
    this is what lets "Raiden x Yae" split into two names while never
    touching the letter x inside a name like "Xinyan" (TOKEN_RE always
    tokenizes such names as one whole word, so a bare "x" token only ever
    arises when it's already a standalone word in the source text). Stops at
    the first lowercase word, stopword, divider punctuation, or end of
    string."""
    tokens = TOKEN_RE.findall(text)
    segments = []
    current = []

    for i, token in enumerate(tokens):
        lowered = token.lower()
        if lowered in SEPARATOR_TOKENS:
            if current:
                segments.append(" ".join(current))
                current = []
            continue
        if lowered in X_SEPARATOR_TOKENS:
            next_token = tokens[i + 1] if i + 1 < len(tokens) else ""
            next_is_name_like = next_token[:1].isupper() or next_token[:1].isdigit()
            if current and next_is_name_like:
                segments.append(" ".join(current))
                current = []
                continue
            # Not between two names (e.g. lone "x" or "X" with no name
            # forming yet) -- fall through to normal token handling below.
        if token in DIVIDER_TOKENS:
            break
        if lowered in STOPWORDS:
            break
        if not (token[:1].isupper() or token[:1].isdigit()):
            break
        current.append(token)

    if current:
        segments.append(" ".join(current))

    return [_clean_segment(s) for s in segments if _clean_segment(s)]


SEPARATOR_PRESENCE_RE = re.compile(r"&|,|\band\b|\bx\b|×", re.IGNORECASE)


def _has_separator(text: str) -> bool:
    return bool(SEPARATOR_PRESENCE_RE.search(text))


GENERIC_NOUN_WORDS = {
    "welcome", "favorite", "foods", "set", "see", "pure", "muscle", "rabbit",
}
_POSSESSIVE_RE = re.compile(r"['’]s$")
_TRAILING_INITIAL_RE = re.compile(r"[A-Za-z]\.?")


def _is_low_quality_name(name: str) -> bool:
    """Flags title-extraction junk: a trailing possessive, a dangling
    single-letter/initial, trailing stray punctuation, or a phrase made
    entirely of generic (non-name) words. This does not try to confirm a
    name IS a real character -- only that the extraction looks broken."""
    words = name.split()
    if not words:
        return False
    last = words[-1]

    if _POSSESSIVE_RE.search(last):
        return True
    if _TRAILING_INITIAL_RE.fullmatch(last):
        return True
    if not last[-1].isalnum():
        return True
    if all(w.lower() in GENERIC_NOUN_WORDS for w in words):
        return True
    return False


def _confidence_for_title_names(names) -> str:
    return "low" if any(_is_low_quality_name(n) for n in names) else "medium"


def split_name_list(text: str):
    """Splits bracket content like 'Mainz, Essex & Ryuuhou' into names."""
    parts = re.split(
        r"\s*,\s*|\s*&\s*|\s+and\s+|\s+x\s+|\s*×\s*", text, flags=re.IGNORECASE
    )
    return [p.strip() for p in parts if p.strip()]


def merge_character_names(canonical: str, title_names):
    """Dedupes title_names against the map's canonical character name, keeping
    canonical spelling first.

    Two rules, because they catch different collisions. Substring matching
    (case-insensitive) handles a partial name -- "Ayaka" inside "Kamisato
    Ayaka". dedupe_names then collapses names that differ only in
    spacing/punctuation, which substring matching cannot see: the map's
    "Hutao" and a title's "Hu Tao" contain neither one another, and used to
    both survive into the same character list."""
    result = [canonical]
    canonical_lower = canonical.lower()
    for name in title_names:
        name_lower = name.lower()
        if canonical_lower in name_lower or name_lower in canonical_lower:
            continue
        if name not in result:
            result.append(name)
    return dedupe_names(result)


def _build_character_index(sub_map: dict) -> dict:
    """Reverse index: character name (lowercase) -> home franchise, built from
    every character-specific entry in subreddit_map.json. This is what lets
    crossover/collab detection know a title-named character's own franchise
    without a separate knowledge base."""
    index = {}
    for entry in sub_map.values():
        character = entry.get("character")
        franchise = entry.get("franchise")
        if character and franchise:
            index[character.lower()] = franchise
    return index


def _lookup_home_franchise(name: str, character_index: dict):
    name_lower = name.lower()
    if name_lower in character_index:
        return character_index[name_lower]
    for key, franchise in character_index.items():
        if key in name_lower or name_lower in key:
            return franchise
    return None


def _apply_group_crossover(names, character_index: dict, fallback_franchise):
    """Resolves each name's home franchise (in title order). 2+ distinct known
    homes -> crossover, franchise = the union in that order. Otherwise falls
    back to whatever franchise context was already known (the subreddit's, or
    None)."""
    resolved = []
    for name in names:
        home = _lookup_home_franchise(name, character_index)
        if home and home not in resolved:
            resolved.append(home)

    if len(resolved) >= 2:
        return resolved, True
    return ([fallback_franchise] if fallback_franchise else []), False


def _apply_collab(name: str, character_index: dict, subreddit_franchise: str):
    """Single title-named character on a franchise-specific subreddit: if the
    character's own (independently known) home franchise differs from the
    subreddit's, it's an official collab -- include both, never a crossover."""
    home = _lookup_home_franchise(name, character_index)
    if home and home != subreddit_franchise:
        return [home, subreddit_franchise]
    return [subreddit_franchise] if subreddit_franchise else []


def _tag_from_metadata(post_metadata: dict, sub_map: dict, character_index: dict, known_franchises: dict):
    """Returns (franchise_list, crossover, Guess)."""
    subreddit = post_metadata.get("subreddit", "")
    title = post_metadata.get("title", "")

    franchise, character = lookup_subreddit(subreddit, sub_map)
    subreddit_franchise = franchise  # captured before any paren/bracket override below

    cleaned, paren_franchise = extract_parenthetical_franchise(title, known_franchises)
    if not franchise and paren_franchise:
        franchise = paren_franchise
    remaining, bracket_kind, bracket_content = extract_bracket(cleaned)

    is_original = bracket_kind == "original"

    if character:
        # Character-specific subreddit: high confidence. Still check for
        # group-shot names, but only when the title actually looks like a
        # list (has a separator) -- otherwise an unrelated capitalized leading
        # word (e.g. "Summer" in "Summer is here !") would get merged in.
        title_names = []
        if bracket_kind == "data":
            title_names = split_name_list(bracket_content)
        elif _has_separator(remaining):
            title_names = extract_leading_names(remaining)
        names = merge_character_names(character, title_names)
        franchise_list, crossover = _apply_group_crossover(title_names, character_index, franchise)
        return franchise_list, crossover, Guess(names, "high", "subreddit")

    # Franchise-only or franchise-unknown subreddit: character comes from title.
    if not is_original and bracket_kind == "data" and not franchise:
        franchise = bracket_content

    if bracket_kind == "data" and franchise and franchise != bracket_content:
        # Franchise already known (from map) -> bracket is a character list.
        names = split_name_list(bracket_content)
    elif is_original:
        names = []
    else:
        names = extract_leading_names(remaining)

    # Same collapse merge_character_names applies on the subreddit path: a
    # title can list one character twice under two spellings, and the group
    # /crossover logic below counts names, so the dedupe has to happen first.
    names = dedupe_names(names)

    if names:
        if len(names) >= 2:
            franchise_list, crossover = _apply_group_crossover(names, character_index, franchise)
        elif subreddit_franchise:
            # franchise came from the subreddit's own map entry -> collab-eligible
            franchise_list = _apply_collab(names[0], character_index, subreddit_franchise)
            crossover = False
        else:
            franchise_list = [franchise] if franchise else []
            crossover = False
        confidence = _confidence_for_title_names(names)
        return franchise_list, crossover, Guess(names, confidence, "title")

    # No character resolved. The list stays EMPTY rather than carrying an
    # "Unknown" placeholder: downstream (folder matching, alias lookup, the
    # review field, the group-vs-single count) a placeholder is indistinguishable
    # from a real name, so it has to be filtered out everywhere or it silently
    # becomes one. An empty list says the same thing and cannot be mistaken for
    # a character. `guess_confidence` still records how much was known.
    franchise_list = [franchise] if franchise else []
    confidence = "low" if franchise else "zero"
    return franchise_list, False, Guess([], confidence, "title")


def guess_franchise_and_character(post_metadata: dict):
    """Returns (franchise_list, crossover, Guess). Used by the tagging driver,
    which needs franchise/crossover alongside the Guess; guess_character()
    below is the stable seam and only returns the Guess."""
    sub_map = _load_subreddit_map()
    character_index = _build_character_index(sub_map)
    known_franchises = _build_known_franchise_index(sub_map, _load_layout())
    return _tag_from_metadata(post_metadata, sub_map, character_index, known_franchises)


def guess_character(image_path, post_metadata) -> Guess:
    _, _, guess = guess_franchise_and_character(post_metadata)
    return guess
