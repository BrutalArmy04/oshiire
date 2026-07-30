"""Read-only history browser: recently processed manifest entries, newest
first, so a past decision can be looked up.

Strictly a VIEWER. It never writes the manifest, never moves or deletes a
file, and offers no edit controls -- reverting the most recent action is
review.py's Undo, and anything older is a deliberate manual fix. Being a
separate process from review.py (like resolve.py) is what makes that
guarantee structural rather than a matter of discipline.

Shows thumbnail, title, status and -- for archived entries -- the destination
the file was filed to. Archived thumbnails are read from ARCHIVE_DIR, since
their staging copy no longer exists.
"""
import os
import warnings
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv
from starlette.exceptions import StarletteDeprecationWarning

from manifest import load_manifest

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)

PAGE_SIZE = 24

STATUS_CHOICES = ["archived", "approved", "rejected", "pending_review", "skipped", "download_failed"]

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

    items = []
    missing = 0
    for post_id, entry in window:
        thumb = _thumbnail_path(entry)
        if thumb is None:
            # Rejected entries (file deleted) and not-yet-synced archive paths
            # legitimately have no viewable image; count them so the absence
            # is explained rather than looking like a bug.
            missing += 1
            continue
        items.append((thumb, _caption(post_id, entry)))

    summary = f"**{total:,}** entr{'y' if total == 1 else 'ies'} match — page {page} of {pages}"
    if missing:
        summary += f" · {missing} on this page have no viewable file (rejected or moved)"
    if not archive_dir:
        summary += "\n\n*ARCHIVE_DIR is not set, so archived thumbnails can't be shown.*"
    return items, summary, page


def on_filter(statuses):
    items, summary, page = _load_page(statuses, 1)
    return items, summary, page


def on_page(statuses, page, delta):
    items, summary, page = _load_page(statuses, (page or 1) + delta)
    return items, summary, page


with gr.Blocks(title="Oshiire history") as demo:
    gr.Markdown("## History — read-only\nRecently processed entries, newest first. Nothing here can be edited.")

    status_filter = gr.CheckboxGroup(
        choices=STATUS_CHOICES, value=["archived"], label="Filter by status (none = all)"
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

    outputs = [gallery, summary_md, page_state]

    demo.load(fn=on_filter, inputs=[status_filter], outputs=outputs)
    status_filter.change(fn=on_filter, inputs=[status_filter], outputs=outputs)
    prev_btn.click(
        fn=lambda s, p: on_page(s, p, -1), inputs=[status_filter, page_state], outputs=outputs
    )
    next_btn.click(
        fn=lambda s, p: on_page(s, p, +1), inputs=[status_filter, page_state], outputs=outputs
    )


if __name__ == "__main__":
    # Archived thumbnails are read from ARCHIVE_DIR, outside the project tree;
    # Gradio serves only cwd/temp by default and raises InvalidPathError
    # otherwise. Read-only -- this UI never writes there.
    allowed_paths = [str(archive_dir)] if archive_dir else []
    demo.launch(css=CUSTOM_CSS, inbrowser=True, allowed_paths=allowed_paths)
