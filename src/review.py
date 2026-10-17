"""Slice 2: Gradio review UI. Reads manifest.json fresh on launch, presents
pending_review entries one at a time in chronological order for human
approve/edit/reject. Makes no network calls and does no downloading; only
reads staging images, reads/writes the manifest, and (on Reject) deletes a
staging file. Reuses manifest.py's load/save -- no reimplementation.
"""
# First, so its clock starts before the heavy imports below (the "imports"
# phase it measures). Pulls in no gradio -- just time.
import startup_timing

import copy
import difflib
import html
import os
import re
import sys
import threading
import time
import warnings
from collections import namedtuple
from functools import partial
from pathlib import Path
from urllib.parse import quote

import gradio as gr
from dotenv import load_dotenv
from starlette.exceptions import StarletteDeprecationWarning

# Merged settings (env > .env > data/config.env) and the app/data folder split.
# Both are stdlib + python-dotenv only.
import config
import datahome
from version import __version__

# Phase B: runs the ingest/backfill/archive CLI pipelines as child processes and
# streams their output. jobs.py imports no gradio and does no network itself.
import jobs

# App-level preferences (data/settings.json), kept out of .env. Pulls in no
# gradio -- just the launch-mode read/write the App settings panel drives.
import settings

# The single-instance lock, shared with app.py's desktop entry point. Kept in
# its own tiny module so importing it here pulls in no gradio and no UI graph.
# record_port writes the bound server port into the lock file so a second launch
# can open the running instance instead of failing.
from instance import acquire_single_instance, record_port

# The reconcile engine, imported for its FUNCTIONS -- build_plan/apply_plan --
# never by shelling out to its CLI. sync.py itself imports nothing that talks
# to the network (archive, hash_index, manifest, shortname), so pulling it in
# here does not breach the UI's no-network invariant; and its dry-run path
# writes nothing at all, which is what makes Scan safe to run from a button.
import sync
# Phase C: Resolve and History are pages inside this app now. Each exposes a
# build_ui() that constructs its components into the current Blocks, plus an
# open function the menu click chains. Importing them only loads state (no
# launch, no network) -- resolve reads ARCHIVE_DIR softly at import for exactly
# this reason (see resolve.py). Neither imports review, so there is no cycle.
import resolve
import history
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
    create_series,
    franchise_folder_and_def,
    is_alias_dismissed,
    is_group_routed,
    load_layout,
    load_shortname_map,
    load_series_aliases,
    load_wallpaper_rules,
    lookup_ci,
    match_shortname,
    merge_character,
    normalize_name_key,
    preview_series_promotion,
    promote_character,
    promote_series,
    remove_character_alias,
    remove_franchise_alias,
    remove_series_alias,
    remove_shortname_entry,
    resolve_character,
    resolve_franchise,
    same_series_name,
    save_character_alias,
    save_character_alias_dismissal,
    save_character_group_route,
    save_franchise_alias,
    save_layout,
    save_series_alias,
    propose_shortname_code,
    save_shortname_entry,
    find_shortname_collision,
    verify_shortname_entry,
    undo_shortname_write,
)

# The heavy imports (gradio + the pipeline modules) are done; everything below
# is review's own module-level startup, timed phase by phase into the log.
startup_timing.mark("imports")



warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)

# Mirrors archive.py's ORIGINAL_RE/is_original -- duplicated rather than
# imported since review.py (Slice 2) shouldn't depend on archive.py (Slice 3a).
# (Shortname-file I/O/matching, unlike routing logic, lives in the shared
# shortname.py module and is imported directly -- see CLAUDE.md.)
_OC_TITLE_RE = re.compile(
    r"\[\s*(?:original(?:\s+character)?|oc|artist(?:['’]s|s)?\s+original)\s*\]",
    re.IGNORECASE,
)


def _gradio_file_url(path):
    """The /gradio_api/file= URL for a file or folder, by its ABSOLUTE path:
    forward slashes, URL-quoted (the app folder may contain spaces). gradio
    resolves a RELATIVE file= path against the cwd, which is the data root, not
    the app folder -- so app files are always addressed absolutely."""
    return "/gradio_api/file=" + quote(Path(path).resolve().as_posix(), safe="/:")


_FONTS_DIR = datahome.APP_DIR / "assets" / "fonts"

CUSTOM_CSS = """
/* ---- Bundled font: Zen Kaku Gothic New (SIL OFL). Subset woff2 in
   assets/fonts/, served locally through gr.set_static_paths (see launch_app).
   NO network: the font loads from the local /gradio_api/file= route only, never
   from a remote web-font host. ---- */
@font-face {
    font-family: 'Zen Kaku Gothic New'; font-style: normal; font-weight: 400;
    font-display: swap;
    src: url('__OSHIIRE_FONTS__/ZenKakuGothicNew-Regular.woff2') format('woff2');
}
@font-face {
    font-family: 'Zen Kaku Gothic New'; font-style: normal; font-weight: 500;
    font-display: swap;
    src: url('__OSHIIRE_FONTS__/ZenKakuGothicNew-Medium.woff2') format('woff2');
}
@font-face {
    font-family: 'Zen Kaku Gothic New'; font-style: normal; font-weight: 700;
    font-display: swap;
    src: url('__OSHIIRE_FONTS__/ZenKakuGothicNew-Bold.woff2') format('woff2');
}

/* ---- The "closet" palette. These are the tokens the whole app paints with;
   the three non-Home pages pick up the colours/font/buttons but keep their own
   layout. ---- */
/* Role tokens -- the names the whole app paints with. One meaning each, so a
   colour can change per role without the "which --indigo did I mean" problem. */
:root {
    --paper:#e9ecee; --door:#f7f8f9; --ink:#1e2a44; --ink-2:#4a5a7a;
    --kanji:#7d8aa3; --frame:#1e2a44; --on-frame:#e9ecee; --line:#1e2a44;
    --btn:#1e2a44; --on-btn:#f7f8f9; --seal:#c8361d;
    --ok:#7fd1a0; --warn:#f2c14e; --off-door:#e1e4e7; --off-ink:#5f6b80;
}
/* Dark mode. gradio puts `.dark` on the app element when the theme is dark
   (?__theme=dark, or prefers-color-scheme in system mode), so flipping our
   tokens here flips in lock-step with gradio's own neutrals. */
.dark {
    --paper:#141925; --door:#1d2436; --ink:#e4e8f1; --ink-2:#a3adc2;
    --kanji:#6c7a98; --frame:#2f3d63; --on-frame:#e4e8f1; --line:#6677a3;
    --btn:#c9d4ee; --on-btn:#141925; --seal:#ef6a4f;
    --ok:#7fd1a0; --warn:#f2c14e; --off-door:#171c29; --off-ink:#7d879c;
}
/* Map gradio's OWN theme variables to our tokens, so every built-in control
   (accent, primary buttons, selected tab, checkbox/radio, focus borders, links,
   slider, loader, surfaces) paints from the closet palette -- no gradio orange
   anywhere. One block: the values are var(--token), which already flip per mode. */
.gradio-container, gradio-app {
    --color-accent: var(--btn);
    --color-accent-soft: color-mix(in srgb, var(--btn) 14%, transparent);
    --button-primary-background-fill: var(--btn);
    --button-primary-background-fill-hover: var(--btn);
    --button-primary-text-color: var(--on-btn);
    --button-primary-border-color: var(--line);
    --checkbox-background-color-selected: var(--btn);
    --checkbox-border-color-selected: var(--line);
    --checkbox-border-color-focus: var(--seal);
    --input-border-color-focus: var(--seal);
    --border-color-accent: var(--line);
    --border-color-accent-subdued: var(--line);
    --link-text-color: var(--ink);
    --link-text-color-hover: var(--seal);
    --link-text-color-active: var(--seal);
    --link-text-color-visited: var(--ink-2);
    --slider-color: var(--btn);
    --loader-color: var(--seal);
    --body-text-color: var(--ink);
    --body-text-color-subdued: var(--ink-2);
    --body-background-fill: var(--paper);
    --background-fill-primary: var(--door);
    --block-background-fill: var(--door);
    --input-background-fill: var(--door);
    background: var(--paper) !important;
    color: var(--ink);
    font-family: 'Zen Kaku Gothic New', system-ui, -apple-system, "Segoe UI", sans-serif !important;
}
/* Remove gradio's footer ("Use via API · Built with Gradio · Settings") and its
   API-docs link. Gradio 6.19 exposes NO launch/Blocks option for either (launch
   has no show_api kwarg), so CSS is the only lever -- and the server binds
   127.0.0.1 only, so the unlinked API schema isn't reachable off-box anyway. */
footer { display: none !important; }

/* ---- Shared button look (every gr.Button carries a primary/secondary/stop
   variant class; internal gradio controls do not, so they are untouched). ---- */
.gradio-container button.primary,
.gradio-container button.secondary,
.gradio-container button.stop {
    border: 2px solid var(--line); border-radius: 2px;
    box-shadow: none !important; background-image: none !important;
    min-height: 44px; font-weight: 700;
}
.gradio-container button.primary { background: var(--btn) !important; color: var(--on-btn) !important; }
.gradio-container button.secondary { background: var(--door) !important; color: var(--ink) !important; }
.gradio-container button.stop { background: var(--seal) !important; border-color: var(--seal) !important; color: var(--on-btn) !important; }
.gradio-container button:focus-visible { outline: 3px solid var(--seal) !important; outline-offset: 2px; }

/* ---- Page centring ----
   Each page is a flex item in gradio's column flex, so a bare `margin: 0 auto`
   on a max-width child doesn't centre it (the item is stretched, and auto
   margins need a definite width to absorb the free space). A definite width via
   width:min(100%, N) + auto inline margins centres it reliably. Home and the
   four job pages cap at 1000px; Manage and History at 1200px; Review and Resolve
   keep their own full width (no class). */
/* Home now caps at 1320px instead, inset 24px a side. */
#home-page .inner-wrap { width: min(100% - 48px, 1320px); margin-inline: auto; }
.page.narrow { width: min(100%, 1000px) !important; margin-inline: auto !important; align-self: center !important; }
.page.wide   { width: min(100%, 1200px) !important; margin-inline: auto !important; align-self: center !important; }

/* ---- Home header: one row -- title left, tools right, bottom-aligned; wraps to
   two rows only when there isn't room (~below 700px). ---- */
/* A grid, not gradio's flex row: as a flex row the title's block grew to the
   full width and pushed the tools onto a second row even at 1900px. Its two
   children get min-width:0 and lose gradio's flex sizing, so the title cell
   (1fr) can shrink and the tools cell (auto) keeps its natural width. One
   column under 700px. */
#home-header {
    display: grid !important; grid-template-columns: 1fr auto; align-items: end;
    gap: 16px; flex-wrap: nowrap !important;
}
#home-header > * { min-width: 0 !important; width: auto !important; flex: none !important; margin: 0 !important; }
#home-title h1 { font-size: 48px; font-weight: 700; letter-spacing: -.01em; line-height: 1; margin: 0; }
#home-title h1 .kanji { color: var(--kanji); margin-left: 14px; font-weight: 700; }
#home-tools { gap: 10px; justify-content: flex-end; flex-wrap: nowrap !important; }
@media (max-width: 699px) {
    #home-header { grid-template-columns: 1fr; }
    #home-tools { justify-content: flex-start; flex-wrap: wrap !important; }
}
/* elem_classes on a gr.Button land on the <button> itself, so these target the
   button directly (NOT ".tool button"). */
.tool {
    flex: 0 0 auto !important; min-height: 44px !important; padding: 0 18px !important;
    width: auto !important; min-width: 0 !important;
    border: 2px solid var(--line) !important; border-radius: 2px !important;
    background: transparent !important; color: var(--ink) !important;
    font-weight: 700; font-size: 16px; box-shadow: none !important; white-space: nowrap;
}
.tool.round { width: 44px !important; min-width: 44px !important; padding: 0 !important; border-radius: 22px !important; }

/* The closet frame + shelves */
.closet { border: 10px solid var(--frame) !important; background: var(--frame) !important; border-radius: 0 !important; padding: 0 !important; gap: 0 !important; }
.shelf { background: var(--paper) !important; padding: 18px 20px 20px !important; gap: 12px !important; border-radius: 0 !important; }
.shelf-name p { margin: 0 !important; font-size: 16px; font-weight: 500; color: var(--ink-2); }
.shelf-row { gap: 14px !important; }
.stack { gap: 14px !important; }

/* Doors are real gr.Buttons; elem_classes land on the wrapper, so the inner
   <button> is the target. The label is a "\\n"-joined string: pre-line renders
   the breaks, ::first-line styles the title. A plain button label cannot style
   only its MIDDLE line, so the Review door's label stops at the count (styled
   by the button's own 20px size) and its description is drawn as ::after
   content at 16px (see _REVIEW_DOOR_DESC). */
/* elem_classes on a gr.Button land on the <button> itself, so the door is
   styled directly. display:block (not flex) so ::first-line can style the title
   line and the "\\n" in the label renders as a real break; pre-line keeps it. */
.door {
    width: 100% !important; min-height: 84px !important; padding: 14px 18px !important;
    border: 2px solid var(--line) !important; border-radius: 2px !important;
    background: var(--door) !important; color: var(--ink-2) !important;
    display: block !important; text-align: left !important;
    white-space: pre-line !important; font-weight: 400 !important; font-size: 16px !important;
    line-height: 1.4 !important; box-shadow: none !important;
}
.door::first-line { font-size: 24px !important; font-weight: 700 !important; color: var(--ink) !important; }
/* Review: title 44px (first line), count 20px (the label's body), then the
   16px description as ::after (see _REVIEW_DOOR_DESC). */
.door.main { min-height: 182px !important; padding: 26px !important; line-height: 1.5 !important; font-size: 20px !important; }
.door.main::first-line { font-size: 44px !important; }
.door.main::after { display: block; margin-top: 4px; font-size: 16px; line-height: 1.4; }
.door:disabled {
    background: var(--off-door) !important; border-style: dashed !important;
    border-color: var(--off-ink) !important; color: var(--off-ink) !important;
    cursor: not-allowed; opacity: 1 !important;
}
.door:disabled::first-line { color: var(--off-ink) !important; }

/* The board: an indigo strip between the shelves. Its text is one gr.HTML
   whose inner .board-text flexes the cells; the warn/job states are a single
   <p>. The Check again / Show progress buttons sit at the right of the strip. */
.board { background: var(--frame) !important; border-radius: 0 !important; padding: 0 !important; align-items: center !important; gap: 0 !important; flex-wrap: nowrap !important; }
.board .board-text { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 28px; font-size: 16px; line-height: 1.5; padding: 13px 20px; }
/* gradio's prose/html container sets its own text colour, so force paper on the
   board text and every child (incl. <strong>). */
.board .board-text, .board .board-text * { color: var(--on-frame) !important; }
.board .board-text p { margin: 0 !important; flex: 999 1 420px; }
.board .board-text .dot { display: inline-block; width: 9px; height: 9px; border-radius: 5px; margin-right: 8px; vertical-align: 1px; }
.board > div:first-child { flex: 999 1 auto; }
.board .tool { border-color: var(--on-frame) !important; color: var(--on-frame) !important; margin: 6px 16px; white-space: nowrap; }

/* ---- Home fills the window. The page is a column at least the viewport tall
   (minus gradio's 16px top + bottom padding on .main); the closet takes what
   is left, shared ~60/40 by the two shelves while the board stays a thin
   strip, and every door grows to fill its shelf (the Review door spans the
   whole Coming-in height). Short (<760px) or narrow (<700px) windows keep
   their natural height and scroll. ---- */
@media (min-height: 760px) and (min-width: 700px) {
    #home-page { min-height: calc(100vh - 32px); }
    #home-page > .inner-wrap { flex: 1 1 auto; display: flex; flex-direction: column; }
    #home-page .closet { flex: 1 1 auto !important; }
    #home-page .shelf.coming-in { flex: 6 1 0 !important; }
    #home-page .shelf.putting-away { flex: 4 1 0 !important; }
    #home-page .board { flex: 0 0 auto !important; }
    #home-page .shelf-row { flex: 1 1 auto !important; align-items: stretch !important; }
    #home-page .shelf-row > .door { height: auto !important; align-self: stretch !important; }
    #home-page .stack > .door { flex: 1 1 0; }
}

/* Preview filing's log is a padded table: monospace so its columns line up. */
.plan-log textarea { font-family: ui-monospace, Consolas, "Cascadia Mono", monospace !important; font-size: 13px !important; white-space: pre !important; }

/* ---- Quit notice (browser mode) ---- */
.quit-note-wrap { margin: 8px 0 0 0 !important; }
.quit-note { margin: 0 !important; font-size: 16px; font-weight: 600; color: var(--ink); }

/* ---- Job pages: result panel + seal ---- */
.pagehead { align-items: center !important; gap: 16px !important; }
.pagehead h2 { margin: 0 !important; font-size: 28px; font-weight: 700; }
.result-frame { border: 10px solid var(--frame) !important; background: var(--paper) !important; border-radius: 0 !important; padding: 26px !important; gap: 22px !important; }
.result-row { align-items: center !important; gap: 22px !important; flex-wrap: wrap; }
.seal {
    flex: 0 0 auto; width: 72px; height: 72px; border: 3px solid var(--seal);
    border-radius: 40px; display: flex; align-items: center; justify-content: center;
    color: var(--seal); font-size: 34px; font-weight: 700; transform: rotate(-8deg); line-height: 1;
}
.result-frame p { margin: 0 !important; }
.result-frame .big { font-size: 23px; font-weight: 700; color: var(--ink); }
.result-frame .small { font-size: 15px; color: var(--ink-2); }
/* The sentence sits directly to the right of the seal, left-aligned, taking the
   remaining width -- not pushed to the far edge where it wrapped oddly. The seal
   keeps its size; the text block flexes. Both are gr.HTML blocks, which gradio
   gives width:100% -- so width:auto is what actually lets them share one row
   (flex alone can't beat an explicit width). */
.result-row { justify-content: flex-start !important; flex-wrap: nowrap !important; }
.result-row .seal-cell { flex: 0 0 auto !important; width: auto !important; }
.result-row .result-text { flex: 1 1 auto !important; width: auto !important; min-width: 0 !important; text-align: left !important; }
.result-frame .big, .result-frame .small { text-align: left !important; }
/* Narrow screens: stack the seal above the sentence so the text gets full width
   instead of wrapping into a thin column beside the seal. */
@media (max-width: 480px) {
    .result-row { flex-direction: column !important; align-items: flex-start !important; }
    .result-row .result-text { text-align: left !important; }
}

/* Preview filing's plan table (a static <table> in a gr.HTML) + its caption,
   below the result frame. The wrapper scrolls vertically so a long plan doesn't
   push the page; the header stays pinned. */
.plan-wrap { margin-top: 14px !important; }
.plan-note p { margin: 8px 2px 0 2px !important; font-size: 13px; color: var(--ink-2); }
.plan-table-wrap { max-height: 60vh; overflow: auto; border: 1px solid var(--line); border-radius: 2px; }
table.plan-table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 14px; }
table.plan-table th, table.plan-table td {
    text-align: left; vertical-align: top; padding: 7px 10px;
    border-bottom: 1px solid var(--line);
    /* Wrap long franchise names and archive paths so every column stays in view
       instead of the Destination column scrolling Status off-screen. */
    white-space: normal; word-break: break-word; overflow-wrap: anywhere;
}
table.plan-table thead th {
    position: sticky; top: 0; z-index: 1; background: var(--paper);
    font-weight: 700; color: var(--ink); border-bottom: 2px solid var(--line);
}
/* Needs-Resolve rows (which also sort first) stand out with the seal accent. */
table.plan-table tr.needs-resolve td { background: color-mix(in srgb, var(--warn) 12%, transparent); }
table.plan-table tr.needs-resolve td:first-child { box-shadow: inset 3px 0 var(--warn); }

/* Settings forms: Save / Delete / New entry stay the same height even when the
   Save button's "Rename “X” → “Y”" label wraps to two lines. */
.form-buttons { align-items: stretch !important; }

/* ---- kept from before: Review / duplicate / toolbar / page plumbing ---- */
#review-image img {
    max-height: 70vh;
    width: auto;
    object-fit: contain;
    margin: 0 auto;
}
.confirm-panel {
    border: 2px solid var(--warn);
    background: color-mix(in srgb, var(--warn) 14%, transparent);
    color: var(--ink);
    border-radius: 8px;
    padding: 12px;
}
/* Duplicate / backfill warnings reuse the confirm-panel look, in the seal red
   for a confident match and amber (warn) for the uncertain band. */
.warn-panel {
    border: 2px solid var(--seal);
    background: color-mix(in srgb, var(--seal) 13%, transparent);
    color: var(--ink);
    border-radius: 8px;
    padding: 12px;
}
.maybe-panel {
    border: 2px solid var(--warn);
    background: color-mix(in srgb, var(--warn) 14%, transparent);
    color: var(--ink);
    border-radius: 8px;
    padding: 12px;
}
#dup-thumb img {
    max-height: 22vh;
    width: auto;
    object-fit: contain;
}
#image-toolbar { gap: 8px; margin-top: 6px; }
#image-toolbar button { min-height: 2.75rem; font-size: 1rem; }
/* The 70vh cap above would otherwise apply inside fullscreen too. */
#review-image img:fullscreen {
    max-height: none; max-width: none;
    width: 100%; height: 100%;
    object-fit: contain; background: #000;
}
#undo-btn { width: 100%; min-height: 3rem; font-size: 1.05rem; margin-top: 6px; }
#undo-btn.undo-cancel {
    border: 2px solid var(--warn) !important;
    background: color-mix(in srgb, var(--warn) 16%, transparent) !important;
}
.page-header { align-items: center; gap: 12px; }
/* The "## Title" markdown brings default h2 margins that leave the header row a
   few px taller than its content, which Gradio answers with a stray scroll
   affordance (the little ▲▼ at the top right). Collapse the heading margins and
   let the row + its blocks overflow visibly so nothing scrolls. */
.page-header, .page-header > * { overflow: visible !important; }
.page-header :is(h1, h2, h3) { margin: 0 !important; line-height: 1.25 !important; }
/* Review's post-info block (title + subreddit/permalink markdown) had the same
   hairline overflow -- same cause, same fix. */
.post-info, .post-info > * { overflow: visible !important; }
.post-info :is(h1, h2, h3, p) { margin: 0 0 6px 0 !important; line-height: 1.4 !important; }
.post-info :is(h1, h2, h3, p):last-child { margin-bottom: 0 !important; }
/* The "### Step N" headings on the Free up saved space page had the same
   hairline overflow -- same cause, same fix. Keep a little top margin so the
   steps stay visually separated, but let the block overflow so no ▲▼ appears
   (identical in light and dark -- the rule is colour-agnostic). */
.step-head, .step-head > * { overflow: visible !important; }
.step-head :is(h1, h2, h3, h4) { margin: 0.6em 0 0 0 !important; line-height: 1.3 !important; }
.page-hidden { display: none !important; }
"""
# The @font-face src URLs carry the fonts folder's absolute path (see
# _gradio_file_url), filled in here so the CSS text above stays readable.
CUSTOM_CSS = CUSTOM_CSS.replace("__OSHIIRE_FONTS__", _gradio_file_url(_FONTS_DIR))

