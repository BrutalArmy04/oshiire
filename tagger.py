"""Slice 1: metadata-based character tagging.

Stable seam: guess_character(image_path, post_metadata) -> Guess. Until Slice 4
this only tries the metadata path (subreddit_map.json + title parsing); no
torch/imgutils import here or anywhere else.
"""
import json
import re
from collections import namedtuple
from pathlib import Path

Guess = namedtuple("Guess", ["name", "confidence", "source"])

SUBREDDIT_MAP_PATH = Path("subreddit_map.json")

STOPWORDS = {
    "the", "a", "an", "this", "that", "these", "those", "when", "while",
    "what", "who", "why", "how", "too", "so", "very", "all", "here", "there",
    "you", "your", "its", "their", "them", "for", "but", "or", "not", "is",
    "are", "was", "were", "with", "without", "from", "of", "in", "on", "at",
    "by",
}

PAREN_RE = re.compile(r"\s*\([^()]*\)")
BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
ID_MARKER_RE = re.compile(r"^\s*(i\s*:\s*\d+|pixiv\s*\d+)\s*$", re.IGNORECASE)
META_TAGS = {"media", "discussion"}

TOKEN_RE = re.compile(r"&|,|\band\b|[A-Za-z][A-Za-z'\-]*|[-:|—]", re.IGNORECASE)
SEPARATOR_TOKENS = {"&", ",", "and"}
DIVIDER_TOKENS = {"-", ":", "|", "—"}


def _load_subreddit_map(path: Path = SUBREDDIT_MAP_PATH) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {k.lower(): v for k, v in data.items() if not k.startswith("_")}


def normalize_subreddit(subreddit: str) -> str:
    return subreddit.strip().lower()


def lookup_subreddit(subreddit: str, sub_map: dict):
    """Returns (franchise, character) or (None, None)/(None, name) for fallbacks."""
    key = normalize_subreddit(subreddit)
    if key in sub_map:
        entry = sub_map[key]
        return entry.get("franchise"), entry.get("character")

    if key.endswith("mains"):
        return None, key[: -len("mains")].capitalize()
    if key.startswith("churchof"):
        return None, key[len("churchof"):].capitalize()
    if key.startswith("onetrue"):
        return None, key[len("onetrue"):].capitalize()

    return None, None


def strip_artist_credit(title: str) -> str:
    return PAREN_RE.sub("", title).strip()


def extract_bracket(title: str):
    """Returns (remaining_title, bracket_kind, bracket_content) using only the
    first bracket group found. bracket_kind is one of:
    "meta", "original", "id", "data", or None (no usable bracket)."""
    match = BRACKET_RE.search(title)
    if not match:
        return title, None, None

    content = match.group(1).strip()
    remaining = (title[: match.start()] + " " + title[match.end():]).strip()

    lowered = content.lower()
    if lowered in META_TAGS:
        return remaining, "meta", content
    if lowered == "original":
        return remaining, "original", content
    if ID_MARKER_RE.match(content):
        return remaining, "id", content
    return remaining, "data", content


def _clean_segment(text: str) -> str:
    return text.strip()


def extract_leading_names(text: str):
    """Scans from the start of text, accumulating consecutive capitalized
    words into name segments; &/,/and start a new segment. Stops at the first
    lowercase word, stopword, divider punctuation, or end of string."""
    tokens = TOKEN_RE.findall(text)
    segments = []
    current = []

    for token in tokens:
        lowered = token.lower()
        if lowered in SEPARATOR_TOKENS:
            if current:
                segments.append(" ".join(current))
                current = []
            continue
        if token in DIVIDER_TOKENS:
            break
        if lowered in STOPWORDS:
            break
        if not token[:1].isupper():
            break
        current.append(token)

    if current:
        segments.append(" ".join(current))

    return [_clean_segment(s) for s in segments if _clean_segment(s)]


SEPARATOR_PRESENCE_RE = re.compile(r"&|,|\band\b", re.IGNORECASE)


def _has_separator(text: str) -> bool:
    return bool(SEPARATOR_PRESENCE_RE.search(text))


def split_name_list(text: str):
    """Splits bracket content like 'Mainz, Essex & Ryuuhou' into names."""
    parts = re.split(r"\s*,\s*|\s*&\s*|\s+and\s+", text, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def merge_character_names(canonical: str, title_names):
    """Dedupes title_names against the map's canonical character name
    (substring match, case-insensitive), keeping canonical spelling first."""
    result = [canonical]
    canonical_lower = canonical.lower()
    for name in title_names:
        name_lower = name.lower()
        if canonical_lower in name_lower or name_lower in canonical_lower:
            continue
        if name not in result:
            result.append(name)
    return result


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


def _tag_from_metadata(post_metadata: dict, sub_map: dict, character_index: dict):
    """Returns (franchise_list, crossover, Guess)."""
    subreddit = post_metadata.get("subreddit", "")
    title = post_metadata.get("title", "")

    franchise, character = lookup_subreddit(subreddit, sub_map)
    subreddit_franchise = franchise  # captured before any bracket override below

    cleaned = strip_artist_credit(title)
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
        return franchise_list, crossover, Guess(names, "medium", "title")

    franchise_list = [franchise] if franchise else []
    confidence = "low" if franchise else "zero"
    return franchise_list, False, Guess(["Unknown"], confidence, "title")


def guess_franchise_and_character(post_metadata: dict):
    """Returns (franchise_list, crossover, Guess). Used by the tagging driver,
    which needs franchise/crossover alongside the Guess; guess_character()
    below is the stable seam and only returns the Guess."""
    sub_map = _load_subreddit_map()
    character_index = _build_character_index(sub_map)
    return _tag_from_metadata(post_metadata, sub_map, character_index)


def guess_character(image_path, post_metadata) -> Guess:
    _, _, guess = guess_franchise_and_character(post_metadata)
    return guess
