"""Slice 3b pass 2: flag-resolution UI. Presents each flagged (approved but
unroutable) manifest entry one at a time and offers the resolution scoped to
why archive.py flagged it -- mapping/creating a franchise folder, mapping/
creating a character subfolder, or proposing a shortname. Writes go only to
layout.json, the shortname file, or a per-image manifest field; this never
moves files or touches staging -- applying the now-resolved routing is still
`python archive.py --apply`. Does not modify review.py.

The flagged queue is rebuilt from a fresh plan_moves() call after every
resolution (not by filtering the manifest for a stale archive_flag field),
since plan_moves is pure/cheap and is correct whether or not --apply has ever
run. This also means resolving one root cause (e.g. a franchise alias) clears
every other flagged entry that shared it, automatically, on the next render.
"""
import copy
import os
import re
import warnings
from pathlib import Path

import gradio as gr
from starlette.exceptions import StarletteDeprecationWarning

from archive import (
    get_archive_dir,
    load_layout,
    save_layout,
    load_shortname_map,
    save_shortname_entry,
    plan_moves,
    resolve_franchise,
)
from manifest import load_manifest, save_manifest

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)

CUSTOM_CSS = """
#resolve-image img {
    max-height: 60vh;
    width: auto;
    object-fit: contain;
    margin: 0 auto;
}
"""

FLAG_CATEGORY_ORDER = {
    "unresolved_franchise": 0,
    "no_folder_alias": 0,
    "needs_folder": 1,
    "needs_shortname": 2,
}

archive_dir = get_archive_dir()
layout = load_layout()
shortname_map = load_shortname_map(layout)
manifest = load_manifest()

rows = []
current_index = 0
last_action = None  # None, or a dict describing the most recent resolution/undo state


def _sort_key(row):
    return (
        FLAG_CATEGORY_ORDER.get(row["flag_reason"], 99),
        row["entry"].get("fetched_at", ""),
    )


def _recompute():
    global rows
    all_rows, _ = plan_moves(manifest, layout, shortname_map, archive_dir)
    candidates = [r for r in all_rows if r["outcome"] == "flag" and r["flag_reason"] != "missing_directory"]
    rows = sorted(candidates, key=_sort_key)


def _current_row():
    if current_index < len(rows):
        return rows[current_index]
    return None


def _propose_shortname(tag_name):
    words = [w for w in re.split(r"[^A-Za-z0-9]+", tag_name or "") if w]
    if not words:
        return ""
    return "".join(w[0].upper() for w in words)


def _snapshot_layout():
    return copy.deepcopy(layout)


def _render_current(status=""):
    row = _current_row()
    undo_update = gr.update(interactive=last_action is not None)

    if row is None:
        return (
            status or "Nothing left to resolve.",
            "All flagged entries handled -- nothing left in the queue.",
            gr.update(visible=False),
            None, "", "",
            gr.update(visible=False),
            gr.update(choices=[], value=None),
            "", "flat",
            gr.update(visible=True),
            gr.update(visible=False),
            "",
            gr.update(visible=False),
            "",
            "",
            gr.update(visible=False),
            gr.update(choices=[], value=None),
            gr.update(visible=False),
            gr.update(visible=False),
            "",
            undo_update,
        )

    entry = row["entry"]
    flag_reason = row["flag_reason"]
    tag = (entry.get("franchise") or [None])[0]

    header = f"Resolving {current_index + 1} of {len(rows)} ({flag_reason})"
    local_path = entry.get("local_path")
    image_path = str(Path(local_path).resolve()) if local_path else None
    title_md = f"### {entry.get('title', '')}"
    permalink = entry.get("permalink", "")
    meta_md = (
        f"**Subreddit:** r/{entry.get('subreddit', '')}  \n"
        f"**Link:** [{permalink}]({permalink})  \n"
        f"**Franchise:** {', '.join(entry.get('franchise') or []) or '-'} · "
        f"**Character(s):** {', '.join(entry.get('character_guess') or []) or '-'}  \n"
        f"**Flag:** {row['flag_detail']}"
    )

    is_franchise_flag = flag_reason in ("unresolved_franchise", "no_folder_alias")
    is_character_flag = flag_reason == "needs_folder"
    is_shortname_flag = flag_reason == "needs_shortname"

    existing_folders = sorted(layout.get("franchises", {}).keys())
    proposed_code = _propose_shortname(tag)

    char_tag = None
    folder_name = None
    franchise_characters = []
    if is_character_flag:
        folder_name, _ = resolve_franchise(tag, layout) if tag else (None, "unmapped")
        franchise_characters = sorted(layout.get("franchises", {}).get(folder_name, {}).get("characters", []))
        char_list = entry.get("character_guess") or []
        char_tag = char_list[0] if char_list else None

    character_context = (
        f"**Character:** {char_tag or '(none tagged)'}  in franchise **{folder_name}**"
        if is_character_flag else ""
    )

    return (
        status,
        header,
        gr.update(visible=True),
        image_path,
        title_md,
        meta_md,
        gr.update(visible=is_franchise_flag),
        gr.update(choices=existing_folders, value=None),
        "",
        "flat",
        gr.update(visible=True),
        gr.update(visible=False),
        proposed_code,
        gr.update(visible=is_character_flag),
        character_context,
        char_tag or "",
        gr.update(visible=is_character_flag and bool(char_tag)),
        gr.update(choices=franchise_characters, value=None),
        gr.update(visible=is_character_flag and bool(char_tag)),
        gr.update(visible=is_shortname_flag),
        proposed_code,
        undo_update,
    )