# Resolve and History render inside this app's Blocks, so their stylesheets have
# to ride along on the single css= passed to demo.launch. (Both define
# .confirm-panel identically to review's, so the duplicate rule is harmless.)
CUSTOM_CSS += resolve.CUSTOM_CSS + history.CUSTOM_CSS

# Client-side toolbar handler (wired with fn=None, js=...). It acts on the
# rendered <img> directly rather than round-tripping to the server, so it is
# not an output of any handler and does not affect the render contract.
FULLSCREEN_JS = """() => { const img = document.querySelector('#review-image img');
                         if (img && img.requestFullscreen) img.requestFullscreen(); }"""

# Shown in the Home page's Help accordion. The one place the whole pipeline is
# described in running order, so a new user knows what each menu button does.
HELP_MD = """\
**The usual order:** Ingest → Review → Resolve → Preview filing → File images.
- **Ingest** — fetch new saves from your Reddit feed. AI tagging runs
  automatically afterwards.
- **Backfill** — work through older saves from your Reddit data export, a
  batch at a time. AI tagging runs automatically afterwards.
- **Review** — check each image's series and character, then accept, reject or skip.
- **Resolve** — sort out entries review flagged.
- **Preview filing** — show where every accepted image would be filed. Moves nothing.
- **File images** — file them. Oshiire then refreshes its duplicate check and
  the list of saves that are safe to remove from Reddit.
- **Manage** — character folders, reconciling files you moved yourself
  (Sync), and settings.
- **History** — look up past decisions. Read-only, and always shows the current state.
- Only one job runs at a time; the page stays locked until it finishes.
  Backfill can be stopped; File images always runs to the end. If you reload
  while a job runs, the menu stays locked until it finishes.
- Ingest and Backfill are greyed out, with the reason shown, when they can't
  run yet.
- To quit, close the Oshiire window. If a job is running, Oshiire asks first
  and never cuts it off halfway.
- Only one copy of Oshiire runs at a time.
"""

# The one status that counts as "waiting in Review". Review's queue is built
# from it (below) and the Home door's live count reads the SAME filter, so the
# two can never say different things.
WAITING_STATUS = "pending_review"

# Door descriptions (second line). Titles are the first line; the two are joined
# with "\n" so the door button's label is one pre-line string (CSS styles the
# first line as the title). Archive-page wording is the renamed "Preview filing"
# / "File images"; code identifiers keep their old names.
_DOOR_DESC = {
    "ingest": "Fetch new saves from Reddit.",
    "backfill": "Work through older saves.",
    "preview_filing": "See where approved images would go. Moves nothing.",
    "file_images": "Move approved images into the archive.",
    "resolve": "Place images filing couldn't.",
}
_DOOR_TITLE = {
    "ingest": "Ingest", "backfill": "Backfill",
    "preview_filing": "Preview filing", "file_images": "File images",
    "resolve": "Resolve", "review": "Review",
}


def _door_label(title, *rest):
    """A door button's label: title line, then one line per description part."""
    return "\n".join([title, *rest])


def _waiting_phrase(n):
    """How the Review door names its queue size: 0 -> 'Nothing waiting',
    1 -> '1 image waiting', else '<n> images waiting'."""
    if n <= 0:
        return "Nothing waiting"
    if n == 1:
        return "1 image waiting"
    return f"{n:,} images waiting"


# The Review door's description. NOT part of the button's label: a label can
# style only its first line (::first-line), and the count line (20px) and this
# line (16px) differ, so CUSTOM_CSS prints it as the button's own ::after
# content -- still inside the one <button>, and still read out as part of its
# accessible name.
_REVIEW_DOOR_DESC = "Approve, fix or reject each one before it's filed."
CUSTOM_CSS += '.door.main::after { content: "%s"; }\n' % (
    _REVIEW_DOOR_DESC.replace("\\", "\\\\").replace('"', '\\"'))


def _review_door_label(n):
    """The two-line Review door label for a live waiting count: the title, then
    the count. The description is not part of the label; CSS draws it as the
    button's ::after content (see _REVIEW_DOOR_DESC)."""
    return _door_label(_DOOR_TITLE["review"], _waiting_phrase(n))


# secondary_label: an optional second next-step button's text, offered beside
# the primary one (only Preview filing uses it, for "File images" when some
# images can be filed but others need Resolve first).
def _images(n):
    """'1 image is' / '<n> images are' -- the subject + verb of a count sentence."""
    return "image is" if n == 1 else "images are"


JobResult = namedtuple("JobResult", "seal sentence second_line next_label secondary_label",
                       defaults=(None,))


def job_result(job, outcome, counts=None):
    """Pure: (job, outcome, counts) -> JobResult for a finished job page.

    job: "ingest" | "backfill" | "preview_filing" | "file_images" |
         "export_unsave".
    outcome: "success" | "cancelled" | "failed".
    counts: optional dict. Recognised keys:
      new_waiting -- Review queue grew by this many (ingest/backfill success);
      filed       -- images moved into the archive (file_images success);
      moves, flagged -- the dry run's structured summary (preview_filing
                     success): images ready to file / images that need Resolve.
                     Absent when the job printed no summary line.
      safe        -- posts safe to unsave (export_unsave success), from the
                     OSHIIRE_UNSAVE count line;
      reason      -- one-line failure reason (failed);
      second_line -- an extra fact the JOB itself reported (e.g. skipped
                     duplicates); shown in indigo-2. Never invented here.
    `next_label` is the primary next-step button's text, or None (then only the
    plain "Back to menu" secondary is offered).
    """
    counts = counts or {}
    second = counts.get("second_line")

    if outcome == "failed":
        reason = counts.get("reason") or "see the log"
        return JobResult(False, f"Didn't finish: {reason}. The full log is under Details.",
                         None, None)
    if outcome == "cancelled":
        # Only Backfill is cancellable, and it checkpoints per batch, so every
        # finished batch is kept and nothing past the stop point was written.
        return JobResult(
            False,
            "Stopped. Oshiire kept every batch it had already finished; "
            "nothing after the stop point was changed.",
            None, None)

    # success
    if job in ("ingest", "backfill"):
        n = int(counts.get("new_waiting", 0) or 0)
        if n <= 0:
            if job == "ingest":
                # Nothing new is the symptom of a full saved listing, so point
                # Ingest straight at the fix (a "Free up space" button is shown
                # beside this by _ingest_result_extra).
                return JobResult(
                    True,
                    "Nothing new to review. If you've saved more than about "
                    "1,000 posts, Reddit stops showing new ones.",
                    second, None)
            return JobResult(True, "Done. Nothing new to review.", second, None)
        img = "image" if n == 1 else "images"
        verb = "is" if n == 1 else "are"
        return JobResult(True, f"Done. {n:,} new {img} {verb} waiting in Review.",
                         second, "Go to Review")
    if job == "preview_filing":
        # The counts come from archive.py's one structured OSHIIRE_SUMMARY line
        # (never from its free-text table). No summary line -> no numbers.
        if "moves" not in counts or "flagged" not in counts:
            # The dry-run job reports no structured count, and parsing its free-text
            # summary is off-limits, so the number is dropped by design.
            return JobResult(True, "Done. Nothing was moved.", second, "File images")
        n, k = int(counts["moves"] or 0), int(counts["flagged"] or 0)
        if n > 0 and k == 0:
            return JobResult(True, f"Done. {n:,} {_images(n)} ready to file. Nothing was moved.",
                             second, "File images")
        if n > 0:
            return JobResult(True, f"Done. {n:,} {_images(n)} ready to file; "
                             f"{k:,} need{'s' if k == 1 else ''} Resolve first.",
                             second, "Resolve", "File images")
        if k > 0:
            return JobResult(True, f"Nothing can be filed yet: {k:,} "
                             f"need{'s' if k == 1 else ''} Resolve first.",
                             second, "Resolve")
        return JobResult(True, "Nothing to file. Approve images in Review first.",
                         second, "Go to Review")
    if job == "file_images":
        n = int(counts.get("filed", 0) or 0)
        if n <= 0:
            return JobResult(True, "Done. Nothing was ready to file.", second, None)
        img = "image" if n == 1 else "images"
        verb = "was" if n == 1 else "were"
        return JobResult(True, f"Done. {n:,} {img} {verb} filed.", second, None)
    if job == "export_unsave":
        n = int(counts.get("safe", 0) or 0)
        subject = "post is" if n == 1 else "posts are"
        return JobResult(
            True, f"{n:,} {subject} safe to unsave (already in your archive).",
            second, None)
    raise ValueError(f"unknown job {job!r}")


manifest = load_manifest()
queue = sorted(
    (post_id for post_id, entry in manifest.items() if entry.get("status") == WAITING_STATUS),
    key=lambda post_id: manifest[post_id].get("fetched_at", ""),
)
current_index = 0
last_action = None  # None, or a dict describing the most recent Skip/Reject/Accept
pending_accept = None  # None, or the parsed values awaiting a map_prompt choice

# Set by _reload_after_job when a job rewrote the manifest; _review_open consumes
# it so navigating to Review repaints once rather than on every visit.
_review_stale = False
# Set when the user visits Resolve, which keeps its OWN copies of the manifest/
# layout/shortname/aliases and may write any of them. Disk is the single source
# of truth, so the next time any manifest-or-config-writing page is opened,
# _ensure_fresh re-reads review's copies first -- see _nav_to.
_external_dirty = False
# Shared between the Backfill run generator and its Stop button (the only
# cancellable job). Cleared at the start of each backfill run.
_backfill_cancel = threading.Event()

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
startup_timing.mark("manifest load")


def _waiting_count(m=None):
    """How many entries are waiting in Review, using the same filter Review's
    queue uses (WAITING_STATUS). Counts `m` if given (Home passes a fresh disk
    read), else the in-memory manifest (the job pages, around their own reload)."""
    m = manifest if m is None else m
    return sum(1 for e in m.values() if e.get("status") == WAITING_STATUS)


def _archived_count(m=None):
    """How many images are "put away" -- entries with status archived. Counts
    `m` if given, else the in-memory manifest (see _waiting_count)."""
    m = manifest if m is None else m
    return sum(1 for e in m.values() if e.get("status") == "archived")


def archive_dir_problem():
    """Why the archive can't be used right now, as (kind, sentence), or
    (None, None) when it's fine.

    `kind` is "unset" (no ARCHIVE_DIR in .env) or "unreachable" (set, but the
    folder can't be reached right now -- e.g. a Google Drive mount that hasn't
    come up), so callers can word themselves by cause. `sentence` is the ready
    banner line for that cause, reused verbatim by both the Home banner and the
    archive-reading Manage tabs so they can never drift apart.

    Called on every Home render, not once at startup: ARCHIVE_DIR sits on a
    synced/removable drive that can mount AFTER launch, so existence is
    re-checked each time. .exists() can itself raise on such a drive, so it is
    guarded -- any error means "not reachable", never a crashed page."""
    if archive_dir is None:
        return (
            "unset",
            "⚠️ ARCHIVE_DIR isn't set in .env. Review, Ingest, Backfill, History "
            "and Settings still work; Preview filing, File images and Resolve are "
            "off until it's set (edit .env, then restart Oshiire).",
        )
    try:
        if archive_dir.exists():
            return (None, None)
    except OSError:
        pass
    return (
        "unreachable",
        f"⚠️ The archive folder isn't reachable: {archive_dir}. If it's on Google "
        "Drive, make sure Drive is running and the folder is available, then click "
        "Check again. Review, Ingest, Backfill, History and Settings still work "
        "meanwhile.",
    )



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
        f"\r  hashing {done} of {total} (one-time; Ingest does this ahead of time for new saves)",
        end="", flush=True,
    ),
)
if _warmed:
    save_manifest(manifest)
    print(f"\r  hashed {_warmed} image(s).{' ' * 40}")
else:
    print("  all staging images already hashed.")
startup_timing.mark("hash warm-up")

archive_hashes = load_archive_hashes()
archive_path_map = build_archive_path_map(manifest)
# What the index ACTUALLY holds. Anything filed since the index was last built
# is missing from it, and find_duplicates uses this to compare those entries
# from their cached manifest hash instead of assuming the index covered them.
indexed_paths = {rel_path for rel_path, _ in archive_hashes}
if not archive_hashes:
    print(
        "  note: no archive pHash index found — duplicate checks will only cover "
        "staging. Run `python src/hash_index.py build` to include the archive."
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
            "they'll be compared from their cached hash. Archive apply refreshes "
            "this automatically (or run `python src/hash_index.py build`)."
        )
# Consulted so a franchise tagged with a known variant name resolves to its
# existing series instead of proposing a duplicate code. Learning new SERIES
# alternates is still resolve.py's job. CHARACTER alternates are learned here
# instead: the misspelling happens in this box, and by the time archive.py
# flags it the reviewer is several screens away from the image that would tell
# them which character it is. Still two clicks -- the panel only opens for a
# name that resolves to nothing, and "just this once" writes nothing.
series_aliases = load_series_aliases()
startup_timing.mark("archive check")


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
    # Gradio can hand a Textbox value through as None (e.g. a cleared box);
    # treat that as empty rather than letting .splitlines() raise.
    if text is None:
        return []
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
        return gr.update(value="Cancel pending accept", interactive=True, elem_classes=["undo-cancel"])
    # Explicit empty list, not omitted: without it the amber class sticks after
    # the confirm panel closes.
    return gr.update(value="Undo last action", interactive=last_action is not None, elem_classes=[])


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


# Pages stay mounted and are hidden with our own .page-hidden class.
# visible=False unmounts and remounts each page, which left the app window
# (WebView2) drawing black pages, and gradio 6.19's visible="hidden" doesn't
# actually hide a Column.
# Per-page max-width class (see the .narrow/.wide CSS). Home centres via its own
# .inner-wrap; Review and Resolve keep their full width, so they appear nowhere.
_PAGE_WIDTH = {
    "ingest": "narrow", "backfill": "narrow",
    "archive_dryrun": "narrow", "archive_apply": "narrow",
    "unsave": "narrow",
    "manage": "wide", "history": "wide",
}


def _page_classes(key, shown):
    """The elem_classes for a page: base 'page', its width class if any, and
    'page-hidden' unless it's the one being shown."""
    classes = ["page"]
    width = _PAGE_WIDTH.get(key)
    if width:
        classes.append(width)
    if not shown:
        classes.append("page-hidden")
    return classes


def _nav_to(key):
    """Show exactly one page; when opening Manage, land on its first tab.

    {component: update} over every page Column plus manage_tabs. Keyed by
    component and resolved at CALL time, exactly like _render_current: the page
    Columns and manage_tabs are module-level globals created in the gr.Blocks()
    block far below, and this only runs after that block has executed.

    Disk is the single source of truth. Resolve keeps its own copies of the
    manifest/layout/shortname/aliases, so after visiting it review's copies must
    be re-read before anything on review's side can save over Resolve's writes:
    opening Resolve marks review dirty, and opening any page that can itself
    write the manifest or config re-reads first. Home and History neither write
    nor need a reload, so they do neither."""
    global _external_dirty
    if key == "resolve":
        _external_dirty = True
    elif key in {"review", "manage", "ingest", "backfill",
                 "archive_dryrun", "archive_apply"}:
        _ensure_fresh()
    pages = {"home": home_page,
             "review": review_page,
             "resolve": resolve_page,
             "archive_dryrun": archive_dryrun_page,
             "archive_apply": archive_apply_page,
             "ingest": ingest_page,
             "backfill": backfill_page,
             "unsave": unsave_page,
             "manage": manage_page,
             "history": history_page}
    if key not in pages:
        raise ValueError(f"unknown nav key: {key!r}")
    # Rebuild each page's classes from scratch on every nav, so the width class
    # (narrow/wide) must be re-applied here too -- otherwise the first navigation
    # strips it and the page stops being centred. Review/Resolve/Home have none.
    result = {page: gr.update(elem_classes=_page_classes(k, shown=(k == key)))
              for k, page in pages.items()}
    # Opening Manage always resets to the Character Folders tab; any other
    # navigation leaves the manage tab selection where the user left it.
    result[manage_tabs] = gr.Tabs(selected="char") if key == "manage" else gr.update()
    return result


def _cancel_pending_accept():
    """Clear an open accept confirmation without saving -- the same effect the
    'Cancel pending accept' control has (see on_undo). Leaving Review this way
    abandons the confirmation; the entry stays pending_review."""
    global pending_accept
    pending_accept = None


# Named wrappers, not lambdas, so the fn name is readable in tests and tracebacks.
def _nav_home():
    return _nav_to("home")


def _nav_home_from_review():
    """Review's Back button: leaving Review abandons any open accept
    confirmation exactly as Cancel would, then goes home."""
    _cancel_pending_accept()
    return _nav_to("home")


def _nav_review():
    return _nav_to("review")


def _nav_resolve():
    return _nav_to("resolve")


def _nav_archive_dryrun():
    return _nav_to("archive_dryrun")


def _nav_archive_apply():
    return _nav_to("archive_apply")


def _nav_ingest():
    return _nav_to("ingest")


def _nav_backfill():
    return _nav_to("backfill")


def _nav_manage():
    return _nav_to("manage")


def _nav_unsave():
    return _nav_to("unsave")


def _nav_history():
    return _nav_to("history")


# The job board's "Show progress" button opens the page of whatever job is
# running. Names come from jobs.*_steps (the job lock's name); map them to the
# nav keys. If nothing is running (a race with a just-finished job), stay home.
_JOB_NAME_TO_PAGE = {
    "Ingest": "ingest", "Backfill": "backfill",
    "Archive dry run": "archive_dryrun", "Archive apply": "archive_apply",
    "Free up space": "unsave",
}

# jobs.running() returns the lock NAME (kept as-is for the lock and the CLI);
# this maps the two renamed ones to their UI display names. Ingest/Backfill are
# unchanged, so .get() falls through to the name itself.
_JOB_DISPLAY_NAME = {
    "Archive dry run": "Preview filing", "Archive apply": "File images",
}


def _job_display(name):
    """The user-facing name for a running job's lock name, or None for None."""
    return _JOB_DISPLAY_NAME.get(name, name) if name else name


def _show_progress():
    return _nav_to(_JOB_NAME_TO_PAGE.get(jobs.running(), "home"))


# Container show/hide uses the .page-hidden CSS class (display:none) -- the same
# mechanism the pages use -- rather than visible=, which unmounts/remounts the
# Column and leaves WebView2 drawing a black frame.
_RESULT_SHOWN = ["result-frame"]
_RESULT_HIDDEN = ["result-frame", "page-hidden"]

# Preview filing's plan table wrapper, shown/hidden the same way (class, not
# visible=). Built always-mounted; driven by value + this class.
_PLAN_SHOWN = ["plan-wrap"]
_PLAN_HIDDEN = ["plan-wrap", "page-hidden"]

_PLAN_COLS = ("Image", "Franchise", "Character", "Destination", "Status")
# Column widths, summing to 100%, matched in the <colgroup> below. Destination
# gets the most; every column stays on screen (the original off-screen-Status
# complaint).
_PLAN_COL_WIDTHS = ("13%", "19%", "19%", "33%", "16%")


