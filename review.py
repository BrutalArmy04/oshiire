"""Slice 2: Gradio review UI. Reads manifest.json fresh on launch, presents
pending_review entries one at a time in chronological order for human
approve/edit/reject. Makes no network calls and does no downloading; only
reads staging images, reads/writes the manifest, and (on Reject) deletes a
staging file. Reuses manifest.py's load/save -- no reimplementation.
"""
import copy
import os
import re
import warnings
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv
from starlette.exceptions import StarletteDeprecationWarning

from manifest import load_manifest, save_manifest
from tagger import (
    normalize_subreddit,
    read_subreddit_map,
    remove_subreddit_map_entry,
    save_subreddit_map_entry,
    subreddit_is_mapped,
)
from imagemeta import (
    build_archive_path_map,
    ensure_image_meta,
    find_duplicates,
    index_hash_size,
    load_archive_hashes,
    suggest_wallpaper,
    warm_image_meta,
)
from shortname import (
    franchise_folder_and_def,
    load_layout,
    load_shortname_map,
    load_series_aliases,
    load_wallpaper_rules,
    match_shortname,
    resolve_character,
    save_character_alias,
    save_layout,
    propose_shortname_code,
    save_shortname_entry,
    find_shortname_collision,
    verify_shortname_entry,
    undo_shortname_write,
)

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)

# Mirrors archive.py's ORIGINAL_RE/is_original -- duplicated rather than
# imported since review.py (Slice 2) shouldn't depend on archive.py (Slice 3a).
# (Shortname-file I/O/matching, unlike routing logic, lives in the shared
# shortname.py module and is imported directly -- see CLAUDE.md.)
_OC_TITLE_RE = re.compile(
    r"\[\s*(?:original(?:\s+character)?|oc|artist(?:['’]s|s)?\s+original)\s*\]",
    re.IGNORECASE,
)

