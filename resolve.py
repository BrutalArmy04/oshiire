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
import warnings
from pathlib import Path

import gradio as gr
from starlette.exceptions import StarletteDeprecationWarning

from archive import get_archive_dir, plan_moves
from manifest import load_manifest, save_manifest
from shortname import (
    franchise_folder_and_def,
    load_layout,
    lookup_ci,
    reversed_name_variant,
    save_character_alias,
    save_layout,
    load_shortname_map,
    load_series_aliases,
    save_series_alias,
    save_series_aliases,
    match_shortname,
    same_series_name,
    propose_shortname_code,
    save_shortname_entry,
    find_shortname_collision,
    verify_shortname_entry,
    undo_shortname_write,
)

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)

CUSTOM_CSS = """
#resolve-image img {
    max-height: 60vh;
    width: auto;
    object-fit: contain;
    margin: 0 auto;
}
/* The learn-alias prompt asks a question that permanently teaches (or
   doesn't teach) an alternate name, so it must not render as muted body text
   the way it did before. Same accented treatment as review.py's
   subreddit-map confirm panel -- a decision panel should look alike in both
   UIs. Static here, unlike review.py's, since this panel has only one tier. */
.confirm-panel {
    border: 2px solid #d97706;
    background: rgba(217, 119, 6, 0.1);
    border-radius: 8px;
    padding: 12px;
}
"""

FLAG_CATEGORY_ORDER = {
    "unresolved_franchise": 0,
    "no_folder_alias": 0,
    "known_series_override_unresolved": 0,
    "needs_folder": 1,
    "needs_shortname": 2,
}

archive_dir = get_archive_dir()
layout = load_layout()
shortname_entries = load_shortname_map(layout)
series_aliases = load_series_aliases()
manifest = load_manifest()

rows = []
current_index = 0
last_action = None  # None, or a dict describing the most recent resolution/undo state
# Set while the "save this variant as an alternate name?" panel is open, so the
# panel's two buttons know which variant/canonical pair they're deciding on.
pending_alias = None


def _sort_key(row):
    return (
        FLAG_CATEGORY_ORDER.get(row["flag_reason"], 99),
        row["entry"].get("fetched_at", ""),
    )


def _recompute():
    global rows
    all_rows, _ = plan_moves(manifest, layout, shortname_entries, archive_dir, series_aliases)
    candidates = [r for r in all_rows if r["outcome"] == "flag" and r["flag_reason"] != "missing_directory"]
    rows = sorted(candidates, key=_sort_key)


def _current_row():
    if current_index < len(rows):
        return rows[current_index]
    return None


def _franchise_folder(entry):
    """(folder_name, franchise_def) for this entry's franchise, or (None, None).

    Goes through lookup_ci and returns the key exactly as layout.json spells
    it, mirroring archive.py's route_entry. resolve_franchise alone isn't
    enough: it can hand back the TAG's casing via the identity path, and an
    exact-case dict lookup on that yields an empty character roster (or a
    KeyError on write) for a franchise that is in fact configured fine.
    """
    tag = (entry.get("franchise") or [None])[0]
    if not tag:
        return None, None
    matched_folder, franchise_def = franchise_folder_and_def(tag, layout, series_aliases)
    if matched_folder is None:
        return None, None
    return matched_folder, franchise_def


def _character_tag(entry):
    char_list = entry.get("character_guess") or []
    return char_list[0] if char_list else None


def _propose_shortname(tag_name):
    return propose_shortname_code(tag_name, shortname_entries)


def _snapshot_layout():
    return copy.deepcopy(layout)


def _snapshot_aliases():
    return copy.deepcopy(series_aliases)


SERIES_CHOICE_SEP = "  --  "


def _series_choices():
    """Dropdown labels for the shortname file, "CODE  --  Full Series Name".
    Gradio's own filtering over these gives the typeahead for free."""
    return [f"{code}{SERIES_CHOICE_SEP}{full}" for code, full in shortname_entries]