def _plan_table_html(plan_rows):
    """A plain HTML <table> for the Preview filing plan -- one row per
    jobs.parse_plan_row dict, columns Image/Franchise/Character/Destination/
    Status. Returned as an HTML string for a gr.HTML, NOT a gr.Dataframe: at real
    scale (~1000 rows) gradio's Dataframe froze the UI for minutes building its
    grid, while a static table of the same rows renders in well under a second in
    both the browser and the native WebView2 window. Needs-Resolve rows are
    marked so they stand out (they also sort first, from archive.py). Empty when
    there are no rows."""
    if not plan_rows:
        return ""
    cols = "".join(f'<col style="width:{w}">' for w in _PLAN_COL_WIDTHS)
    head = "".join(f"<th>{c}</th>" for c in _PLAN_COLS)
    body = []
    for r in plan_rows:
        cells = (r.get("image", ""), r.get("franchise", ""), r.get("character", ""),
                 r.get("destination", ""), r.get("status", ""))
        tds = "".join(f"<td>{html.escape(str(c), quote=False)}</td>" for c in cells)
        tr_cls = ' class="needs-resolve"' if r.get("needs_resolve") else ""
        body.append(f"<tr{tr_cls}>{tds}</tr>")
    return (f'<table class="plan-table"><colgroup>{cols}</colgroup>'
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>")


def _toggle_help(open_now):
    """Flip the Home help panel via .page-hidden. `open_now` is its current open
    state (a State), so the button is a pure toggle with no page reload."""
    new = not open_now
    return gr.update(elem_classes=[] if new else ["page-hidden"]), new


# --------------------------------------------------------------------------- #
# Launch mode (browser vs native) and the Quit control.
#
# The running mode is decided by app.py (or `python src/review.py`, which is always
# browser) and pushed in via set_launch_mode BEFORE the first paint. It only
# affects the Home Quit button: in a native window the OS close button quits, so
# Quit is hidden; in a browser tab there is no window to close, so Quit is how
# you stop the server. Default "browser" -- the setting's default and what the
# plain `python src/review.py` entry is.
# --------------------------------------------------------------------------- #
_launch_mode = "browser"


def set_launch_mode(mode):
    """Set the running launch mode ("browser" | "native"); anything else is
    treated as "browser"."""
    global _launch_mode
    _launch_mode = mode if mode in ("browser", "native") else "browser"


def launch_mode():
    return _launch_mode


def _quit_btn_update(running):
    """The Quit button's state for the current mode. Native -> hidden via the
    .page-hidden class (never visible=). Browser -> shown, and disabled while a
    job runs (the board already says why)."""
    if _launch_mode != "browser":
        return gr.update(elem_classes=["tool", "page-hidden"])
    return gr.update(elem_classes=["tool"], interactive=(running is None))


_QUIT_STOPPED = (
    '<p class="quit-note">Oshiire has stopped. You can close this tab.</p>')
_QUIT_BUSY = (
    '<p class="quit-note">A job is running — it has to finish before Oshiire '
    'can quit.</p>')

# Best-effort browser close guard: warn before unloading the tab WHILE a job is
# running. Installed once per page (demo.load). It needs no job wiring: a running
# job's run button is relabelled "Running…" and disabled (see _job_stream's first
# yield), so the presence of such a button IS the signal. The job keeps running
# server-side regardless of what the tab does -- this only saves the user from
# losing sight of it by accident.
_CLOSE_GUARD_JS = """
() => {
  if (window.__oshiireGuard) return;
  window.__oshiireGuard = true;
  const jobRunning = () => [...document.querySelectorAll('button')]
    .some(b => b.textContent.trim().startsWith('Running') && b.disabled);
  window.addEventListener('beforeunload', (e) => {
    if (jobRunning()) { e.preventDefault(); e.returnValue = ''; }
  });
}
"""

# gradio 6.19 has no spellcheck= option on gr.Textbox, so turn the red squiggles
# off at load for the name/folder boxes (Review's character/franchise, and every
# Settings name box -- all tagged .nospell). A MutationObserver re-applies it to
# inputs that mount lazily (a Settings sub-tab is only built when first opened).
_NOSPELL_JS = """
() => {
  if (window.__oshiireNoSpell) return;
  window.__oshiireNoSpell = true;
  const apply = () => document
    .querySelectorAll('.nospell textarea, .nospell input')
    .forEach(el => el.setAttribute('spellcheck', 'false'));
  apply();
  new MutationObserver(apply).observe(document.body, {childList: true, subtree: true});
}
"""

# After the Quit response lands (and ONLY when it was the "stopped" one, not the
# busy refusal), replace the whole page with a static, closet-styled goodbye.
# The server closes ~0.6s later; with gradio's live UI gone from the DOM there is
# nothing left to try to reconnect, so no connection-error toasts or spinners.
# The :root palette vars live in <head>, so they survive the body replace.
_QUIT_REPLACE_JS = """
() => {
  const n = document.querySelector('.quit-note');
  if (!n || !n.textContent.includes('has stopped')) return;
  document.title = 'Oshiire — stopped';
  document.body.innerHTML =
    '<div style="min-height:100vh;display:flex;flex-direction:column;align-items:center;'
    + 'justify-content:center;gap:14px;background:var(--paper,#e9ecee);color:var(--ink,#1e2a44);'
    + 'font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;text-align:center;padding:0 24px;">'
    + '<div style="font-size:48px;line-height:1;" lang="ja">\\u62bc\\u5165\\u308c</div>'
    + '<div style="font-size:22px;font-weight:700;">Oshiire has stopped.</div>'
    + '<div style="font-size:16px;color:var(--ink-2,#4a5a7a);">You can close this tab.</div>'
    + '</div>';
}
"""


def _schedule_server_shutdown(delay=0.6):
    """Close the Gradio server from a short-lived daemon thread, after a small
    delay so the 'stopped' message reaches the browser first. Closing the server
    unblocks demo.block_thread(), so app.py's browser launch (and `python
    review.py`) returns and the process exits, releasing the lock."""
    def _run():
        time.sleep(delay)
        try:
            demo.close()
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def _quit():
    """Home 'Quit Oshiire' handler (browser mode only). Refuses while a job is
    running -- the job keeps going server-side regardless of the browser tab --
    and otherwise shuts the server down cleanly."""
    if jobs.running() is not None:
        return {
            quit_notice: gr.update(value=_QUIT_BUSY, elem_classes=["quit-note-wrap"]),
            quit_btn: gr.update(interactive=False),
        }
    _schedule_server_shutdown()
    return {
        quit_notice: gr.update(value=_QUIT_STOPPED, elem_classes=["quit-note-wrap"]),
        quit_btn: gr.update(interactive=False, value="Quit Oshiire"),
    }


# --------------------------------------------------------------------------- #
# Idle auto-shutdown (browser mode only)
# --------------------------------------------------------------------------- #
# In browser mode, closing the tab leaves this server running with no window and
# nobody watching it. The open page pings _note_heartbeat on a timer (an
# invisible gr.Timer); a daemon watchdog shuts the server down cleanly -- the
# same path Quit uses -- once there has been no ping for IDLE_SHUTDOWN_SECONDS
# AND no job is running. Native mode never arms the watchdog: there the OS window
# owns the lifecycle, so an idle tab must not close it.
IDLE_SHUTDOWN_SECONDS = 600  # 10 minutes with no heartbeat -> shut down
HEARTBEAT_SECONDS = 30       # the page's ping interval (the gr.Timer value)
_IDLE_CHECK_SECONDS = 15     # how often the watchdog re-evaluates
_IDLE_CHECKS_REQUIRED = 2    # consecutive idle checks before shutting down

_last_heartbeat = None       # monotonic stamp of the last ping (or launch time)
_heartbeat_lock = threading.Lock()


def _note_heartbeat(now=None):
    """Record a page heartbeat. The gr.Timer tick calls this with no argument;
    `now` lets a test drive the clock."""
    global _last_heartbeat
    with _heartbeat_lock:
        _last_heartbeat = time.monotonic() if now is None else now


def _idle_expired(now, last, job_running, limit=IDLE_SHUTDOWN_SECONDS):
    """Pure decision for the watchdog: True only when the page has been quiet
    for `limit` seconds AND no job is running. A running job, or no heartbeat
    recorded yet (`last` is None), both keep the server alive."""
    if job_running:
        return False
    if last is None:
        return False
    return (now - last) >= limit


def _idle_watchdog():
    """Daemon loop (browser mode only): once the page has looked idle past
    IDLE_SHUTDOWN_SECONDS on _IDLE_CHECKS_REQUIRED checks IN A ROW, close the
    server the same way Quit does (release the lock when block_thread returns,
    flush the log). Requiring two consecutive idle checks is what lets a laptop
    waking from sleep -- where the last heartbeat is stale but the tab is still
    open -- receive its next heartbeat and reset, instead of quitting on the
    first post-wake check. Any fresh heartbeat resets the streak."""
    idle_checks = 0
    while True:
        time.sleep(_IDLE_CHECK_SECONDS)
        with _heartbeat_lock:
            last = _last_heartbeat
        if _idle_expired(time.monotonic(), last, jobs.running() is not None):
            idle_checks += 1
            if idle_checks >= _IDLE_CHECKS_REQUIRED:
                _schedule_server_shutdown(delay=0)
                return
        else:
            idle_checks = 0


# --------------------------------------------------------------------------- #
# App settings panel (Manage -> Settings -> App): the launch-mode radio.
# Save-label discipline matches the other panels -- Save is disabled until the
# chosen mode differs from the saved one.
# --------------------------------------------------------------------------- #
def _app_mode_changed(mode):
    """Enable Save only when the picked mode differs from the saved one; clear
    any stale 'Saved.' status as soon as the choice changes again."""
    changed = (mode != settings.window_mode())
    return gr.update(interactive=changed)


def _app_save(mode):
    """Persist the launch mode, then disable Save and confirm. Returns
    (save_btn update, status markdown)."""
    settings.save_window_mode(mode)
    return (gr.update(interactive=False),
            "Saved. Takes effect the next time you open Oshiire.")


_APP_NOTE = "Takes effect the next time you open Oshiire."


def _app_theme_changed(theme_value):
    """Enable the Appearance Save only when the pick differs from what's saved."""
    return gr.update(interactive=(theme_value != settings.theme()))


def _app_theme_save(theme_value):
    """Persist the appearance, disable Save, confirm. Applied live in the page by
    the JS wired after this (toggling .dark), and used on the next launch too."""
    settings.save_theme(theme_value)
    return (gr.update(interactive=False),
            "Saved. Applied now, and used the next time Oshiire opens.")


# Toggle the .dark class live when the appearance is saved, so the open page
# recolours at once. "dark" -> force dark; "light" -> force light; "system" ->
# follow the OS right now. gradio reads .dark on the app element; set it on all
# the plausible hosts so both our tokens and gradio's neutrals switch together.
_APPLY_THEME_JS = """
(theme) => {
  const dark = theme === 'dark' || (theme === 'system' && window.matchMedia
      && window.matchMedia('(prefers-color-scheme: dark)').matches);
  [document.documentElement, document.body, document.querySelector('gradio-app')]
    .filter(Boolean).forEach(el => el.classList.toggle('dark', dark));
  // Keep the address bar in step so a reload holds the saved appearance:
  // ?__theme=light|dark for a forced theme, none for "system". Other query
  // params are preserved.
  try {
    const u = new URL(window.location.href);
    if (theme === 'light' || theme === 'dark') u.searchParams.set('__theme', theme);
    else u.searchParams.delete('__theme');
    window.history.replaceState(null, '', u);
  } catch (e) {}
}
"""


def _app_render():
    """Re-read the saved launch mode AND appearance from disk so both radios show
    what's stored, each with its Save disabled. Run when the App sub-tab is
    selected and when Manage opens -- the file can change between visits (a fresh
    launch writes the mode, the user edits by hand). Returns {component: update}."""
    return {
        app_window_radio: gr.update(value=settings.window_mode()),
        app_save_btn: gr.update(interactive=False),
        app_status_md: _APP_NOTE,
        app_theme_radio: gr.update(value=settings.theme()),
        app_theme_save_btn: gr.update(interactive=False),
        app_theme_status_md: "",
    }


# A job page keeps its result panel between visits, so opening one must wipe the
# previous run's result and restore the pre-run controls. Each returns updates
# for exactly the components listed in its wiring's `outputs`.
def _reset_job_common(run_label):
    return (
        gr.update(visible=True, interactive=True, value=run_label),  # run_btn
        gr.update(elem_classes=_RESULT_HIDDEN),  # result_group
        gr.update(open=False),     # log_acc
        "",                        # log_box
        "",                        # status_md
        gr.update(elem_classes=["page-hidden"]),  # primary_btn
        "",                        # seal (empty HTML renders nothing)
    )


def _reset_ingest():
    # + hide the nothing-new "Free up space" button from a previous run.
    return _reset_job_common("Fetch new saves") + (
        gr.update(elem_classes=["page-hidden"]),)


def _reset_archive_dryrun():
    # + the log accordion closed under its Details name, the two extra next-step
    # buttons hidden, and (last two) the plan table cleared and hidden, so a
    # fresh open shows none of the previous run.
    common = list(_reset_job_common("Show the plan"))
    common[2] = gr.update(open=False, label=_LOG_ACC_LABEL)
    return tuple(common) + (gr.update(elem_classes=["page-hidden"]),
                            gr.update(elem_classes=["page-hidden"]),
                            gr.update(value=""),
                            gr.update(elem_classes=_PLAN_HIDDEN))


def _reset_backfill():
    # + hide the stop button and the drain reminder.
    return _reset_job_common("Run backfill") + (
        gr.update(visible=False), gr.update(visible=False))


def _reset_archive_apply():
    # + clear the dry-run checkbox and hide the drain reminder.
    return _reset_job_common("Apply") + (
        gr.update(value=False), gr.update(visible=False))


def _reset_unsave():
    # + clear the three per-button status lines (Open folder / helper / saved
    # page) so a reopened page shows none of a previous visit's messages.
    return _reset_job_common("Make the list") + ("", "", "")


# --------------------------------------------------------------------------- #
# Phase B: job pages (ingest / backfill / archive) and the menu glue.
#
# Every component referenced below is a module-level global created in the
# gr.Blocks() block far down this file. Like _nav_to / _render_current, these
# functions resolve those names at CALL time, which is always after the block
# has run -- so defining them up here is fine.
# --------------------------------------------------------------------------- #
def _reload_from_disk():
    """Re-read every piece of state review derives from disk at import, IN PLACE
    where handler closures hold the reference (the `manifest` dict, the `queue`
    list, the `layout` dict) and rebinding the rest, then reset the cursor,
    drop any in-progress action/confirmation, and mark the Review page stale.

    Disk is the single source of truth. Another page -- Resolve -- keeps its own
    copies and may have written layout.json, the shortname file, the series
    aliases or the manifest since review last read them; if review saved a stale
    copy back it would silently revert those writes. So this reloads all of it.

    The duplicate-detection snapshots (`archive_hashes`, `archive_path_map`,
    `indexed_paths`) are deliberately NOT rebuilt: they are read once at launch
    and no page writes the archive index, so they stay as they were until the
    app restarts (the same cache the pre-Phase-B reload left stale)."""
    global shortname_entries, wallpaper_rules, series_aliases
    global current_index, last_action, pending_accept, _review_stale
    manifest.clear()
    manifest.update(load_manifest())
    layout.clear()
    layout.update(load_layout())
    shortname_entries = load_shortname_map(layout)
    wallpaper_rules = load_wallpaper_rules(layout)
    series_aliases = load_series_aliases()
    queue[:] = sorted(
        (post_id for post_id, entry in manifest.items() if entry.get("status") == "pending_review"),
        key=lambda post_id: manifest[post_id].get("fetched_at", ""),
    )
    current_index = 0
    last_action = None
    pending_accept = None
    _review_stale = True


def _reload_after_job():
    """A job rewrote the manifest; re-read all review state from disk. Jobs don't
    write layout/shortname/aliases, but a full reload is harmless and keeps one
    reload path (see _reload_from_disk)."""
    _reload_from_disk()


def _ensure_fresh():
    """Re-read review's disk-derived copies if a page that may have written them
    (Resolve) has been visited since review last read disk. Called by _nav_to
    before opening any page that can itself save the manifest or config."""
    global _external_dirty
    if _external_dirty:
        _external_dirty = False
        _reload_from_disk()


def _review_open():
    """Chained after the Review menu button. Repaint only if a job invalidated
    the queue since it was last shown; otherwise skip every output so opening
    Review stays cheap and never clobbers in-progress edits."""
    global _review_stale
    if _review_stale:
        _review_stale = False
        return _render_current()
    return {c: gr.skip() for c in outputs}


_MENU_BUTTONS = None  # all eight menu buttons; bound once the Blocks block runs


def _board_connected_html(archived):
    """The normal board: put-away count + an ok-dot 'connected'."""
    return (
        '<div class="board-text">'
        f'<span><strong>{archived:,}</strong> {"image" if archived == 1 else "images"} put away</span>'
        '<span><span class="dot" style="background:var(--ok)"></span>'
        'Archive folder connected</span></div>'
    )


def _board_warn_html(sentence):
    """The archive-problem board: a warn-dot and the problem sentence, with its
    leading clause bold. Reuses archive_dir_problem's wording verbatim (minus its
    leading ⚠️, which the dot replaces) so Home and the Manage tabs never drift."""
    s = sentence.replace("⚠️", "").strip()
    if "reachable:" in s:  # unreachable: bold up to and including "reachable:"
        head, _, tail = s.partition("reachable:")
        lead, rest = head + "reachable:", tail
    else:  # unset: bold the first sentence
        lead, _, rest = s.partition(". ")
        lead = lead + "." if rest else s
        rest = " " + rest if rest else ""
    return (
        '<div class="board-text"><p>'
        '<span class="dot" style="background:var(--warn)"></span>'
        f'<strong>{html.escape(lead, quote=False)}</strong>'
        f'{html.escape(rest, quote=False)}</p></div>'
    )


def _board_job_html(running):
    """The job-running board. `running` is the lock name; shown by display name."""
    return (
        '<div class="board-text"><p>'
        f'{html.escape(_job_display(running), quote=False)} is running. '
        'Everything else waits until it finishes.'
        '</p></div>'
    )


def _menu_availability():
    """Keep the menu honest and repaint the board. While a job runs, lock EVERY
    door (a reload or a second tab lands here, and must not reach a
    manifest-writing page mid-job) and show the job board with a Show-progress
    button; otherwise enable the doors, grey out Ingest/Backfill (reason on the
    door's second line) or the whole Putting-away shelf (archive unreachable) as
    needed, and show the connected / warning board. Read fresh every call."""
    running = jobs.running()
    kind, sentence = archive_dir_problem()  # re-checked: the drive can mount/drop
    # Counts come from a FRESH disk read (the same loader Review uses), so Home
    # reflects what's on disk right now -- not review's in-memory snapshot. The
    # global `manifest` is deliberately NOT replaced, so Review's state (queue,
    # cursor, pending edits) is untouched. One read (~30ms) covers both counts.
    disk_manifest = load_manifest()
    review_label = _review_door_label(_waiting_count(disk_manifest))

    if running is not None:
        locked = {btn: gr.update(interactive=False) for btn in _MENU_BUTTONS}
        locked.update({
            menu_review_btn: gr.update(interactive=False, value=review_label),
            board_html: _board_job_html(running),
            archive_check_again_btn: gr.update(visible=False),
            job_progress_btn: gr.update(visible=True),
            quit_btn: _quit_btn_update(running),
        })
        return locked

    ingest_ok, ingest_reason = jobs.ingest_available()
    backfill_ok, backfill_reason = jobs.backfill_available()
    # Archive unreachable greys out the whole Putting-away shelf (Preview filing,
    # File images, Resolve) -- none can run without it. The top shelf, History
    # and Manage stay live; Manage gates its own archive-reading Sync tab.
    archive_ok = kind is None
    putting_away = (
        (menu_archive_dryrun_btn, "preview_filing"),
        (menu_archive_apply_btn, "file_images"),
        (menu_resolve_btn, "resolve"),
    )
    updates = {btn: gr.update(interactive=True) for btn in _MENU_BUTTONS}
    updates[menu_review_btn] = gr.update(interactive=True, value=review_label)
    # Ingest / Backfill: disabled with the reason on the door's second line when
    # short (every current reason is well under 60 chars, so this branch always
    # fits; no separate reason line is kept).
    updates[menu_ingest_btn] = gr.update(
        interactive=ingest_ok,
        value=_door_label(_DOOR_TITLE["ingest"],
                          _DOOR_DESC["ingest"] if ingest_ok else ingest_reason))
    updates[menu_backfill_btn] = gr.update(
        interactive=backfill_ok,
        value=_door_label(_DOOR_TITLE["backfill"],
                          _DOOR_DESC["backfill"] if backfill_ok else backfill_reason))
    for btn, key in putting_away:
        updates[btn] = gr.update(
            interactive=archive_ok,
            value=_door_label(_DOOR_TITLE[key],
                              _DOOR_DESC[key] if archive_ok else "Needs the archive folder."))
    updates[board_html] = (
        _board_warn_html(sentence) if kind else _board_connected_html(_archived_count(disk_manifest)))
    updates[archive_check_again_btn] = gr.update(visible=bool(kind))
    updates[job_progress_btn] = gr.update(visible=False)
    updates[quit_btn] = _quit_btn_update(None)
    return updates


_SEAL_HTML = '<span class="seal" lang="ja" aria-label="Done">済</span>'
_LOG_ACC_LABEL = "Details: the full job log"


def _result_text_html(res):
    """The result sentence (.big) and, where the job reported one, a second
    line (.small). HTML so the two sizes/colours apply (gr.Markdown would strip
    the classes)."""
    parts = [f'<p class="big">{html.escape(res.sentence, quote=False)}</p>']
    if res.second_line:
        parts.append(f'<p class="small">{html.escape(res.second_line, quote=False)}</p>')
    return "".join(parts)


def _job_stream(steps, *, run_btn, run_label, back_btn, status_md, log_box,
                name, cancellable, writes_manifest, job_key,
                result_group, seal_html, result_text, primary_btn, log_acc,
                extra_running=None, extra_done=None, extra_result=None,
                plan_table=None, plan_wrap=None):
    """Shared generator behind every job page's run button.

    Takes the single-job lock (refusing, with no lock, if a job is already
    running or a Review confirmation is open), locks this page's run + back
    buttons, opens the log accordion and streams the child output into `log_box`
    (throttled, last 2000 lines). When it ends it fills the result panel from
    the pure job_result(job_key, outcome, counts): the 済 seal on success, a
    plain sentence, and the next-step button; the log accordion closes on
    success or cancel and stays open on failure. Reloads the manifest whenever
    the pipeline wrote it, and leaves refreshing the menu to the
    .then(_menu_availability) on the click.

    A child's OSHIIRE_SUMMARY line (jobs.parse_summary) is never shown: its
    counts go to job_result instead. When `plan_table`/`plan_wrap` are passed
    (Preview filing), the child's OSHIIRE_PLAN lines (jobs.parse_plan_row) are
    likewise kept out of the log and fill the plan table shown on success.
    `extra_result(outcome, res)`, if given, returns updates merged last into the
    final yield (Preview filing uses it for its own next-step buttons).

    run_steps no longer frees the lock, so this holds it across the reload: a
    failed or cancelled backfill/ingest/apply may already have written the
    manifest (backfill checkpoints every 50 rows), and without the reload
    Review's next save would wipe those entries. So the reload runs on success,
    failure AND cancel -- and once more in `finally` for the browser-gone /
    exception path -- before the lock is released."""
    if pending_accept is not None:
        yield {status_md: "Finish or cancel the open confirmation in Review first."}
        return
    if not jobs.try_start(name):
        yield {status_md: f"⏳ “{_job_display(jobs.running())}” is already running. One job at a time."}
        return

    # Counts read around the job, fresh from the manifest the reload updates.
    waiting_before, archived_before = _waiting_count(), _archived_count()
    gen = None
    reloaded = False
    try:
        if cancellable:
            _backfill_cancel.clear()
        first = {
            run_btn: gr.update(interactive=False, value="Running…"),
            back_btn: gr.update(interactive=False),
            status_md: "",
            log_box: "",
            result_group: gr.update(elem_classes=_RESULT_HIDDEN),
            log_acc: gr.update(open=True),
        }
        if plan_wrap is not None:
            first[plan_wrap] = gr.update(elem_classes=_PLAN_HIDDEN)
        if extra_running:
            first.update(extra_running)
        yield first

        lines = []
        ok, summary = True, ""
        job_counts = {}  # from a structured OSHIIRE_SUMMARY line, if one came
        plan_rows = []   # from OSHIIRE_PLAN lines (Preview filing's table)
        last_emit = 0.0
        gen = jobs.run_steps(steps, _backfill_cancel, cancellable)
        for event in gen:
            kind = event[0]
            if kind == "step":
                lines.append(f"── {event[1]} ──")
            elif kind == "line":
                parsed = jobs.parse_summary(event[1])
                if parsed is not None:  # data for the result panel, not the log
                    job_counts = parsed
                    continue
                unsave = jobs.parse_unsave_count(event[1])
                if unsave is not None:  # the Free-up-space count, not the log
                    job_counts.update(unsave)
                    continue
                plan_row = jobs.parse_plan_row(event[1])
                if plan_row is not None:  # a Preview filing table row, not the log
                    plan_rows.append(plan_row)
                    continue
                # Relabel command-line-only hints for the app log (display only;
                # the line is already redacted, data/oshiire.log is untouched).
                lines.append(jobs.relabel_for_app(event[1]))
            elif kind == "done":
                ok, summary = event[1], event[2]
                break
            if len(lines) > 2000:
                del lines[:-2000]
            now = time.monotonic()
            if now - last_emit >= 0.3:
                last_emit = now
                yield {log_box: "\n".join(lines)}

        # Close the child generator (its finally cleans up the process) before
        # reloading, so a non-cancellable child has fully finished writing.
        gen.close()
        if writes_manifest:
            _reload_after_job()
            reloaded = True

        # success / cancelled / failed from the done event ("Cancelled" is the
        # exact summary run_steps yields on a cancel).
        if ok:
            outcome = "success"
        elif summary == "Cancelled":
            outcome = "cancelled"
        else:
            outcome = "failed"
        counts = {
            "new_waiting": max(0, _waiting_count() - waiting_before),
            "filed": max(0, _archived_count() - archived_before),
            "reason": summary,
            **job_counts,
        }
        res = job_result(job_key, outcome, counts)
        done = {
            run_btn: gr.update(visible=False),
            back_btn: gr.update(interactive=True),
            status_md: "",
            log_box: "\n".join(lines),
            result_group: gr.update(elem_classes=_RESULT_SHOWN),
            seal_html: (_SEAL_HTML if res.seal else ""),
            result_text: _result_text_html(res),
            primary_btn: gr.update(value=res.next_label or "",
                                   elem_classes=[] if res.next_label else ["page-hidden"]),
            log_acc: gr.update(open=(outcome == "failed")),
        }
        if plan_table is not None:
            # The table IS the dry run's result: show it on success, built from
            # the structured OSHIIRE_PLAN rows (never by parsing the text table).
            show = outcome == "success" and bool(plan_rows)
            done[plan_table] = gr.update(value=_plan_table_html(plan_rows))
            if plan_wrap is not None:
                done[plan_wrap] = gr.update(
                    elem_classes=_PLAN_SHOWN if show else _PLAN_HIDDEN)
        if extra_done:
            done.update(extra_done(ok))
        if extra_result:
            done.update(extra_result(outcome, res))
        yield done
    finally:
        # Reached on every path that took the lock, including browser-gone /
        # exception. Close the child (idempotent) and, if a manifest-writing job
        # didn't already reload, reload now -- then always release the lock.
        try:
            if gen is not None:
                gen.close()
            if writes_manifest and not reloaded:
                _reload_after_job()
        finally:
            jobs.finish()


def _ingest_result_extra(outcome, res):
    """Ingest's result shows a 'Free up space' button when the run found nothing
    new -- a full saved listing is the usual cause. That is exactly the ingest
    success whose job_result offers no next-step button (seal, no next_label)."""
    nothing_new = outcome == "success" and res.seal and not res.next_label
    return {ingest_freeup_btn: gr.update(
        elem_classes=[] if nothing_new else ["page-hidden"])}


def on_ingest_run():
    yield from _job_stream(
        jobs.ingest_steps(),
        run_btn=ingest_run_btn, run_label="Fetch new saves",
        back_btn=ingest_back_btn, status_md=ingest_status_md, log_box=ingest_log_box,
        name="Ingest", cancellable=False, writes_manifest=True, job_key="ingest",
        result_group=ingest_result_group, seal_html=ingest_seal,
        result_text=ingest_result_text, primary_btn=ingest_primary_btn,
        log_acc=ingest_log_acc,
        extra_running={ingest_freeup_btn: gr.update(elem_classes=["page-hidden"])},
        extra_result=_ingest_result_extra,
    )


def _reddit_username():
    """REDDIT_USERNAME from the merged config, read fresh (never logged). '' when unset."""
    return config.get("REDDIT_USERNAME").strip()


_OLD_REDDIT_URL = "https://old.reddit.com/"
_REDDIT_SAVED_URL = "https://old.reddit.com/user/{user}/saved"


def _saved_page_url(user):
    """The old.reddit saved-page URL. Uses the REDDIT_USERNAME from .env when
    set; otherwise `/user/me/saved`, which old.reddit resolves to the logged-in
    account (user-verified). Read-only: the app only opens it in the browser."""
    user = (user or "").strip()
    return _REDDIT_SAVED_URL.format(user=user or "me")


_USERSCRIPT_PATH = str(datahome.APP_DIR / "assets" / "oshiire_unsave.user.js")
_USERSCRIPT_NAME = Path(_USERSCRIPT_PATH).name


def _static_paths():
    """Files Gradio is allowed to serve: the bundled fonts, and the unsave
    userscript -- that ONE file, so the Free-up-space page can hand it to the
    browser's userscript extension at its absolute /gradio_api/file= URL
    (_gradio_file_url).
    Nothing else in the repo is exposed."""
    return [str(_FONTS_DIR), _USERSCRIPT_PATH]


def _blocked_paths():
    """The settings files gradio must never serve, as absolute paths: both .env
    files and data/config.env. gradio checks blocked_paths before anything it
    allows -- on the /file= route AND when a handler's output file is moved to
    its cache -- so these win even though the data root (cwd) is otherwise
    servable as component output."""
    paths = [path for _, path in config.source_files()]
    paths.append(datahome.APP_DIR / ".env")  # always, even if config read another app dir
    blocked = []
    for path in paths:
        text = str(Path(path).absolute())
        if text not in blocked:
            blocked.append(text)
    return blocked


def on_export_unsave_run():
    """Free-up-space step 1: run export_unsave_list.py as a normal job. It only
    reads the manifest + backfill log and writes data/unsave_list.json, so it
    never touches the review manifest (writes_manifest=False)."""
    yield from _job_stream(
        jobs.export_unsave_steps(),
        run_btn=unsave_run_btn, run_label="Make the list",
        back_btn=unsave_back_btn, status_md=unsave_status_md, log_box=unsave_log_box,
        name="Free up space", cancellable=False, writes_manifest=False,
        job_key="export_unsave",
        result_group=unsave_result_group, seal_html=unsave_seal,
        result_text=unsave_result_text, primary_btn=unsave_primary_btn,
        log_acc=unsave_log_acc,
    )


def _reveal_unsave_list():
    """Open the OS file manager at data/unsave_list.json (local, single-user
    app; works in both browser and native mode since the handler runs on this
    machine). Falls back to the data/ folder when the file isn't built yet.
    Never raises into the UI -- returns a short status line."""
    import subprocess
    target = Path.cwd() / "data" / "unsave_list.json"  # the data root
    folder = target.parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            if target.exists():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)] if target.exists()
                             else ["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception:
        return f"Couldn't open it automatically. The file lives at `{target}`."
    if target.exists():
        return "Opened the folder with **unsave_list.json** selected."
    return "No list yet — run **Make the list** first. Opened the `data` folder."


CHANGE_SETUP_MSG = ("The setup window is open. Changes take effect the next time you "
                    "start Oshiire.")

# The one "Change setup" child, kept so a second click never opens a second window.
_setup_proc = None


def _change_setup_env():
    """The environment for the setup child: the process environment from BEFORE
    config.load() copied file values into it (config.launch_environ), so the
    child reads .env/config.env itself and shows each setting's true source. A
    jobs._child_env() copy would carry every merged value as if it were a real
    environment variable, and the window would show them all as read-only."""
    env = config.launch_environ()
    env = dict(os.environ) if env is None else env
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _open_change_setup(popen=None):
    """Settings -> App: open the setup window in its own process (it is a Tk
    window, and this process belongs to gradio). At most one at a time."""
    global _setup_proc
    import subprocess
    if _setup_proc is not None and _setup_proc.poll() is None:
        return CHANGE_SETUP_MSG
    popen = popen or subprocess.Popen
    kwargs = {"cwd": str(Path.cwd()), "env": _change_setup_env(),
              "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        _setup_proc = popen(
            [jobs._child_python(), str(datahome.CODE_DIR / "setup_window.py"), "--change"],
            **kwargs)
    except OSError as exc:
        return f"Couldn't open the setup window ({type(exc).__name__})."
    return CHANGE_SETUP_MSG


def _open_data_folder():
    """Home ? panel: show the data root (the cwd) in the file manager."""
    import subprocess
    folder = Path.cwd()
    try:
        if sys.platform == "win32":
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception:
        return f"Couldn't open it automatically. Oshiire's data folder is `{folder}`."
    return f"Opened Oshiire's data folder: `{folder}`"


def _open_userscript():
    """Open the app-served userscript URL in the browser so the extension offers
    to install it. The one file is allowlisted via _static_paths()."""
    import webbrowser
    port = getattr(demo, "server_port", None)
    if not port:
        return ("Couldn't find the local server port. Open "
                f"`{_USERSCRIPT_NAME}` from the Oshiire folder in your browser.")
    webbrowser.open(f"http://127.0.0.1:{port}{_gradio_file_url(_USERSCRIPT_PATH)}")
    return ("Opening the helper script — your userscript extension "
            "(Tampermonkey / Violentmonkey) should offer to install it.")


def _open_saved_page():
    """Open the user's own old.reddit saved page in the browser (username from
    .env read fresh, else the /user/me/saved form). The app never clicks
    anything there; it only opens the page for the user."""
    import webbrowser
    webbrowser.open(_saved_page_url(_reddit_username()))
    return "Opening your saved page in your browser…"


def _open_old_reddit():
    """Open old.reddit's front page so the user can confirm they're logged in."""
    import webbrowser
    webbrowser.open(_OLD_REDDIT_URL)
    return "Opening old Reddit — check you're logged in (your username is top-right)."


def _drain_next_text():
    """The post-run drain reminder, shared by Backfill and Archive apply. Points
    at the Free up saved space page and links straight to the saved page, using
    the same URL as that page (REDDIT_USERNAME from .env when set, else the
    /user/me/saved form)."""
    url = _saved_page_url(_reddit_username())
    return (
        "**Next:** the saves just captured are safe to unsave. Open **Free up "
        "saved space** (under **Manage**) for the full steps — or, if the unsave "
        "helper is already set up, load the updated `data/unsave_list.json` and "
        f"[open your saved page]({url})."
    )


def _backfill_done_extra(ok):
    update = {backfill_stop_btn: gr.update(visible=False)}
    if ok:
        update[backfill_next_md] = gr.update(value=_drain_next_text(), visible=True)
    return update


def on_backfill_run(limit):
    limit = int(limit or 0)
    if limit < 1:
        yield {backfill_status_md: "Enter at least 1 post to sweep."}
        return
    yield from _job_stream(
        jobs.backfill_steps(limit),
        run_btn=backfill_run_btn, run_label="Run backfill",
        back_btn=backfill_back_btn, status_md=backfill_status_md, log_box=backfill_log_box,
        name="Backfill", cancellable=True, writes_manifest=True, job_key="backfill",
        result_group=backfill_result_group, seal_html=backfill_seal,
        result_text=backfill_result_text, primary_btn=backfill_primary_btn,
        log_acc=backfill_log_acc,
        extra_running={backfill_stop_btn: gr.update(visible=True),
                       backfill_next_md: gr.update(visible=False)},
        extra_done=_backfill_done_extra,
    )


def _backfill_stop():
    """Stop button: ask the cancellable backfill run to wind down."""
    _backfill_cancel.set()


def _dryrun_result_extra(outcome, res):
    """Preview filing's own result controls. The plan table (filled in
    _job_stream) is the result, so the raw text log stays under a CLOSED
    "Details" accordion on success and only opens when the run failed. The three
    next-step buttons follow job_result: Resolve / Go to Review only as the
    primary, File images as the primary or as the secondary beside Resolve."""
    def shown(on):
        return gr.update(elem_classes=[] if on else ["page-hidden"])
    nxt, sec = res.next_label, res.secondary_label
    file_role = ("primary" if nxt == "File images"
                 else "secondary" if sec == "File images" else None)
    return {
        archive_dryrun_resolve_btn: shown(nxt == "Resolve"),
        archive_dryrun_review_btn: shown(nxt == "Go to Review"),
        archive_dryrun_primary_btn: gr.update(
            value="File images", variant=file_role or "primary",
            elem_classes=[] if file_role else ["page-hidden"]),
        archive_dryrun_log_acc: gr.update(open=(outcome == "failed"), label=_LOG_ACC_LABEL),
    }


def on_archive_dryrun_run():
    yield from _job_stream(
        jobs.archive_dryrun_steps(),
        run_btn=archive_dryrun_run_btn, run_label="Show the plan",
        back_btn=archive_dryrun_back_btn, status_md=archive_dryrun_status_md,
        log_box=archive_dryrun_log_box,
        name="Archive dry run", cancellable=False, writes_manifest=False,
        job_key="preview_filing",
        result_group=archive_dryrun_result_group, seal_html=archive_dryrun_seal,
        result_text=archive_dryrun_result_text, primary_btn=archive_dryrun_primary_btn,
        log_acc=archive_dryrun_log_acc, extra_result=_dryrun_result_extra,
        plan_table=archive_dryrun_plan_table, plan_wrap=archive_dryrun_plan_wrap,
    )


def _archive_apply_done_extra(ok):
    update = {archive_apply_chk: gr.update(value=False)}
    if ok:
        # Apply has just refreshed the duplicate check and the unsave list; the
        # saves it archived are now safe to drain, so show the same reminder
        # Backfill shows.
        update[archive_apply_next_md] = gr.update(value=_drain_next_text(), visible=True)
    return update


def on_archive_apply_run(checked):
    if not checked:
        yield {archive_apply_status_md: "Tick “I've checked Preview filing” first."}
        return
    yield from _job_stream(
        jobs.archive_apply_steps(),
        run_btn=archive_apply_run_btn, run_label="Apply",
        back_btn=archive_apply_back_btn, status_md=archive_apply_status_md,
        log_box=archive_apply_log_box,
        name="Archive apply", cancellable=False, writes_manifest=True,
        job_key="file_images",
        result_group=archive_apply_result_group, seal_html=archive_apply_seal,
        result_text=archive_apply_result_text, primary_btn=archive_apply_primary_btn,
        log_acc=archive_apply_log_acc,
        extra_running={archive_apply_next_md: gr.update(visible=False)},
        extra_done=_archive_apply_done_extra,
    )


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


def _character_validation_md(character_list, franchise_list, crossover=False,
                             same_series_group=False, is_oc=False,
                             known_series=False):
    """Inline, read-only advisory: which typed character names DON'T resolve to
    a folder, each with a closest-folder suggestion. A sibling of
    _character_alias_candidate, gated the SAME way so the inline hint and the
    Accept-time alias prompt agree on when a name should resolve:

    * silent unless EXACTLY ONE franchise (multi-franchise can't tell which
      roster a name belongs to), that franchise resolves to a NESTED def with a
      non-empty roster, and none of crossover / OC / Known-Series /
      same-series-group is set (each routes the image where a character folder
      is moot, so a non-match there is expected);
    * a name already answered persistently (group-routed or dismissed) is
      skipped, exactly as the alias prompt skips it.

    The VERDICT is shortname.resolve_character -- the same call archive.py routes
    with -- never a second matcher. The "did you mean" is difflib over the
    roster, a SUGGESTION only that never changes the verdict and writes nothing.
    """
    if not character_list or not franchise_list:
        return ""
    if len(franchise_list) != 1 or crossover or known_series or is_oc or same_series_group:
        return ""
    folder, franchise_def = franchise_folder_and_def(franchise_list[0], layout, series_aliases)
    if not folder or not franchise_def:
        return ""
    if franchise_def.get("style") != "nested":
        return ""
    choices = sorted(franchise_def.get("characters", []) or [])
    if not choices:
        return ""

    lower_to_folder = {c.casefold(): c for c in choices}
    unresolved = []
    for name in character_list:
        if is_group_routed(folder, name, layout) or is_alias_dismissed(folder, name, layout):
            continue
        if resolve_character(folder, franchise_def, name, layout) is not None:
            continue
        match = difflib.get_close_matches(name.casefold(), list(lower_to_folder), n=1, cutoff=0.6)
        unresolved.append((name, lower_to_folder[match[0]] if match else None))

    if not unresolved:
        return ""

    lines = [f"**Doesn't match a character folder in {folder}:**"]
    for name, suggestion in unresolved:
        if suggestion:
            lines.append(f"- **{name}** — did you mean **{suggestion}**?")
        else:
            lines.append(f"- **{name}** — no matching folder.")
    lines.append("Fix the spelling, or press **Accept** to file as-is and save "
                 "it as an alternate name.")
    return "\n".join(lines)


def _franchise_validation_md(franchise_list):
    """Inline, read-only advisory: which typed FRANCHISE names don't resolve,
    each with a closest-franchise suggestion. The franchise-level sibling of
    _character_validation_md, firing precisely where that one goes silent -- on a
    franchise the character check needs resolved before it does anything.

    VERDICT is shortname.resolve_franchise, the same call archive.py flags with.
    resolve_franchise returns (folder, status); a franchise is KNOWN whenever
    status != "unmapped", INCLUDING an alias to null ("known, no folder yet"),
    so those are never flagged. The "did you mean" is difflib over the names
    resolve_franchise accepts (franchise folders + franchise-alias keys), a
    SUGGESTION only that never changes the verdict.

    Ungated on purpose: every typed franchise is independently checkable, so it
    validates each name even on a crossover -- the multi-franchise case the
    character check deliberately can't touch.
    """
    if not franchise_list:
        return ""
    pool = list((layout.get("franchises") or {}).keys()) + \
           list((layout.get("franchise_aliases") or {}).keys())
    lower_to_name = {}
    for name in pool:
        lower_to_name.setdefault(name.casefold(), name)

    unresolved = []
    for name in franchise_list:
        _, status = resolve_franchise(name, layout, series_aliases)
        if status != "unmapped":
            continue
        match = difflib.get_close_matches(name.casefold(), list(lower_to_name), n=1, cutoff=0.6)
        unresolved.append((name, lower_to_name[match[0]] if match else None))

    if not unresolved:
        return ""

    lines = ["**Doesn't match a known franchise:**"]
    for name, suggestion in unresolved:
        if suggestion:
            lines.append(f"- **{name}** — did you mean **{suggestion}**?")
        else:
            lines.append(f"- **{name}** — no matching franchise.")
    lines.append("Fix the spelling if this is a typo.")
    return "\n".join(lines)


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


def on_character_validation(character_text, franchise_text, crossover_value,
                            same_series_group_value, oc_value, known_series_value):
    """Live inline validation as the reviewer edits the boxes. Advisory only --
    returns the Markdown string, writes nothing. Wired through gr.on so it also
    fires when _render_current sets the boxes on navigation, keeping the line in
    sync without a place in `outputs`."""
    return _character_validation_md(
        _parse_lines(character_text), _parse_lines(franchise_text),
        crossover=bool(crossover_value), same_series_group=bool(same_series_group_value),
        is_oc=bool(oc_value), known_series=bool(known_series_value),
    )


def on_franchise_validation(franchise_text):
    """Live inline franchise validation as the reviewer edits the Franchise box.
    Advisory only. .change() fires on programmatic set too, so the line tracks
    the shown entry on navigation and clears on the empty-queue branch."""
    return _franchise_validation_md(_parse_lines(franchise_text))


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
        _cancel_pending_accept()
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


# ---------------------------------------------------------------------------
# Settings panels: shared rename-guard intent.
#
# Every one of the five Settings sub-panels (subreddit map, franchise aliases,
# character aliases, series aliases, shortnames) is a picker + a name box + a
# Save button. Typing a different name into the box and clicking Save used to
# silently RENAME the picked entry (remove + re-add) -- and a rename onto an
# existing name silently overwrote it. _form_intent decides what a Save click
# actually means from the picker, the name box and the live table, so the
# button label always matches the action and Save never renames by surprise.
# ---------------------------------------------------------------------------

def _sub_same(a, b):
    """Subreddit identity: the same rule remove_subreddit_map_entry matches on."""
    return normalize_subreddit(a or "") == normalize_subreddit(b or "")


def _name_same(a, b):
    """Character-name identity: the same rule remove_character_alias matches on."""
    return normalize_name_key(a or "") == normalize_name_key(b or "")


def _form_intent(selected, key, existing_keys, same, noun):
    """What a Save click means, as (intent, button_label, variant, interactive).

    `selected` is the picker value (the entry being edited, or None/"" for a
    fresh form); `key` is the name-box text; `existing_keys` is the live table;
    `same(a, b)` is the panel's identity rule -- the SAME normaliser its remover
    uses, so "exists" means exactly what a save would collide with. Intents:

      exists        no pick, box names an existing entry  -> refuse, pick it
      new           no pick, box names nothing yet         -> save a new one
      update        a pick, box unchanged (or empty)       -> save in place
      rename_clash  a pick, box names a DIFFERENT existing -> refuse
      rename        a pick, box names a new name           -> remove + re-add
    """
    key = (key or "").strip()
    # A `selected` that matches no live entry is not a real pick -- the
    # subreddit picker allows custom values and hands an unknown typed string
    # straight back, which would otherwise read as "editing" a nonexistent row
    # (a spurious "Save changes", then a rename of something that isn't there).
    if selected and not any(same(selected, k) for k in existing_keys):
        selected = None
    if not selected:
        clash = next((k for k in existing_keys if key and same(key, k)), None)
        if clash is not None:
            return ("exists", f"“{clash}” already exists — pick it above to edit",
                    "secondary", False)
        return ("new", f"Save new {noun}", "primary", True)
    if not key or same(key, selected):
        return ("update", "Save changes", "primary", True)
    clash = next((k for k in existing_keys
                  if not same(k, selected) and same(key, k)), None)
    if clash is not None:
        return ("rename_clash", f"Can't rename — “{clash}” already exists",
                "secondary", False)
    return ("rename", f"Rename “{selected}” → “{key}”", "stop", True)


def _form_buttons(selected, key, existing_keys, same, noun, sel_present):
    """The (Save, Delete) button updates a Settings *_render appends to its
    outputs. Save follows _form_intent; Delete acts on the PICKER value only."""
    _intent, label, variant, interactive = _form_intent(
        selected, key, existing_keys, same, noun)
    save = gr.update(value=label, variant=variant, interactive=interactive)
    if sel_present:
        delete = gr.update(value=f"Delete “{selected}”", variant="stop", interactive=True)
    else:
        delete = gr.update(value="Delete", variant="stop", interactive=False)
    return save, delete


def _resolve_pick(selected, keys, same):
    """Resolve a picker value against the live keys using the panel's identity
    rule. The pickers allow typed values, so `selected` can be anything the user
    typed. Returns:
      ('match', canonical_key)  it names an existing entry (exact, or e.g. a
                                case variant under `same`) -> select that entry
      ('new', text)             a non-empty value that's in no entry -> start a
                                new entry with this name
      ('empty', None)           nothing typed
    """
    text = (selected or "").strip()
    if not text:
        return ("empty", None)
    if text in keys:
        return ("match", text)
    canon = next((k for k in keys if same(text, k)), None)
    if canon is not None:
        return ("match", canon)
    return ("new", text)


def _new_entry_status(text, noun):
    """The status line shown when a typed picker value becomes a new entry."""
    return f"“{text}” isn’t in the list yet. Fill in the rest, then Save new {noun}."


def _rename_guard(intent, label, shown_label):
    """Save-time gate shared by all five panels. Returns a refusal status string
    when the click must NOT write, or None to let the panel's save path run.

      exists / rename_clash -> never write; `label` already names the clash.
      rename whose button label isn't the one the user clicked (`shown_label`
        stale, or None from a test/positional call) -> never write: the form
        changed between the last keystroke and the click.

    'new'/'update', and a rename whose label matches the clicked button, fall
    through (return None)."""
    if intent in ("exists", "rename_clash"):
        return f"⚠️ {label}. Nothing saved."
    if intent == "rename" and shown_label != label:
        return ("⚠️ The form changed before you clicked — check the button and "
                "click again. Nothing saved.")
    return None


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
    box = key_text if key_text is not None else (selected or "")
    # The label shows the name as it will actually be WRITTEN (lowercased,
    # trimmed), so a typed "Asuka Langley " reads as "→ asuka langley". The
    # .input and save handlers normalise the same way, so shown_label matches.
    save_btn, delete_btn = _form_buttons(
        selected, normalize_subreddit(box), keys, _sub_same, "subreddit entry",
        entry is not None)
    return {
        # Match the other four panels: an unknown typed value is not a pick, so
        # it doesn't linger in the picker (allow_custom_value would keep it).
        settings_dropdown: gr.update(choices=keys, value=selected if entry is not None else None),
        settings_key_box: box,
        settings_franchise_box: gr.update(
            value="" if franchise is None else str(franchise),
            interactive=not is_null,
        ),
        settings_null_box: is_null,
        settings_character_box: (entry or {}).get("character") or "",
        settings_extras_md: _settings_extras_md(entry),
        settings_status_md: status,
        settings_count_md: f"**{len(keys)}** subreddit entries.",
        settings_save_btn: save_btn,
        settings_delete_btn: delete_btn,
    }


def on_settings_select(selected):
    kind, val = _resolve_pick(selected, _settings_keys(), _sub_same)
    if kind == "match":
        return _settings_render(selected=val)
    if kind == "new":
        return _settings_render(selected=None, key_text=val,
                                status=_new_entry_status(val, "subreddit entry"))
    return _settings_render(selected=None)


def on_settings_key_input(key_text, selected):
    """Repaint ONLY the Save/Delete buttons as the user types, so the label
    tracks what the next click will do. Never touches the text boxes -- it would
    fight the typing."""
    raw = _settings_raw()
    return _form_buttons(selected, normalize_subreddit(key_text or ""),
                         _settings_keys(raw), _sub_same, "subreddit entry",
                         bool(selected) and selected in raw)


def on_settings_add():
    return _settings_render(
        selected=None, key_text="",
        status="Form cleared — type a name, fill in the rest, then Save new subreddit entry.",
    )


def on_settings_null_toggle(is_null):
    """A null franchise and an empty one are different things in this file, so
    the control that chooses between them also greys out the textbox -- an
    editable box whose contents are about to be ignored is a trap."""
    return gr.update(interactive=not is_null)


def on_settings_save(key_text, franchise_text, is_null, character_text, selected,
                     shown_label=None):
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
    keys_before = _settings_keys(raw_before)
    # Only a selection that names a live entry counts as a pick, and the label
    # is computed from the normalised key -- both the same way _settings_render
    # and on_settings_key_input did, so `intent`, `renamed_from` and the
    # button the user saw all agree.
    selected = selected if selected in keys_before else None
    intent, label, _v, _i = _form_intent(
        selected, key, keys_before, _sub_same, "subreddit entry")
    refusal = _rename_guard(intent, label, shown_label)
    if refusal is not None:
        return _settings_render(selected=selected if intent != "exists" else None,
                                key_text=key_text, status=refusal)

    renamed_from = selected if intent == "rename" else None
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
    rewrite an archive_path to somewhere the file no longer is.

    Also the archive gate, re-checked every open: Scan reads ARCHIVE_DIR, so
    when the archive is unreachable the same problem sentence the Home banner
    shows is surfaced here and Scan is disabled (Apply is already disabled with
    the plan cleared)."""
    global sync_plan
    sync_plan = None
    kind, sentence = archive_dir_problem()
    if kind:
        return {
            sync_archive_problem_md: gr.update(value=sentence, visible=True),
            sync_scan_btn: gr.update(interactive=False),
            sync_apply_btn: gr.update(interactive=False),
            sync_report_md: "",
            sync_status_md: "",
        }
    return {
        sync_archive_problem_md: gr.update(value="", visible=False),
        sync_scan_btn: gr.update(interactive=True),
        sync_apply_btn: gr.update(interactive=False),
        sync_report_md: "",
        sync_status_md: (
            "**Scan** reads the archive and writes nothing at all. Only "
            "**Apply reconcile** writes, and only to `manifest.json` and the "
            "pHash index — no file is ever moved, copied or deleted from here."
        ),
    }


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
                     "`python src/hash_index.py build` to fully resync it.")

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
            f"Move them into `{new_rel}/` in your file browser, then open "
            f"**Manage → Sync**.")


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


# ---------------------------------------------------------------------------
# Character Folders tab: Series folders -- give a series filed under
# Others/Known Series its own folder, or create a brand-new series folder.
#
# Both are layout.json ONLY (create_series / promote_series). The shortname file
# is never written -- the "CODE = Full Name" legend stays as the permanent
# decoder for the `_CODE` suffix already-filed images carry -- and no file under
# ARCHIVE_DIR is moved: the user drags the archived images across in their file
# browser and reconciles them on the Sync tab. So, exactly like promote/merge
# above, every finished operation ends with a count of how many archived images
# are still waiting to be moved.
#
# Outcome language only: the words "alias", "roster" and "shortname entry" never
# reach the screen. A promoted series is one whose FUTURE images "file to
# <folder>/"; the legend line that keeps old filenames decodable is "kept".
# ---------------------------------------------------------------------------


def _series_promotion_scan(manifest, layout, code, shortname_entries, series_aliases):
    """Read-only survey of what promoting shortname `code` affects. Every input
    is an argument (no module globals) so it is testable in isolation.

    Returns (tags, archived_in_known, override_count):
      - tags: the distinct primary-franchise tags (entry["franchise"][0]) across
        every manifest entry, ANY status, that currently reach the shortname
        entry `code` (match_shortname) AND do not resolve to a franchise folder
        yet (franchise_folder_and_def is (None, None)). These are exactly the
        tags that fall through to Others/Known Series today, and the ones
        promote_series must alias onto the new folder.
      - archived_in_known: how many ARCHIVED entries sit under
        special_folders["others_known_series"] (sync._under, the Sync tab's own
        comparison, reused so the two never disagree) whose primary tag matches
        `code` -- the files the user still has to drag across after promoting.
      - override_count: how many of THOSE were put there deliberately with
        "File as Known Series" (archive_override == "known_series"), so the UI
        can warn before the user moves them.

    An empty/None `code` matches nothing: there is no legend to promote from, and
    without this guard a None code would spuriously collect every tag that
    matches no shortname at all (None == None)."""
    if not code:
        return [], 0, 0
    known_root = (layout.get("special_folders") or {}).get("others_known_series") or ""
    tags = []
    archived_in_known = 0
    override_count = 0
    for entry in manifest.values():
        if not isinstance(entry, dict):
            continue
        franchise_list = entry.get("franchise") or []
        primary = franchise_list[0] if franchise_list else None
        if not primary:
            continue
        if match_shortname(primary, shortname_entries, series_aliases) != code:
            continue

        folder, _ = franchise_folder_and_def(primary, layout, series_aliases)
        if folder is None and not any(same_series_name(primary, seen) for seen in tags):
            tags.append(primary)

        if entry.get("status") == "archived":
            rel = entry.get("archive_path")
            if rel and sync._under(rel, known_root):
                archived_in_known += 1
                if entry.get("archive_override") == "known_series":
                    override_count += 1
    return tags, archived_in_known, override_count


def _series_code_for(full_name, entries):
    """The shortname code decoding `full_name`, or None. Identity is the full
    name (same_series_name), the same rule the Shortnames settings panel uses."""
    for code, full in entries:
        if same_series_name(full, full_name):
            return code
    return None


def _series_choices():
    """The shortname lines as ("Full Name (CODE)", full_name) options, read LIVE
    off disk. The value is the full name (the entry's identity), so a picked line
    round-trips to its code through _series_code_for."""
    return [(f"{full} ({code})", full) for code, full in load_shortname_map(layout)]


def _series_confirm_label(full_name, folder):
    folder = (folder or "").strip()
    if full_name and folder:
        return f"Future {full_name} images file to “{folder}/”"
    return "Confirm before applying"


def _series_move_line(count, override_count, code, folder, known_root):
    """The move-then-Sync line every finished promotion ends with, plus the
    on-purpose warning. Count only -- moving the files would be a write inside
    ARCHIVE_DIR, which this process never does."""
    if not count:
        return f"No archived images are filed under `{known_root}/` for this series."
    line = (f"**{count}** archived image(s) are in `{known_root}/` — their filenames "
            f"end in `_{code}`. Move them into `{folder}/` in your file browser, "
            f"then open **Manage → Sync**.")
    if override_count:
        line += (f" **{override_count}** of them were filed there on purpose with "
                 "“File as Known Series” — leave those if you want them kept there.")
    return line


def _series_render(selected=None, folder=None, preview="", status=""):
    """Full repaint of the "Give a series its own folder" block as
    {component: value}. Every render resets the confirm box to unticked so each
    Apply needs a fresh confirmation."""
    return {
        series_dropdown: gr.update(choices=_series_choices(), value=selected),
        series_folder_box: folder if folder is not None else (selected or ""),
        series_preview_md: preview,
        series_confirm_chk: gr.update(value=False,
                                      label=_series_confirm_label(selected, folder)),
        series_status_md: status,
    }


def _series_tab_open():
    """Repopulate on tab open: refresh the dropdown off the live shortname file,
    clear the rest. Selection is dropped because the folder box below is prefilled
    from it and a stale selection would show a folder for a series the user isn't
    looking at."""
    return _series_render()


def on_series_select(selected):
    """Prefill the folder box with the full name when a line is picked; the user
    edits it if they want a shorter folder name."""
    return _series_render(selected=selected, folder=selected)


def on_series_preview(selected, folder):
    """Describe the promotion without writing anything: new-or-existing folder,
    which spellings start filing there, which are left alone, how many archived
    images must be moved, and that the legend line is kept. A ValueError (bad
    folder name, non-flat target) renders as its message, never a traceback."""
    if not selected:
        return _series_render(selected, folder,
                              status="⚠️ Pick a series line first.")
    entries = load_shortname_map(layout)
    code = _series_code_for(selected, entries)
    tags, archived_in_known, override_count = _series_promotion_scan(
        manifest, layout, code, entries, series_aliases
    )
    try:
        report = preview_series_promotion(layout, code, selected, folder, tags, series_aliases)
    except ValueError as exc:
        return _series_render(selected, folder, preview=f"⚠️ {exc}")

    dest = report["folder"]
    lines = []
    if report["created"]:
        lines.append(f"A new folder **`{dest}/`** will be created.")
    else:
        lines.append(f"The existing folder **`{dest}/`** will be used.")

    if report["aliases_added"]:
        spellings = ", ".join(f"**{key}**" for key, _ in report["aliases_added"])
        lines.append(f"These spellings will file there from now on: {spellings}.")
    else:
        lines.append("No new spellings needed — they already file there.")

    if report["skipped"]:
        left = "; ".join(f"**{tag}** ({reason})" for tag, reason in report["skipped"])
        lines.append(f"Left alone: {left}.")

    known_root = (layout.get("special_folders") or {}).get("others_known_series") or ""
    lines.append(_series_move_line(archived_in_known, override_count, code, dest, known_root))
    if code:
        lines.append(f"The `{code} = {selected}` legend line is kept, so already-filed "
                     "images keep decoding.")

    return _series_render(selected, folder, preview="\n\n".join(lines))


def on_series_apply(selected, folder, confirm):
    """Commit the promotion, then show the same move-then-Sync count line. Gated
    on the confirm box; a ValueError renders as its message."""
    if not selected:
        return _series_render(selected, folder, status="⚠️ Pick a series line first.")
    if not confirm:
        return _series_render(selected, folder,
                              status="⚠️ Tick the confirmation box first — nothing changed.")
    entries = load_shortname_map(layout)
    code = _series_code_for(selected, entries)
    tags, archived_in_known, override_count = _series_promotion_scan(
        manifest, layout, code, entries, series_aliases
    )
    try:
        report = promote_series(code, selected, folder, tags, layout, series_aliases)
    except ValueError as exc:
        return _series_render(selected, folder, status=f"⚠️ {exc}")

    dest = report["folder"]
    known_root = (layout.get("special_folders") or {}).get("others_known_series") or ""
    move = _series_move_line(archived_in_known, override_count, code, dest, known_root)
    return _series_render(
        status=f"✅ Future **{selected}** images now file to `{dest}/`. {move}"
    )


# ---------- Create a new series (a fresh flat folder, no promotion) ----------

def _create_series_render(name="", status=""):
    return {
        create_name_box: name,
        create_confirm_chk: gr.update(value=False),
        create_status_md: status,
    }


def on_series_create(name, confirm):
    """Create a brand-new flat series folder. Gated on the confirm box; a
    ValueError (bad name, collision, intercepting alias) renders as its message."""
    if not confirm:
        return _create_series_render(name,
                                     status="⚠️ Tick the confirmation box first — nothing created.")
    try:
        create_series(name, layout)
    except ValueError as exc:
        return _create_series_render(name, status=f"⚠️ {exc}")
    stripped = (name or "").strip()
    return _create_series_render(
        status=(f"✅ Future images tagged exactly **{stripped}** now file to "
                f"`{stripped}/`. Other spellings can be added under "
                "**Settings → Franchise aliases**.")
    )


# ---------------------------------------------------------------------------
# Settings tab: Franchise aliases -- layout.json `franchise_aliases`, a FLAT
# {variant: folder-or-null} map, edited through save_franchise_alias /
# remove_franchise_alias.
#
# Operates on review.py's OWN in-memory `layout`, the same dict routing and the
# Character Folders tab mutate: the writers change it in place and persist, so
# an edit here reaches the next routed image with no reload. A null value is a
# SHAPE ("franchise known, no folder yet"), distinct from empty -- the same
# null/empty split the subreddit panel makes.
# ---------------------------------------------------------------------------

def _fa_table():
    return layout.get("franchise_aliases") or {}


def _fa_folder_exists(folder):
    """True iff `folder` names a real franchise folder in layout, matched the
    same case-insensitive way routing resolves it."""
    matched, _ = lookup_ci(layout.get("franchises") or {}, folder)
    return matched is not None


def _fa_caution(folder, is_null):
    """Soft guard: a NON-null alias whose folder doesn't exist won't route
    anything. Warn, never block -- null is always fine."""
    if is_null or not folder:
        return ""
    if _fa_folder_exists(folder):
        return ""
    return (f"⚠️ No franchise folder **{folder}** exists in layout yet — this alias "
            f"won't route anything until that folder is created. (A null alias is "
            f"always fine.)")


def _fa_render(selected=None, status="", key_text=None):
    table = _fa_table()
    keys = sorted(table)
    has = selected in table
    value = table.get(selected) if has else None
    is_null = has and value is None
    folder = "" if value is None else str(value)
    box = key_text if key_text is not None else (selected or "")
    save_btn, delete_btn = _form_buttons(
        selected, box, keys, same_series_name, "franchise alias", has)
    return {
        fa_dropdown: gr.update(choices=keys, value=selected if has else None),
        fa_key_box: box,
        fa_folder_box: gr.update(value=folder, interactive=not is_null),
        fa_null_box: is_null,
        fa_caution_md: _fa_caution(folder, is_null),
        fa_status_md: status,
        fa_count_md: f"**{len(keys)}** franchise alias(es).",
        fa_save_btn: save_btn,
        fa_delete_btn: delete_btn,
    }


def on_fa_select(selected):
    kind, val = _resolve_pick(selected, sorted(_fa_table()), same_series_name)
    if kind == "match":
        return _fa_render(selected=val)
    if kind == "new":
        return _fa_render(selected=None, key_text=val,
                          status=_new_entry_status(val, "franchise alias"))
    return _fa_render(selected=None)


def on_fa_key_input(key_text, selected):
    table = _fa_table()
    return _form_buttons(selected, key_text, sorted(table), same_series_name,
                         "franchise alias", selected in table)


def on_fa_add():
    return _fa_render(selected=None, key_text="",
                      status="Form cleared — type a name, fill in the rest, then "
                             "Save new franchise alias.")


def on_fa_null_toggle(is_null):
    """Same trap as the subreddit null box: an editable folder field whose
    contents are about to be ignored. Grey it out when null is chosen."""
    return gr.update(interactive=not is_null)


def on_fa_save(key_text, folder_text, is_null, selected, shown_label=None):
    key = (key_text or "").strip()
    if not key:
        return _fa_render(selected=selected, key_text=key_text,
                          status="⚠️ Variant name is empty — nothing saved.")
    folder_text = (folder_text or "").strip()
    if is_null:
        folder = None
    elif not folder_text:
        return _fa_render(selected=selected, key_text=key_text,
                          status="⚠️ Folder is empty — type a folder, or tick "
                                 "**No folder yet** to write `null` (franchise known, "
                                 "no folder yet).")
    else:
        folder = folder_text

    intent, label, _v, _i = _form_intent(
        selected, key_text, sorted(_fa_table()), same_series_name, "franchise alias")
    refusal = _rename_guard(intent, label, shown_label)
    if refusal is not None:
        return _fa_render(selected=selected if intent != "exists" else None,
                          key_text=key_text, status=refusal)

    renamed_from = selected if intent == "rename" else None
    if renamed_from:
        remove_franchise_alias(renamed_from, layout)
    save_franchise_alias(key, folder, layout)

    shown = "`null` (no folder yet)" if folder is None else f"→ **{folder}**"
    action = (f"Renamed **{renamed_from}** → **{key}**" if renamed_from
              else f"Saved **{key}**")
    return _fa_render(selected=key, status=f"✅ {action} {shown}.")


def on_fa_delete(selected):
    if not selected:
        return _fa_render(selected=None, status="⚠️ Nothing selected — nothing deleted.")
    if selected not in _fa_table():
        return _fa_render(selected=None,
                          status=f"⚠️ **{selected}** isn't in the table — nothing deleted.")
    remove_franchise_alias(selected, layout)
    return _fa_render(selected=None, key_text="", status=f"🗑️ Deleted **{selected}**.")


# ---------------------------------------------------------------------------
# Settings tab: Character aliases -- layout.json `character_aliases`, nested by
# franchise FOLDER {folder: {variant: canonical}}. Two-level: pick a nested
# franchise folder first, then edit that folder's aliases via
# save_character_alias / remove_character_alias. Same in-memory `layout`.
#
# HARD guard: a variant that normalizes (normalize_name_key) to the franchise
# folder's OWN name is refused -- that "Genshin Impact": "Ayaka" shape files
# every franchise-only tag into one character, and is exactly the store that bug
# lived in.
# ---------------------------------------------------------------------------

def _ca_aliases(folder):
    return (layout.get("character_aliases") or {}).get(folder) or {}


def _ca_impact(folder, variant):
    """Informational only: how many already-archived images tagged `variant`
    sit under this franchise folder. An alias change re-routes FUTURE images;
    these are read-only context, reusing _archived_count_under."""
    if not folder or not variant:
        return ""
    count = _archived_count_under(variant, folder)
    if not count:
        return ""
    return (f"ℹ️ **{count}** archived image(s) tagged “{variant}” are already filed "
            f"under `{folder}/`. Changing this alias only affects images filed "
            f"from now on.")


def _ca_render(folder=None, selected=None, status="", key_text=None, impact=""):
    folders = _nested_franchises()
    valid_folder = folder if folder in folders else None
    aliases = _ca_aliases(valid_folder) if valid_folder else {}
    variants = sorted(aliases)
    has = bool(valid_folder) and selected in aliases
    canonical = aliases.get(selected, "") if has else ""
    if valid_folder:
        count = f"**{valid_folder}** — {len(variants)} character alias(es)."
    else:
        count = f"**{len(folders)}** series file into character folders."
    box = key_text if key_text is not None else (selected or "")
    save_btn, delete_btn = _form_buttons(
        selected, box, variants, _name_same, "character alias", has)
    return {
        ca_franchise_dropdown: gr.update(choices=folders, value=valid_folder),
        ca_variant_dropdown: gr.update(choices=variants, value=selected if has else None),
        ca_key_box: box,
        ca_canonical_box: str(canonical),
        ca_impact_md: impact,
        ca_status_md: status,
        ca_count_md: count,
        ca_save_btn: save_btn,
        ca_delete_btn: delete_btn,
    }


def on_ca_franchise_select(folder):
    return _ca_render(folder=folder)


def on_ca_variant_select(folder, selected):
    aliases = _ca_aliases(folder) if folder in _nested_franchises() else {}
    kind, val = _resolve_pick(selected, sorted(aliases), _name_same)
    if kind == "match":
        return _ca_render(folder=folder, selected=val, impact=_ca_impact(folder, val))
    if kind == "new":
        return _ca_render(folder=folder, selected=None, key_text=val,
                          status=_new_entry_status(val, "character alias"))
    return _ca_render(folder=folder, selected=None)


def on_ca_key_input(folder, key_text, selected):
    folders = _nested_franchises()
    aliases = _ca_aliases(folder) if folder in folders else {}
    return _form_buttons(selected, key_text, sorted(aliases), _name_same,
                         "character alias", selected in aliases)


def on_ca_add(folder):
    if not folder or folder not in _nested_franchises():
        return _ca_render(folder=folder, status="⚠️ Pick a series first.")
    return _ca_render(folder=folder, key_text="",
                      status="Form cleared — type a name, fill in the rest, then "
                             "Save new character alias.")


def on_ca_save(folder, key_text, canonical_text, selected, shown_label=None):
    if not folder or folder not in _nested_franchises():
        return _ca_render(folder=folder, status="⚠️ Pick a series first — nothing saved.")
    key = (key_text or "").strip()
    if not key:
        return _ca_render(folder=folder, selected=selected, key_text=key_text,
                          status="⚠️ Name is empty — nothing saved.")
    # HARD guard: a variant that resolves to the franchise's own folder name
    # would route every franchise-only tag into one character.
    if normalize_name_key(key) == normalize_name_key(folder):
        return _ca_render(folder=folder, selected=selected, key_text=key_text,
                          status=(f"🚫 **{key}** is the series’ own name — saving it as a "
                                  f"character alias would file every **{folder}** image "
                                  f"into one character folder. Nothing saved."))
    canonical = (canonical_text or "").strip()
    if not canonical:
        return _ca_render(folder=folder, selected=selected, key_text=key_text,
                          status="⚠️ Target folder is empty — nothing saved.")

    intent, label, _v, _i = _form_intent(
        selected, key_text, sorted(_ca_aliases(folder)), _name_same, "character alias")
    refusal = _rename_guard(intent, label, shown_label)
    if refusal is not None:
        return _ca_render(folder=folder,
                          selected=selected if intent != "exists" else None,
                          key_text=key_text, status=refusal)

    renamed_from = selected if intent == "rename" else None
    if renamed_from:
        remove_character_alias(folder, renamed_from, layout)
    save_character_alias(folder, key, canonical, layout)

    action = (f"Renamed **{renamed_from}** → **{key}**" if renamed_from else f"Saved **{key}**")
    return _ca_render(folder=folder, selected=key,
                      status=f"✅ {action} — files under **{canonical}** in {folder}.")


def on_ca_delete(folder, selected):
    if not folder or not selected:
        return _ca_render(folder=folder, status="⚠️ Nothing selected — nothing deleted.")
    if selected not in _ca_aliases(folder):
        return _ca_render(folder=folder,
                          status=f"⚠️ **{selected}** isn't an alias in {folder} — nothing deleted.")
    remove_character_alias(folder, selected, layout)
    return _ca_render(folder=folder, key_text="", status=f"🗑️ Deleted **{selected}**.")


# ---------------------------------------------------------------------------
# Settings tab: Series aliases -- data/series_aliases.json {variant: canonical},
# a FLAT map edited through save_series_alias / remove_series_alias (both go
# through save_series_aliases, now raise-on-malformed). Read LIVE each open;
# after a write the module-level `series_aliases` routing/validation consult is
# refreshed, so an edit here applies to the next tagged post immediately.
# ---------------------------------------------------------------------------

def _refresh_series_aliases():
    global series_aliases
    series_aliases = load_series_aliases()


def _sa_render(selected=None, status="", key_text=None):
    table = load_series_aliases()
    keys = sorted(table)
    has = selected in table
    canonical = table.get(selected, "") if has else ""
    box = key_text if key_text is not None else (selected or "")
    save_btn, delete_btn = _form_buttons(
        selected, box, keys, same_series_name, "series alias", has)
    return {
        sa_dropdown: gr.update(choices=keys, value=selected if has else None),
        sa_key_box: box,
        sa_canonical_box: str(canonical),
        sa_status_md: status,
        sa_count_md: f"**{len(keys)}** series alias(es).",
        sa_save_btn: save_btn,
        sa_delete_btn: delete_btn,
    }


def on_sa_select(selected):
    kind, val = _resolve_pick(selected, sorted(load_series_aliases()), same_series_name)
    if kind == "match":
        return _sa_render(selected=val)
    if kind == "new":
        return _sa_render(selected=None, key_text=val,
                          status=_new_entry_status(val, "series alias"))
    return _sa_render(selected=None)


def on_sa_key_input(key_text, selected):
    table = load_series_aliases()
    return _form_buttons(selected, key_text, sorted(table), same_series_name,
                         "series alias", selected in table)


def on_sa_add():
    return _sa_render(selected=None, key_text="",
                      status="Form cleared — type a name, fill in the rest, then "
                             "Save new series alias.")


def on_sa_save(key_text, canonical_text, selected, shown_label=None):
    key = (key_text or "").strip()
    if not key:
        return _sa_render(selected=selected, key_text=key_text,
                          status="⚠️ Variant name is empty — nothing saved.")
    canonical = (canonical_text or "").strip()
    if not canonical:
        return _sa_render(selected=selected, key_text=key_text,
                          status="⚠️ Canonical name is empty — nothing saved.")
    intent, label, _v, _i = _form_intent(
        selected, key_text, sorted(load_series_aliases()), same_series_name, "series alias")
    refusal = _rename_guard(intent, label, shown_label)
    if refusal is not None:
        return _sa_render(selected=selected if intent != "exists" else None,
                          key_text=key_text, status=refusal)
    renamed_from = selected if intent == "rename" else None
    if renamed_from:
        remove_series_alias(renamed_from)
    save_series_alias(key, canonical)
    _refresh_series_aliases()
    action = (f"Renamed **{renamed_from}** → **{key}**" if renamed_from else f"Saved **{key}**")
    return _sa_render(selected=key, status=f"✅ {action} → **{canonical}**.")


def on_sa_delete(selected):
    if not selected:
        return _sa_render(selected=None, status="⚠️ Nothing selected — nothing deleted.")
    if selected not in load_series_aliases():
        return _sa_render(selected=None,
                          status=f"⚠️ **{selected}** isn't in the table — nothing deleted.")
    remove_series_alias(selected)
    _refresh_series_aliases()
    return _sa_render(selected=None, key_text="", status=f"🗑️ Deleted **{selected}**.")


# ---------------------------------------------------------------------------
# Settings tab: Shortnames -- the line-based shortname file ("CODE = Full Name"),
# edited through save_shortname_entry / remove_shortname_entry. Identity is
# full_name (both writers match on same_series_name), so the dropdown value is
# the full name. Read LIVE each open; after a write the module-level
# `shortname_entries` the Accept path consults is refreshed. Changing a CODE
# affects only images filed FROM NOW ON -- already-filed suffixes are inert and
# nothing on disk renames.
# ---------------------------------------------------------------------------

def _sn_path():
    return Path(layout["shortname_file"])


def _sn_entries():
    return load_shortname_map(layout)


def _sn_find(full_name):
    for code, full in _sn_entries():
        if same_series_name(full, full_name):
            return code, full
    return None


def _refresh_shortname_entries():
    global shortname_entries
    shortname_entries = load_shortname_map(layout)


def _sn_render(selected=None, status="", code_text=None, full_text=None):
    entries = _sn_entries()
    choices = [(f"{code} = {full}", full) for code, full in entries]
    found = _sn_find(selected) if selected else None
    full_box = (full_text if full_text is not None
                else (found[1] if found else (selected or "")))
    save_btn, delete_btn = _form_buttons(
        selected, full_box, [full for _c, full in entries], same_series_name,
        "shortname", found is not None)
    return {
        sn_dropdown: gr.update(choices=choices, value=found[1] if found else None),
        sn_code_box: code_text if code_text is not None else (found[0] if found else ""),
        sn_fullname_box: full_box,
        sn_status_md: status,
        sn_count_md: f"**{len(entries)}** shortname entr(ies).",
        sn_save_btn: save_btn,
        sn_delete_btn: delete_btn,
    }


def on_sn_select(selected):
    fulls = [full for _c, full in _sn_entries()]
    kind, val = _resolve_pick(selected, fulls, same_series_name)
    if kind == "match":
        return _sn_render(selected=val)
    if kind == "new":
        return _sn_render(selected=None, full_text=val,
                          status=_new_entry_status(val, "shortname"))
    return _sn_render(selected=None)


def on_sn_key_input(full_text, selected):
    """Shortnames key on the FULL series name, so this listens on the full-name
    box, not the code box. The code never changes what a Save means."""
    entries = _sn_entries()
    return _form_buttons(selected, full_text, [full for _c, full in entries],
                         same_series_name, "shortname", _sn_find(selected) is not None)


def on_sn_add():
    return _sn_render(selected=None, code_text="", full_text="",
                      status="Form cleared — type a name, fill in the rest, then "
                             "Save new shortname.")


def on_sn_save(code_text, full_text, selected, shown_label=None):
    code = (code_text or "").strip()
    full = (full_text or "").strip()
    if not full:
        return _sn_render(selected=selected, code_text=code_text, full_text=full_text,
                          status="⚠️ Full series name is empty — nothing saved.")
    if not code:
        return _sn_render(selected=selected, code_text=code_text, full_text=full_text,
                          status="⚠️ Code is empty — nothing saved.")
    path = _sn_path()
    collision = find_shortname_collision(path, code, full)
    if collision:
        return _sn_render(selected=selected, code_text=code_text, full_text=full_text,
                          status=(f"⚠️ Code **{code}** is already used by “{collision}”. "
                                  f"Pick a different code — nothing saved."))
    # Identity is full_name, so a changed full name is a rename: drop the old
    # line first, or the file keeps both under two codes.
    intent, label, _v, _i = _form_intent(
        selected, full_text, [f for _c, f in _sn_entries()], same_series_name, "shortname")
    refusal = _rename_guard(intent, label, shown_label)
    if refusal is not None:
        return _sn_render(selected=selected if intent != "exists" else None,
                          code_text=code_text, full_text=full_text, status=refusal)
    renamed_from = selected if intent == "rename" else None
    if renamed_from:
        remove_shortname_entry(path, renamed_from)
    save_shortname_entry(path, code, full)
    _refresh_shortname_entries()
    if not verify_shortname_entry(path, code, full):
        return _sn_render(selected=full,
                          status="⚠️ The save didn't persist to disk — check the shortname file path.")
    action = (f"Renamed “{renamed_from}” → " if renamed_from else "Saved ")
    return _sn_render(selected=full, status=f"✅ {action}**{code} = {full}**.")


def on_sn_delete(selected):
    if not selected:
        return _sn_render(selected=None, status="⚠️ Nothing selected — nothing deleted.")
    if _sn_find(selected) is None:
        return _sn_render(selected=None, status=f"⚠️ “{selected}” isn't in the file — nothing deleted.")
    remove_shortname_entry(_sn_path(), selected)
    _refresh_shortname_entries()
    return _sn_render(selected=None, code_text="", full_text="", status=f"🗑️ Deleted “{selected}”.")


# delete_cache=(freq, age): sweep every `freq`s, drop served-file copies older
# than `age`s. Gradio copies every image it serves into %TEMP%\gradio and never
# prunes it otherwise, so a long-lived desktop session grows it without bound.
with gr.Blocks(title="Oshiire", analytics_enabled=False, delete_cache=(86400, 86400)) as demo:
  with gr.Column(elem_id="pages"):
   with gr.Column(elem_classes=["page"], elem_id="home-page") as home_page:
    with gr.Column(elem_classes=["inner-wrap"]):
     # Header: the name + kanji on the left, the two "always" doors and the Help
     # toggle on the right. History/Manage are real gr.Buttons (one tab stop
     # each) styled as small outline "tools"; "?" toggles the help panel below,
     # replacing the old accordion with the same content.
     with gr.Row(elem_id="home-header"):
        gr.HTML('<h1>Oshiire <span class="kanji" lang="ja">押入れ</span></h1>', elem_id="home-title")
        with gr.Row(elem_id="home-tools"):
            menu_history_btn = gr.Button("History", elem_classes=["tool"])
            menu_manage_btn = gr.Button("Manage", elem_classes=["tool"])
            # Browser mode only -- hidden in a native window (where the OS close
            # button quits). Starts hidden via .page-hidden; _menu_availability
            # reveals it when the launch mode is browser.
            quit_btn = gr.Button(
                "Quit Oshiire", elem_classes=["tool", "page-hidden"])
            help_btn = gr.Button("?", elem_classes=["tool", "round"])
     # The Quit message ("Oshiire has stopped…" / the busy reason). Hidden until
     # Quit is pressed; rendered as HTML so it can carry its own styling.
     quit_notice = gr.HTML("", elem_classes=["quit-note-wrap", "page-hidden"])
     # Help panel: hidden until "?" is pressed. Same text as the old accordion.
     # The State tracks open/closed so "?" is a pure toggle (no page reload).
     help_state = gr.State(False)
     with gr.Column(elem_id="home-help", elem_classes=["page-hidden"]) as help_panel:
        help_md = gr.Markdown(HELP_MD)
        # Where the manifest, staging/ and data/ live (the data root), and which
        # Oshiire this is.
        with gr.Row():
            data_folder_btn = gr.Button("Open data folder", elem_classes=["tool"],
                                        size="sm", scale=0)
            version_md = gr.Markdown(f"Oshiire {__version__}", elem_id="app-version")
        data_folder_md = gr.Markdown("")

     with gr.Column(elem_classes=["closet"]):
        # Shelf 1 -- "Coming in": Ingest + Backfill stacked on the left, the big
        # Review door on the right (workflow reads left to right).
        with gr.Column(elem_classes=["shelf", "coming-in"]):
            gr.Markdown("Coming in", elem_classes=["shelf-name"])
            with gr.Row(elem_classes=["shelf-row"]):
                with gr.Column(scale=1, min_width=200, elem_classes=["stack"]):
                    menu_ingest_btn = gr.Button(
                        _door_label(_DOOR_TITLE["ingest"], _DOOR_DESC["ingest"]),
                        elem_classes=["door"])
                    menu_backfill_btn = gr.Button(
                        _door_label(_DOOR_TITLE["backfill"], _DOOR_DESC["backfill"]),
                        elem_classes=["door"])
                # The Review door: a plain door twice the stack's width that
                # fills the shelf's full height (#home-page .shelf-row > .door
                # stretches it) and centres its own text, like every other door.
                menu_review_btn = gr.Button(
                    _review_door_label(len(queue)), elem_classes=["door", "main"],
                    scale=2, min_width=320)
        # The board: one gr.HTML strip whose text _menu_availability repaints
        # (put-away count + connected / archive warning / job-running), plus the
        # two context buttons that share the strip.
        with gr.Row(elem_classes=["board"]):
            board_html = gr.HTML()
            archive_check_again_btn = gr.Button(
                "Check again", elem_classes=["tool"], scale=0, visible=False)
            job_progress_btn = gr.Button(
                "Show progress", elem_classes=["tool"], scale=0, visible=False)
        # Shelf 2 -- "Putting away": three equal doors.
        with gr.Column(elem_classes=["shelf", "putting-away"]):
            gr.Markdown("Putting away", elem_classes=["shelf-name"])
            with gr.Row(elem_classes=["shelf-row"]):
                menu_archive_dryrun_btn = gr.Button(
                    _door_label(_DOOR_TITLE["preview_filing"], _DOOR_DESC["preview_filing"]),
                    elem_classes=["door"])
                menu_archive_apply_btn = gr.Button(
                    _door_label(_DOOR_TITLE["file_images"], _DOOR_DESC["file_images"]),
                    elem_classes=["door"])
                menu_resolve_btn = gr.Button(
                    _door_label(_DOOR_TITLE["resolve"], _DOOR_DESC["resolve"]),
                    elem_classes=["door"])
   # Indentation inside the pages is deliberately shallow (1 space per level):
   # wrapping the existing panel in a page Column must not reflow every line of
   # it, because the diff of a re-indented block hides whether anything else
   # moved. The component order below, the `outputs` list and all nine event
   # wirings are unchanged -- only the nesting is new.
   with gr.Column(elem_classes=["page", "page-hidden"]) as review_page:
    with gr.Row(elem_classes=["page-header"]):
        review_back_btn = gr.Button("← Menu", size="sm", scale=0)
        gr.Markdown("## Review")
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
                image = gr.Image(interactive=False, show_label=False, elem_id="review-image", buttons=[])
                with gr.Row(elem_id="image-toolbar"):
                    fullscreen_btn = gr.Button("⛶ Fullscreen", variant="secondary")
            with gr.Column(scale=1):
                title_md = gr.Markdown(elem_classes=["post-info"])
                meta_md = gr.Markdown(elem_classes=["post-info"])
                character_box = gr.Textbox(label="Character guess (one per line)", lines=3,
                                           elem_classes=["nospell"])
                char_validation_md = gr.Markdown()
                franchise_box = gr.Textbox(label="Franchise (one per line)", lines=2,
                                           elem_classes=["nospell"])
                franchise_validation_md = gr.Markdown()
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

                undo_btn = gr.Button("Undo last action", elem_id="undo-btn")

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
    # Client-side only: no server round-trip, not in `outputs`, no effect on the
    # render contract (EXPECTED_OUTPUT_COUNT stays 27).
    fullscreen_btn.click(fn=None, inputs=None, outputs=None, js=FULLSCREEN_JS)

    # Live inline character/folder validation. .change() (not .input) is
    # intentional: it fires when _render_current sets the boxes programmatically
    # on navigation and at demo.load, so the line refreshes on every entry and
    # clears on the empty-queue branch (boxes set to ""). char_validation_md is
    # deliberately NOT in `outputs` -- it is advisory and driven by its own
    # events, so the render contract stays at EXPECTED_OUTPUT_COUNT.
    gr.on(
        triggers=[character_box.change, franchise_box.change, crossover_box.change,
                  same_series_group_box.change, oc_box.change, known_series_box.change],
        fn=on_character_validation,
        inputs=[character_box, franchise_box, crossover_box, same_series_group_box,
                oc_box, known_series_box],
        outputs=[char_validation_md],
    )
    franchise_box.change(
        fn=on_franchise_validation,
        inputs=[franchise_box],
        outputs=[franchise_validation_md],
    )

   with gr.Column(elem_classes=["page", "page-hidden"]) as resolve_page:
    with gr.Row(elem_classes=["page-header"]):
        resolve_back_btn = gr.Button("← Menu", size="sm", scale=0)
        gr.Markdown("## Resolve")
    resolve_outputs, resolve_open = resolve.build_ui()

   with gr.Column(elem_classes=["page", "narrow", "page-hidden"]) as archive_dryrun_page:
    with gr.Row(elem_classes=["page-header", "pagehead"]):
        archive_dryrun_back_btn = gr.Button("← Menu", size="sm", scale=0, variant="secondary")
        gr.Markdown("## Preview filing")
    archive_dryrun_intro_md = gr.Markdown("Show where every accepted image would be filed. Moves nothing.")
    archive_dryrun_run_btn = gr.Button("Show the plan", variant="primary")
    archive_dryrun_status_md = gr.Markdown()
    # Result panel in the 10px indigo frame; hidden until the job ends.
    with gr.Column(elem_classes=["result-frame", "page-hidden"]) as archive_dryrun_result_group:
        with gr.Row(elem_classes=["result-row"]):
            archive_dryrun_seal = gr.HTML(elem_classes=["seal-cell"])  # value-driven: _SEAL_HTML on success, "" otherwise
            archive_dryrun_result_text = gr.HTML(elem_classes=["result-text"])
        with gr.Row(elem_classes=["result-actions"]):
            # Three fixed next steps, shown/hidden by _dryrun_result_extra, so
            # each keeps one static click wiring: Resolve, File images (primary,
            # or secondary beside Resolve), Go to Review.
            archive_dryrun_resolve_btn = gr.Button(
                "Resolve", variant="primary", elem_classes=["page-hidden"])
            archive_dryrun_primary_btn = gr.Button(variant="primary", elem_classes=["page-hidden"])
            archive_dryrun_review_btn = gr.Button(
                "Go to Review", variant="primary", elem_classes=["page-hidden"])
            archive_dryrun_result_back_btn = gr.Button("Back to menu", variant="secondary")
    # The plan rendered as a real table (filled from archive.py --json's
    # structured rows, needs-Resolve first). A gr.HTML holding a static <table>,
    # not a gr.Dataframe: at ~1000 rows the Dataframe's grid froze the UI for
    # minutes, while this renders in under a second (see _plan_table_html).
    # Always-mounted; shown/hidden by the wrapper's class, driven by value.
    with gr.Column(elem_classes=_PLAN_HIDDEN) as archive_dryrun_plan_wrap:
        archive_dryrun_plan_table = gr.HTML(elem_classes=["plan-table-wrap"])
        # A post id can appear on more than one row: a multi-character or gallery
        # post, and the extra "wallpaper copy" row an image gets when it is also
        # filed as a wallpaper. The Status column says which is which.
        gr.Markdown(
            "_A post id can show up more than once — the **Status** column says "
            "why (e.g. a separate **wallpaper copy** row for an image also filed "
            "as a wallpaper)._",
            elem_classes=["plan-note"],
        )
    # The raw text log stays available under a CLOSED "Details" accordion below
    # the table (opened only on failure). See _dryrun_result_extra.
    with gr.Accordion(_LOG_ACC_LABEL, open=False) as archive_dryrun_log_acc:
        archive_dryrun_log_box = gr.Textbox(
            lines=20, max_lines=30, autoscroll=True, interactive=False, show_label=False,
            elem_classes=["plan-log"],
        )

   with gr.Column(elem_classes=["page", "narrow", "page-hidden"]) as archive_apply_page:
    with gr.Row(elem_classes=["page-header", "pagehead"]):
        archive_apply_back_btn = gr.Button("← Menu", size="sm", scale=0, variant="secondary")
        gr.Markdown("## File images")
    archive_apply_intro_md = gr.Markdown(
        "Move every accepted image into the archive. Run Preview filing "
        "first. Afterwards Oshiire refreshes its duplicate check and the "
        "unsave list.")
    # Gate: the handler refuses unless this is ticked, and unticks it when done.
    # File images is never cancellable -- a half-applied move set is worse than
    # waiting -- so there is no Stop button.
    archive_apply_chk = gr.Checkbox(label="I've checked Preview filing")
    archive_apply_run_btn = gr.Button("Apply", variant="primary")
    archive_apply_status_md = gr.Markdown()
    with gr.Column(elem_classes=["result-frame", "page-hidden"]) as archive_apply_result_group:
        with gr.Row(elem_classes=["result-row"]):
            archive_apply_seal = gr.HTML(elem_classes=["seal-cell"])  # value-driven: _SEAL_HTML on success, "" otherwise
            archive_apply_result_text = gr.HTML(elem_classes=["result-text"])
        with gr.Row(elem_classes=["result-actions"]):
            archive_apply_primary_btn = gr.Button(variant="primary", elem_classes=["page-hidden"])
            archive_apply_result_back_btn = gr.Button("Back to menu", variant="secondary")
    # Shown after a successful apply: the same drain reminder Backfill shows,
    # since apply has just refreshed the unsave list for the posts it archived.
    archive_apply_next_md = gr.Markdown(visible=False)
    with gr.Accordion("Details: the full job log", open=False) as archive_apply_log_acc:
        archive_apply_log_box = gr.Textbox(
            lines=20, max_lines=30, autoscroll=True, interactive=False, show_label=False
        )

   with gr.Column(elem_classes=["page", "narrow", "page-hidden"]) as ingest_page:
    with gr.Row(elem_classes=["page-header", "pagehead"]):
        ingest_back_btn = gr.Button("← Menu", size="sm", scale=0, variant="secondary")
        gr.Markdown("## Ingest")
    ingest_intro_md = gr.Markdown("Fetch new saves from your Reddit feed. AI tagging runs afterwards.")
    ingest_run_btn = gr.Button("Fetch new saves", variant="primary")
    ingest_status_md = gr.Markdown()
    with gr.Column(elem_classes=["result-frame", "page-hidden"]) as ingest_result_group:
        with gr.Row(elem_classes=["result-row"]):
            ingest_seal = gr.HTML(elem_classes=["seal-cell"])  # value-driven: _SEAL_HTML on success, "" otherwise
            ingest_result_text = gr.HTML(elem_classes=["result-text"])
        with gr.Row(elem_classes=["result-actions"]):
            ingest_primary_btn = gr.Button(variant="primary", elem_classes=["page-hidden"])
            # Shown only when the run found nothing new (see _ingest_result_extra):
            # the full-listing fix lives on the Free up saved space page.
            ingest_freeup_btn = gr.Button("Free up space", variant="secondary",
                                          elem_classes=["page-hidden"])
            ingest_result_back_btn = gr.Button("Back to menu", variant="secondary")
    with gr.Accordion("Details: the full job log", open=False) as ingest_log_acc:
        ingest_log_box = gr.Textbox(
            lines=20, max_lines=30, autoscroll=True, interactive=False, show_label=False
        )

   with gr.Column(elem_classes=["page", "narrow", "page-hidden"]) as backfill_page:
    with gr.Row(elem_classes=["page-header", "pagehead"]):
        backfill_back_btn = gr.Button("← Menu", size="sm", scale=0, variant="secondary")
        gr.Markdown("## Backfill")
    backfill_intro_md = gr.Markdown(
        "Work through older saves from your Reddit data export, a batch "
        "at a time. AI tagging runs afterwards.")
    backfill_count_num = gr.Number(
        value=500, minimum=1, precision=0, label="Posts to sweep this run"
    )
    with gr.Row():
        backfill_run_btn = gr.Button("Run backfill", variant="primary")
        # Visible only while a run is in progress (toggled by the generator).
        backfill_stop_btn = gr.Button("Stop", variant="stop", visible=False)
    backfill_status_md = gr.Markdown()
    with gr.Column(elem_classes=["result-frame", "page-hidden"]) as backfill_result_group:
        with gr.Row(elem_classes=["result-row"]):
            backfill_seal = gr.HTML(elem_classes=["seal-cell"])  # value-driven: _SEAL_HTML on success, "" otherwise
            backfill_result_text = gr.HTML(elem_classes=["result-text"])
        with gr.Row(elem_classes=["result-actions"]):
            backfill_primary_btn = gr.Button(variant="primary", elem_classes=["page-hidden"])
            backfill_result_back_btn = gr.Button("Back to menu", variant="secondary")
    # Shown after a successful run: the drain reminder + saved-page link.
    backfill_next_md = gr.Markdown(visible=False)
    with gr.Accordion("Details: the full job log", open=False) as backfill_log_acc:
        backfill_log_box = gr.Textbox(
            lines=20, max_lines=30, autoscroll=True, interactive=False, show_label=False
        )

   # Free up saved space: the in-app drain path (reached from Manage and from the
   # Ingest "nothing new" result). Same job-page shape as the pages above:
   # containers hide via .page-hidden, never visible=.
   with gr.Column(elem_classes=["page", "narrow", "page-hidden"]) as unsave_page:
    with gr.Row(elem_classes=["page-header", "pagehead"]):
        unsave_back_btn = gr.Button("← Menu", size="sm", scale=0, variant="secondary")
        gr.Markdown("## Free up saved space")
    gr.Markdown(
        "Reddit only shows about the newest **1,000** saved posts. Past that, "
        "new saves are stored but never appear in your saved list or the feed, "
        "so Ingest stops finding anything. Unsaving posts Oshiire has **already "
        "captured** frees room so new ones show up again.\n\n"
        "Oshiire never clicks anything on Reddit itself — it stays 100% local "
        "(no Reddit API, no login, nothing written back). The unsaving happens "
        "in *your* browser, driven by you.")

    gr.Markdown("### Step 1 — Make the list", elem_classes=["step-head"])
    gr.Markdown(
        "Build `data/unsave_list.json`: the posts that are provably in your "
        "archive and so are safe to unsave. Read-only — it reads the manifest "
        "and the backfill log and writes just that one file.")
    unsave_run_btn = gr.Button("Make the list", variant="primary")
    unsave_status_md = gr.Markdown()
    with gr.Column(elem_classes=["result-frame", "page-hidden"]) as unsave_result_group:
        with gr.Row(elem_classes=["result-row"]):
            unsave_seal = gr.HTML(elem_classes=["seal-cell"])  # 済 on success, else ""
            unsave_result_text = gr.HTML(elem_classes=["result-text"])
        with gr.Row(elem_classes=["result-actions"]):
            unsave_primary_btn = gr.Button(variant="primary", elem_classes=["page-hidden"])
            unsave_openfolder_btn = gr.Button("Open folder", variant="secondary")
            unsave_result_back_btn = gr.Button("Back to menu", variant="secondary")
    unsave_openfolder_status = gr.Markdown()
    with gr.Accordion("Details: the full job log", open=False) as unsave_log_acc:
        unsave_log_box = gr.Textbox(
            lines=14, max_lines=24, autoscroll=True, interactive=False, show_label=False
        )

    gr.Markdown("### Step 2 — Install the helper (once)", elem_classes=["step-head"])
    gr.Markdown(
        "The unsaving is done by a small **userscript** that runs in your own "
        "browser. You install it once:\n\n"
        "1. Install a userscript manager — **Tampermonkey** (Chrome/Edge) or "
        "**Violentmonkey** (Firefox) — from your browser's extension store.\n"
        "2. Click the button below. Your extension opens and offers to install "
        "`oshiire_unsave.user.js`; confirm the install.")
    unsave_userscript_btn = gr.Button("Open the helper script", variant="secondary")
    unsave_userscript_status = gr.Markdown()

    gr.Markdown("### Step 3 — Unsave in your browser", elem_classes=["step-head"])
    gr.Markdown(
        "The helper's panel is titled **Oshiire · bulk unsave**.\n\n"
        "1. Open old Reddit and make sure you're logged in to **your** account "
        "(your username shows top-right).\n"
        "2. Go to your saved page (button below).\n"
        "3. The Oshiire panel appears on that page. Load `data/unsave_list.json` "
        "with its file picker, or paste it in, and press **Load list**.\n"
        "4. Leave the mode on **Dry run — count only** and press **Start dry "
        "run**. It only counts and changes nothing — check that the number looks "
        "right.\n"
        "5. Switch to **Live run — actually unsave**, confirm the prompt, and "
        "press **Start LIVE run**. Do it in small batches, re-run the dry-run "
        "count between batches, and stop if something looks off. Unsaving "
        "**can't be undone**.")
    with gr.Row(elem_classes=["result-actions"]):
        unsave_oldreddit_btn = gr.Button("Open old Reddit", variant="secondary")
        unsave_saved_btn = gr.Button("Open my saved page", variant="secondary")
    unsave_saved_status = gr.Markdown()

   with gr.Column(elem_classes=["page", "wide", "page-hidden"]) as manage_page:
    with gr.Row(elem_classes=["page-header"]):
        manage_back_btn = gr.Button("← Menu", size="sm", scale=0)
        gr.Markdown("## Manage")
    with gr.Row(elem_classes=["manage-lead"]):
        gr.Markdown(
            "Saved listing full and Ingest finding nothing new? "
            "**Free up saved space** walks you through unsaving posts Oshiire "
            "has already captured.")
        manage_unsave_btn = gr.Button("Free up saved space →",
                                      elem_classes=["tool"], scale=0)
    with gr.Tabs() as manage_tabs:
     with gr.Tab("Character Folders", id="char") as char_tab:
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
          # Explicit triggers, so this does NOT also fire at page load: the rows
          # are rebuilt when Manage is opened from the menu (which resets to this
          # tab) and whenever the user selects the tab, never on the startup path.
          triggers=[menu_manage_btn.click, char_tab.select,
                    char_franchise_dropdown.input, char_tick.change],
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

      # .input(), not .change(): a handler that re-selects the same franchise
      # would otherwise re-render with an empty status and swallow its own
      # confirmation message (see settings_dropdown).
      char_franchise_dropdown.input(
          fn=on_character_select, inputs=[char_franchise_dropdown], outputs=char_outputs
      )

      # -- Series folders ------------------------------------------------------
      # Static components (no @gr.render): unlike the character rows above, the
      # controls here are a fixed set, so the ordinary render-dict pattern fits.
      gr.Markdown("---")
      gr.Markdown(
          "### Series folders\n"
          "Give a series that is currently filed under **Others/Known Series** its "
          "own folder, or create a brand-new series folder. Both decide where "
          "**future** images are filed — images already in the archive stay where "
          "they are until you move them yourself, and each change tells you how "
          "many that is."
      )

      with gr.Group():
          gr.Markdown("#### Give a series its own folder")
          series_dropdown = gr.Dropdown(
              choices=_series_choices(), value=None, label="Full Name (CODE)",
              filterable=True,
              # Seeded here and refreshed on tab-open; custom values allowed for
              # the same reason as the other dropdowns -- gr.update only repaints
              # the client, so a value the server component wasn't built with is
              # rejected on submit.
              allow_custom_value=True,
              info="Type to filter. Read live from the shortname file each time this tab opens.",
          )
          series_folder_box = gr.Textbox(
              label="Folder name",
              info="Prefilled with the full name — shorten it if you want a tidier folder.",
          )
          series_preview_md = gr.Markdown()
          series_confirm_chk = gr.Checkbox(
              value=False, label=_series_confirm_label(None, None)
          )
          with gr.Row():
              series_preview_btn = gr.Button("Preview")
              series_apply_btn = gr.Button("Apply", variant="primary")
          series_status_md = gr.Markdown()

          series_outputs = [series_dropdown, series_folder_box, series_preview_md,
                            series_confirm_chk, series_status_md]

      with gr.Group():
          gr.Markdown("#### Create a new series")
          gr.Markdown(
              "Make a new folder for a series you collect but haven't filed yet. "
              "Future images tagged with this exact name file straight into it."
          )
          create_name_box = gr.Textbox(label="Series folder name")
          create_confirm_chk = gr.Checkbox(value=False, label="Create this series folder")
          create_btn = gr.Button("Create", variant="primary")
          create_status_md = gr.Markdown()

          create_outputs = [create_name_box, create_confirm_chk, create_status_md]

      # The shortname dropdown is live-read every time the tab opens: _series_tab_open
      # is chained off the Manage button (after _character_tab_open) and off
      # char_tab.select, so both the character panel and this one repaint their own
      # components on open.
      # .input(), not .change(): re-selecting the same line must not swallow a
      # confirmation message (same rule as char_franchise_dropdown above).
      series_dropdown.input(fn=on_series_select, inputs=[series_dropdown], outputs=series_outputs)
      series_preview_btn.click(
          fn=on_series_preview, inputs=[series_dropdown, series_folder_box],
          outputs=series_outputs,
      )
      series_apply_btn.click(
          fn=on_series_apply,
          inputs=[series_dropdown, series_folder_box, series_confirm_chk],
          outputs=series_outputs,
      )
      create_btn.click(
          fn=on_series_create, inputs=[create_name_box, create_confirm_chk],
          outputs=create_outputs,
      )
     with gr.Tab("Sync", id="sync") as sync_tab:
      gr.Markdown(
          "### Sync\n"
          "Tells oshiire about images you have moved around in the archive "
          "yourself. **Scan** looks and writes nothing; **Apply reconcile** "
          "records the moves it is sure about in `manifest.json` and the "
          "duplicate-detection index. Neither one moves, copies or deletes a "
          "file — that stays your file browser's job."
      )
      # Scan reads ARCHIVE_DIR, so this tab is the one Manage page that can't do
      # its job when the archive is unreachable. _sync_tab_open shows the same
      # problem sentence the Home banner uses and disables Scan, re-checked every
      # time the tab opens. (Apply never reads the archive -- it rewrites the
      # manifest + index from a plan a Scan produced -- but it stays disabled
      # until a Scan runs, so disabling Scan gates it too.)
      sync_archive_problem_md = gr.Markdown(visible=False)
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
      # Opening the tab additionally repaints the archive-problem line and the
      # Scan button (the archive gate), so its output set is the superset.
      sync_open_outputs = [sync_archive_problem_md, sync_scan_btn, sync_report_md,
                           sync_status_md, sync_apply_btn]

      # Populated when the tab is OPENED, not by a second demo.load. The archive
      # changes underneath this panel constantly -- that is the whole premise of
      # the tab -- so a once-at-launch population would be stale by definition.
      sync_scan_btn.click(fn=on_sync_scan, inputs=[sync_scope_box], outputs=sync_outputs)
      sync_apply_btn.click(fn=on_sync_apply, outputs=sync_outputs)
     with gr.Tab("Settings", id="settings", elem_classes=["nospell"]) as settings_tab:
      # Each config store gets its OWN sub-tab. The nesting is 1 space per level,
      # same convention as the outer Tabs, so wrapping the subreddit panel doesn't
      # reflow it beyond the +2 the extra level costs; each panel keeps its own
      # outputs list, so the Review render contract is untouched.
      with gr.Tabs():
       with gr.Tab("Subreddit map") as subreddit_subtab:
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
            choices=_settings_keys(), value=None,
            label="Pick an existing subreddit to edit", filterable=True,
            allow_custom_value=True,
            info="Type to filter.",
        )
        settings_key_box = gr.Textbox(
            label="Subreddit name", max_lines=1,
            info="Lowercased on save. To add a new one, click New entry first. "
                 "Changing the name of a picked entry offers a rename.",
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
        with gr.Row(elem_classes=["form-buttons"]):
            settings_save_btn = gr.Button("Save", variant="primary")
            settings_delete_btn = gr.Button("Delete", variant="stop")
            settings_add_btn = gr.Button("New entry")
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
            settings_save_btn,
            settings_delete_btn,
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
        subreddit_subtab.select(fn=_settings_render, outputs=settings_outputs)
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
        # Re-label the Save/Delete buttons live as the name box is typed, so the
        # button says what the next click will do (Save changes / Rename / …).
        # Buttons only -- repainting the boxes here would fight the typing.
        # always_last so the FINAL keystroke always wins (gradio otherwise can
        # drop the last event, leaving the label a character behind); hidden
        # progress so the relabel doesn't flicker; .blur to settle on exit too.
        settings_key_box.input(
            fn=on_settings_key_input,
            inputs=[settings_key_box, settings_dropdown],
            outputs=[settings_save_btn, settings_delete_btn],
            trigger_mode="always_last", show_progress="hidden",
        )
        settings_key_box.blur(
            fn=on_settings_key_input,
            inputs=[settings_key_box, settings_dropdown],
            outputs=[settings_save_btn, settings_delete_btn],
        )
        # The save button is wired as its OWN last input: a Button's value is its
        # label, so the handler sees the label the user actually clicked and can
        # refuse a rename whose button changed underneath them.
        settings_save_btn.click(
            fn=on_settings_save,
            inputs=[settings_key_box, settings_franchise_box, settings_null_box,
                    settings_character_box, settings_dropdown, settings_save_btn],
            outputs=settings_outputs,
        )
        settings_delete_btn.click(
            fn=on_settings_delete, inputs=[settings_dropdown], outputs=settings_outputs
        )
        settings_add_btn.click(fn=on_settings_add, outputs=settings_outputs)

       with gr.Tab("Franchise aliases") as franchise_alias_subtab:
        gr.Markdown(
            "### Franchise aliases\n"
            "Maps a franchise name as it's **tagged** to the archive folder it "
            "files into (`Azure Lane` → `Azur Lane`). Tick **No folder yet** to "
            "record a franchise that has no folder yet (writes `null`). Edits apply "
            "to the next routed image immediately."
        )
        fa_count_md = gr.Markdown()
        fa_dropdown = gr.Dropdown(
            choices=sorted(_fa_table()), value=None,
            label="Pick an existing franchise alias to edit",
            filterable=True, allow_custom_value=True, info="Type to filter.",
        )
        fa_key_box = gr.Textbox(
            label="Franchise name as tagged", max_lines=1,
            info="To add a new one, click New entry first. Changing the name of a "
                 "picked entry offers a rename.",
        )
        fa_folder_box = gr.Textbox(
            label="Folder", max_lines=1,
            info="The archive folder this franchise files into.",
        )
        fa_null_box = gr.Checkbox(
            label="No folder yet — record the franchise but leave it unrouted (writes null)",
        )
        fa_caution_md = gr.Markdown()
        with gr.Row(elem_classes=["form-buttons"]):
            fa_save_btn = gr.Button("Save", variant="primary")
            fa_delete_btn = gr.Button("Delete", variant="stop")
            fa_add_btn = gr.Button("New entry")
        fa_status_md = gr.Markdown()

        fa_outputs = [fa_dropdown, fa_key_box, fa_folder_box, fa_null_box,
                      fa_caution_md, fa_status_md, fa_count_md,
                      fa_save_btn, fa_delete_btn]

        franchise_alias_subtab.select(fn=_fa_render, outputs=fa_outputs)
        fa_dropdown.input(fn=on_fa_select, inputs=[fa_dropdown], outputs=fa_outputs)
        fa_null_box.input(fn=on_fa_null_toggle, inputs=[fa_null_box], outputs=[fa_folder_box])
        fa_key_box.input(
            fn=on_fa_key_input, inputs=[fa_key_box, fa_dropdown],
            outputs=[fa_save_btn, fa_delete_btn],
            trigger_mode="always_last", show_progress="hidden",
        )
        fa_key_box.blur(
            fn=on_fa_key_input, inputs=[fa_key_box, fa_dropdown],
            outputs=[fa_save_btn, fa_delete_btn],
        )
        fa_save_btn.click(
            fn=on_fa_save,
            inputs=[fa_key_box, fa_folder_box, fa_null_box, fa_dropdown, fa_save_btn],
            outputs=fa_outputs,
        )
        fa_delete_btn.click(fn=on_fa_delete, inputs=[fa_dropdown], outputs=fa_outputs)
        fa_add_btn.click(fn=on_fa_add, outputs=fa_outputs)

       with gr.Tab("Character aliases") as character_alias_subtab:
        gr.Markdown(
            "### Character aliases\n"
            "Per-series: maps a character name as **tagged** to the character "
            "folder it files into (`Raiden Shogun` → `Raiden`). Pick a series, then "
            "edit its aliases. Only series filed into character subfolders appear."
        )
        ca_count_md = gr.Markdown()
        ca_franchise_dropdown = gr.Dropdown(
            choices=_nested_franchises(), value=None, label="Series",
            filterable=True, allow_custom_value=True, info="Type to filter.",
        )
        ca_variant_dropdown = gr.Dropdown(
            choices=[], value=None, label="Pick an existing alias to edit",
            filterable=True, allow_custom_value=True,
            info="Pick one to edit, or click New entry.",
        )
        ca_key_box = gr.Textbox(
            label="Character name as tagged", max_lines=1,
            info="To add a new one, click New entry first. Changing the name of a "
                 "picked entry offers a rename.",
        )
        ca_canonical_box = gr.Textbox(
            label="Files under (folder)", max_lines=1,
            info="An existing character folder in this series.",
        )
        ca_impact_md = gr.Markdown()
        with gr.Row(elem_classes=["form-buttons"]):
            ca_save_btn = gr.Button("Save", variant="primary")
            ca_delete_btn = gr.Button("Delete", variant="stop")
            ca_add_btn = gr.Button("New entry")
        ca_status_md = gr.Markdown()

        ca_outputs = [ca_franchise_dropdown, ca_variant_dropdown, ca_key_box,
                      ca_canonical_box, ca_impact_md, ca_status_md, ca_count_md,
                      ca_save_btn, ca_delete_btn]

        character_alias_subtab.select(fn=_ca_render, outputs=ca_outputs)
        ca_franchise_dropdown.input(
            fn=on_ca_franchise_select, inputs=[ca_franchise_dropdown], outputs=ca_outputs
        )
        ca_variant_dropdown.input(
            fn=on_ca_variant_select,
            inputs=[ca_franchise_dropdown, ca_variant_dropdown], outputs=ca_outputs,
        )
        ca_key_box.input(
            fn=on_ca_key_input,
            inputs=[ca_franchise_dropdown, ca_key_box, ca_variant_dropdown],
            outputs=[ca_save_btn, ca_delete_btn],
            trigger_mode="always_last", show_progress="hidden",
        )
        ca_key_box.blur(
            fn=on_ca_key_input,
            inputs=[ca_franchise_dropdown, ca_key_box, ca_variant_dropdown],
            outputs=[ca_save_btn, ca_delete_btn],
        )
        ca_save_btn.click(
            fn=on_ca_save,
            inputs=[ca_franchise_dropdown, ca_key_box, ca_canonical_box,
                    ca_variant_dropdown, ca_save_btn],
            outputs=ca_outputs,
        )
        ca_delete_btn.click(
            fn=on_ca_delete, inputs=[ca_franchise_dropdown, ca_variant_dropdown], outputs=ca_outputs
        )
        ca_add_btn.click(fn=on_ca_add, inputs=[ca_franchise_dropdown], outputs=ca_outputs)

       with gr.Tab("Series aliases") as series_alias_subtab:
        gr.Markdown(
            "### Series aliases\n"
            "Maps a variant series name to its canonical name (`Re:Zero` → "
            "`Re Zero`), so a tag spelled differently still resolves. Edits apply "
            "to the next tagged post immediately."
        )
        sa_count_md = gr.Markdown()
        sa_dropdown = gr.Dropdown(
            choices=sorted(load_series_aliases()), value=None,
            label="Pick an existing series alias to edit",
            filterable=True, allow_custom_value=True, info="Type to filter.",
        )
        sa_key_box = gr.Textbox(
            label="Series name as tagged", max_lines=1,
            info="To add a new one, click New entry first. Changing the name of a "
                 "picked entry offers a rename.",
        )
        sa_canonical_box = gr.Textbox(
            label="Canonical name", max_lines=1,
            info="The series name everything should resolve to.",
        )
        with gr.Row(elem_classes=["form-buttons"]):
            sa_save_btn = gr.Button("Save", variant="primary")
            sa_delete_btn = gr.Button("Delete", variant="stop")
            sa_add_btn = gr.Button("New entry")
        sa_status_md = gr.Markdown()

        sa_outputs = [sa_dropdown, sa_key_box, sa_canonical_box, sa_status_md,
                      sa_count_md, sa_save_btn, sa_delete_btn]

        series_alias_subtab.select(fn=_sa_render, outputs=sa_outputs)
        sa_dropdown.input(fn=on_sa_select, inputs=[sa_dropdown], outputs=sa_outputs)
        sa_key_box.input(
            fn=on_sa_key_input, inputs=[sa_key_box, sa_dropdown],
            outputs=[sa_save_btn, sa_delete_btn],
            trigger_mode="always_last", show_progress="hidden",
        )
        sa_key_box.blur(
            fn=on_sa_key_input, inputs=[sa_key_box, sa_dropdown],
            outputs=[sa_save_btn, sa_delete_btn],
        )
        sa_save_btn.click(
            fn=on_sa_save, inputs=[sa_key_box, sa_canonical_box, sa_dropdown, sa_save_btn],
            outputs=sa_outputs
        )
        sa_delete_btn.click(fn=on_sa_delete, inputs=[sa_dropdown], outputs=sa_outputs)
        sa_add_btn.click(fn=on_sa_add, outputs=sa_outputs)

       with gr.Tab("Shortnames") as shortname_subtab:
        gr.Markdown(
            "### Shortnames\n"
            "The `CODE = Full Series Name` table for series filed under "
            "`Others/Known Series`. Changing a code affects only images filed from "
            "now on — already-filed names keep their (inert) suffix, so nothing on "
            "disk renames."
        )
        sn_count_md = gr.Markdown()
        sn_dropdown = gr.Dropdown(
            choices=[(f"{c} = {f}", f) for c, f in load_shortname_map(layout)],
            value=None, label="Pick an existing shortname to edit",
            filterable=True, allow_custom_value=True,
            info="Type to filter.",
        )
        sn_code_box = gr.Textbox(
            label="Code", max_lines=1,
            info="The short suffix added to filenames.",
        )
        sn_fullname_box = gr.Textbox(
            label="Full series name", max_lines=1,
            info="To add a new one, click New entry first. Changing the name of a "
                 "picked entry offers a rename.",
        )
        with gr.Row(elem_classes=["form-buttons"]):
            sn_save_btn = gr.Button("Save", variant="primary")
            sn_delete_btn = gr.Button("Delete", variant="stop")
            sn_add_btn = gr.Button("New entry")
        sn_status_md = gr.Markdown()

        sn_outputs = [sn_dropdown, sn_code_box, sn_fullname_box, sn_status_md,
                      sn_count_md, sn_save_btn, sn_delete_btn]

        shortname_subtab.select(fn=_sn_render, outputs=sn_outputs)
        sn_dropdown.input(fn=on_sn_select, inputs=[sn_dropdown], outputs=sn_outputs)
        sn_fullname_box.input(
            fn=on_sn_key_input, inputs=[sn_fullname_box, sn_dropdown],
            outputs=[sn_save_btn, sn_delete_btn],
            trigger_mode="always_last", show_progress="hidden",
        )
        sn_fullname_box.blur(
            fn=on_sn_key_input, inputs=[sn_fullname_box, sn_dropdown],
            outputs=[sn_save_btn, sn_delete_btn],
        )
        sn_save_btn.click(
            fn=on_sn_save, inputs=[sn_code_box, sn_fullname_box, sn_dropdown, sn_save_btn],
            outputs=sn_outputs
        )
        sn_delete_btn.click(fn=on_sn_delete, inputs=[sn_dropdown], outputs=sn_outputs)
        sn_add_btn.click(fn=on_sn_add, outputs=sn_outputs)

       with gr.Tab("App") as app_subtab:
        gr.Markdown(
            "### App\n"
            "How Oshiire opens. Oshiire writes its own preferences and the setup "
            "window's settings in its data folder, and never changes your `.env`."
        )
        app_window_radio = gr.Radio(
            choices=[("Your web browser", "browser"), ("Its own window", "native")],
            value=settings.window_mode(),
            label="Open Oshiire in",
        )
        # Save disabled until the choice differs from what's on disk, same
        # discipline as the other panels.
        app_save_btn = gr.Button("Save", variant="primary", interactive=False)
        app_status_md = gr.Markdown("Takes effect the next time you open Oshiire.")

        app_theme_radio = gr.Radio(
            choices=[("Match my system", "system"), ("Light", "light"), ("Dark", "dark")],
            value=settings.theme(),
            label="Appearance",
        )
        app_theme_save_btn = gr.Button("Save", variant="primary", interactive=False)
        app_theme_status_md = gr.Markdown("")

        # The feed link, archive folder and username, in the setup window
        # (setup_window.py --change, its own process).
        change_setup_btn = gr.Button("Change setup...")
        change_setup_md = gr.Markdown("")
        change_setup_btn.click(fn=_open_change_setup, outputs=[change_setup_md])

        app_outputs = [app_window_radio, app_save_btn, app_status_md,
                       app_theme_radio, app_theme_save_btn, app_theme_status_md]
        # Re-read the file whenever the panel is opened, so the radios reflect
        # what's saved with Save disabled (a fresh launch or a hand-edit may have
        # changed it since last visit). Manage-open chains _app_render too (below).
        app_subtab.select(fn=_app_render, outputs=app_outputs)
        # .input (user only), not .change: _app_render sets the value
        # programmatically on open, and .change would re-fire on that.
        app_window_radio.input(
            fn=_app_mode_changed, inputs=[app_window_radio], outputs=[app_save_btn])
        app_save_btn.click(
            fn=_app_save, inputs=[app_window_radio],
            outputs=[app_save_btn, app_status_md])
        app_theme_radio.input(
            fn=_app_theme_changed, inputs=[app_theme_radio], outputs=[app_theme_save_btn])
        # Save writes the file, then the JS applies it live (toggles .dark).
        app_theme_save_btn.click(
            fn=_app_theme_save, inputs=[app_theme_radio],
            outputs=[app_theme_save_btn, app_theme_status_md]
        ).then(fn=None, inputs=[app_theme_radio], js=_APPLY_THEME_JS)

   with gr.Column(elem_classes=["page", "wide", "page-hidden"]) as history_page:
    with gr.Row(elem_classes=["page-header"]):
        history_back_btn = gr.Button("← Menu", size="sm", scale=0)
        gr.Markdown("## History")
    history_outputs, history_open = history.build_ui(show_title=False)

  # Pages are plain Columns with no .select of their own, so each page's live
  # open-read is chained explicitly off the menu button that opens it. Manage
  # also resets to its first tab, and the in-Manage tab selects re-read too.
  nav_outputs = [home_page, review_page, resolve_page, archive_dryrun_page,
                 archive_apply_page, ingest_page, backfill_page, unsave_page,
                 manage_page, history_page, manage_tabs]
  BACK_BUTTONS = [review_back_btn, resolve_back_btn, archive_dryrun_back_btn,
                  archive_apply_back_btn, ingest_back_btn, backfill_back_btn,
                  unsave_back_btn, manage_back_btn, history_back_btn]
  # All eight menu buttons, in menu order. _menu_availability locks every one of
  # them while a job runs; the six ungated ones are otherwise always enabled.
  _MENU_BUTTONS = [menu_review_btn, menu_resolve_btn, menu_archive_dryrun_btn,
                   menu_archive_apply_btn, menu_ingest_btn, menu_backfill_btn,
                   menu_manage_btn, menu_history_btn]
  # What _menu_availability repaints: every door/tool button (labels + enabled),
  # the board's HTML, and the board's two context buttons (Check again when the
  # archive is unreachable, Show progress while a job runs).
  menu_avail_outputs = _MENU_BUTTONS + [board_html, archive_check_again_btn,
                                        job_progress_btn, quit_btn]
  # Gate the menu from the first paint, not only after the first job.
  demo.load(fn=_menu_availability, outputs=menu_avail_outputs)
  # Arm the browser close guard (best effort; see _CLOSE_GUARD_JS).
  demo.load(js=_CLOSE_GUARD_JS)
  # Turn spellcheck off on the name/folder boxes (see _NOSPELL_JS).
  demo.load(js=_NOSPELL_JS)
  # Idle auto-shutdown heartbeat: an invisible timer pings the server every
  # HEARTBEAT_SECONDS so the idle watchdog (browser mode) knows a tab is still
  # open. gr.Timer renders nothing and the tick is a near-free server call that
  # just stamps the clock; native mode ignores it (no watchdog is armed).
  heartbeat_timer = gr.Timer(HEARTBEAT_SECONDS)
  heartbeat_timer.tick(fn=_note_heartbeat, inputs=None, outputs=None)
  # Check again re-reads the lock/availability without leaving Home, so a drive
  # that mounts after launch is picked up without a page reload.
  archive_check_again_btn.click(fn=_menu_availability, outputs=menu_avail_outputs)
  # Show progress (shown only while a job runs) opens the running job's page.
  job_progress_btn.click(fn=_show_progress, outputs=nav_outputs)
  # "?" toggles the help panel.
  help_btn.click(fn=_toggle_help, inputs=[help_state], outputs=[help_panel, help_state])
  data_folder_btn.click(fn=_open_data_folder, outputs=[data_folder_md])
  # Quit (browser mode): refuse while a job runs, else stop the server cleanly.
  # When it was the "stopped" response, swap the page for a static goodbye so the
  # closing server leaves no live UI trying to reconnect.
  quit_btn.click(fn=_quit, outputs=[quit_notice, quit_btn]).then(
      fn=None, js=_QUIT_REPLACE_JS)

  # Opening Review repaints only if a job invalidated the queue since last view.
  menu_review_btn.click(fn=_nav_review, outputs=nav_outputs).then(
      fn=_review_open, outputs=outputs)
  # Opening Resolve re-reads disk (it's its own page's open fn) and marks review
  # dirty (see _nav_to) so review re-reads before it can save over any write.
  menu_resolve_btn.click(fn=_nav_resolve, outputs=nav_outputs).then(
      fn=resolve_open, outputs=resolve_outputs)
  # Job pages keep a finished run's result panel between visits, so opening one
  # resets it to the pre-run controls (the run button back, result panel hidden,
  # log accordion closed and empty).
  _ingest_reset_outputs = [ingest_run_btn, ingest_result_group, ingest_log_acc,
                           ingest_log_box, ingest_status_md, ingest_primary_btn,
                           ingest_seal, ingest_freeup_btn]
  _unsave_reset_outputs = [unsave_run_btn, unsave_result_group, unsave_log_acc,
                           unsave_log_box, unsave_status_md, unsave_primary_btn,
                           unsave_seal, unsave_openfolder_status,
                           unsave_userscript_status, unsave_saved_status]
  _dryrun_reset_outputs = [archive_dryrun_run_btn, archive_dryrun_result_group,
                           archive_dryrun_log_acc, archive_dryrun_log_box,
                           archive_dryrun_status_md, archive_dryrun_primary_btn, archive_dryrun_seal,
                           archive_dryrun_resolve_btn, archive_dryrun_review_btn,
                           archive_dryrun_plan_table, archive_dryrun_plan_wrap]
  _backfill_reset_outputs = [backfill_run_btn, backfill_result_group, backfill_log_acc,
                             backfill_log_box, backfill_status_md, backfill_primary_btn,
                             backfill_seal, backfill_stop_btn, backfill_next_md]
  _apply_reset_outputs = [archive_apply_run_btn, archive_apply_result_group, archive_apply_log_acc,
                          archive_apply_log_box, archive_apply_status_md, archive_apply_primary_btn,
                          archive_apply_seal, archive_apply_chk, archive_apply_next_md]
  menu_archive_dryrun_btn.click(fn=_nav_archive_dryrun, outputs=nav_outputs).then(
      fn=_reset_archive_dryrun, outputs=_dryrun_reset_outputs)
  menu_archive_apply_btn.click(fn=_nav_archive_apply, outputs=nav_outputs).then(
      fn=_reset_archive_apply, outputs=_apply_reset_outputs)
  menu_ingest_btn.click(fn=_nav_ingest, outputs=nav_outputs).then(
      fn=_reset_ingest, outputs=_ingest_reset_outputs)
  menu_backfill_btn.click(fn=_nav_backfill, outputs=nav_outputs).then(
      fn=_reset_backfill, outputs=_backfill_reset_outputs)
  # Opening Manage resets to the Character Folders tab (see _nav_to), and a
  # programmatic tab change does not fire .select, so its live reads are
  # chained here explicitly.
  menu_manage_btn.click(fn=_nav_manage, outputs=nav_outputs).then(
      fn=_character_tab_open, inputs=[char_franchise_dropdown], outputs=char_outputs
  ).then(fn=_series_tab_open, outputs=series_outputs
  ).then(fn=_app_render, outputs=app_outputs)
  # History re-reads the manifest from disk on open, so it always shows current
  # state (and resets to its default filter).
  menu_history_btn.click(fn=_nav_history, outputs=nav_outputs).then(
      fn=history_open, outputs=history_outputs)

  # Every page except home carries a back button straight home; returning to the
  # menu re-checks whether Ingest / Backfill can run (a job may have changed it).
  # Review's back button also abandons any open accept confirmation on the way
  # out (the entry stays pending), the same as Cancel.
  for _back_btn in BACK_BUTTONS:
      _home_fn = _nav_home_from_review if _back_btn is review_back_btn else _nav_home
      _back_btn.click(fn=_home_fn, outputs=nav_outputs).then(
          fn=_menu_availability, outputs=menu_avail_outputs)

  # Job pages: each run button streams its pipeline (now filling a result panel),
  # then re-checks availability. The result-panel outputs are added to each.
  ingest_run_btn.click(fn=on_ingest_run, outputs=[
      ingest_run_btn, ingest_back_btn, ingest_status_md, ingest_log_box,
      ingest_result_group, ingest_seal, ingest_result_text, ingest_primary_btn,
      ingest_log_acc, ingest_freeup_btn,
  ]).then(fn=_menu_availability, outputs=menu_avail_outputs)
  # The Ingest "nothing new" button and the Manage entry both open the Free up
  # saved space page (resetting its result panel), same as a menu door.
  ingest_freeup_btn.click(fn=_nav_unsave, outputs=nav_outputs).then(
      fn=_reset_unsave, outputs=_unsave_reset_outputs)
  manage_unsave_btn.click(fn=_nav_unsave, outputs=nav_outputs).then(
      fn=_reset_unsave, outputs=_unsave_reset_outputs)
  # Step 1: run the export job. Step 2/3 buttons open local files / the browser;
  # none of them touch Reddit, so they carry no job lock.
  unsave_run_btn.click(fn=on_export_unsave_run, outputs=[
      unsave_run_btn, unsave_back_btn, unsave_status_md, unsave_log_box,
      unsave_result_group, unsave_seal, unsave_result_text, unsave_primary_btn,
      unsave_log_acc,
  ]).then(fn=_menu_availability, outputs=menu_avail_outputs)
  unsave_openfolder_btn.click(fn=_reveal_unsave_list, outputs=unsave_openfolder_status)
  unsave_userscript_btn.click(fn=_open_userscript, outputs=unsave_userscript_status)
  unsave_oldreddit_btn.click(fn=_open_old_reddit, outputs=unsave_saved_status)
  unsave_saved_btn.click(fn=_open_saved_page, outputs=unsave_saved_status)
  backfill_run_btn.click(fn=on_backfill_run, inputs=[backfill_count_num], outputs=[
      backfill_run_btn, backfill_back_btn, backfill_status_md, backfill_log_box,
      backfill_stop_btn, backfill_next_md,
      backfill_result_group, backfill_seal, backfill_result_text, backfill_primary_btn, backfill_log_acc,
  ]).then(fn=_menu_availability, outputs=menu_avail_outputs)
  backfill_stop_btn.click(fn=_backfill_stop)
  archive_dryrun_run_btn.click(fn=on_archive_dryrun_run, outputs=[
      archive_dryrun_run_btn, archive_dryrun_back_btn,
      archive_dryrun_status_md, archive_dryrun_log_box,
      archive_dryrun_result_group, archive_dryrun_seal, archive_dryrun_result_text,
      archive_dryrun_primary_btn, archive_dryrun_log_acc,
      archive_dryrun_resolve_btn, archive_dryrun_review_btn,
      archive_dryrun_plan_table, archive_dryrun_plan_wrap,
  ]).then(fn=_menu_availability, outputs=menu_avail_outputs)
  archive_apply_run_btn.click(fn=on_archive_apply_run, inputs=[archive_apply_chk], outputs=[
      archive_apply_run_btn, archive_apply_back_btn,
      archive_apply_status_md, archive_apply_log_box, archive_apply_chk,
      archive_apply_next_md,
      archive_apply_result_group, archive_apply_seal, archive_apply_result_text,
      archive_apply_primary_btn, archive_apply_log_acc,
  ]).then(fn=_menu_availability, outputs=menu_avail_outputs)

  # Result-panel buttons. The primary next-step opens the natural next page; the
  # secondary "Back to menu" goes home and re-checks the menu (same as the header
  # back button). Ingest/Backfill -> Review; Preview filing -> File images;
  # File images has no next step (only Back to menu).
  # Preview filing can also offer Resolve or Review (see _dryrun_result_extra).
  ingest_primary_btn.click(fn=_nav_review, outputs=nav_outputs).then(
      fn=_review_open, outputs=outputs)
  backfill_primary_btn.click(fn=_nav_review, outputs=nav_outputs).then(
      fn=_review_open, outputs=outputs)
  archive_dryrun_primary_btn.click(fn=_nav_archive_apply, outputs=nav_outputs).then(
      fn=_reset_archive_apply, outputs=_apply_reset_outputs)
  archive_dryrun_resolve_btn.click(fn=_nav_resolve, outputs=nav_outputs).then(
      fn=resolve_open, outputs=resolve_outputs)
  archive_dryrun_review_btn.click(fn=_nav_review, outputs=nav_outputs).then(
      fn=_review_open, outputs=outputs)
  for _rb in (ingest_result_back_btn, backfill_result_back_btn,
              archive_dryrun_result_back_btn, archive_apply_result_back_btn,
              unsave_result_back_btn):
      _rb.click(fn=_nav_home, outputs=nav_outputs).then(
          fn=_menu_availability, outputs=menu_avail_outputs)

  # The user can also switch tabs inside Manage, so each tab re-reads on select.
  char_tab.select(fn=_character_tab_open, inputs=[char_franchise_dropdown], outputs=char_outputs)
  char_tab.select(fn=_series_tab_open, outputs=series_outputs)
  sync_tab.select(fn=_sync_tab_open, outputs=sync_open_outputs)
  settings_tab.select(fn=_settings_render, outputs=settings_outputs)

# The gr.Blocks graph is fully built; the server launch is timed in launch_app.
startup_timing.mark("Blocks build")


def launch_app(inbrowser=True, block=True) -> str:
    """Start the Gradio server and return its local URL.

    `block=True` (the default, and what `python src/review.py` uses) runs the server
    in the foreground until interrupted; `block=False` returns immediately once
    the server is up so a host (app.py's native window) can load the URL itself.
    """
    # Allowlist the two things gradio may serve (see _static_paths): the bundled
    # font files (assets/fonts/), which the @font-face rules reference as
    # /gradio_api/file=assets/fonts/<name>.woff2 (by absolute path; the cwd is
    # the data root), and the one
    # unsave userscript the Free up saved space page hands to the browser. 100%
    # local; no remote web-font host, no network.
    gr.set_static_paths(_static_paths())
    # The ONLY extra path Gradio needs: ARCHIVE_DIR, where a duplicate's
    # thumbnail can live, is outside the project tree. Gradio serves cwd/temp by
    # default and raises InvalidPathError otherwise, so the archive root is
    # allowlisted explicitly (read-only -- the UI never writes there). Everything
    # else the UI shows -- Review's staging images and History's data/thumbs
    # cache -- sits under cwd and is already served without an entry here.
    allowed_paths = [str(archive_dir)] if archive_dir else []
    blocked_paths = _blocked_paths()
    # Launch non-blocking so the browser can be opened at the themed URL
    # (?__theme=light), then block here if the caller asked to. gradio follows
    # the system's dark mode otherwise, which would wreck the closet palette.
    # Bind loopback only, on a fixed port (7860) -- gradio falls through to the
    # next free port if it's taken. The actual port is recorded in the lock file
    # so a second launch can open THIS instance instead of starting a rival.
    _, local_url, _ = demo.launch(
        css=CUSTOM_CSS,
        inbrowser=False,
        server_name="127.0.0.1",
        server_port=None,
        allowed_paths=allowed_paths,
        blocked_paths=blocked_paths,
        prevent_thread_lock=True,
    )
    startup_timing.mark("server up")
    try:
        record_port(demo.server_port)
    except Exception:
        pass  # a missing port record only costs the second-launch "open its tab"
    # Browser mode: no OS window watches this server, so arm the idle watchdog.
    # Stamp a heartbeat now (launch time) so even a tab that never connects still
    # shuts the server down after the idle window. Native mode skips this -- its
    # window owns the lifecycle and an idle tab must not close it.
    if _launch_mode == "browser":
        _note_heartbeat()
        threading.Thread(target=_idle_watchdog, daemon=True).start()
    # Carry the saved appearance into the URL: nothing for "system" (gradio
    # follows prefers-color-scheme), else ?__theme=light|dark.
    query = settings.theme_param(settings.theme())
    if query:
        themed_url = local_url + ("&" if "?" in local_url else "?") + query
    else:
        themed_url = local_url
    if inbrowser:
        import webbrowser
        webbrowser.open(themed_url)
        startup_timing.mark("browser open")
    # The full per-phase breakdown, logged once the UI is up (before we block).
    print(startup_timing.format_breakdown())
    if block:
        demo.block_thread()
    return themed_url


if __name__ == "__main__":
    # Same single-instance lock as the desktop app (app.py): only one Oshiire,
    # browser or window, may run at a time -- both serve the same Gradio app and
    # hold the same manifest open. Lose the race -> bail out instead of starting
    # a rival server on another port. _lock stays referenced for the life of the
    # process (launch_app blocks), which is what keeps the lock held.
    _lock = acquire_single_instance()
    if _lock is None:
        print("Oshiire is already open (close the other copy first).")
        sys.exit(1)
    launch_app()