CUSTOM_CSS = """
#review-image img {
    max-height: 70vh;
    width: auto;
    object-fit: contain;
    margin: 0 auto;
}
.confirm-panel {
    border: 2px solid #d97706;
    background: rgba(217, 119, 6, 0.1);
    border-radius: 8px;
    padding: 12px;
}
/* Duplicate / backfill warnings reuse the confirm-panel look, in red for a
   confident match and amber for the uncertain band. */
.warn-panel {
    border: 2px solid #dc2626;
    background: rgba(220, 38, 38, 0.1);
    border-radius: 8px;
    padding: 12px;
}
.maybe-panel {
    border: 2px solid #d97706;
    background: rgba(217, 119, 6, 0.1);
    border-radius: 8px;
    padding: 12px;
}
#dup-thumb img {
    max-height: 22vh;
    width: auto;
    object-fit: contain;
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

layout = load_layout()
shortname_entries = load_shortname_map(layout)
wallpaper_rules = load_wallpaper_rules(layout)

# Duplicate-detection setup. ARCHIVE_DIR is read softly (not via
# hash_index.get_archive_dir, which exits when unset) -- without it the
# archive half of the corpus simply goes unused and review still works.
load_dotenv()
_archive_dir_value = os.environ.get("ARCHIVE_DIR")
archive_dir = Path(_archive_dir_value) if _archive_dir_value else None
hash_size = index_hash_size()

# Top-up pass: hash any staging image that doesn't have a current hash yet, so
# duplicate detection compares against the whole corpus rather than only
# images already viewed this session. Normally a no-op, because the
# maintenance cycle runs `imagemeta.py warm` straight after ingest -- this is
# just the safety net for images that arrived some other way.
print("Checking image hashes for duplicate detection ...")
_warmed = warm_image_meta(
    manifest,
    hash_size,
    progress=lambda done, total: print(
        f"\r  hashing {done} of {total} (one-time; run launcher 7 to do this during maintenance)",
        end="", flush=True,
    ),
)
if _warmed:
    save_manifest(manifest)
    print(f"\r  hashed {_warmed} image(s).{' ' * 40}")
else:
    print("  all staging images already hashed.")

archive_hashes = load_archive_hashes()
archive_path_map = build_archive_path_map(manifest)
# What the index ACTUALLY holds. Anything filed since the index was last built
# is missing from it, and find_duplicates uses this to compare those entries
# from their cached manifest hash instead of assuming the index covered them.
indexed_paths = {rel_path for rel_path, _ in archive_hashes}
if not archive_hashes:
    print(
        "  note: no archive pHash index found — duplicate checks will only cover "
        "staging. Run `python hash_index.py build` to include the archive."
    )
else:
    print(f"  archive index: {len(archive_hashes):,} image(s) available for comparison.")
    _unindexed = sum(
        1 for entry in manifest.values()
        if entry.get("archive_path") and entry["archive_path"] not in indexed_paths
    )
    if _unindexed:
        # Not fatal -- those entries are compared from their cached hash below
        # -- but a large number means the index is behind and archive files
        # added outside the pipeline aren't being compared at all.
        print(
            f"  note: {_unindexed:,} archived entr(ies) aren't in the index yet; "
            "they'll be compared from their cached hash. Run launcher 7 (or "
            "`python hash_index.py build`) to refresh it."
        )
# Consulted so a franchise tagged with a known variant name resolves to its
# existing series instead of proposing a duplicate code. Learning new SERIES
# alternates is still resolve.py's job. CHARACTER alternates are learned here
# instead: the misspelling happens in this box, and by the time archive.py
# flags it the reviewer is several screens away from the image that would tell
# them which character it is. Still two clicks -- the panel only opens for a
# name that resolves to nothing, and "just this once" writes nothing.
series_aliases = load_series_aliases()


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
        "archive_override": entry.get("archive_override"),
    }


def _parse_lines(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


WALLPAPER_CHOICES = ["none", "pc", "phone", "both"]


def _wallpaper_choices(suggested):
    """Radio choices with the suggested target(s) marked. Marking is a (label,
    value) relabel only -- the stored value is unchanged and nothing is
    auto-selected, per the rule that this is a suggestion, never a decision."""
    labelled = []
    for choice in WALLPAPER_CHOICES:
        if choice in suggested and choice != "both":
            labelled.append((f"{choice} ★ suggested", choice))
        elif choice == "both" and len(suggested) == 2:
            labelled.append(("both ★ suggested", "both"))
        else:
            labelled.append((choice, choice))
    return labelled


def _wallpaper_hint(entry, suggested):
    """Dimension line shown above the Wallpaper control. Dims are shown
    ALWAYS (so a borderline image can be judged by eye), with the suggestion
    appended only when the rules actually match."""
    width, height = entry.get("width"), entry.get("height")
    if not width or not height:
        return "**Wallpaper** — dimensions unavailable."
    dims = f"{width}×{height}"
    if len(suggested) == 2:
        return f"**Wallpaper** — {dims} — suitable as PC *and* phone wallpaper."
    if suggested:
        target = "PC" if suggested[0] == "pc" else "phone"
        return f"**Wallpaper** — {dims} — suitable as {target} wallpaper."
    return f"**Wallpaper** — {dims} — no wallpaper rule matches."


def _servable_thumb(path):
    """A duplicate's thumbnail path, or None if it can't safely be shown.

    Two hazards, both fatal to the whole panel if they reach Gradio. Archive
    matches are built as `archive_dir / rel_path` in imagemeta.py without any
    existence check, so the file may be gone, stale, or on a disconnected
    drive where even .exists() raises rather than returning False. And Gradio
    refuses to serve anything outside its allowed_paths -- it raises
    InvalidPathError from inside postprocess, AFTER this handler returns,
    where no try/except of ours could catch it. So the containment check has
    to happen here, before the path is handed back. Any doubt -> no
    thumbnail; the banner text still shows.
    """
    if not path:
        return None
    roots = [Path.cwd()]
    if archive_dir:
        roots.append(archive_dir)
    try:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            return None
        if not any(resolved.is_relative_to(root.resolve()) for root in roots):
            return None
        return str(resolved)
    except OSError:
        return None


def _queue_position(post_id):
    """This entry's index in the review queue, or None if it isn't in it.

    `queue` is built once at launch and never mutated -- Skip/Reject/Accept
    only advance `current_index` -- so list order IS the order the images are
    encountered, and an index below `current_index` means "already seen this
    session". That makes it the only correct basis for the earlier/later test
    below; fetched_at or manifest order would answer a different question.
    """
    try:
        return queue.index(post_id)
    except ValueError:
        return None


def _is_unseen_twin(other_id):
    """Whether a matched manifest entry is a pending copy we haven't reached.

    Passed to find_duplicates as its `exclude` predicate, so these matches are
    dropped from the corpus entirely -- no banner, no line, no thumbnail.
    Rationale: on the EARLIER copy of a pair the twin hasn't been looked at
    yet, so there is nothing to decide and nothing to compare against; the
    decision belongs on the second copy, where the first has been seen and
    judged. Warning on both just asks the same question twice.

    A pending entry with no queue position is treated the same way. It arrived
    after launch (the queue is built once), so it is likewise unseen -- and
    excluding it keeps the invariant that every certain match still shown is
    actionable, which is what lets the reject button appear unconditionally.
    """
    if not other_id:
        return False
    other = manifest.get(other_id)
    if (other or {}).get("status") != "pending_review":
        return False
    position = _queue_position(other_id)
    return position is None or position > current_index


def _match_is_actionable(match):
    """Whether this match earns the one-click reject, or stays advisory.

    Only a CERTAIN match qualifies: the 9-11 amber band is uncertain by
    definition, and a one-click reject there would launder a maybe into a
    decision.

    The not-yet-seen twin case is handled upstream by _is_unseen_twin, which
    keeps those matches out of the results altogether -- so every pending twin
    reaching this point is one already seen this session. The position check
    below is kept as the belt-and-braces restatement of that rule.
    """
    if not match.is_certain:
        return False
    if match.source == "archive":
        return True
    other = manifest.get(match.post_id) if match.post_id else None
    status = (other or {}).get("status")
    if status == "approved":
        # Already decided this session (or a previous one) -- the keeper is
        # settled regardless of where it sits in the queue.
        return True
    if status == "pending_review":
        position = _queue_position(match.post_id)
        # Unknown position -> treat as not-yet-seen and stay passive.
        return position is not None and position < current_index
    # Anything else (rejected, download_failed, skipped) has no keeper to
    # defer to -- its file is already gone. Advisory only.
    return False


def _duplicate_action_label(matches):
    """Label for the banner's one-click reject, or None to stay advisory.

    `matches` arrives sorted nearest-first, so the nearest ACTIONABLE match
    wins; the distance goes in the label so the button can never read more
    confident than its evidence.
    """
    for match in matches:
        if not match.is_certain:
            # Sorted nearest-first, so the first uncertain match means there
            # are no certain ones left.
            break
        if not _match_is_actionable(match):
            continue
        if match.source == "archive":
            return f"Reject as duplicate — archived copy is the keeper (distance {match.distance})"
        other = manifest.get(match.post_id) if match.post_id else None
        if (other or {}).get("status") == "approved":
            return f"Reject as duplicate — other copy already approved (distance {match.distance})"
        return f"Reject this — keep the copy you already saw (distance {match.distance})"
    return None


def _duplicate_banner(post_id, entry):
    """(group_update, markdown, thumbnail) for the warning panel above the
    image. Surfaces two independent signals in one place: a pHash match
    against the archive/staging corpus, and backfill.py's `backfill_uncertain`
    flag (which until now was written to the manifest but never displayed)."""
    lines = []
    thumb = None
    certain = False
    dropped_thumb = False

    if entry.get("backfill_uncertain"):
        # Deliberately does NOT set `certain`: this flag means backfill landed
        # in its 9-11 uncertain band, so amber is the honest colour. Only a
        # pHash hit at <= DUPLICATE_MAX escalates the panel to red.
        match_path = entry.get("backfill_match_path") or "?"
        distance = entry.get("backfill_match_distance")
        lines.append(
            f"**Flagged during backfill as a possible duplicate** "
            f"(distance {distance}) of `{match_path}`."
        )

    matches = find_duplicates(
        post_id, entry, manifest, archive_hashes, archive_path_map, archive_dir,
        exclude=_is_unseen_twin, indexed_paths=indexed_paths,
        # One artwork reaches the corpus as several rows, and which one
        # REPRESENTS it decides whether this banner can offer a reject. Without
        # this, the representative was just the nearest row, so a rejected twin
        # sitting nearer than the archived keeper hid the keeper and left a red
        # "Duplicate" banner with no button.
        prefer=_match_is_actionable,
    )

    # Which match the single thumbnail actually shows. Resolved BEFORE the
    # lines are written, because it is not always the nearest one: a match
    # whose file is gone (a rejected entry keeps its hash but not its image)
    # has nothing to preview, so the preview falls through to the next match.
    # Leaving that implicit is what let a "distance 0" line sit above a
    # picture of a different image entirely.
    thumb_index = None
    for index, match in enumerate(matches):
        if not match.image_path:
            continue
        candidate = _servable_thumb(match.image_path)
        if candidate is not None:
            thumb, thumb_index = candidate, index
            break
        dropped_thumb = True

    for index, match in enumerate(matches):
        if match.is_certain:
            certain = True
        # The two tiers must not read alike: red is a decision you can act on
        # in one click, amber explicitly asks you to look at both.
        label = "Duplicate" if match.is_certain else "Possibly related — review both"
        line = f"**{label}** (distance {match.distance}) — “{match.title}” · {match.where}"
        if index == thumb_index and len(matches) > 1:
            line += " — *shown below*"
        lines.append(line)

    if not lines:
        return gr.update(visible=False, elem_classes=[]), "", None, gr.update(visible=False)

    if thumb is None and dropped_thumb:
        # Say so rather than letting a missing preview read as "there was
        # never an image to show".
        lines.append("*(preview unavailable — the matched file couldn't be read)*")

    action_label = _duplicate_action_label(matches)
    if certain and action_label is None:
        # A certain duplicate with no reject button is legitimate -- the only
        # copy is one there's nothing to defer to (its file is already gone).
        # Say so: an unexplained missing button reads as the UI failing, which
        # is exactly how the collapse bug above used to present.
        lines.append(
            "*(no one-click reject — the matched copy's own file is already "
            "gone, so there's no keeper to defer to)*"
        )
    panel = "warn-panel" if certain else "maybe-panel"
    return (
        gr.update(visible=True, elem_classes=[panel]),
        "\n\n".join(lines),
        thumb,
        gr.update(
            visible=action_label is not None,
            value=action_label or "",
            # Same gate as Skip/Reject/Accept: no acting while the subreddit-map
            # confirm panel is waiting on an answer.
            interactive=pending_accept is None,
        ),
    )


def _undo_button():
    """The Undo control's state. While the confirm panel is open it is the way
    to back OUT of the pending accept, so it says so and is always clickable --
    a button reading "Undo last action" there invited exactly the mistake it
    used to make (reverting the previous entry, one picture back)."""
    if pending_accept is not None:
        return gr.update(value="Cancel pending accept", interactive=True)
    return gr.update(value="Undo last action", interactive=last_action is not None)


def _render_current(status=""):
    """Return a FULL repaint as {component: value}.

    Keyed by component object rather than positionally: Gradio maps such a dict
    into `outputs` order itself (convert_component_dict_to_list), so adding a
    control to the UI can no longer silently shift a hardcoded index and write
    a correct value into the wrong widget.

    EVERY one of the components in `outputs` is present on BOTH branches, on
    purpose. Gradio would happily fill an omitted one with skip(), but a partial
    repaint is what previously let typed field values silently revert -- so the
    dict is complete and the callers below override by name.

    The component names are module-level globals created in the `with
    gr.Blocks()` block far below. They resolve at CALL time, which is always
    after that block has executed, so defining this function first is fine.
    """
    entry = _current_entry()
    if entry is None:
        return {
            header_md: status or "All reviewed — nothing left in the queue.",
            review_group: gr.update(visible=False),
            image: None,
            title_md: "",
            meta_md: "",
            character_box: "",
            franchise_box: "",
            crossover_box: False,
            same_series_group_box: False,
            wallpaper_box: gr.update(choices=_wallpaper_choices([]), value="none"),
            undo_btn: _undo_button(),
            map_prompt_group: gr.update(visible=False, elem_classes=[]),
            map_prompt_md: "",
            map_prompt_radio: gr.update(choices=[], value=None),
            skip_btn: gr.update(interactive=pending_accept is None),
            reject_btn: gr.update(interactive=pending_accept is None),
            accept_btn: gr.update(interactive=pending_accept is None),
            known_series_box: False,
            oc_box: False,
            dup_group: gr.update(visible=False, elem_classes=[]),
            dup_md: "",
            dup_thumb_img: None,
            wallpaper_hint_md: "",
            dup_action_btn: gr.update(visible=False),
            char_alias_group: gr.update(visible=False, elem_classes=[]),
            char_alias_md: "",
            char_alias_dropdown: gr.update(choices=[], value=None),
        }

    header = f"Reviewing {current_index + 1} of {len(queue)}"
    if status:
        header = f"{header} — {status}"
    # Locals are named *_text / *_update, never after a component: the dict
    # below keys on the component objects, so a local of the same name would
    # shadow one and the key would silently become a plain string.
    title_text = f"### {entry.get('title', '')}"
    local_path = entry.get("local_path")
    image_path = str(Path(local_path).resolve()) if local_path else None
    permalink = entry.get("permalink", "")
    meta_text = (
        f"**Subreddit:** r/{entry.get('subreddit', '')}  \n"
        f"**Link:** [{permalink}]({permalink})  \n"
        f"**Guess confidence:** {entry.get('guess_confidence')} · "
        f"**source:** {entry.get('guess_source')}"
    )
    is_oc = not entry.get("franchise") and bool(_OC_TITLE_RE.search(entry.get("title", "")))
    if is_oc:
        meta_text += "\n\n**Original character (OC)** — will file to `Others/Artist's Original`."
    character_text = "\n".join(entry.get("character_guess", []))
    franchise_text = "\n".join(entry.get("franchise", []))
    crossover_value = bool(entry.get("crossover", False))
    same_series_group_value = bool(entry.get("same_series_group", False))
    wallpaper_value = entry.get("wallpaper", "none")
    known_series_value = entry.get("archive_override") == "known_series"
    oc_value = entry.get("archive_override") == "artist_original"

    # Hash/dims are normally already cached by the launch warm-up; this covers
    # an entry whose file appeared since (e.g. restored by Undo). Only write
    # the manifest when something was actually computed.
    if not (entry.get("phash") and entry.get("phash_bits") == hash_size * hash_size):
        if ensure_image_meta(entry, hash_size):
            save_manifest(manifest)
    suggested = suggest_wallpaper(entry.get("width"), entry.get("height"), wallpaper_rules)
    try:
        dup_group_update, dup_text, dup_thumb, dup_action = _duplicate_banner(
            queue[current_index], entry
        )
    except Exception as exc:
        # A duplicate warning is advisory. It shares one repaint with every
        # other control, so a raise here would cost the reviewer the whole
        # panel -- image, fields and buttons all stale. Degrade to no banner.
        print(f"  warning: duplicate check failed for {queue[current_index]}: {exc}")
        dup_group_update, dup_text, dup_thumb = gr.update(visible=False, elem_classes=[]), "", None
        dup_action = gr.update(visible=False)

    return {
        header_md: header,
        review_group: gr.update(visible=True),
        image: image_path,
        title_md: title_text,
        meta_md: meta_text,
        character_box: character_text,
        franchise_box: franchise_text,
        crossover_box: crossover_value,
        same_series_group_box: same_series_group_value,
        wallpaper_box: gr.update(choices=_wallpaper_choices(suggested), value=wallpaper_value),
        undo_btn: _undo_button(),
        map_prompt_group: gr.update(visible=False, elem_classes=[]),
        map_prompt_md: "",
        map_prompt_radio: gr.update(choices=[], value=None),
        skip_btn: gr.update(interactive=pending_accept is None),
        reject_btn: gr.update(interactive=pending_accept is None),
        accept_btn: gr.update(interactive=pending_accept is None),
        known_series_box: known_series_value,
        oc_box: oc_value,
        dup_group: dup_group_update,
        dup_md: dup_text,
        dup_thumb_img: dup_thumb,
        wallpaper_hint_md: _wallpaper_hint(entry, suggested),
        dup_action_btn: dup_action,
        char_alias_group: gr.update(
            visible=_alias_stage_open(),
            elem_classes=["confirm-panel"] if _alias_stage_open() else [],
        ),
        char_alias_md: _character_alias_prompt_md(pending_accept),
        char_alias_dropdown: gr.update(
            choices=(pending_accept or {}).get("alias_choices", []) or [],
            value=None,
        ),
    }


def on_skip():
    global current_index, last_action, pending_accept
    pending_accept = None
    entry = _current_entry()
    if entry is not None:
        last_action = {"type": "skip", "index": current_index}
        current_index += 1
    return _render_current()


def on_reject(status=""):
    global current_index, last_action, pending_accept
    pending_accept = None
    entry = _current_entry()
    if entry is None:
        return _render_current()

    prior = _snapshot(entry)
    local_path = Path(entry["local_path"])
    file_bytes = local_path.read_bytes() if local_path.exists() else None

    # Hash BEFORE deleting: this is the last moment the file exists, and
    # without a stored hash a rejected entry can never take part in duplicate
    # detection again (its file is gone for good).
    ensure_image_meta(entry, hash_size)

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
    return _render_current(status)


def on_reject_duplicate():
    """The banner's one-click reject. Deliberately just on_reject with a
    confirmation message: the decision ("this copy goes, the other stays") is
    identical to a normal Reject, so it must not grow a second code path --
    undo, hash-before-delete and queue advance all come along unchanged."""
    return on_reject("Rejected as a duplicate — the other copy is kept.")


def _alias_stage_open():
    """Whether the deferred accept is currently sitting on the alias panel."""
    return (pending_accept or {}).get("stage") == "character"


def _character_alias_candidate(franchise_list, character_list, crossover=False,
                               known_series=False, is_oc=False):
    """(folder, variant, choices) for a typed character name that resolves to no
    subfolder, or None when there is nothing to learn.

    The exclusions below are about ROUTING PRECEDENCE and NAME OWNERSHIP, not
    just franchise style. An alias is only worth learning when this character
    name is the thing that picks the folder, and when the roster it would be
    saved into is provably the one that name belongs to:

    * EXACTLY ONE franchise. A multi-franchise entry gets no prompt at all,
      because nothing here can tell which franchise a given name belongs to.
      Resolving every name against franchise #1's roster means a character
      owned by franchise #2 becomes the candidate and gets written into
      franchise #1's alias table -- a permanently wrong mapping that then
      misroutes every future image using that name. Multi-franchise entries do
      reach this function (they fail `eligible` in on_accept and fall through
      the not-eligible branch), so this is a live path, not a theoretical one.
    * NOT crossover. Precedence 1 sends the image to Crossover/ regardless of
      franchise or character, so the name never reaches the path.
    * NO archive_override. "File as Known Series" routes to
      Others/Known Series/ (precedence 6) and OC routes to
      Others/Artist's Original (precedence 7); in both the character name is
      likewise unused.
    * NESTED style with a non-empty roster. For flat/shortname the name never
      reaches the path either, and an alias has to point AT something.

    Matching goes through shortname.resolve_character, the same call archive.py
    routes with, so this can never offer to teach an alias for a name that
    already resolves (spacing/order variants included).
    """
    if not franchise_list or not character_list:
        return None
    if len(franchise_list) != 1 or crossover or known_series or is_oc:
        return None
    folder, franchise_def = franchise_folder_and_def(franchise_list[0], layout, series_aliases)
    if not folder or not franchise_def:
        return None
    if franchise_def.get("style") != "nested":
        return None
    choices = sorted(franchise_def.get("characters", []) or [])
    if not choices:
        return None
    for name in character_list:
        if resolve_character(folder, franchise_def, name, layout) is None:
            return folder, name, choices
    return None


def _character_alias_prompt_md(pending):
    if not pending or not pending.get("alias_variant"):
        return ""
    return (
        f"**“{pending['alias_variant']}”** doesn't match any character folder in "
        f"**{pending['alias_folder']}**.\n\n"
        f"Pick the folder it belongs to and save it, and every future image "
        f"tagged “{pending['alias_variant']}” files itself. Not saving files this "
        f"one image the same way but learns nothing."
    )


def _character_alias_panel(characters, franchise, crossover, same_series_group,
                           wallpaper, known_series, is_oc, status=""):
    """Re-render the current entry with the character-alias confirm panel open.

    Same shape as the subreddit-map panel's repaint: the typed values are put
    back into the boxes, because _render_current would otherwise redraw the
    stale pre-edit entry underneath an open confirm panel.

    `status` is threaded through rather than left to the caller because
    _render_current bakes it into the header, which this then keeps -- a caller
    that renders the panel and sets the header itself would have to know that.
    """
    render = _render_current(status)
    render[character_box] = "\n".join(characters)
    render[franchise_box] = "\n".join(franchise)
    render[crossover_box] = crossover
    render[same_series_group_box] = same_series_group
    render[wallpaper_box] = wallpaper
    render[known_series_box] = known_series
    render[oc_box] = is_oc
    return render


def _finalize_accept(entry, parsed, map_write, post_id, index, layout_snapshot=None):
    """Commit an accept. `post_id`/`index` identify the entry being accepted and
    are CAPTURED BY THE CALLER at the moment Accept was pressed -- never
    re-read from `current_index` here.

    That matters because the mapping-prompt path finalizes in a LATER Gradio
    event than the one that read the entry: sampling the global position at
    commit time can pair the entry actually mutated with a different entry's
    id, and Undo would then write one image's snapshot onto another.
    """
    global current_index, last_action, shortname_entries
    if parsed["edited"]:
        entry["guess_source"] = "manual"
    entry["character_guess"] = parsed["characters"]
    entry["franchise"] = parsed["franchise"]
    entry["crossover"] = parsed["crossover"]
    entry["same_series_group"] = parsed["same_series_group"]
    entry["wallpaper"] = parsed["wallpaper"]

    shortname_write = None
    status = ""
    if parsed["is_oc"]:
        # Human-asserted OC wins over the Known Series checkbox; routes to
        # Others/Artist's Original regardless of character/franchise fields.
        entry["archive_override"] = "artist_original"
    elif parsed["known_series"]:
        primary_franchise = parsed["franchise"][0] if parsed["franchise"] else None
        code = match_shortname(primary_franchise, shortname_entries, series_aliases) if primary_franchise else None
        if primary_franchise and not code:
            shortname_path = Path(layout["shortname_file"])
            proposed = propose_shortname_code(primary_franchise, shortname_entries)
            collision = find_shortname_collision(shortname_path, proposed, primary_franchise)
            if collision:
                status = (
                    f"Known Series NOT applied: code '{proposed}' is already used by "
                    f"'{collision}'. Fix it in resolve.py."
                )
            else:
                prior_text = shortname_path.read_text(encoding="utf-8") if shortname_path.exists() else None
                save_shortname_entry(shortname_path, proposed, primary_franchise)
                if verify_shortname_entry(shortname_path, proposed, primary_franchise):
                    code = proposed
                    shortname_entries = load_shortname_map(layout)
                    shortname_write = {
                        "path": shortname_path,
                        "existed_before": prior_text is not None,
                        "snapshot": prior_text,
                    }
                else:
                    status = "Known Series NOT applied: the shortname save didn't persist to disk."
        if code:
            entry["archive_override"] = "known_series"
        else:
            # Never silently swallow a ticked checkbox -- if nothing above
            # produced a code, say so rather than dropping the override.
            entry.pop("archive_override", None)
            if primary_franchise and not status:
                status = f"Known Series NOT applied: no shortname could be resolved for '{primary_franchise}'."
            elif not primary_franchise:
                status = "Known Series NOT applied: this entry has no franchise tag."
    elif entry.get("archive_override") in ("known_series", "artist_original"):
        entry.pop("archive_override", None)

    entry["status"] = "approved"
    save_manifest(manifest)

    last_action = {
        "type": "accept",
        "index": index,
        "post_id": post_id,
        "prior": parsed["prior"],
        "map_write": map_write,
        "shortname_write": shortname_write,
        # Deep copy of layout.json taken BEFORE a character alias was learned,
        # or None when none was. Undo restores it wholesale, same as resolve.py.
        "layout_snapshot": layout_snapshot,
    }
    # Resume after the entry that was just accepted, which is not necessarily
    # "one past wherever the cursor happens to be" on the deferred path.
    current_index = index + 1
    return status


def on_accept(character_text, franchise_text, crossover_value, same_series_group_value, wallpaper_value,
              known_series_value, oc_value):
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
    new_known_series = bool(known_series_value)
    new_oc = bool(oc_value)

    # same_series_group/wallpaper are routing hints, not identity edits --
    # they don't flip guess_source to "manual". Asserting OC (like crossover)
    # is an identity edit, so it does.
    prior_oc = prior["archive_override"] == "artist_original"
    edited = (
        new_characters != prior["character_guess"]
        or new_franchise != prior["franchise"]
        or new_crossover != prior["crossover"]
        or new_oc != prior_oc
    )

    parsed = {
        "prior": prior,
        "characters": new_characters,
        "franchise": new_franchise,
        "crossover": new_crossover,
        "same_series_group": new_same_series_group,
        "wallpaper": new_wallpaper,
        "known_series": new_known_series,
        "is_oc": new_oc,
        "edited": edited,
    }

    subreddit = entry.get("subreddit", "")
    single_franchise = len(new_franchise) == 1
    eligible = not subreddit_is_mapped(subreddit) and single_franchise

    if not eligible:
        # No subreddit question to ask -- but a character alias may still be
        # worth learning, and that panel defers the accept the same way.
        alias = _character_alias_candidate(
            new_franchise, new_characters, crossover=new_crossover,
            known_series=new_known_series, is_oc=new_oc,
        )
        if alias:
            pending_accept = {
                "entry": entry, "parsed": parsed, "subreddit": subreddit,
                "franchise": None, "character": None,
                "post_id": queue[current_index], "index": current_index,
                "map_write": None, "stage": "character",
                "alias_folder": alias[0], "alias_variant": alias[1], "alias_choices": alias[2],
            }
            return _character_alias_panel(new_characters, new_franchise, new_crossover,
                                          new_same_series_group, new_wallpaper,
                                          new_known_series, new_oc)
        status = _finalize_accept(entry, parsed, map_write=None,
                                  post_id=queue[current_index], index=current_index)
        return _render_current(status)

    single_character = len(new_characters) == 1
    franchise = new_franchise[0]
    character = new_characters[0] if single_character else None
    allow_both = single_character and not new_crossover and not new_same_series_group

    # post_id/index are captured HERE, with the entry, because the accept is
    # finalized in a later event (the confirm click). See _finalize_accept.
    pending_accept = {"entry": entry, "parsed": parsed, "subreddit": subreddit,
                       "franchise": franchise, "character": character,
                       "post_id": queue[current_index], "index": current_index,
                       "map_write": None, "stage": "subreddit"}

    choices = []
    if allow_both:
        choices.append((f'Always map r/{subreddit} to franchise "{franchise}" and character "{character}"', "both"))
    choices.append((f'Always map r/{subreddit} to franchise "{franchise}" only', "franchise_only"))
    choices.append(("Just this once (don't remember)", "once"))

    # Keep the textboxes showing what the user just typed (not the stale
    # pre-edit entry values _render_current() would otherwise re-render).
    render = _render_current()
    render[character_box] = "\n".join(new_characters)
    render[franchise_box] = "\n".join(new_franchise)
    render[crossover_box] = new_crossover
    render[same_series_group_box] = new_same_series_group
    render[wallpaper_box] = new_wallpaper
    render[map_prompt_group] = gr.update(visible=True, elem_classes=["confirm-panel"])
    render[map_prompt_md] = f"r/{subreddit} isn't mapped yet. Remember this for future posts?"
    render[map_prompt_radio] = gr.update(choices=choices, value="once")
    render[known_series_box] = new_known_series
    render[oc_box] = new_oc
    return render


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

    # Chain, don't finalize: the two questions are independent, so a subreddit
    # answer must not swallow an unlearned character name. The accept stays
    # deferred through the second panel and still commits at one point.
    parsed = pending["parsed"]
    alias = _character_alias_candidate(
        parsed["franchise"], parsed["characters"], crossover=parsed["crossover"],
        known_series=parsed["known_series"], is_oc=parsed["is_oc"],
    )
    if alias:
        pending_accept = {
            **pending, "map_write": map_write, "stage": "character",
            "alias_folder": alias[0], "alias_variant": alias[1], "alias_choices": alias[2],
        }
        return _character_alias_panel(
            parsed["characters"], parsed["franchise"], parsed["crossover"],
            parsed["same_series_group"], parsed["wallpaper"],
            parsed["known_series"], parsed["is_oc"],
        )

    status = _finalize_accept(pending["entry"], parsed, map_write=map_write,
                              post_id=pending["post_id"], index=pending["index"])
    return _render_current(status)


def _commit_pending_accept(layout_snapshot=None):
    """Finish the deferred accept held in pending_accept."""
    global pending_accept
    pending = pending_accept
    pending_accept = None
    if pending is None:
        return _render_current()
    status = _finalize_accept(
        pending["entry"], pending["parsed"], map_write=pending.get("map_write"),
        post_id=pending["post_id"], index=pending["index"],
        layout_snapshot=layout_snapshot,
    )
    return _render_current(status)


def on_character_alias_save(selected_character):
    """Learn variant -> canonical for this franchise, then finish the accept.

    Explicit confirm only: nothing is written until this button. The snapshot is
    taken BEFORE the write and handed to _finalize_accept, so one Undo reverts
    both the alias and the accept -- they were one user action.
    """
    pending = pending_accept
    if pending is None or pending.get("stage") != "character":
        return _render_current()
    selected = (selected_character or "").strip()
    if not selected:
        # Stay on the panel rather than silently falling through to "don't
        # save": the reviewer asked to save and gave nothing to save it as.
        # Repaint through _character_alias_panel, not _render_current: the
        # latter redraws every field from the ENTRY, which still holds the
        # pre-edit values (the accept is deferred and hasn't written them), so
        # a plain re-render would silently discard whatever was typed while
        # leaving the panel open on top of it.
        parsed = pending["parsed"]
        return _character_alias_panel(
            parsed["characters"], parsed["franchise"], parsed["crossover"],
            parsed["same_series_group"], parsed["wallpaper"],
            parsed["known_series"], parsed["is_oc"],
            status="Pick the character folder first, or choose \"Just this once\".",
        )
    snapshot = copy.deepcopy(layout)
    save_character_alias(pending["alias_folder"], pending["alias_variant"], selected, layout)
    return _commit_pending_accept(layout_snapshot=snapshot)


def on_character_alias_skip():
    """File this one image and learn nothing -- no layout.json write at all."""
    return _commit_pending_accept()


def on_undo():
    global current_index, last_action, pending_accept, shortname_entries

    if pending_accept is not None:
        # The confirm panel is open, so the accept hasn't been written yet --
        # here Undo means "cancel that", and it must stay on THIS entry.
        # Previously it fell through and reverted the PREVIOUS entry's action
        # instead, jumping back a picture and silently dropping the pending
        # accept: the one way to leave this screen on the wrong image.
        pending_accept = None
        return _render_current("Accept cancelled — nothing was saved.")

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
        if action["prior"].get("archive_override") is None:
            entry.pop("archive_override", None)
        save_manifest(manifest)
        if action.get("map_write"):
            remove_subreddit_map_entry(action["map_write"])
        if action.get("shortname_write"):
            sw = action["shortname_write"]
            undo_shortname_write(sw["path"], sw["existed_before"], sw["snapshot"])
            shortname_entries = load_shortname_map(layout)
        if action.get("layout_snapshot") is not None:
            # A character alias was learned as part of this accept -- unlearn it.
            # Mutate in place: layout is shared by reference with the helpers
            # that read it, so rebinding the global would leave them stale.
            layout.clear()
            layout.update(action["layout_snapshot"])
            save_layout(layout)
        current_index = action["index"]

    return _render_current()


# ---------------------------------------------------------------------------
# Settings tab: subreddit_map.json editor
#
# Scope is deliberately this one file. layout.json's aliases, series_aliases.json
# and the shortname file each have different write semantics (per-franchise
# scoping, alias-chain resolution, a line-oriented text format) and are not
# editable here.
#
# Nothing is cached. tagger._load_subreddit_map re-reads from disk on every
# lookup, so an edit made here applies to the next tagged post with no
# invalidation step -- and these handlers likewise re-read before every render,
# so the form can never show a stale file.
# ---------------------------------------------------------------------------

# Keys the form owns. Anything else on an entry (`_note`) is shown read-only and
# preserved by save_subreddit_map_entry's merge.
_SETTINGS_FORM_KEYS = ("franchise", "character")


def _settings_raw():
    """The file as-is, including `_comment`. Returns {} when it doesn't exist --
    read_subreddit_map raises rather than exiting, which matters here because
    these run inside the server process and sys.exit would take down every tab.

    A malformed file is deliberately NOT caught: Gradio surfaces the exception
    without killing the server, whereas swallowing it would show an empty
    editor over a file that still has 167 entries in it."""
    try:
        return read_subreddit_map()
    except FileNotFoundError:
        return {}


def _settings_keys(raw=None):
    """Entry keys only. `_comment` is file-level documentation, not a subreddit,
    so it is never offered for editing -- but it is never dropped either; every
    write round-trips the raw file."""
    raw = _settings_raw() if raw is None else raw
    return sorted(k for k in raw if not k.startswith("_"))


def _settings_extras_md(entry):
    """Read-only view of the keys the form doesn't own. Shown because they are
    invisible otherwise and a save that silently dropped them is exactly the bug
    this tab had to fix first."""
    if not isinstance(entry, dict):
        return ""
    extras = {k: v for k, v in entry.items() if k not in _SETTINGS_FORM_KEYS}
    if not extras:
        return ""
    lines = ["**Other keys on this entry** — kept as-is when you save:", ""]
    for key, value in extras.items():
        lines.append(f"- `{key}`: {value}")
    return "\n".join(lines)


def _settings_render(selected=None, status="", key_text=None):
    """Full repaint of the Settings form as {component: value}.

    Same discipline as _render_current: every Settings component is present on
    every branch, addressed by component object rather than position, so adding
    a control here can't silently shift an index."""
    raw = _settings_raw()
    keys = _settings_keys(raw)
    entry = raw.get(selected) if selected else None
    if not isinstance(entry, dict):
        entry = None

    franchise = entry.get("franchise") if entry else None
    # None is a real, meaningful value here (parse the franchise from the
    # title), so it drives the checkbox rather than emptying the textbox.
    is_null = entry is not None and "franchise" in entry and franchise is None
    return {
        settings_dropdown: gr.update(choices=keys, value=selected),
        settings_key_box: key_text if key_text is not None else (selected or ""),
        settings_franchise_box: gr.update(
            value="" if franchise is None else str(franchise),
            interactive=not is_null,
        ),
        settings_null_box: is_null,
        settings_character_box: (entry or {}).get("character") or "",
        settings_extras_md: _settings_extras_md(entry),
        settings_status_md: status,
        settings_count_md: f"**{len(keys)}** subreddit entries.",
    }