def _finish_resolution(row):
    global current_index
    resolved_post_id = row["post_id"]
    prior_total = len(rows)
    _recompute()
    still_flagged = next((r for r in rows if r["post_id"] == resolved_post_id), None)
    if still_flagged is not None:
        current_index = rows.index(still_flagged)
        status = f"Still flagged ({still_flagged['flag_reason']}) -- check your input and try again."
    else:
        current_index = 0
        cleared = prior_total - len(rows)
        status = f"Resolved. {cleared} flagged row(s) cleared."
    return _render_current(status)


def on_style_change(style):
    is_shortname = style == "shortname"
    return gr.update(visible=not is_shortname), gr.update(visible=is_shortname)


def on_map_existing_folder(selected_folder):
    global last_action
    row = _current_row()
    if row is None or not selected_folder:
        return _render_current("Pick a folder first.")
    tag = (row["entry"].get("franchise") or [None])[0]
    if not tag:
        return _render_current("This entry has no franchise tag to alias.")

    snapshot = _snapshot_layout()
    layout.setdefault("franchise_aliases", {})[tag] = selected_folder
    save_layout(layout)
    last_action = {"kind": "layout", "snapshot": snapshot, "resolved_post_id": row["post_id"]}
    return _finish_resolution(row)


def on_create_new_folder(new_name, style):
    global last_action
    row = _current_row()
    if row is None:
        return _render_current()
    new_name = (new_name or "").strip()
    if not new_name:
        return _render_current("Enter a folder name first.")
    if style == "shortname":
        return _render_current("Pick 'flat' or 'nested', or use the shortname box below instead.")
    if new_name in layout.get("franchises", {}):
        return _render_current(f"'{new_name}' already exists in layout.json -- use 'map to existing folder' instead.")

    tag = (row["entry"].get("franchise") or [None])[0]
    snapshot = _snapshot_layout()
    (archive_dir / new_name).mkdir(parents=True, exist_ok=True)
    franchise_def = {"style": style}
    if style == "nested":
        franchise_def["characters"] = []
    layout.setdefault("franchises", {})[new_name] = franchise_def
    if tag and tag != new_name:
        layout.setdefault("franchise_aliases", {})[tag] = new_name
    save_layout(layout)
    last_action = {"kind": "layout", "snapshot": snapshot, "resolved_post_id": row["post_id"]}
    return _finish_resolution(row)


def on_route_unknown_sauce():
    global last_action
    row = _current_row()
    if row is None:
        return _render_current()
    entry = row["entry"]
    prior = entry.get("archive_override")
    entry["archive_override"] = "unknown_source"
    save_manifest(manifest)
    last_action = {
        "kind": "manifest_field", "post_id": row["post_id"], "field": "archive_override",
        "prior_value": prior, "resolved_post_id": row["post_id"],
    }
    return _finish_resolution(row)