def _canonical_from_choice(selection):
    """Maps a dropdown selection back to (code, canonical_full_name). Accepts a
    label picked from the list, or a bare code typed in by hand (the dropdown
    allows custom values). Returns (None, None) if nothing matches."""
    selection = (selection or "").strip()
    if not selection:
        return None, None
    if SERIES_CHOICE_SEP in selection:
        code, full = selection.split(SERIES_CHOICE_SEP, 1)
        return code.strip(), full.strip()
    code = match_shortname(selection, shortname_entries, series_aliases)
    if not code:
        return None, None
    return next(((c, f) for c, f in shortname_entries if c == code), (None, None))


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
            gr.update(choices=[], value=None),
            gr.update(visible=False, elem_classes=[]),
            "",
            gr.update(visible=False),
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

    # known_series_override_unresolved belongs here too: the entry is marked
    # "file as known series" but its franchise tag matched no shortname, and the
    # fixes on offer -- pick the existing shortname it's really a variant of, or
    # map/create a folder -- are exactly this group's controls. Without this it
    # rendered with every action group hidden.
    is_franchise_flag = flag_reason in (
        "unresolved_franchise", "no_folder_alias", "known_series_override_unresolved",
    )
    is_character_flag = flag_reason == "needs_folder"
    is_shortname_flag = flag_reason == "needs_shortname"

    existing_folders = sorted(layout.get("franchises", {}).keys())
    series_choices = _series_choices()
    proposed_code = _propose_shortname(tag)

    char_tag = None
    folder_name = None
    franchise_characters = []
    if is_character_flag:
        folder_name, franchise_def = _franchise_folder(entry)
        franchise_characters = sorted((franchise_def or {}).get("characters", []))
        char_tag = _character_tag(entry)

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
        gr.update(choices=series_choices, value=None),
        gr.update(
            visible=pending_alias is not None,
            elem_classes=["confirm-panel"] if pending_alias is not None else [],
        ),
        _learn_prompt_md(),
        gr.update(visible=True),
    )


