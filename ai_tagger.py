"""Slice 4: WD14 AI fallback character tagger. The only module that imports
imgutils/onnxruntime, per tagger.py's "no torch/imgutils import here or
anywhere else" rule. Mirrors tagger.py's two-tier shape:
guess_franchise_and_character_ai() for the driver (needs franchise too),
guess_character_ai() as the thin seam-shaped wrapper.

Only runs on images the metadata path (tagger.py) left unresolved -- see
ai_tag.py for the selection logic. Never sets crossover or status.
"""
import re

from imgutils.tagging.wd14 import get_wd14_tags

from tagger import Guess

# Provisional thresholds, sanity-checked against real staging/ images during
# Slice 4 Step 1 (confidence on genuine matches came back 0.98-0.99, well
# clear of these bands).
AI_CHARACTER_THRESHOLD_FLOOR = 0.30   # floor passed to get_wd14_tags(); below
                                       # this WD14 itself never returns the tag
AI_CONFIDENCE_HIGH = 0.85             # matches WD14's own default character_threshold
AI_CONFIDENCE_MEDIUM = 0.60
MAX_GROUP_CHARACTERS = 6              # cap on co-occurring "high" tags treated as a group shot

# WD14 character tags are usually "name (franchise_slug)" (e.g.
# "yoimiya (genshin impact)") but well-known names sometimes come back bare,
# e.g. "yae miko", "raiden shogun" -- confirmed on real staging images.
_TAG_FRANCHISE_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")


def _split_tag(tag: str):
    """Returns (name, slug_or_None)."""
    match = _TAG_FRANCHISE_RE.match(tag)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return tag.strip(), None


def _normalize_for_match(s: str) -> str:
    return _PUNCT_RE.sub("", s.lower()).strip()


def _franchise_matches_hint(slug: str, hints: list) -> bool:
    """Same fuzzy substring-containment style as tagger.py's
    _lookup_home_franchise, but normalized first: WD14 slugs never contain
    punctuation, while manifest franchise strings sometimes do
    ("NieR: Automata", "Re:Zero"), so a raw substring check would falsely
    reject correct matches."""
    norm_slug = _normalize_for_match(slug)
    for hint in hints:
        norm_hint = _normalize_for_match(hint)
        if norm_hint and (norm_hint in norm_slug or norm_slug in norm_hint):
            return True
    return False


def _humanize_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split(" ") if w)


def guess_franchise_and_character_ai(image_path, franchise_hint=None):
    """Returns (franchise_list, Guess). No crossover element -- AI never sets
    crossover; that stays exactly as metadata/human left it."""
    franchise_hint = list(franchise_hint or [])
    _, _, character_tags = get_wd14_tags(
        str(image_path), character_threshold=AI_CHARACTER_THRESHOLD_FLOOR, no_underline=True,
    )

    candidates = [(*_split_tag(tag), score) for tag, score in character_tags.items()]

    if franchise_hint:
        # A tag with no franchise slug can't confirm OR contradict the hint --
        # keep it. Only reject tags whose slug names a different franchise
        # (a spurious cross-franchise tag).
        candidates = [
            c for c in candidates
            if c[1] is None or _franchise_matches_hint(c[1], franchise_hint)
        ]

    if not candidates:
        return franchise_hint, Guess(["Unknown"], "zero", "ai")

    candidates.sort(key=lambda c: c[2], reverse=True)
    top_score = candidates[0][2]

    if top_score >= AI_CONFIDENCE_HIGH:
        chosen = [c for c in candidates if c[2] >= AI_CONFIDENCE_HIGH][:MAX_GROUP_CHARACTERS]
        confidence = "high"
    elif top_score >= AI_CONFIDENCE_MEDIUM:
        chosen = candidates[:1]
        confidence = "medium"
    else:
        chosen = candidates[:1]
        confidence = "low"

    names = [c[0].title() for c in chosen]

    if franchise_hint:
        franchise_list = franchise_hint  # unchanged -- metadata already knew it
    else:
        franchise_list = []
        for c in chosen:
            if not c[1]:
                continue  # bare tag, no slug to infer franchise from
            human = _humanize_slug(c[1])
            if human not in franchise_list:
                franchise_list.append(human)

    return franchise_list, Guess(names, confidence, "ai")


def guess_character_ai(image_path, franchise_hint=None) -> Guess:
    """Thin wrapper matching the guess_character(image_path, ...) seam shape."""
    _, guess = guess_franchise_and_character_ai(image_path, franchise_hint)
    return guess