def on_create_character_folder(folder_name_text):
    global last_action
    row = _current_row()
    if row is None:
        return _render_current()
    entry = row["entry"]
    tag = (entry.get("franchise") or [None])[0]
    char_list = entry.get("character_guess") or []
    char_tag = char_list[0] if char_list else None
    if not tag or not char_tag:
        return _render_current("This entry has no character to create a folder for.")

    folder_name, _ = resolve_franchise(tag, layout)
    if not folder_name or folder_name not in layout.get("franchises", {}):
        return _render_current("Franchise isn't resolved yet -- resolve its folder first.")

    final_name = (folder_name_text or "").strip() or char_tag

    snapshot = _snapshot_layout()
    (archive_dir / folder_name / final_name).mkdir(parents=True, exist_ok=True)
    characters = layout["franchises"][folder_name].setdefault("characters", [])
    if final_name not in characters:
        characters.append(final_name)
    if final_name != char_tag:
        layout.setdefault("character_aliases", {}).setdefault(folder_name, {})[char_tag] = final_name
    save_layout(layout)
    last_action = {"kind": "layout", "snapshot": snapshot, "resolved_post_id": row["post_id"]}
    return _finish_resolution(row)


def on_map_character_alias(selected_character):
    global last_action
    row = _current_row()
    if row is None or not selected_character:
        return _render_current("Pick a character first.")
    entry = row["entry"]
    tag = (entry.get("franchise") or [None])[0]
    char_list = entry.get("character_guess") or []
    char_tag = char_list[0] if char_list else None
    if not tag or not char_tag:
        return _render_current("This entry has no character to alias.")

    folder_name, _ = resolve_franchise(tag, layout)
    if not folder_name:
        return _render_current("Franchise isn't resolved yet -- resolve its folder first.")

    snapshot = _snapshot_layout()
    layout.setdefault("character_aliases", {}).setdefault(folder_name, {})[char_tag] = selected_character
    save_layout(layout)
    last_action = {"kind": "layout", "snapshot": snapshot, "resolved_post_id": row["post_id"]}
    return _finish_resolution(row)


def on_route_group():
    global last_action
    row = _current_row()
    if row is None:
        return _render_current()
    entry = row["entry"]
    prior = entry.get("same_series_group", False)
    entry["same_series_group"] = True
    save_manifest(manifest)
    last_action = {
        "kind": "manifest_field", "post_id": row["post_id"], "field": "same_series_group",
        "prior_value": prior, "resolved_post_id": row["post_id"],
    }
    return _finish_resolution(row)


def on_save_shortname(code_text):
    global shortname_map, last_action
    row = _current_row()
    if row is None:
        return _render_current()
    entry = row["entry"]
    tag = (entry.get("franchise") or [None])[0]
    code = (code_text or "").strip()
    if not tag or not code:
        return _render_current("Enter a shortname code first.")

    shortname_path = Path(layout["shortname_file"])
    prior_text = shortname_path.read_text(encoding="utf-8") if shortname_path.exists() else None
    save_shortname_entry(shortname_path, code, tag)
    shortname_map = load_shortname_map(layout)
    last_action = {
        "kind": "shortname", "path": shortname_path, "existed_before": prior_text is not None,
        "snapshot": prior_text, "resolved_post_id": row["post_id"],
    }
    return _finish_resolution(row)


def on_load():
    _recompute()
    return _render_current()


def on_undo():
    global last_action, current_index, shortname_map
    if last_action is None:
        return _render_current()

    action = last_action
    last_action = None

    if action["kind"] == "layout":
        layout.clear()
        layout.update(action["snapshot"])
        save_layout(layout)
    elif action["kind"] == "shortname":
        path = action["path"]
        if action["existed_before"]:
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(action["snapshot"], encoding="utf-8")
            os.replace(tmp_path, path)
        elif path.exists():
            path.unlink()
        shortname_map = load_shortname_map(layout)
    elif action["kind"] == "manifest_field":
        entry = manifest[action["post_id"]]
        field = action["field"]
        prior_value = action["prior_value"]
        if prior_value is None:
            entry.pop(field, None)
        else:
            entry[field] = prior_value
        save_manifest(manifest)

    _recompute()
    resolved_post_id = action["resolved_post_id"]
    target = next((r for r in rows if r["post_id"] == resolved_post_id), None)
    current_index = rows.index(target) if target is not None else 0
    return _render_current("Undone.")


