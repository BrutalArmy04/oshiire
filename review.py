"""Slice 2: Gradio review UI. Reads manifest.json fresh on launch, presents
pending_review entries one at a time in chronological order for human
approve/edit/reject. Makes no network calls and does no downloading; only
reads staging images, reads/writes the manifest, and (on Reject) deletes a
staging file. Reuses manifest.py's load/save -- no reimplementation.
"""
import re
import warnings
from pathlib import Path

import gradio as gr
from starlette.exceptions import StarletteDeprecationWarning

from manifest import load_manifest, save_manifest
from tagger import remove_subreddit_map_entry, save_subreddit_map_entry, subreddit_is_mapped

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)

# Mirrors archive.py's ORIGINAL_RE/is_original -- duplicated rather than
# imported since review.py (Slice 2) shouldn't depend on archive.py (Slice 3a).
_OC_TITLE_RE = re.compile(r"\[\s*original\s*\]", re.IGNORECASE)

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
pending_accept = None  # None, or the parsed values awaiting a map_prompt choice


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
            gr.update(visible=False),
            "",
            gr.update(choices=[], value=None),
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
    is_oc = not entry.get("franchise") and bool(_OC_TITLE_RE.search(entry.get("title", "")))
    if is_oc:
        meta_md += "\n\n**Original character (OC)** — will file to `Others/Artist's Original`."
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
        gr.update(visible=False),
        "",
        gr.update(choices=[], value=None),
    )


def on_skip():
    global current_index, last_action, pending_accept
    pending_accept = None
    entry = _current_entry()
    if entry is not None:
        last_action = {"type": "skip", "index": current_index}
        current_index += 1
    return _render_current()


def on_reject():
    global current_index, last_action, pending_accept
    pending_accept = None
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


def _finalize_accept(entry, parsed, map_write):
    global current_index, last_action
    if parsed["edited"]:
        entry["guess_source"] = "manual"
    entry["character_guess"] = parsed["characters"]
    entry["franchise"] = parsed["franchise"]
    entry["crossover"] = parsed["crossover"]
    entry["same_series_group"] = parsed["same_series_group"]
    entry["wallpaper"] = parsed["wallpaper"]
    entry["status"] = "approved"
    save_manifest(manifest)

    last_action = {
        "type": "accept",
        "index": current_index,
        "post_id": queue[current_index],
        "prior": parsed["prior"],
        "map_write": map_write,
    }
    current_index += 1


def on_accept(character_text, franchise_text, crossover_value, same_series_group_value, wallpaper_value):
    global pending_accept
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

    parsed = {
        "prior": prior,
        "characters": new_characters,
        "franchise": new_franchise,
        "crossover": new_crossover,
        "same_series_group": new_same_series_group,
        "wallpaper": new_wallpaper,
        "edited": edited,
    }

    subreddit = entry.get("subreddit", "")
    single_franchise = len(new_franchise) == 1
    eligible = not subreddit_is_mapped(subreddit) and single_franchise

    if not eligible:
        _finalize_accept(entry, parsed, map_write=None)
        return _render_current()

    single_character = len(new_characters) == 1 and new_characters[0] != "Unknown"
    franchise = new_franchise[0]
    character = new_characters[0] if single_character else None
    allow_both = single_character and not new_crossover and not new_same_series_group

    pending_accept = {"entry": entry, "parsed": parsed, "subreddit": subreddit,
                       "franchise": franchise, "character": character}

    choices = []
    if allow_both:
        choices.append((f'Always map r/{subreddit} to franchise "{franchise}" and character "{character}"', "both"))
    choices.append((f'Always map r/{subreddit} to franchise "{franchise}" only', "franchise_only"))
    choices.append(("Just this once (don't remember)", "once"))

    # Keep the textboxes showing what the user just typed (not the stale
    # pre-edit entry values _render_current() would otherwise re-render).
    base = list(_render_current())
    base[5] = "\n".join(new_characters)
    base[6] = "\n".join(new_franchise)
    base[7] = new_crossover
    base[8] = new_same_series_group
    base[9] = new_wallpaper
    base[11] = gr.update(visible=True)
    base[12] = f"r/{subreddit} isn't mapped yet. Remember this for future posts?"
    base[13] = gr.update(choices=choices, value="once")
    return tuple(base)


def on_map_prompt_confirm(choice):
    global pending_accept
    pending = pending_accept
    pending_accept = None
    if pending is None:
        return _render_current()

    map_write = None
    if choice == "both":
        save_subreddit_map_entry(pending["subreddit"], pending["franchise"], pending["character"])
        map_write = pending["subreddit"]
    elif choice == "franchise_only":
        save_subreddit_map_entry(pending["subreddit"], pending["franchise"])
        map_write = pending["subreddit"]

    _finalize_accept(pending["entry"], pending["parsed"], map_write=map_write)
    return _render_current()


def on_undo():
    global current_index, last_action, pending_accept
    pending_accept = None
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
        if action.get("map_write"):
            remove_subreddit_map_entry(action["map_write"])
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

                with gr.Group(visible=False) as map_prompt_group:
                    map_prompt_md = gr.Markdown()
                    map_prompt_radio = gr.Radio(choices=[], value=None, label="Remember this subreddit?")
                    map_prompt_confirm_btn = gr.Button("Confirm")

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
        map_prompt_group,
        map_prompt_md,
        map_prompt_radio,
    ]

    demo.load(fn=_render_current, outputs=outputs)
    skip_btn.click(fn=on_skip, outputs=outputs)
    reject_btn.click(fn=on_reject, outputs=outputs)
    accept_btn.click(
        fn=on_accept,
        inputs=[character_box, franchise_box, crossover_box, same_series_group_box, wallpaper_box],
        outputs=outputs,
    )
    map_prompt_confirm_btn.click(fn=on_map_prompt_confirm, inputs=[map_prompt_radio], outputs=outputs)
    undo_btn.click(fn=on_undo, outputs=outputs)


if __name__ == "__main__":
    demo.launch(css=CUSTOM_CSS, inbrowser=True)