def on_settings_select(selected):
    return _settings_render(selected=selected)


def on_settings_add():
    return _settings_render(
        selected=None, key_text="",
        status="Type a subreddit key, set its franchise, then Save. "
               "New entries are appended to the end of the file.",
    )


def on_settings_null_toggle(is_null):
    """A null franchise and an empty one are different things in this file, so
    the control that chooses between them also greys out the textbox -- an
    editable box whose contents are about to be ignored is a trap."""
    return gr.update(interactive=not is_null)


def on_settings_save(key_text, franchise_text, is_null, character_text, selected):
    key = normalize_subreddit(key_text or "")
    if not key:
        return _settings_render(selected=selected, key_text=key_text,
                                status="⚠️ Subreddit key is empty — nothing saved.")

    franchise_text = (franchise_text or "").strip()
    if is_null:
        franchise = None
    elif not franchise_text:
        # Refusing beats guessing: "" and null mean different things and only
        # the user knows which one was meant.
        return _settings_render(
            selected=selected, key_text=key_text,
            status="⚠️ Franchise is empty — nothing saved. Type a franchise, or "
                   "tick **No franchise** to write `null` (franchise parsed from the title).",
        )
    else:
        franchise = franchise_text
    character = (character_text or "").strip() or None

    raw_before = _settings_raw()
    renamed_from = selected if selected and selected != key else None
    existed = key in raw_before

    if renamed_from:
        remove_subreddit_map_entry(renamed_from)
    save_subreddit_map_entry(key, franchise, character)

    raw_after = _settings_raw()
    kept = [k for k in (raw_after.get(key) or {}) if k not in _SETTINGS_FORM_KEYS]

    shown_franchise = "`null` (parsed from title)" if franchise is None else f"“{franchise}”"
    if renamed_from:
        action = (f"Renamed **{renamed_from}** → **{key}**. Note: a rename is a "
                  f"remove + re-add, so **{key}** now sits at the END of the file "
                  f"rather than in its old position.")
    elif existed:
        action = f"Updated **{key}**."
    else:
        action = f"Added **{key}** at the end of the file."

    detail = f"franchise = {shown_franchise}"
    detail += f", character = “{character}”" if character else ", no character"
    if kept:
        detail += f". Preserved: {', '.join('`' + k + '`' for k in kept)}"

    return _settings_render(
        selected=key, status=f"✅ {action} {detail}.",
    )


