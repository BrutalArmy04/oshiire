"""Read-only history browser: recently processed manifest entries, newest
first, so a past decision can be looked up. It is a PAGE inside the app now
(review.py imports build_ui and embeds it), and also still runs standalone via
`python history.py`.

Strictly a VIEWER. It never writes the manifest, never moves or deletes a
file, and offers no edit controls -- reverting the most recent action is
review.py's Undo, and anything older is a deliberate manual fix. The read-only
guarantee is now structural rather than process-based: this module never
imports save_manifest, save_layout, or any other writer, so there is simply no
code path here that can mutate anything. Every view re-reads the manifest from
disk, so it always shows the current state.

Shows thumbnail, title, status and -- for archived entries -- the destination
the file was filed to. Archived thumbnails are read from ARCHIVE_DIR, since
their staging copy no longer exists.
"""
import hashlib
import os
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv
from PIL import Image, ImageOps
from starlette.exceptions import StarletteDeprecationWarning

from manifest import load_manifest

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)

PAGE_SIZE = 24

# Cached, downscaled JPEGs of the gallery images. The archive lives on a synced
# drive (Google Drive), so sending gradio the full-res originals meant a page
# read-and-uploaded tens of megabytes every time; a 512px thumbnail is a few KB.
# Keyed on the source path + size + mtime, so an edited or replaced file misses
# the cache and regenerates. Gitignored (derived from personal images).
THUMBS_DIR = Path("data/thumbs")
# Long edge of a cached thumbnail, and the JPEG quality it is written at.
THUMB_EDGE = 512
THUMB_QUALITY = 82

STATUS_CHOICES = ["archived", "approved", "rejected", "pending_review", "skipped", "download_failed"]
DEFAULT_STATUSES = ["archived"]

CUSTOM_CSS = """
#history-gallery .thumbnail-item img {
    object-fit: contain;
}
"""

manifest = load_manifest()
load_dotenv()
_archive_dir_value = os.environ.get("ARCHIVE_DIR")
archive_dir = Path(_archive_dir_value) if _archive_dir_value else None


def _thumbnail_path(entry):
    """Where this entry's image can be read from now, or None if nowhere.

    Order matters: an archived entry's staging path still exists as a stale
    string in the manifest, but the file was MOVED, so the archive location is
    the only valid one.

    ARCHIVE_DIR sits on a synced/removable drive, so .exists() there can raise
    rather than simply return False; a thumbnail is never worth losing the page
    over, so any error just means "nowhere".
    """
    archive_path = entry.get("archive_path")
    if archive_path and archive_dir:
        try:
            candidate = archive_dir / archive_path
            if candidate.exists():
                return str(candidate)
        except OSError:
            return None
    local_path = entry.get("local_path")
    try:
        if local_path and Path(local_path).exists():
            return str(Path(local_path).resolve())
    except OSError:
        return None
    return None


def _thumbnail_for(src: Path) -> str:
    """A cached 512px JPEG for `src`, or `str(src)` itself if one can't be made.

    The cache key is sha1 of ``"<src>|<size>|<mtime_ns>"``, so an edited or
    replaced file (different size or mtime) lands on a new key and regenerates;
    a hit returns the cached path WITHOUT opening the source at all. Thumbnails
    are a pure optimisation -- on ANY failure (a dropped drive, a truncated or
    unidentifiable file, a decompression bomb, ...) this returns the original
    path so the page still shows the image, and it never raises."""
    src = Path(src)
    tmp = None
    try:
        st = src.stat()
        key = hashlib.sha1(
            f"{src}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8")
        ).hexdigest()
        dest = THUMBS_DIR / f"{key}.jpg"
        if dest.exists():
            return str(dest)

        THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as img:
            # draft only affects JPEGs (a no-op otherwise) and must precede the
            # first decode: it lets libjpeg decode a big source at ~1/N scale
            # straight to roughly thumbnail size instead of fully then shrinking.
            img.draft("RGB", (THUMB_EDGE * 2, THUMB_EDGE * 2))
            # Animated files (GIF/APNG/WEBP) open on frame 0 already; that first
            # frame is the thumbnail. EXIF orientation is baked in before sizing.
            img = ImageOps.exif_transpose(img)
            img.thumbnail((THUMB_EDGE, THUMB_EDGE))
            if img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info
            ):
                # Flatten transparency onto white, so a JPEG (which has no alpha)
                # doesn't render the transparent regions as black.
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
                img = flat
            else:
                img = img.convert("RGB")
            # Atomic write: a reader (this very page, or a parallel worker) never
            # sees a half-written JPEG -- it sees the old file or the new one.
            tmp = dest.with_name(f"{key}.{os.getpid()}.tmp")
            img.save(tmp, "JPEG", quality=THUMB_QUALITY)
        os.replace(tmp, dest)
        return str(dest)
    except Exception:
        # Clean up a partial temp so repeated failures can't litter data/thumbs.
        if tmp is not None:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass
        return str(src)


def _sort_key(item):
    """Newest first, preferring the time the entry reached its current state
    (archived_at) and falling back to when it was fetched."""
    _post_id, entry = item
    return entry.get("archived_at") or entry.get("fetched_at") or ""