with gr.Blocks(title="Oshiire resolve flags") as demo:
    status_md = gr.Markdown()
    header_md = gr.Markdown()

    with gr.Group() as resolve_group:
        with gr.Row(equal_height=False):
            with gr.Column(scale=1):
                image = gr.Image(interactive=False, show_label=False, elem_id="resolve-image")
            with gr.Column(scale=1):
                title_md = gr.Markdown()
                meta_md = gr.Markdown()

                with gr.Group(visible=False) as franchise_group:
                    gr.Markdown("**Franchise has no folder/alias**")
                    existing_folder_dropdown = gr.Dropdown(label="Map to existing folder")
                    map_existing_btn = gr.Button("Map alias to selected folder")

                    gr.Markdown("--- or create a new folder ---")
                    new_folder_name_box = gr.Textbox(label="New folder name")
                    new_folder_style_radio = gr.Radio(
                        choices=["flat", "nested", "shortname"], value="flat", label="Style"
                    )
                    create_folder_btn = gr.Button("Create new franchise folder")
                    with gr.Group(visible=False) as franchise_shortname_subgroup:
                        franchise_shortname_code_box = gr.Textbox(label="Shortname code")
                        franchise_confirm_shortname_btn = gr.Button("Save shortname mapping")

                    gr.Markdown("--- or ---")
                    route_unknown_btn = gr.Button("Route just this image to Unknown Sauce")

                with gr.Group(visible=False) as character_group:
                    character_context_md = gr.Markdown()
                    character_folder_name_box = gr.Textbox(label="Character folder name")
                    create_char_folder_btn = gr.Button("Create character subfolder")

                    gr.Markdown("--- or ---")
                    existing_character_dropdown = gr.Dropdown(label="Map to existing character")
                    map_character_btn = gr.Button("Map alias to selected character")

                    gr.Markdown("--- or ---")
                    route_group_btn = gr.Button("Route to Others_Group (same-series group)")

                with gr.Group(visible=False) as shortname_group:
                    shortname_code_box = gr.Textbox(label="Shortname code")
                    confirm_shortname_btn = gr.Button("Save shortname mapping")

        undo_btn = gr.Button("Undo last action")

    outputs = [
        status_md,
        header_md,
        resolve_group,
        image,
        title_md,
        meta_md,
        franchise_group,
        existing_folder_dropdown,
        new_folder_name_box,
        new_folder_style_radio,
        create_folder_btn,
        franchise_shortname_subgroup,
        franchise_shortname_code_box,
        character_group,
        character_context_md,
        character_folder_name_box,
        create_char_folder_btn,
        existing_character_dropdown,
        map_character_btn,
        shortname_group,
        shortname_code_box,
        undo_btn,
    ]

    demo.load(fn=on_load, outputs=outputs)

    new_folder_style_radio.change(
        fn=on_style_change,
        inputs=[new_folder_style_radio],
        outputs=[create_folder_btn, franchise_shortname_subgroup],
    )

    map_existing_btn.click(fn=on_map_existing_folder, inputs=[existing_folder_dropdown], outputs=outputs)
    create_folder_btn.click(fn=on_create_new_folder, inputs=[new_folder_name_box, new_folder_style_radio], outputs=outputs)
    franchise_confirm_shortname_btn.click(fn=on_save_shortname, inputs=[franchise_shortname_code_box], outputs=outputs)
    route_unknown_btn.click(fn=on_route_unknown_sauce, outputs=outputs)

    create_char_folder_btn.click(fn=on_create_character_folder, inputs=[character_folder_name_box], outputs=outputs)
    map_character_btn.click(fn=on_map_character_alias, inputs=[existing_character_dropdown], outputs=outputs)
    route_group_btn.click(fn=on_route_group, outputs=outputs)

    confirm_shortname_btn.click(fn=on_save_shortname, inputs=[shortname_code_box], outputs=outputs)

    undo_btn.click(fn=on_undo, outputs=outputs)


if __name__ == "__main__":
    demo.launch(css=CUSTOM_CSS)
