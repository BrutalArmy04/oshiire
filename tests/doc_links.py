"""Test helper: the links in the Markdown docs, and the anchors GitHub gives
their headings -- so a test can check every relative link and #anchor resolves.

Not a test module itself (the name doesn't match test*.py).
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
_HTML_SRC = re.compile(r"""<img[^>]+src=["']([^"']+)["']""")
_FENCE = re.compile(r"^(```|~~~)")


def _prose_lines(text):
    """The lines outside fenced code blocks."""
    inside = False
    for line in text.splitlines():
        if _FENCE.match(line.strip()):
            inside = not inside
            continue
        if not inside:
            yield line


def github_slug(heading):
    """GitHub's anchor for a heading: lowercase, drop everything but letters,
    digits, spaces, hyphens and underscores, then each space becomes a hyphen
    (so "A / B" becomes "a--b")."""
    text = re.sub(r"[`*]", "", heading.strip())  # code and emphasis markers
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path):
    """Every heading anchor in a Markdown file (duplicates get -1, -2, ...)."""
    seen, out = {}, set()
    for line in _prose_lines(Path(path).read_text(encoding="utf-8")):
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        slug = github_slug(match.group(1))
        n = seen.get(slug, 0)
        out.add(slug if n == 0 else f"{slug}-{n}")
        seen[slug] = n + 1
    return out


def links(path):
    """(kind, target) for every Markdown link, image and <img src> outside code
    blocks; kind is "link" or "image"."""
    found = []
    for line in _prose_lines(Path(path).read_text(encoding="utf-8")):
        found += [("link", m) for m in _LINK.findall(line)]
        found += [("image", m) for m in _IMAGE.findall(line)]
        found += [("image", m) for m in _HTML_SRC.findall(line)]
    return found


def broken_relative_links(path):
    """Relative links/anchors in `path` that don't resolve: a missing file, or
    an #anchor no heading in the target file has. Absolute URLs are skipped."""
    path = Path(path)
    problems = []
    for kind, target in links(path):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
            continue  # https:, mailto: ...
        file_part, _, anchor = target.partition("#")
        dest = (path.parent / file_part).resolve() if file_part else path
        if not dest.exists():
            problems.append(f"{target}: no such file")
            continue
        if anchor and dest.suffix.lower() == ".md" and anchor not in anchors(dest):
            problems.append(f"{target}: no heading with that anchor")
    return problems
