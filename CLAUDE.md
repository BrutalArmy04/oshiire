# Oshiire

A local, single-user pipeline that archives saved anime-style artwork from the
owner's Reddit account. It reads the owner's private Reddit "saved" RSS feed,
downloads the images, guesses the character(s) via post metadata (fast path) and
a local AI tagger (fallback), then presents each image in a small web UI for
human approve/edit/reject before filing it into a sorted archive.
Human-in-the-loop: nothing reaches the final archive without explicit approval.

Full design: see `docs/blueprint.md`.

## Ingestion approach (important — read before writing ingestion code)
- Reddit disabled self-serve API app creation, so this project does NOT use
  `praw` or the Data API. Ingestion is via Reddit's private saved-posts RSS/Atom
  feed, parsed with `feedparser`.
- The feed URL is a secret (it contains a per-account token). It lives only in
  `.env` as `REDDIT_SAVED_FEED_URL` and is loaded via python-dotenv. Never
  hardcode, print, log, or commit it.
- The feed is READ-ONLY. The app cannot unsave, vote, or write anything back to
  Reddit. "Unsave after archiving" is explicitly OUT OF SCOPE.
- The feed returns only recent saves, not full history. Design for incremental
  polling ("catch new saves"), not a one-shot full-history dump.

## Tech stack
- Python 3.14+ (numpy/imgutils compatibility forces this floor — see the numpy
  gotcha in README.md's setup instructions)
- Ingestion: `feedparser` (parses the saved RSS/Atom feed) + `requests` (image
  downloads)
- AI tagger: WD14 via `imgutils` (installed only when Slice 4 begins)
- UI: `gradio`
- Config/secrets: `python-dotenv`

## Security rules (do not violate)
- The only secret is `REDDIT_SAVED_FEED_URL` in `.env`. Reference it as
  `os.environ["REDDIT_SAVED_FEED_URL"]`. Never inline or log it.
- `.env`, `staging/`, `archive/`, and `manifest.json` are gitignored and must
  stay that way. If you add a new file holding secrets or account-derived data,
  add it to `.gitignore` in the same change.
- Ship a `.env.example` with an empty `REDDIT_SAVED_FEED_URL=`, never a filled
  one.

## Architectural invariants
- **`manifest.json` is the single source of truth.** The downloader and the UI
  never call each other directly — they communicate only via the manifest.
- **Dedup is manifest-based.** Each saved post has a stable id; the downloader
  skips anything already in the manifest, so re-seeing an archived post in the
  feed is a harmless no-op. Ingestion is idempotent. (This is what replaces
  unsaving.)
- **All manifest writes are atomic**: write to a temp file, then `os.replace()`.
  Never write the manifest in place.
- **The character guesser is a stable seam.** One function:
  `guess_character(image_path, post_metadata) -> Guess(name, confidence, source)`.
  Everything downstream depends on this signature, not on how the guess is made.
  Until Slice 4 it tries metadata and otherwise returns
  `Guess("Unknown", 0.0, "stub")`. Do not import torch/imgutils anywhere else.
- The UI process makes no network calls and does no downloading. It only reads
  images from `staging/`, reads/writes the manifest, and moves files on approval.

## Manifest schema (defined once, reused everywhere)
Each entry is keyed by the stable Reddit fullname (`t3_...`) for ordinary
(single-image) posts. **Gallery posts** (see Slice 0 below) instead get one
entry per image, keyed `t3_..._1`, `t3_..._2`, ... (matching each entry's
`image_index`) — for those, the dict key and the `post_id` field diverge:
`post_id` always holds the shared PARENT fullname, never the suffixed key.
Post-level dedup is therefore done by collecting every entry's `post_id`
field (see the Dedup rule below), never by dict-key membership. Fields as
built in Slice 0, extended by later slices:
- `post_id` — the post's stable Reddit fullname (`t3_...`). For gallery
  entries this is the shared parent id, not the entry's own dict key.
- `title` — post title.
- `subreddit` — source subreddit.
- `permalink` — the Reddit post URL.
- `image_url` — the direct reddit-hosted image (i.redd.it etc.). What Slice 0
  downloads. (Note: the earlier `reddit_image_url`/`source_link` split was NOT
  built — Slice 0 shipped a single `image_url`. In practice the "original
  source" is not a clean outbound link anyway; it's a pixiv/image id embedded in
  the title, e.g. `(pixiv 18567314)` or `[i: 146619696]`. A future
  "prefer-original-source" step parses those ids from the title, so no
  `source_link` field is needed.)
- `local_path`, `fetched_at` — set by Slice 0.
- `image_index` — 1-based position within a gallery post. Only set on
  gallery per-image entries; absent on single-image entries.
- `status` — one of: `pending_review` (awaiting review, or Skipped for later),
  `approved` (Accepted in review; awaits Slice 3's move to the archive),
  `rejected` (Rejected in review; staging file deleted, record kept for dedup),
  `download_failed`, `skipped` (auto-skipped at ingest, e.g. non-image).
  Note: a user Skip in the review UI does NOT change status — it stays
  `pending_review` so the entry returns next session. `skipped` is only the
  ingest-time auto-skip.
- `skip_reason` — why an entry was auto-`skipped` at ingest. Null otherwise.
  Distinct from `reason`, which is used only for `download_failed`/`error`
  entries — the two fields are never interchangeable.

Added by Slice 1 (metadata tagging):
- `franchise` — a **list** of source works (e.g. `["Genshin Impact"]`,
  `["Azure Lane"]`, `["VTuber"]`). May be set even when the character is
  Unknown — franchise is a valid fallback sort dimension, and it scopes the
  Slice 4 AI tagger to a character roster (big accuracy win). See the
  franchise/crossover/collab rules below for how the list is populated.
- `crossover` — boolean. `true` when the image mixes characters from unrelated
  franchises in fan art (no official relationship). Drives Slice 3 routing (see
  below). Default `false`.
- `character_guess` — a **list** of names (supports group shots); `["Unknown"]`
  when unresolved.
- `guess_confidence` — rough score: high (character-specific subreddit match),
  medium (parsed from title), low/zero (Unknown).
- `guess_source` — `"subreddit"`, `"title"`, `"ai"` (Slice 4), or `"manual"`
  (edited in the UI).

Added by Slice 3 (archiving):
- `same_series_group` — boolean, set via the review UI's "Same-series group"
  checkbox (see Archiving rules below). Default `false`.
- `wallpaper` — `"none"` | `"pc"` | `"phone"` | `"both"`, set via the review
  UI's Wallpaper control. Default `"none"`.
- `archive_override` — per-image routing override, set in the review UI or in
  `resolve.py`, that wins over the franchise's normal `layout.json` resolution
  without ever mutating `layout.json` itself. Values: `"unknown_source"`
  (routes to `Others/Unknown Sauce`, set only from `resolve.py`) or
  `"known_series"` (routes to `Others/Known Series/` shortname-style, set from
  the review UI's "File as Known Series" checkbox). Absent by default.
- `archive_flag`, `archive_flag_detail`, `archive_flag_at` — set only by
  `archive.py --apply` when an entry can't be routed; cleared again once it
  successfully moves. Not written during dry-run.

**Slice 3 routing precedence (decided now, built later):**
`crossover: true` → the file goes to a dedicated crossover folder, regardless of
`franchise`. Otherwise route normally (by character/franchise). The `franchise`
list on a crossover entry is metadata for search/filter only — it does NOT split
the file across franchise folders.

**Dedup rule:** skip a post when it already exists in the manifest in a *handled*
state (downloaded or `skipped`) — not merely "seen." Checked via the `post_id`
field across every existing entry (`known_post_ids` in ingest.py), not via dict-
key membership, since a gallery post's per-image entries are keyed `t3_..._N`
while still sharing one `post_id`. Keep `skipped` entries reprocessable: any
entry whose `skip_reason` is `gallery_post`, `gallery_fetch_failed`, or
`gallery_parse_error` is automatically retried on every ingest run
(`retry_skipped_galleries`) until it either expands into real per-image entries
or settles on a confirmed-non-gallery reason (`gallery_no_images`, or one of the
original non-gallery reasons) — the reason value itself is the retry state
machine, no separate "already tried" flag needed.

## Metadata tagging rules (Slice 1)
- Signal priority: character-specific subreddit (highest) → franchise subreddit
  + title parse → Unknown.
- `subreddit_map.json` is an editable lookup, seeded from real saves. Each sub
  maps to a `franchise` and optionally a `character` (when the sub is
  character-specific). New subs get added here, not hardcoded.
- Subreddit patterns: strip `Mains` suffix (`AyakaMains` → Ayaka); strip known
  prefixes (`ChurchOf`, `OneTrue`).
- Title parsing for franchise subs: strip trailing parenthetical artist credits
  `(@handle)`, `(pixiv ...)`, `(alias)`; strip Reddit meta-tags `[Media]`,
  `[Discussion]`; extract names from `[...]` or leading position. `[original]`
  is a signal → tag Unknown (it's an OC).
- Group shots: names joined by `and` / `&` / commas → store all in the
  `character_guess` list. Group shots are ~20% of real saves, not an edge case.

### Franchise, crossover, and collab (franchise is a LIST)
- **VTuber is a valid franchise, not noise.** For VTuber art, the franchise IS
  "VTuber" (or the specific agency when known — "Hololive", "Nijisanji"; prefer
  the agency, fall back to generic "VTuber"). Do NOT strip `[VTuber]` as a
  meta-tag. AZKi → `["Hololive"]` is the same idea at higher precision.
- **Collab (official dual-membership), context-driven — no knowledge base:**
  a character officially licensed into another game belongs to BOTH its home
  franchise and the collab franchise. The signal is the SUBREDDIT: if a
  character's parsed home franchise differs from the subreddit's mapped
  franchise, include both in the `franchise` list. Example: 2B posted on
  r/AzureLane → `["NieR: Automata", "Azure Lane"]`; the same 2B art on any other
  sub → `["NieR: Automata"]` only. The subreddit is the collab evidence, so no
  `collabs.json` is needed. (Note: the home franchise for a title-named
  character often isn't known at metadata time — it may resolve at the Slice 4
  AI step or in the review UI. That's fine; tag what's knowable now.)
  Collab entries are NOT crossovers: `crossover` stays `false`, and they route
  normally in Slice 3.
- **Crossover (fan art mixing unrelated sources):** when characters from
  different franchises appear together with no official relationship (e.g.
  `Acheron & Raiden Shogun Ei` = Honkai + Genshin), set `crossover: true`. The
  `franchise` list is the UNION of every character's home franchise
  (`["Honkai: Star Rail", "Genshin Impact"]`), kept searchable. Each character
  still keeps its own real franchise — never invent a fake "crossover" franchise
  value. Routing to the crossover folder is driven by the flag, not the list.
- Do NOT overfit the title parser. A good first guess plus honest `Unknown` is
  the goal — the Slice 2 human review closes the gap in two clicks.

## Review UI rules (Slice 2)
- Gradio app. Reads `manifest.json` fresh on launch, filters to
  `pending_review`, presents them ONE AT A TIME in chronological (as-saved)
  order. No grid.
- Reuses `manifest.py`'s load + atomic save. The UI must NOT reimplement manifest
  writing.
- Per image, displays: the image (from `local_path`), title, subreddit, a
  clickable permalink, and read-only `guess_confidence`/`guess_source` as
  context. Editable: `character_guess` (list), `franchise` (list), and a
  `crossover` checkbox. Editing any of these before Accept sets
  `guess_source: "manual"`.
- Three actions:
  - **Skip** → leaves `status: "pending_review"`, keeps the file. Returns next
    session. (Does not write a `skipped` status — that value is ingest-only.)
  - **Reject** → `status: "rejected"`, DELETES the staging file. Manifest record
    stays so dedup never re-downloads it.
  - **Accept** → `status: "approved"`, KEEPS the file in staging. Records the
    human-confirmed character(s)/franchise(s)/crossover. Does NOT move the file —
    the physical move to the sorted archive is Slice 3.
- Only Reject deletes a file. Provide an **Undo last action** that reverts the
  previous Skip/Reject/Accept (including restoring a just-deleted file), since
  Reject is destructive.
- INVARIANT: the UI makes no network calls and does no downloading. It only reads
  images, reads/writes the manifest, and (on Reject) deletes staging files.
- Show a clear "all reviewed" state when no `pending_review` entries remain.

## Archiving rules (Slice 3)
Split into two slices:
- **3a — routing engine + `layout.json`.** Given an `approved` entry whose
  decisions are already made, move its staging file to the correct archive
  location. Pure filing logic driven by config. Build/verify this first.
- **3b — review-UI decisions that feed 3a.** Adds controls to the Slice 2 UI,
  all optional/per-image, written to the manifest for 3a to execute:
  - **Same-series group** checkbox — forces `Others_Group` routing for a nested
    franchise regardless of how many names are listed. This is the explicit way
    to mark a group; do NOT rely on a magic "Group" string typed into the
    character list (that's fragile). Group intent is either this checkbox or a
    multi-name `character_guess`, never a sentinel string. (If a nested entry has
    a single unmatched character like the literal "Group", 3a correctly flags it
    for review rather than guessing — the fix is this checkbox or real names.)
  - **Create folder** button — for an unmatched character in a nested franchise,
    lets the user explicitly create the subfolder. New folders come ONLY from
    this click, never from a guess.
  - **Shortname proposal** — for a known series with no shortname yet, propose one
    (derived from the series name) for the user to confirm/edit; writes it to the
    shortname file.
  - **Wallpaper** selection — none / PC / phone / both (see below).
  - **Character-alias resolution** — let the user map a tagged name to an existing
    folder name, persisting the alias to `layout.json`.
  - **File as Known Series** checkbox (in the Slice 2 review UI itself, alongside
    crossover/group/wallpaper) — forces the CURRENT image to file shortname-style
    in `Others/Known Series/`, overriding its franchise's normal `layout.json`
    routing. On Accept, reuses an existing shortname-file entry for the
    franchise if one matches (see the shortname-matching rules below), else
    proposes and saves a new collision-checked code. Sets a per-image
    `archive_override: "known_series"` manifest field — never touches the
    franchise's global `layout.json` mapping. Undo reverts both the manifest
    field and any new shortname-file line it wrote.
  - **Flag-resolution pass (`resolve.py`)** — a companion screen (not part of
    Slice 2's UI) that presents each `flag`ged `approved` entry one at a time
    and offers the fix scoped to why `archive.py` flagged it: map/create a
    franchise folder, map/create a character subfolder, or propose a
    shortname. Also offers a per-image `archive_override: "unknown_source"`
    manifest field that forces routing to `Others/Unknown Sauce` without
    touching `layout.json`, for a franchise that genuinely doesn't belong
    anywhere yet. Writes to `layout.json`/the shortname file are atomic (tmp
    file + `os.replace`, same pattern as the manifest).
  - Shared shortname-file/layout.json I/O, matching, and code-proposal helpers
    (used by `archive.py`, `resolve.py`, and the review UI's Known Series
    control) live in `shortname.py`, not `archive.py` — kept separate so the
    Slice 2 review UI never has to import Slice 3a's routing logic to reuse
    this generic file I/O. Franchise-tag matching against the shortname file
    checks both the code and full-name columns, case-insensitively, including
    the tag being a leading token of a longer full name (e.g. `"NIKKE"` matches
    `NIKKE = NIKKE The Goddess of Victory`) — this also resolves the tag
    directly to its existing code without creating a duplicate, near-miss
    entry.

Config files:
- `layout.json` — the user's PERSONAL archive layout. **Gitignored.** Describes
  each franchise's filing style and name aliases. Seeded from the user's real
  folder tree.
- `layout.example.json` — committed template documenting the format.
- Shortname file (`000___Known_Series_Names.txt` for this user;
  `known_series_names.txt` generic) — maps SHORTNAME = Full Series Name for the
  `shortname` style. Personal one gitignored; `.example` committed.
- `ARCHIVE_DIR` in `.env` — the archive root (e.g. a Google Drive synced
  folder). Never hardcode the archive path.

Filing styles (per franchise in `layout.json`):
- `flat` — file goes directly in `ARCHIVE_DIR/<Franchise>/`. No character
  subfolders; same-series groups also land here.
- `nested` — `ARCHIVE_DIR/<Franchise>/<Character>/`. Same-series groups (all
  characters share one franchise) go to `<Franchise>/<group_subfolder>/` where
  `group_subfolder` is always `Others_Group`. A nested franchise may set an
  optional `"fallback": "root"` in `layout.json` to route a character with no
  matching subfolder to the franchise's own top-level folder instead of
  flagging — useful for gradually promoting characters into subfolders in a
  big flat-ish franchise without every un-promoted character flagging for
  review. Default (key absent) is unchanged: flag as before.
- `shortname` — art from a lightly-collected known series goes in
  `Others/Known Series/` with a `_SHORTNAME` filename suffix, decoded by the
  shortname file. Not its own folder.

Routing precedence (first match wins):
1. `crossover: true` → `ARCHIVE_DIR/Crossover/` regardless of franchise/characters.
2. Franchise resolves (via `franchise_aliases` → folder) and style is `flat` →
   the flat folder.
3. Franchise resolves, style `nested`, single character that matches a subfolder
   (via `character_aliases`) → that character subfolder.
4. Franchise resolves, style `nested`, multiple same-franchise characters →
   `<Franchise>/Others_Group/`.
5. Franchise resolves, style `nested`, character has NO matching subfolder →
   FLAG for review (offer "create folder for <Character>" or "route to
   Others_Group"), UNLESS the franchise sets `"fallback": "root"` in
   `layout.json`, in which case route to the franchise's own top-level folder
   instead. Never auto-create a folder from a guess.
6. Known series but only a `shortname` (no folder) → `Others/Known Series/` with
   the suffix; if the series has no shortname yet → propose one, user confirms in
   review.
7. OC (`[original]`) → `Others/Artist's Original/`.
8. Belongs somewhere but unidentifiable → `Others/Unknown Sauce/`.

Name matching:
- `franchise_aliases`: tag franchise name → folder name (`"Azure Lane"` →
  `"Azur Lane"`, `"Re:Zero"` → `"Re Zero"`). A `null` alias means no folder
  exists yet → flag at review.
- `character_aliases[franchise]`: tag character name → folder name
  (`"Raiden Shogun"` → `"Raiden"`). Unmatched → flag, never guess.

Wallpaper (3b): an image can ALSO be copied to `Wallpaper/PC/` and/or
`Wallpaper/<phone>/` (folder name is `Telefon` for this user) on top of its
normal archive placement. Set via a review-UI control, stored in the manifest.

Hard rules:
- New folders are ONLY created by an explicit user click in the review UI.
- Moves go into `ARCHIVE_DIR`; Google-Drive-synced-folder = plain file move, the
  Drive client uploads it. Do NOT add "delete local after upload" for a synced
  folder — that needs rclone/API confirmation, out of scope.
- The archive path always comes from `.env`; nothing about it is hardcoded.

## Build order (build vertically, one slice per session)
0. Read `REDDIT_SAVED_FEED_URL`, fetch + parse the saved feed with feedparser,
   extract the direct image + metadata per the manifest schema above, download
   from `reddit_image_url` into `staging/`, write entries as `pending_review`.
   Record (don't act on) `source_link`. Non-image/link posts get a `skipped`
   entry with a `skip_reason`. Gallery posts are expanded into one
   `pending_review` entry per image (see the manifest schema's `image_index`
   note above) — the RSS feed has no per-image data, so this requires an
   extra fetch of the post's plain `old.reddit.com` permalink HTML (Reddit's
   `.json`/API endpoints return 403 for this project's User-Agent; the HTML
   permalink works, with `Cookie: over18=1` needed for NSFW posts). A gallery
   whose fetch fails or can't be parsed gets a retryable `skipped` placeholder
   instead (see the Dedup rule above). Skip anything already handled in the
   manifest. Manifest lives at `staging/manifest.json`. No AI, no UI, no
   archiving.
1. Metadata tagging: set `franchise`, `character_guess` (list), `guess_confidence`,
   `guess_source` per the rules above, driven by an editable `subreddit_map.json`.
   Pure/deterministic; runs over existing manifest entries.
2. Gradio review UI per "Review UI rules" above: one-at-a-time, chronological,
   Skip/Reject/Accept + Undo, editable character/franchise/crossover. Sets status
   only — no file moving (that's Slice 3). Reject deletes the staging file.
3. Archiving. **3a:** routing engine — move approved files into ARCHIVE_DIR per
   layout.json (flat/nested/shortname, aliases, Crossover/Others_Group/Others,
   precedence rules above). **3b:** review-UI decisions (create-folder button,
   shortname proposal, wallpaper, alias resolution). See "Archiving rules".
4. Swap the stub for the WD14 tagger behind the same `guess_character` seam.
5. (Optional, last) Packaging. Do NOT design earlier slices around PyInstaller.

## Conventions
- Keep functions small and testable; the manifest schema is defined in one place
  and reused.
- Prefer `pathlib` over string paths.
- Not every saved post is an image (some are text/links/galleries). Handle
  non-image and gallery posts gracefully rather than crashing.
- Ask (plan mode) before introducing a new heavy dependency or changing the
  manifest schema.