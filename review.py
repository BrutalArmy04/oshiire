"""Slice 2: Gradio review UI. Reads manifest.json fresh on launch, presents
pending_review entries one at a time in chronological order for human
approve/edit/reject. Makes no network calls and does no downloading; only
reads staging images, reads/writes the manifest, and (on Reject) deletes a
staging file. Reuses manifest.py's load/save -- no reimplementation.
"""
import warnings
from pathlib import Path

import gradio as gr
from starlette.exceptions import StarletteDeprecationWarning

from manifest import load_manifest, save_manifest

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)

CUSTOM_CSS = """
#review-image img {
    max-height: 70vh;
    width: auto;
    object-fit: contain;
    margin: 0 auto;
}
"""

manifest = load_manifest()
queue = sorted(
    (post_id for post_id, entry in manifest.items() if entry.get("status") == "pending_review"),
    key=lambda post_id: manifest[post_id].get("fetched_at", ""),
)
current_index = 0
last_action = None  # None, or a dict describing the most recent Skip/Reject/Accept


def _current_entry():
    if current_index < len(queue):
        return manifest[queue[current_index]]
    return None


def _snapshot(entry):
    return {
        "status": entry.get("status"),
        "character_guess": list(entry.get("character_guess", [])),
        "franchise": list(entry.get("franchise", [])),
        "crossover": entry.get("crossover", False),
        "same_series_group": entry.get("same_series_group", False),
        "wallpaper": entry.get("wallpaper", "none"),
        "guess_source": entry.get("guess_source"),
    }


def _parse_lines(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


def _render_current():
    entry = _current_entry()
    if entry is None:
        return (
            "All reviewed — nothing left in the queue.",
            gr.update(visible=False),
            None,
            "",
            "",
            "",
            "",
            False,
            False,
            "none",
            gr.update(interactive=last_action is not None),
        )

    header = f"Reviewing {current_index + 1} of {len(queue)}"
    title_md = f"### {entry.get('title', '')}"
    local_path = entry.get("local_path")
    image_path = str(Path(local_path).resolve()) if local_path else None
    permalink = entry.get("permalink", "")
    meta_md = (
        f"**Subreddit:** r/{entry.get('subreddit', '')}  \n"
        f"**Link:** [{permalink}]({permalink})  \n"
        f"**Guess confidence:** {entry.get('guess_confidence')} · "
        f"**source:** {entry.get('guess_source')}"
    )
    character_text = "\n".join(entry.get("character_guess", []))
    franchise_text = "\n".join(entry.get("franchise", []))
    crossover_value = bool(entry.get("crossover", False))
    same_series_group_value = bool(entry.get("same_series_group", False))
    wallpaper_value = entry.get("wallpaper", "none")

    return (
        header,
        gr.update(visible=True),
        image_path,
        title_md,
        meta_md,
        character_text,
        franchise_text,
        crossover_value,
        same_series_group_value,
        wallpaper_value,
        gr.update(interactive=last_action is not None),
    )


def on_skip():
    global current_index, last_action
    entry = _current_entry()
    if entry is not None:
        last_action = {"type": "skip", "index": current_index}
        current_index += 1
    return _render_current()


def on_reject():
    global current_index, last_action
    entry = _current_entry()
    if entry is None:
        return _render_current()

    prior = _snapshot(entry)
    local_path = Path(entry["local_path"])
    file_bytes = local_path.read_bytes() if local_path.exists() else None

    entry["status"] = "rejected"
    save_manifest(manifest)
    if local_path.exists():
        local_path.unlink()

    last_action = {
        "type": "reject",
        "index": current_index,
        "post_id": queue[current_index],
        "prior": prior,
        "file_bytes": file_bytes,
        "local_path": str(local_path),
    }
    current_index += 1
    return _render_current()


def on_accept(character_text, franchise_text, crossover_value, same_series_group_value, wallpaper_value):
    global current_index, last_action
    entry = _current_entry()
    if entry is None:
        return _render_current()

    prior = _snapshot(entry)
    new_characters = _parse_lines(character_text)
    new_franchise = _parse_lines(franchise_text)
    new_crossover = bool(crossover_value)
    new_same_series_group = bool(same_series_group_value)
    new_wallpaper = wallpaper_value or "none"

    # same_series_group/wallpaper are routing hints, not identity edits --
    # they don't flip guess_source to "manual".
    edited = (
        new_characters != prior["character_guess"]
        or new_franchise != prior["franchise"]
        or new_crossover != prior["crossover"]
    )

    entry["character_guess"] = new_characters
    entry["franchise"] = new_franchise
    entry["crossover"] = new_crossover
    entry["same_series_group"] = new_same_series_group
    entry["wallpaper"] = new_wallpaper
    if edited:
        entry["guess_source"] = "manual"
    entry["status"] = "approved"
    save_manifest(manifest)

    last_action = {
        "type": "accept",
        "index": current_index,
        "post_id": queue[current_index],
        "prior": prior,
    }
    current_index += 1
    return _render_current()


def on_undo():
    global current_index, last_action
    if last_action is None:
        return _render_current()

    action = last_action
    last_action = None

    if action["type"] == "skip":
        current_index = action["index"]
    elif action["type"] == "reject":
        entry = manifest[action["post_id"]]
        entry["status"] = action["prior"]["status"]
        save_manifest(manifest)
        if action["file_bytes"] is not None:
            Path(action["local_path"]).write_bytes(action["file_bytes"])
        current_index = action["index"]
    elif action["type"] == "accept":
        entry = manifest[action["post_id"]]
        entry.update(action["prior"])
        save_manifest(manifest)
        current_index = action["index"]

    return _render_current()


with gr.Blocks(title="Oshiire review") as demo:
    header_md = gr.Markdown()

    with gr.Group() as review_group:
        with gr.Row(equal_height=False):
            with gr.Column(scale=1):
                image = gr.Image(interactive=False, show_label=False, elem_id="review-image")
            with gr.Column(scale=1):
                title_md = gr.Markdown()
                meta_md = gr.Markdown()
                character_box = gr.Textbox(label="Character guess (one per line)", lines=3)
                franchise_box = gr.Textbox(label="Franchise (one per line)", lines=2)
                crossover_box = gr.Checkbox(label="Crossover")
                same_series_group_box = gr.Checkbox(label="Same-series group")
                wallpaper_box = gr.Radio(
                    choices=["none", "pc", "phone", "both"], value="none", label="Wallpaper"
                )

                with gr.Row():
                    skip_btn = gr.Button("Skip")
                    reject_btn = gr.Button("Reject", variant="stop")
                    accept_btn = gr.Button("Accept", variant="primary")

                undo_btn = gr.Button("Undo last action")

    outputs = [
        header_md,
        review_group,
        image,
        title_md,
        meta_md,
        character_box,
        franchise_box,
        crossover_box,
        same_series_group_box,
        wallpaper_box,
        undo_btn,
    ]

    demo.load(fn=_render_current, outputs=outputs)
    skip_btn.click(fn=on_skip, outputs=outputs)
    reject_btn.click(fn=on_reject, outputs=outputs)
    accept_btn.click(
        fn=on_accept,
        inputs=[character_box, franchise_box, crossover_box, same_series_group_box, wallpaper_box],
        outputs=outputs,
    )
    undo_btn.click(fn=on_undo, outputs=outputs)


if __name__ == "__main__":
    demo.launch(css=CUSTOM_CSS)
