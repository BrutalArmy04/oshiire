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
from functools import partial
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv
from starlette.exceptions import StarletteDeprecationWarning

# The reconcile engine, imported for its FUNCTIONS -- build_plan/apply_plan --
# never by shelling out to its CLI. sync.py itself imports nothing that talks
# to the network (archive, hash_index, manifest, shortname), so pulling it in
# here does not breach the UI's no-network invariant; and its dry-run path
# writes nothing at all, which is what makes Scan safe to run from a button.
import sync
from hash_index import DEFAULT_DB
from manifest import display_permalink, load_manifest, save_manifest
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
    is_alias_dismissed,
    is_group_routed,
    load_layout,
    load_shortname_map,
    load_series_aliases,
    load_wallpaper_rules,
    match_shortname,
    merge_character,
    normalize_name_key,
    promote_character,
    resolve_character,
    save_character_alias,
    save_character_alias_dismissal,
    save_character_group_route,
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
    # Display-only host normalization; the manifest keeps its own spelling
    # (see manifest.display_permalink).
    permalink = display_permalink(entry.get("permalink", ""))
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
                               known_series=False, is_oc=False,
                               same_series_group=False):
    """(folder, variant, choices) for a typed character name that resolves to no
    subfolder, or None when there is nothing to learn.

    Two kinds of exclusion, and the difference between them is the thing to
    hold onto -- they are NOT interchangeable, and reasoning that fits one does
    not transfer to the other.

    STRUCTURAL: this prompt cannot be asked safely or answered meaningfully.

    * EXACTLY ONE franchise. A multi-franchise entry gets no prompt at all,
      because nothing here can tell which franchise a given name belongs to.
      Resolving every name against franchise #1's roster means a character
      owned by franchise #2 becomes the candidate and gets written into
      franchise #1's alias table -- a permanently wrong mapping that then
      misroutes every future image using that name. Multi-franchise entries do
      reach this function (they fail `eligible` in on_accept and fall through
      the not-eligible branch), so this is a live path, not a theoretical one.
    * NESTED style with a non-empty roster. For flat/shortname the name never
      reaches the path, and an alias has to point AT something.

    DECIDED: the reviewer has already answered the routing question on this
    screen, so asking a second time is noise. Each of these is a control the
    reviewer set (or left set) before pressing Accept:

    * Crossover -> Crossover/ (precedence 1).
    * Original character -> Others/Artist's Original/ (precedence 7).
    * File as Known Series -> Others/Known Series/ (precedence 6).
    * Same-series group -> <Franchise>/Others_Group/ (precedence 4). This was
      the one missing: an unmatched name on a group shot is EXPECTED, and
      prompting anyway put a click on every group shot.

    The DECIDED set is about the DECISION, not about where the image lands --
    which is why `len(identities) >= 2` is deliberately NOT here even though it
    reaches the very same group_dir return in archive.route_entry as the
    same-series-group checkbox does. Nobody decided that: it is inferred from
    the tags, and the tags are exactly what this prompt is for correcting. A
    multi-name entry is where a name is MOST likely to be wrong, so it is the
    last place to go quiet. Do not "finish the job" by excluding it for
    symmetry with the checkbox -- the symmetry is in the destination only.

    Matching goes through shortname.resolve_character, the same call archive.py
    routes with, so this can never offer to teach an alias for a name that
    already resolves (spacing/order variants included).

    A name the reviewer has already answered PERSISTENTLY is skipped too --
    either pinned to the group folder or explicitly dismissed. Both answers are
    stored per franchise folder in layout.json and matched by the same rule
    resolve_character uses, so an answer given under one spelling still holds
    when the next post tags the character differently. Without this the prompt
    had no memory of being declined and re-offered the same name forever.
    """
    if not franchise_list or not character_list:
        return None
    if len(franchise_list) != 1 or crossover or known_series or is_oc or same_series_group:
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
        if is_group_routed(folder, name, layout) or is_alias_dismissed(folder, name, layout):
            continue
        if resolve_character(folder, franchise_def, name, layout) is None:
            return folder, name, choices
    return None