def _learn_prompt_md():
    if pending_alias is None:
        return ""
    return (
        f"Selected: **{pending_alias['code']}  --  {pending_alias['canonical']}**\n\n"
        f"Save **'{pending_alias['variant']}'** as an alternate name for "
        f"**'{pending_alias['canonical']}'**?\n\n"
        "Saving teaches it permanently -- every future image tagged "
        f"'{pending_alias['variant']}' routes itself. "
        "Not saving retags just this one image and learns nothing."
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


def on_use_existing_shortname(selection):
    """Opens the learn-confirm panel for a flagged franchise that's really a
    variant name of a series already in the shortname file. Writes nothing --
    both outcomes are the panel's own buttons."""
    global pending_alias
    row = _current_row()
    if row is None:
        return _render_current()
    tag = (row["entry"].get("franchise") or [None])[0]
    if not tag:
        return _render_current("This entry has no franchise tag to resolve.")

    code, canonical = _canonical_from_choice(selection)
    if not code:
        return _render_current("Pick a series from the list (or type an existing shortname code).")

    pending_alias = {"variant": tag, "canonical": canonical, "code": code, "post_id": row["post_id"]}
    return _render_current()


def on_learn_save():
    """Records variant -> canonical, permanently. No archive_override is set:
    with the alternate known, the tag canonicalizes and archive.py's existing
    resolution routes it on its own -- to the canonical's real folder if it has
    one, else to Others/Known Series via the shortname fallback."""
    global series_aliases, last_action, pending_alias
    if pending_alias is None:
        return _render_current()
    row = _current_row()
    if row is None:
        pending_alias = None
        return _render_current()

    snapshot = _snapshot_aliases()
    variant, canonical = pending_alias["variant"], pending_alias["canonical"]
    pending_alias = None
    series_aliases = save_series_alias(variant, canonical)
    last_action = {"kind": "series_aliases", "snapshot": snapshot, "resolved_post_id": row["post_id"]}
    return _finish_resolution(row)


def on_learn_skip():
    """One-off: retags THIS entry's franchise to the canonical name so it
    routes normally, without teaching the variant. Undo restores the old tag."""
    global last_action, pending_alias
    if pending_alias is None:
        return _render_current()
    row = _current_row()
    if row is None:
        pending_alias = None
        return _render_current()

    entry = row["entry"]
    canonical = pending_alias["canonical"]
    pending_alias = None
    prior = entry.get("franchise")
    entry["franchise"] = [canonical] + list(prior or [])[1:]
    save_manifest(manifest)
    last_action = {
        "kind": "manifest_field", "post_id": row["post_id"], "field": "franchise",
        "prior_value": prior, "resolved_post_id": row["post_id"],
    }
    return _finish_resolution(row)


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


def on_route_artist_original():
    global last_action
    row = _current_row()
    if row is None:
        return _render_current()
    entry = row["entry"]
    prior = entry.get("archive_override")
    entry["archive_override"] = "artist_original"
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
    char_tag = _character_tag(entry)
    if not char_tag:
        return _render_current("This entry has no character to create a folder for.")

    folder_name, franchise_def = _franchise_folder(entry)
    if not folder_name:
        return _render_current("Franchise isn't resolved yet -- resolve its folder first.")

    final_name = (folder_name_text or "").strip() or char_tag

    # A needs_folder flag already proves neither name order matched (archive.py's
    # resolve_character tries the swap), so reversal can't rescue the lookup --
    # but it CAN stop this click from creating "Kuki Shinobu/" next to an
    # existing "Shinobu Kuki/". Creating a folder is the one irreversible half
    # of this screen, so the near-duplicate gets refused, not merged.
    swapped = reversed_name_variant(final_name)
    existing_swapped, _ = lookup_ci(franchise_def.get("characters", []), swapped) if swapped else (None, None)
    if existing_swapped:
        return _render_current(
            f"'{existing_swapped}' already exists in {folder_name} -- same name, reversed order. "
            f"Use 'map alias to selected character' instead of creating a second folder."
        )

    snapshot = _snapshot_layout()
    (archive_dir / folder_name / final_name).mkdir(parents=True, exist_ok=True)
    characters = franchise_def.setdefault("characters", [])
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
    char_tag = _character_tag(entry)
    if not char_tag:
        return _render_current("This entry has no character to alias.")

    folder_name, _ = _franchise_folder(entry)
    if not folder_name:
        return _render_current("Franchise isn't resolved yet -- resolve its folder first.")

    snapshot = _snapshot_layout()
    save_character_alias(folder_name, char_tag, selected_character, layout)
    last_action = {"kind": "layout", "snapshot": snapshot, "resolved_post_id": row["post_id"]}
    return _finish_resolution(row)


def on_route_group():
    """Files this image under <Franchise>/Others_Group/, for a character not
    worth a dedicated subfolder.

    Creates the group folder if it's missing. Without that this action was a
    silent dead end: plan_moves flags a non-existent destination as
    missing_directory, and _recompute filters missing_directory out of this
    queue -- so the entry would vanish from here while archive.py quietly
    refused to move it, forever. Creating on an explicit click is the same
    rule the create-subfolder button already follows."""
    global last_action
    row = _current_row()
    if row is None:
        return _render_current()
    entry = row["entry"]

    folder_name, _ = _franchise_folder(entry)
    if not folder_name:
        return _render_current("Franchise isn't resolved yet -- resolve its folder first.")
    (archive_dir / folder_name / layout["group_subfolder"]).mkdir(parents=True, exist_ok=True)

    prior = entry.get("same_series_group", False)
    entry["same_series_group"] = True
    save_manifest(manifest)
    last_action = {
        "kind": "manifest_field", "post_id": row["post_id"], "field": "same_series_group",
        "prior_value": prior, "resolved_post_id": row["post_id"],
    }
    return _finish_resolution(row)


def on_send_back_to_review():
    """Escape hatch for any flagged entry: return it to the review UI so tag
    errors (e.g. a franchise typo that caused the flag) can be fixed there.
    Sets status back to pending_review; the entry then drops out of plan_moves
    (approved-only) and clears from this queue. Undo restores 'approved'."""
    global last_action
    row = _current_row()
    if row is None:
        return _render_current()
    entry = row["entry"]
    # Also clear any archive_flag* left by a previous `archive.py --apply`.
    # plan_moves ignores those fields, but review.py would otherwise show a
    # stale flag on an entry that's being re-reviewed precisely to fix it.
    fields = ("status", "archive_flag", "archive_flag_detail", "archive_flag_at")
    prior = {f: entry.get(f) for f in fields}
    entry["status"] = "pending_review"
    for f in fields[1:]:
        entry.pop(f, None)
    save_manifest(manifest)
    last_action = {
        "kind": "manifest_fields", "post_id": row["post_id"],
        "prior": prior, "resolved_post_id": row["post_id"],
    }
    return _finish_resolution(row)


def on_save_shortname(code_text):
    global shortname_entries, last_action, pending_alias
    row = _current_row()
    if row is None:
        return _render_current()
    entry = row["entry"]
    tag = (entry.get("franchise") or [None])[0]
    code = (code_text or "").strip()
    if not tag or not code:
        return _render_current("Enter a shortname code first.")

    # Same/matching series may already have an entry under a stricter-looking
    # string (e.g. tag "NIKKE" vs file entry "NIKKE = NIKKE The Goddess of
    # Victory") -- reuse it instead of treating it as a code collision.
    already_matched_code = match_shortname(tag, shortname_entries, series_aliases)
    if already_matched_code:
        matched_full = next((f for c, f in shortname_entries if c == already_matched_code), None)
        # The tag reuses an existing entry under a different string -- the most
        # common place a variant name surfaces. Offer to learn it rather than
        # silently routing and meeting the same variant again next week.
        if matched_full and not same_series_name(tag, matched_full):
            pending_alias = {
                "variant": tag, "canonical": matched_full,
                "code": already_matched_code, "post_id": row["post_id"],
            }
            return _render_current()
        return _finish_resolution(row)

    shortname_path = Path(layout["shortname_file"])

    collision = find_shortname_collision(shortname_path, code, tag)
    if collision:
        return _render_current(f"Code '{code}' is already used by '{collision}'. Pick a different code.")

    prior_text = shortname_path.read_text(encoding="utf-8") if shortname_path.exists() else None
    save_shortname_entry(shortname_path, code, tag)

    if not verify_shortname_entry(shortname_path, code, tag):
        return _render_current("Shortname save did not persist to disk -- please retry.")

    shortname_entries = load_shortname_map(layout)
    last_action = {
        "kind": "shortname", "path": shortname_path, "existed_before": prior_text is not None,
        "snapshot": prior_text, "resolved_post_id": row["post_id"],
    }
    return _finish_resolution(row)


def on_load():
    _recompute()
    return _render_current()


def on_undo():
    global last_action, current_index, shortname_entries, series_aliases, pending_alias
    if last_action is None:
        return _render_current()

    action = last_action
    last_action = None
    pending_alias = None

    if action["kind"] == "layout":
        layout.clear()
        layout.update(action["snapshot"])
        save_layout(layout)
    elif action["kind"] == "series_aliases":
        series_aliases = action["snapshot"]
        save_series_aliases(series_aliases)
    elif action["kind"] == "shortname":
        undo_shortname_write(action["path"], action["existed_before"], action["snapshot"])
        shortname_entries = load_shortname_map(layout)
    elif action["kind"] == "manifest_field":
        entry = manifest[action["post_id"]]
        field = action["field"]
        prior_value = action["prior_value"]
        if prior_value is None:
            entry.pop(field, None)
        else:
            entry[field] = prior_value
        save_manifest(manifest)
    elif action["kind"] == "manifest_fields":
        entry = manifest[action["post_id"]]
        for field, prior_value in action["prior"].items():
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

                    gr.Markdown("--- or, if this is a series you already know under another name ---")
                    series_dropdown = gr.Dropdown(
                        label="Use existing shortname",
                        allow_custom_value=True,
                        filterable=True,
                        info="Type to filter by code or series name.",
                    )
                    use_shortname_btn = gr.Button("Use this shortname")

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

                # Per-image escape routes. These used to live inside
                # franchise_group, which made them unreachable for a
                # needs_folder entry -- exactly the case where "this character
                # isn't worth solving properly" is the honest answer. They set
                # an archive_override on this one image and touch no config, so
                # they're valid whatever the flag reason is.
                with gr.Group(visible=False) as override_group:
                    gr.Markdown("--- or route just this image elsewhere ---")
                    route_unknown_btn = gr.Button("Route just this image to Unknown Sauce")
                    route_artist_original_btn = gr.Button("Route just this image to Artist's Original")

        # Learn-confirm panel. Shared by both paths that discover a variant
        # name -- the "use existing shortname" control above and the reuse case
        # inside on_save_shortname -- so an alternate is never recorded without
        # the variant and the canonical both being spelled out first.
        with gr.Group(visible=False) as learn_group:
            learn_md = gr.Markdown()
            with gr.Row():
                learn_save_btn = gr.Button("Save alternate name", variant="primary")
                learn_skip_btn = gr.Button("Just route this image, don't save")

        gr.Markdown("--- or, if the tags themselves are wrong ---")
        send_back_btn = gr.Button("Send back to review")

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
        series_dropdown,
        learn_group,
        learn_md,
        override_group,
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
    use_shortname_btn.click(fn=on_use_existing_shortname, inputs=[series_dropdown], outputs=outputs)
    learn_save_btn.click(fn=on_learn_save, outputs=outputs)
    learn_skip_btn.click(fn=on_learn_skip, outputs=outputs)
    route_unknown_btn.click(fn=on_route_unknown_sauce, outputs=outputs)
    route_artist_original_btn.click(fn=on_route_artist_original, outputs=outputs)

    create_char_folder_btn.click(fn=on_create_character_folder, inputs=[character_folder_name_box], outputs=outputs)
    map_character_btn.click(fn=on_map_character_alias, inputs=[existing_character_dropdown], outputs=outputs)
    route_group_btn.click(fn=on_route_group, outputs=outputs)

    confirm_shortname_btn.click(fn=on_save_shortname, inputs=[shortname_code_box], outputs=outputs)

    send_back_btn.click(fn=on_send_back_to_review, outputs=outputs)

    undo_btn.click(fn=on_undo, outputs=outputs)


if __name__ == "__main__":
    demo.launch(css=CUSTOM_CSS, inbrowser=True)