def on_settings_delete(selected):
    if not selected:
        return _settings_render(selected=None,
                                status="⚠️ Nothing selected — nothing deleted.")
    raw = _settings_raw()
    if selected not in raw:
        return _settings_render(selected=None,
                                status=f"⚠️ **{selected}** isn't in the file — nothing deleted.")
    remove_subreddit_map_entry(selected)
    return _settings_render(
        selected=None, key_text="",
        status=f"🗑️ Deleted **{selected}**.",
    )


with gr.Blocks(title="Oshiire review") as demo:
  with gr.Tabs():
   # Indentation inside the tabs is deliberately shallow (1 space per level):
   # wrapping the existing panel in a Tab must not reflow every line of it,
   # because the diff of a re-indented block hides whether anything else moved.
   # The component order below, the `outputs` list and all nine event wirings
   # are unchanged -- only the nesting is new.
   with gr.Tab("Review"):
    header_md = gr.Markdown()

    with gr.Group() as review_group:
        with gr.Group(visible=False) as dup_group:
            dup_md = gr.Markdown()
            dup_thumb_img = gr.Image(
                interactive=False, show_label=False, elem_id="dup-thumb", height=200
            )
            # Below the thumbnail on purpose: eyeball the match, then act.
            # Only ever shown for a certain (red) match -- see
            # _duplicate_action_label.
            dup_action_btn = gr.Button(visible=False, variant="stop")
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
                oc_box = gr.Checkbox(label="Original character (OC)")
                wallpaper_hint_md = gr.Markdown()
                wallpaper_box = gr.Radio(
                    choices=_wallpaper_choices([]), value="none", label="Wallpaper"
                )
                # Looks redundant now that archive.py falls back to the
                # shortname file on its own, and for MOST uses it is: a
                # franchise with no layout.json folder but a shortname entry
                # routes itself. It is kept because that fallback is only
                # reached when franchise resolution FAILS -- so this checkbox
                # is the one way to say "file this image under Others/Known
                # Series even though its franchise does have a folder", a case
                # no other screen can express (such an entry never flags, so
                # resolve.py never sees it either).
                known_series_box = gr.Checkbox(label="File as Known Series (shortname)")

                with gr.Row():
                    skip_btn = gr.Button("Skip")
                    reject_btn = gr.Button("Reject", variant="stop")
                    accept_btn = gr.Button("Accept", variant="primary")

                undo_btn = gr.Button("Undo last action")

                with gr.Group(visible=False) as map_prompt_group:
                    map_prompt_md = gr.Markdown()
                    map_prompt_radio = gr.Radio(choices=[], value=None, label="Remember this subreddit?")
                    map_prompt_confirm_btn = gr.Button("Confirm")

                # Shown after the subreddit panel (they are independent
                # questions, so answering one must not swallow the other).
                with gr.Group(visible=False) as char_alias_group:
                    char_alias_md = gr.Markdown()
                    # filterable + custom value, matching resolve.py's series
                    # dropdown -- a long roster is unusable as a plain select.
                    char_alias_dropdown = gr.Dropdown(
                        choices=[], value=None, label="Files under",
                        allow_custom_value=True, filterable=True,
                        info="Type to filter. The target is a FOLDER, which may be "
                             "a grouping (e.g. \"Boys\") rather than a character.",
                    )
                    with gr.Row():
                        char_alias_save_btn = gr.Button("Save alternate name", variant="primary")
                        char_alias_skip_btn = gr.Button("Just file this image, don't save")

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
        skip_btn,
        reject_btn,
        accept_btn,
        known_series_box,
        oc_box,
        dup_group,
        dup_md,
        dup_thumb_img,
        wallpaper_hint_md,
        dup_action_btn,
        char_alias_group,
        char_alias_md,
        char_alias_dropdown,
    ]

    demo.load(fn=_render_current, outputs=outputs)
    skip_btn.click(fn=on_skip, outputs=outputs)
    reject_btn.click(fn=on_reject, outputs=outputs)
    accept_btn.click(
        fn=on_accept,
        inputs=[character_box, franchise_box, crossover_box, same_series_group_box, wallpaper_box,
                known_series_box, oc_box],
        outputs=outputs,
    )
    map_prompt_confirm_btn.click(fn=on_map_prompt_confirm, inputs=[map_prompt_radio], outputs=outputs)
    char_alias_save_btn.click(fn=on_character_alias_save, inputs=[char_alias_dropdown], outputs=outputs)
    char_alias_skip_btn.click(fn=on_character_alias_skip, outputs=outputs)
    dup_action_btn.click(fn=on_reject_duplicate, outputs=outputs)
    undo_btn.click(fn=on_undo, outputs=outputs)

   with gr.Tab("Settings") as settings_tab:
    gr.Markdown(
        "### subreddit_map.json\n"
        "The lookup that turns a subreddit into a franchise (and, for "
        "character-specific subs, a character). Edits apply to the next tagged "
        "post immediately — nothing caches this file."
    )
    settings_count_md = gr.Markdown()
    # Choices are set HERE as well as by the load event, and allow_custom_value
    # is on, because Dropdown.preprocess validates the submitted value against
    # the SERVER-side component's choices -- and gr.update(choices=...) only
    # repaints the client. Built with choices=[], every selection came back
    # "Value: asuka is not in the list of choices: []". Seeding them covers the
    # entries that exist at launch; allow_custom_value covers the ones added
    # through this tab afterwards, which the server object still won't know
    # about. (Same reason char_alias_dropdown sets it.)
    settings_dropdown = gr.Dropdown(
        choices=_settings_keys(), value=None, label="Subreddit", filterable=True,
        allow_custom_value=True,
        info="Type to filter.",
    )
    settings_key_box = gr.Textbox(
        label="Subreddit key", max_lines=1,
        info="Lowercased on save. Changing this renames the entry.",
    )
    settings_franchise_box = gr.Textbox(
        label="Franchise", max_lines=1,
        info="The source work this sub is about.",
    )
    # A null franchise is a SHAPE, not a missing value: it marks a sub whose
    # franchise comes from the title. Seven entries rely on it, so the UI has
    # to let the user say "null" distinctly from "empty".
    settings_null_box = gr.Checkbox(
        label="No franchise — parse it from the post title (writes null)",
    )
    settings_character_box = gr.Textbox(
        label="Character", max_lines=1,
        info="Only for character-specific subs. Leave empty to clear it.",
    )
    settings_extras_md = gr.Markdown()
    with gr.Row():
        settings_save_btn = gr.Button("Save", variant="primary")
        settings_delete_btn = gr.Button("Delete", variant="stop")
        settings_add_btn = gr.Button("Add new")
    settings_status_md = gr.Markdown()

    # Registration list for the Settings form, mirroring `outputs` above. The
    # handlers return {component: value} dicts, so this list only declares
    # which components a Settings event may repaint -- never an order the
    # handlers have to match.
    settings_outputs = [
        settings_dropdown,
        settings_key_box,
        settings_franchise_box,
        settings_null_box,
        settings_character_box,
        settings_extras_md,
        settings_status_md,
        settings_count_md,
    ]

    # Populated when the tab is OPENED, not by a second demo.load at startup.
    # The panel reads subreddit_map.json, and that file changes underneath it:
    # the Review tab's own subreddit-map confirm panel writes to it. A
    # once-at-launch population would show a stale map for the rest of the
    # session, so refreshing on open is the correct trigger independently of
    # any startup-timing concern. It also keeps the panel off the startup path
    # entirely -- opening the tab is always what fills it in.
    #
    # No `inputs`: gradio injects SelectData only into a parameter type-hinted
    # for it, and _settings_render has no hints, so this calls it with no
    # arguments.
    settings_tab.select(fn=_settings_render, outputs=settings_outputs)
    # .input(), not .change(): .change() also fires when a handler sets the
    # value programmatically, so every Save -- which re-selects the saved key --
    # would immediately re-render with an empty status and swallow its own
    # confirmation message.
    settings_dropdown.input(
        fn=on_settings_select, inputs=[settings_dropdown], outputs=settings_outputs
    )
    settings_null_box.input(
        fn=on_settings_null_toggle, inputs=[settings_null_box],
        outputs=[settings_franchise_box],
    )
    settings_save_btn.click(
        fn=on_settings_save,
        inputs=[settings_key_box, settings_franchise_box, settings_null_box,
                settings_character_box, settings_dropdown],
        outputs=settings_outputs,
    )
    settings_delete_btn.click(
        fn=on_settings_delete, inputs=[settings_dropdown], outputs=settings_outputs
    )
    settings_add_btn.click(fn=on_settings_add, outputs=settings_outputs)


if __name__ == "__main__":
    # A duplicate's thumbnail can live in ARCHIVE_DIR, which is outside the
    # project tree; Gradio serves only cwd/temp by default and raises
    # InvalidPathError otherwise. Read-only -- the UI never writes there.
    allowed_paths = [str(archive_dir)] if archive_dir else []
    demo.launch(css=CUSTOM_CSS, inbrowser=True, allowed_paths=allowed_paths)