def _character_alias_prompt_md(pending):
    """The four answers, described by what each one actually does.

    Two of them decide where the image files and two don't, and that split is
    the thing the reviewer has to see: "stop asking" is a decision about the
    PROMPT, not about routing, and reading it as "ignore this name" would be
    read as "file it somewhere sensible" -- which nothing here does.
    """
    if not pending or not pending.get("alias_variant"):
        return ""
    name = pending["alias_variant"]
    folder = pending["alias_folder"]
    group = layout.get("group_subfolder", "Others_Group")
    root_fallback = (layout.get("franchises", {}).get(folder) or {}).get("fallback") == "root"
    unfiled = (
        f"it still files into **{folder}** itself, because that franchise sets "
        f"`\"fallback\": \"root\"`"
        if root_fallback else
        f"archive.py will still flag it as needing a folder in **{folder}**"
    )
    return (
        f"**“{name}”** doesn't match any character folder in **{folder}**.\n\n"
        f"**Save alternate name** — pick the folder below; this image and every "
        f"future one tagged “{name}” files there.\n\n"
        f"**Always file to {group}** — “{name}” goes to "
        f"**{folder}/{group}** from now on, without naming a character folder.\n\n"
        f"**Just file this image, don't save** — accepts this image and remembers "
        f"nothing, so the next image tagged “{name}” asks again.\n\n"
        f"**Stop asking about this name** — retires this prompt for “{name}” in "
        f"**{folder}** and nothing more. It does *not* decide where the image "
        f"files: {unfiled}."
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
            same_series_group=new_same_series_group,
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
        same_series_group=parsed["same_series_group"],
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
    """File this one image and learn nothing -- no layout.json write at all.
    The name is offered again on the next image that carries it; the two
    persistent answers below are how that stops."""
    return _commit_pending_accept()


def _persistent_alias_answer(writer):
    """Shared body of the two persistent answers to the alias prompt.

    Both follow on_character_alias_save's undo discipline exactly: the layout is
    deep-copied BEFORE the write and handed to _finalize_accept, so one Undo
    reverts the layout write and the accept together -- they were one click.
    """
    pending = pending_accept
    if pending is None or pending.get("stage") != "character":
        return _render_current()
    snapshot = copy.deepcopy(layout)
    writer(pending["alias_folder"], pending["alias_variant"], layout)
    return _commit_pending_accept(layout_snapshot=snapshot)


def on_character_alias_group_route():
    """Pin this name to the franchise's group subfolder, then finish the accept.

    Unlike a dismissal this DOES decide routing: archive.py honours the record
    ahead of a roster match, so the image files without a character folder ever
    being created for the name."""
    return _persistent_alias_answer(save_character_group_route)


def on_character_alias_dismiss():
    """Stop prompting about this name, then finish the accept.

    Review-side only, deliberately: the reviewer said the question is noise,
    not that they'd decided where the image goes. Routing is unchanged, so on a
    nested franchise without `"fallback": "root"` archive.py still flags it."""
    return _persistent_alias_answer(save_character_alias_dismissal)


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


# ---------------------------------------------------------------------------
# Settings tab: Sync -- reconcile the manifest with an archive the user has
# reorganised by hand, and show sync.py's layout-health audit.
#
# The engine is sync.py and it is called as a LIBRARY (build_plan / apply_plan),
# never as a subprocess: the plan object carries the buckets, the untracked
# list and the audit findings that this tab renders, and a CLI would hand back
# only the printed report.
#
# The split this tab exists to make obvious: Scan is build_plan, which reads
# file NAMES and writes nothing whatsoever; Apply is apply_plan, which rewrites
# archive_path for the MOVED_OK bucket only and re-keys the index alongside.
# Neither ever moves, copies or deletes a file -- moving files stays something
# the user does in their file browser.
#
# Both run against review.py's OWN in-memory manifest, not a fresh load. That
# is deliberate: apply_plan commits with one save_manifest over the dict it was
# given, so handing it a second copy would write back a manifest missing every
# decision the Review tab has made this session.
# ---------------------------------------------------------------------------

# Module-level so a test can point it at a sandbox. The db is only ever
# re-keyed, never created -- move_indexed_file returns False for an absent one.
sync_db_path = DEFAULT_DB

# The last Scan's SyncPlan, or None. This is the whole gate on Apply: a plan is
# the only thing that says which entries are unambiguously repairable, and one
# built before the user's last round of file moves would rewrite archive_path
# to a location that is no longer true. Cleared again after every Apply.
sync_plan = None


def _sync_counts_md(plan):
    """The bucket table, in sync.py's own report order and with its own labels
    -- the CLI and this tab must never disagree about what a bucket is called."""
    counts = plan.counts()
    lines = ["| | |", "|---|---:|"]
    for bucket in sync.BUCKET_ORDER:
        lines.append(f"| {sync.BUCKET_LABELS[bucket]} | {counts.get(bucket, 0):,} |")
    lines.append(f"| {sync.UNTRACKED_LABEL} | {len(plan.untracked):,} |")
    return "\n".join(lines)


def _sync_report_md(plan):
    """The full dry-run report as markdown: counts, every repairable move, every
    item needing a human, the untracked count and the layout audit.

    The MOVED_OK list is shown in FULL rather than truncated. It is the exact
    set Apply is about to rewrite, and a reviewer who can only see the first
    ten of them is approving the rest blind.
    """
    index_note = "" if Path(sync_db_path).exists() else "  _(not built)_"
    parts = [
        f"**Archive:** `{archive_dir}`",
        f"**Index:** `{sync_db_path}`{index_note}",
    ]
    if plan.scope:
        parts.append(f"**Scope:** `{plan.scope}/`")
    parts.append(f"**Images on disk:** {plan.disk_files:,}")
    parts.append("")
    parts.append("#### Archived manifest entries")
    parts.append(_sync_counts_md(plan))

    moved_ok = plan.by_bucket(sync.MOVED_OK)
    parts.append("")
    parts.append(f"#### Repairable moves ({len(moved_ok)})")
    if moved_ok:
        parts.append("Apply rewrites these, and only these.")
        parts.append("")
        for item in moved_ok:
            parts.append(f"- `{item.key}`  \n  `{item.old_rel}`  \n  → `{item.new_rel}`")
    else:
        parts.append("_None._")

    attention = [item for bucket in sync.ATTENTION_BUCKETS for item in plan.by_bucket(bucket)]
    parts.append("")
    parts.append(f"#### Needs your decision ({len(attention)})")
    if attention:
        parts.append("Never applied automatically — each of these is ambiguous in "
                     "a way no rule can settle.")
        parts.append("")
        for item in attention:
            line = f"- **[{sync.BUCKET_LABELS[item.bucket]}]** `{item.key}` (`{item.old_rel}`)"
            if item.detail:
                line += f"  \n  {item.detail}"
            parts.append(line)
    else:
        parts.append("_None._")

    # Count only, deliberately: untracked files predate oshiire and are none of
    # its business. Listing thousands of them would bury the two sections above.
    parts.append("")
    parts.append(f"#### Untracked files ({len(plan.untracked):,})")
    parts.append("Images under the archive that oshiire did not put there. "
                 "Nothing here is ever touched.")

    parts.append("")
    parts.append(f"#### layout.json health ({len(plan.audit)} finding(s))")
    if plan.audit:
        parts.append("Advisory only — nothing is ever written for these.")
        parts.append("")
        for finding in plan.audit:
            parts.append(
                f"- **[{finding.kind}]** {finding.franchise}: "
                f"`{finding.alias}` → `{finding.target}`  \n  {finding.detail}"
            )
    else:
        parts.append("_No findings._")

    return "\n".join(parts)


def _sync_render(report="", status="", enable_apply=False):
    """Full repaint of the Sync panel as {component: value} -- same discipline
    as _settings_render: every component present on every branch, addressed by
    component object rather than position."""
    return {
        sync_report_md: report,
        sync_status_md: status,
        sync_apply_btn: gr.update(interactive=enable_apply),
    }


def _sync_tab_open():
    """Opening the tab clears any earlier plan, so Apply starts disabled.

    A plan is a statement about the archive at the moment it was built, and the
    user leaves this tab precisely in order to go and move files. Coming back to
    a live Apply button holding a stale plan is the one way this tab could
    rewrite an archive_path to somewhere the file no longer is."""
    global sync_plan
    sync_plan = None
    return _sync_render(
        status="**Scan** reads the archive and writes nothing at all. Only "
               "**Apply reconcile** writes, and only to `manifest.json` and the "
               "pHash index — no file is ever moved, copied or deleted from here.",
    )


def on_sync_scan(scope_text):
    """Dry-run the reconcile over the whole archive (or one scope prefix)."""
    global sync_plan
    if archive_dir is None:
        sync_plan = None
        return _sync_render(
            status="⚠️ `ARCHIVE_DIR` is not set in `.env`, so there is no "
                   "archive to scan.",
        )

    scope = (scope_text or "").strip().strip("/") or None
    sync_plan = sync.build_plan(manifest, layout, archive_dir, scope=scope)

    repairable = len(sync_plan.by_bucket(sync.MOVED_OK))
    attention = sum(len(sync_plan.by_bucket(bucket)) for bucket in sync.ATTENTION_BUCKETS)
    if repairable:
        status = (f"✅ Scan complete — nothing was written. **{repairable}** "
                  f"move(s) can be repaired; press **Apply reconcile** to record them.")
    else:
        status = ("✅ Scan complete — nothing was written, and there is "
                  "nothing to repair.")
    if attention:
        status += f" **{attention}** item(s) need your decision (listed below)."
    return _sync_render(
        report=_sync_report_md(sync_plan), status=status, enable_apply=bool(repairable),
    )


def on_sync_apply():
    """Rewrite archive_path for the MOVED_OK bucket, and nothing else."""
    global sync_plan
    if sync_plan is None:
        return _sync_render(
            status="⚠️ Run **Scan** first — Apply only ever acts on the "
                   "plan a scan produced.",
        )

    plan = sync_plan
    moved_ok = plan.by_bucket(sync.MOVED_OK)
    result = sync.apply_plan(plan, manifest, sync_db_path)
    # The plan described the archive as it was BEFORE this write, so it is spent
    # either way -- re-scanning is the only way to get a true one.
    sync_plan = None

    if not result.moved:
        return _sync_render(status="Nothing to apply — the plan had no repairable moves.")

    parts = [f"#### Applied — {result.moved} archive_path(s) rewritten", ""]
    for item in moved_ok:
        parts.append(f"- `{item.key}`  \n  `{item.old_rel}`  \n  → `{item.new_rel}`")
    parts.append("")
    parts.append(
        f"Index re-keyed for **{result.index_updated}**, missed **{result.index_missed}**."
    )
    if result.index_missed:
        # The index is a rebuildable cache and `hash_index.py build` is its
        # source of truth -- but a file missing from it is compared against
        # NOTHING, so this hint is not optional decoration.
        parts.append("")
        parts.append("⚠️ Some rows were not in the index. Run "
                     "`python hash_index.py build` to fully resync it.")

    status = ("✅ Manifest written. No file was moved, copied or deleted. "
              "Scan again to see the archive's new state.")
    return _sync_render(report="\n".join(parts), status=status, enable_apply=False)


# ---------------------------------------------------------------------------
# Settings tab: Character Folders -- promote / merge, in outcome language.
#
# Two engine calls, and no raw JSON editing anywhere:
#   promote_character(franchise, name, layout)        "give it its own folder"
#   merge_character(franchise, name, into, layout)    "group it under <folder>"
# Both are atomic and no-op-safe, and both are described here by WHAT HAPPENS TO
# THE FILES. The words "alias" and "roster" never reach the screen: a name with
# a `characters` entry is one with its **own folder**, a name with a
# `character_aliases` entry is one **grouped in** another folder, and those two
# phrases are the entire model the user needs.
#
# What this tab does NOT do is move a single file. layout.json decides where
# FUTURE images are filed; images already archived stay exactly where they are,
# which is why every edit ends with a count of how many of them the user still
# has to drag across in their own file browser (and then reconcile in the Sync
# tab). Nothing here moves, copies or deletes anything under ARCHIVE_DIR.
# ---------------------------------------------------------------------------

# Bumped by every promote/merge and fed to the @gr.render block below as an
# input, because the per-row controls are dynamic: the rows ARE the data, so a
# write has to rebuild them rather than repaint a fixed set of components. The
# static parts of the panel (dropdown, counts, status) still use the ordinary
# render-dict pattern.
character_tick = 0


def _nested_franchises():
    """Franchise folders this tab can edit: nested style AND a non-empty roster.

    Both conditions matter. For a flat franchise the character name never
    reaches the path at all, so promoting or grouping one would be an edit with
    no observable effect; and a nested franchise with an empty roster has no
    folders to group anything INTO, which is the one thing merge_character
    refuses outright."""
    franchises = layout.get("franchises") or {}
    return sorted(
        folder
        for folder, definition in franchises.items()
        if isinstance(definition, dict)
        and definition.get("style") == "nested"
        and (definition.get("characters") or [])
    )


def _character_rows(franchise):
    """(own_folder, grouped) for one franchise, as the two lists the tab shows.

    `own_folder` is the roster, sorted for the screen only -- layout.json's own
    order is hand-curated and is never rewritten from here. `grouped` is
    (name, folder it files into) for each character alias.
    """
    definition = (layout.get("franchises") or {}).get(franchise)
    if not isinstance(definition, dict):
        return [], []
    own = sorted((definition.get("characters") or []), key=lambda name: str(name).casefold())
    table = (layout.get("character_aliases") or {}).get(franchise) or {}
    grouped = sorted(
        ((name, str(target)) for name, target in table.items()),
        key=lambda row: row[0].casefold(),
    )
    return own, grouped


def _merge_targets(franchise, name):
    """The folders `name` may be grouped under: every OTHER folder in this
    franchise.

    Excluding the row's own name is not cosmetic. merge_character raises
    ValueError on a self-merge -- it would drop the very roster entry the new
    alias points at -- so leaving the name in its own dropdown makes an
    exception reachable by a plain double-click. Matching is normalize_name_key
    so a respelling of the same name ("Hu Tao" vs "Hutao") is excluded too."""
    own, _ = _character_rows(franchise)
    own_key = normalize_name_key(name)
    return [other for other in own if normalize_name_key(other) != own_key]


def _archived_count_under(name, folder_rel):
    """How many ARCHIVED entries tagged `name` still sit under `folder_rel`.

    Read-only, and the only thing this tab ever computes from the manifest.
    `_under` is sync.py's own comparison (case-insensitive, POSIX, "the folder
    or inside it"), reused rather than re-derived so this count and the Sync
    tab's buckets can never disagree about what "under a folder" means."""
    target = normalize_name_key(name)
    if not target:
        return 0
    count = 0
    for entry in manifest.values():
        if not isinstance(entry, dict) or entry.get("status") != "archived":
            continue
        rel = entry.get("archive_path")
        if not rel or not sync._under(rel, folder_rel):
            continue
        if any(normalize_name_key(tagged) == target
               for tagged in (entry.get("character_guess") or [])):
            count += 1
    return count


def _still_to_move_md(name, old_rel, new_rel):
    """The one line that closes the loop after an edit: layout.json now files
    FUTURE images somewhere new, and these already-archived ones are still in
    the old folder until the user drags them across.

    Count only. Moving them from here would be a write inside ARCHIVE_DIR,
    which this process does not do."""
    count = _archived_count_under(name, old_rel)
    if not count:
        return f"No archived images are still filed under `{old_rel}/`."
    return (f"**{count}** archived image(s) for {name} are still in `{old_rel}/`. "
            f"Move them into `{new_rel}/` in your file browser, then open the "
            f"**Sync** tab.")


def _character_counts_md(franchise):
    if not franchise:
        return f"**{len(_nested_franchises())}** series file into character folders."
    own, grouped = _character_rows(franchise)
    return (f"**{franchise}** — {len(own)} character folder(s), "
            f"{len(grouped)} name(s) grouped into one of them.")


def _character_render(franchise, status="", bump=False):
    """Full repaint of the Character Folders panel as {component: value}.

    `bump` advances the tick that re-runs the @gr.render row block; only an
    edit needs it, since selecting a franchise already re-runs that block off
    the dropdown's own input event."""
    global character_tick
    if bump:
        character_tick += 1
    return {
        char_franchise_dropdown: gr.update(choices=_nested_franchises(), value=franchise),
        char_count_md: _character_counts_md(franchise),
        char_status_md: status,
        char_tick: character_tick,
    }


def _character_tab_open(franchise):
    """Repopulate on tab open, KEEPING the current selection -- the row block
    below is triggered by the same tab-select event and reads the dropdown's
    value, so resetting it here would leave the two showing different
    franchises for one frame."""
    return _character_render(franchise if franchise in _nested_franchises() else None)


def on_character_select(franchise):
    return _character_render(franchise)


def on_character_promote(franchise, name, old_folder):
    """Give `name` its own folder. `old_folder` is the folder it is grouped in
    today, and is passed from the row rather than re-derived -- the alias is
    gone by the time the status line is built."""
    try:
        promote_character(franchise, name, layout)
    except ValueError as exc:
        return _character_render(franchise, status=f"⚠️ {exc}", bump=True)

    definition = (layout.get("franchises") or {}).get(franchise) or {}
    # The roster spelling as configured, not the caller's casing -- it is what
    # the folder on disk is called.
    new_folder = resolve_character(franchise, definition, name, layout) or name
    note = _still_to_move_md(name, f"{franchise}/{old_folder}", f"{franchise}/{new_folder}")
    return _character_render(
        franchise,
        status=(f"✅ **{name}** now has its own folder, `{franchise}/{new_folder}/`. "
                f"{note}"),
        bump=True,
    )


def on_character_merge(franchise, name, into):
    """Group `name` under the existing `into` folder.

    `into` empty is a no-op with a message rather than a call: merge_character
    would raise ValueError for it, and an exception is not how a UI says "you
    did not pick anything"."""
    if not into:
        return _character_render(
            franchise,
            status=f"⚠️ Pick a folder to group **{name}** under first.",
        )
    try:
        merge_character(franchise, name, into, layout)
    except ValueError as exc:
        return _character_render(franchise, status=f"⚠️ {exc}", bump=True)

    # merge_character stores the CONFIGURED spelling of the target under the
    # name as given, so reading the alias back is how the real folder name is
    # recovered without re-deriving the lookup.
    table = (layout.get("character_aliases") or {}).get(franchise) or {}
    new_folder = table.get((name or "").strip()) or into
    note = _still_to_move_md(name, f"{franchise}/{name}", f"{franchise}/{new_folder}")
    return _character_render(
        franchise,
        status=(f"✅ **{name}** is now grouped in `{franchise}/{new_folder}/`. "
                f"{note}"),
        bump=True,
    )


with gr.Blocks(title="Oshiire review", analytics_enabled=False) as demo:
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
                    # The two persistent answers. Static like the pair above --
                    # deliberately NOT in `outputs`: the panel's visibility is
                    # carried by char_alias_group, and adding a component to
                    # that list changes the repaint contract every handler has
                    # to satisfy (see tests/test_render_contract.py).
                    with gr.Row():
                        char_alias_group_route_btn = gr.Button("Always file to Others_Group")
                        char_alias_dismiss_btn = gr.Button("Stop asking about this name")

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
    char_alias_group_route_btn.click(fn=on_character_alias_group_route, outputs=outputs)
    char_alias_dismiss_btn.click(fn=on_character_alias_dismiss, outputs=outputs)
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

   with gr.Tab("Sync") as sync_tab:
    gr.Markdown(
        "### Sync\n"
        "Tells oshiire about images you have moved around in the archive "
        "yourself. **Scan** looks and writes nothing; **Apply reconcile** "
        "records the moves it is sure about in `manifest.json` and the "
        "duplicate-detection index. Neither one moves, copies or deletes a "
        "file — that stays your file browser's job."
    )
    sync_scope_box = gr.Textbox(
        label="Only look at (optional)", max_lines=1, placeholder="Genshin Impact",
        info="A folder prefix inside the archive. Leave empty to scan all of it.",
    )
    with gr.Row():
        sync_scan_btn = gr.Button("Scan", variant="primary")
        # Disabled until a Scan has run: Apply acts on the PLAN a scan
        # produced, never on the archive directly, so without one there is
        # literally nothing for it to do.
        sync_apply_btn = gr.Button("Apply reconcile", interactive=False)
    sync_status_md = gr.Markdown()
    sync_report_md = gr.Markdown()

    # Same registration-list discipline as settings_outputs: declares which
    # components a Sync event may repaint, never an order the handlers match.
    sync_outputs = [sync_report_md, sync_status_md, sync_apply_btn]

    # Populated when the tab is OPENED, not by a second demo.load. The archive
    # changes underneath this panel constantly -- that is the whole premise of
    # the tab -- so a once-at-launch population would be stale by definition.
    sync_tab.select(fn=_sync_tab_open, outputs=sync_outputs)
    sync_scan_btn.click(fn=on_sync_scan, inputs=[sync_scope_box], outputs=sync_outputs)
    sync_apply_btn.click(fn=on_sync_apply, outputs=sync_outputs)

   with gr.Tab("Character Folders") as char_tab:
    gr.Markdown(
        "### Character folders\n"
        "Which characters get their own folder in a series, and which are "
        "grouped in with another one. Changes here decide where **future** "
        "images are filed — images already in the archive stay where they "
        "are, and each change tells you how many of them you still need to "
        "move yourself."
    )
    char_franchise_dropdown = gr.Dropdown(
        choices=_nested_franchises(), value=None, label="Series", filterable=True,
        # Seeded here as well as by the tab-select event, and custom values
        # allowed, for the same reason settings_dropdown does it: gr.update
        # only repaints the client, so a value the server-side component was
        # not built with is rejected on submit.
        allow_custom_value=True,
        info="Type to filter. Only series filed into character subfolders appear here.",
    )
    char_count_md = gr.Markdown()
    char_status_md = gr.Markdown()
    # Advanced by every promote/merge; an input to the row block below, which
    # is the only way a write can rebuild rows that are themselves the data.
    char_tick = gr.State(0)

    char_outputs = [char_franchise_dropdown, char_count_md, char_status_md, char_tick]

    @gr.render(
        inputs=[char_franchise_dropdown, char_tick],
        # Explicit triggers, so this does NOT also fire at page load -- same
        # rule as settings_tab.select: the panel is filled in when its tab is
        # opened, never on the startup path.
        triggers=[char_tab.select, char_franchise_dropdown.input, char_tick.change],
    )
    def _character_rows_ui(franchise, _tick):
        """The per-row controls. Dynamic because the rows ARE the layout: one
        row per character folder and one per grouped name, each wired to the
        engine call for that specific name."""
        if not franchise or franchise not in _nested_franchises():
            gr.Markdown("_Pick a series above._")
            return

        own, grouped = _character_rows(franchise)

        gr.Markdown("#### Own folder")
        gr.Markdown(
            "Each of these files into its own folder. Pick another folder and "
            "press Apply to group it in there instead."
        )
        for name in own:
            with gr.Row(equal_height=True):
                gr.Markdown(f"**{name}**")
                target = gr.Dropdown(
                    # The row's own name is excluded, so merge_character's
                    # self-merge ValueError is unreachable from the UI.
                    choices=_merge_targets(franchise, name), value=None,
                    label="Group under…", filterable=True, allow_custom_value=True,
                    show_label=False, container=False,
                )
                gr.Button("Apply", size="sm").click(
                    fn=partial(on_character_merge, franchise, name),
                    inputs=[target], outputs=char_outputs,
                )

        gr.Markdown("#### Grouped in another folder")
        if not grouped:
            gr.Markdown("_Nothing is grouped in this series._")
        for name, folder in grouped:
            with gr.Row(equal_height=True):
                gr.Markdown(f"**{name}** — grouped in `{folder}/`")
                gr.Button("Give it its own folder", size="sm").click(
                    # `folder` is captured now because the alias that names it
                    # is gone by the time the status line is built.
                    fn=partial(on_character_promote, franchise, name, folder),
                    outputs=char_outputs,
                )

    char_tab.select(
        fn=_character_tab_open, inputs=[char_franchise_dropdown], outputs=char_outputs
    )
    # .input(), not .change(): a handler that re-selects the same franchise
    # would otherwise re-render with an empty status and swallow its own
    # confirmation message (see settings_dropdown).
    char_franchise_dropdown.input(
        fn=on_character_select, inputs=[char_franchise_dropdown], outputs=char_outputs
    )


if __name__ == "__main__":
    # A duplicate's thumbnail can live in ARCHIVE_DIR, which is outside the
    # project tree; Gradio serves only cwd/temp by default and raises
    # InvalidPathError otherwise. Read-only -- the UI never writes there.
    allowed_paths = [str(archive_dir)] if archive_dir else []
    demo.launch(css=CUSTOM_CSS, inbrowser=True, allowed_paths=allowed_paths)