def _caption(post_id, entry):
    status = entry.get("status", "?")
    title = entry.get("title") or post_id
    if len(title) > 70:
        title = title[:69] + "…"
    parts = [title, f"[{status}]"]
    destination = entry.get("archive_path")
    if destination:
        parts.append(f"→ {Path(destination).parent.as_posix()}/")
    return "  ".join(parts)


def _load_page(statuses, page):
    page = max(1, int(page or 1))
    selected = set(statuses or [])
    rows = [
        (post_id, entry)
        for post_id, entry in manifest.items()
        if not selected or entry.get("status") in selected
    ]
    rows.sort(key=_sort_key, reverse=True)

    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    start = (page - 1) * PAGE_SIZE
    window = rows[start:start + PAGE_SIZE]

    # Resolve each entry to a real file path first (cheap stat/exists), then
    # build the thumbnails for the whole page at once.
    resolved = []
    missing = 0
    for post_id, entry in window:
        source = _thumbnail_path(entry)
        if source is None:
            # Rejected entries (file deleted) and not-yet-synced archive paths
            # legitimately have no viewable image; count them so the absence
            # is explained rather than looking like a bug.
            missing += 1
            continue
        resolved.append((source, _caption(post_id, entry)))

    # Thumbnailing reads (and on a miss, decodes) each file; the archive reads
    # hit a synced drive, so overlapping four at a time hides that latency.
    # pool.map preserves order, so the gallery stays in as-saved order.
    items = []
    if resolved:
        with ThreadPoolExecutor(max_workers=4) as pool:
            thumbs = pool.map(_thumbnail_for, [source for source, _ in resolved])
        items = [(thumb, caption) for thumb, (_, caption) in zip(thumbs, resolved)]

    summary = f"**{total:,}** entr{'y' if total == 1 else 'ies'} match — page {page} of {pages}"
    if missing:
        summary += f" · {missing} on this page have no viewable file (rejected or moved)"
    if not archive_dir:
        summary += "\n\n*ARCHIVE_DIR is not set, so archived thumbnails can't be shown.*"
    return items, summary, page


def _refresh_manifest():
    """Re-read the manifest from disk IN PLACE, so every view reflects the
    current state rather than an import-time snapshot. Read-only: this is a
    load, never a write."""
    manifest.clear()
    manifest.update(load_manifest())


def on_open():
    """Open function the host chains on the History menu click (and standalone
    demo.load). Re-reads the manifest and resets to the default filter, page 1,
    so returning to History always shows current state with a matching filter
    control. Zero-input by design -- the host wires it with outputs only."""
    _refresh_manifest()
    items, summary, page = _load_page(DEFAULT_STATUSES, 1)
    return items, summary, page, gr.update(value=DEFAULT_STATUSES)


def on_filter(statuses):
    _refresh_manifest()
    items, summary, page = _load_page(statuses, 1)
    return items, summary, page


def on_page(statuses, page, delta):
    items, summary, page = _load_page(statuses, (page or 1) + delta)
    return items, summary, page


def build_ui(show_title=True):
    """Build History's components and event wiring into the CURRENT Blocks
    context (review.py calls this inside its own gr.Blocks; standalone __main__
    below calls it inside its own). Returns (open_outputs, open_fn): the host
    chains open_fn on the History menu click, and standalone chains it via
    demo.load. No demo.load here -- the open trigger belongs to the Blocks owner.

    show_title=True (standalone) keeps the "## History — read-only" heading;
    pass False when History is a page under the app's own header, so the heading
    isn't shown twice. The second line stays either way."""
    if show_title:
        gr.Markdown("## History — read-only\nRecently processed entries, newest first. Nothing here can be edited.")
    else:
        gr.Markdown("Recently processed entries, newest first. Nothing here can be edited.")

    status_filter = gr.CheckboxGroup(
        choices=STATUS_CHOICES, value=DEFAULT_STATUSES, label="Filter by status (none = all)"
    )
    summary_md = gr.Markdown()
    page_state = gr.State(1)

    gallery = gr.Gallery(
        label=None, show_label=False, columns=4, height=760,
        object_fit="contain", elem_id="history-gallery",
    )

    with gr.Row():
        prev_btn = gr.Button("← Newer")
        next_btn = gr.Button("Older →")

    # The open function also resets the filter control, so it carries one more
    # output than the filter/paginate handlers (which leave the control alone).
    filter_outputs = [gallery, summary_md, page_state]
    open_outputs = [gallery, summary_md, page_state, status_filter]

    status_filter.change(fn=on_filter, inputs=[status_filter], outputs=filter_outputs)
    prev_btn.click(
        fn=lambda s, p: on_page(s, p, -1), inputs=[status_filter, page_state], outputs=filter_outputs
    )
    next_btn.click(
        fn=lambda s, p: on_page(s, p, +1), inputs=[status_filter, page_state], outputs=filter_outputs
    )

    return open_outputs, on_open


if __name__ == "__main__":
    with gr.Blocks(title="Oshiire history") as demo:
        _outputs, _open = build_ui()
        demo.load(fn=_open, outputs=_outputs)
    # Archived thumbnails are read from ARCHIVE_DIR, outside the project tree;
    # Gradio serves only cwd/temp by default and raises InvalidPathError
    # otherwise. Read-only -- this UI never writes there.
    allowed_paths = [str(archive_dir)] if archive_dir else []
    demo.launch(css=CUSTOM_CSS, inbrowser=True, allowed_paths=allowed_paths)
