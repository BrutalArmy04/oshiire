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
- Python 3.14. This is what the project is developed and run on — every `.bat`
  launcher invokes a `.venv-win` built with it, and it is the only interpreter
  the AI tagger, the perceptual-hash tooling and the review UI have ever been
  exercised against. Nothing in the SYNTAX needs it (the highest requirement
  anywhere is `int.bit_count()`, 3.10+); the floor is an untested-below line,
  not a technical one. On 3.13+ the numpy pin needs an override — see the numpy
  gotcha in docs/SETUP.md, which therefore always applies here.
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
  `name` is a LIST and is empty when nothing was identified (see
  `character_guess` below); the Slice 4 AI fallback lives behind the same seam.
  Do not import torch/imgutils anywhere else.
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
- `phash`, `phash_bits`, `width`, `height` — perceptual hash and pixel
  dimensions, written by `imagemeta.py` from ONE decode of the staging file
  (see "Duplicate detection" below). Absent until the image has been hashed;
  absent forever for an entry whose file was never on disk. `phash_bits`
  exists so hashes built at different sizes are never compared — a mismatch
  means recompute, never compare.
Added by Slice 1 (metadata tagging):
- `franchise` — a **list** of source works (e.g. `["Genshin Impact"]`,
  `["Azure Lane"]`, `["VTuber"]`). May be set even when the character is
  Unknown — franchise is a valid fallback sort dimension, and it scopes the
  Slice 4 AI tagger to a character roster (big accuracy win). See the
  franchise/crossover/collab rules below for how the list is populated.
- `crossover` — boolean. `true` when the image mixes characters from unrelated
  franchises in fan art (no official relationship). Drives Slice 3 routing (see
  below). Default `false`.
- `character_guess` — a **list** of names (supports group shots). **EMPTY when
  no character was identified** — never a `"Unknown"` placeholder. A
  placeholder is indistinguishable from a real name to folder matching, alias
  lookup, the review field and the group-vs-single count, so it has to be
  special-cased everywhere or it silently becomes a character. `[]` says the
  same thing and can't be mistaken; `guess_confidence` is what records how
  much was known. (Entries written before this rule carried `["Unknown"]` and
  were migrated.)
- `guess_confidence` — rough score: high (character-specific subreddit match),
  medium (parsed from title), low/zero (nothing identified).
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
  (routes to `Others/Unknown Sauce`, set from `resolve.py`), `"known_series"`
  (routes to `Others/Known Series/` shortname-style, set from the review UI's
  "File as Known Series" checkbox), or `"artist_original"` (routes to
  `Others/Artist's Original`, set from the review UI's "Original character (OC)"
  checkbox — which takes precedence over "File as Known Series" — or from
  `resolve.py`'s "Route to Artist's Original" button, for a human-asserted OC
  the title parser missed). Absent by default.
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
  + title parse → no character (an empty `character_guess`).
- `subreddit_map.json` is an editable lookup, seeded from real saves. Each sub
  maps to a `franchise` and optionally a `character` (when the sub is
  character-specific). New subs get added here, not hardcoded.
- Subreddit patterns: strip `Mains` suffix (`AyakaMains` → Ayaka); strip known
  prefixes (`ChurchOf`, `OneTrue`).
- Title parsing for franchise subs: strip trailing parenthetical artist credits
  `(@handle)`, `(pixiv ...)`, `(alias)`; strip Reddit meta-tags `[Media]`,
  `[Discussion]`; extract names from `[...]` or leading position. `[original]`
  is a signal → no character and no franchise (it's an OC).
- Group shots: names joined by `and` / `&` / commas → store all in the
  `character_guess` list. Group shots are ~20% of real saves, not an edge case.
- **One character appears once.** The subreddit map and the title parser reach
  the same entry independently and spell names differently ("Hutao" vs "Hu
  Tao"), so both the tagger's merge and `archive.py`'s nested routing collapse
  names on `shortname.normalize_name_key` (casefold, drop spaces/punctuation).
  Routing counts characters to choose single-folder vs `Others_Group`, so it
  must count characters, not spellings — and it dedupes AFTER alias
  resolution, so an alias and its folder name also count once. Key-only: the
  name stored in the manifest is always a real spelling.
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
- Do NOT overfit the title parser. A good first guess plus an honest "no
  character" is the goal — the Slice 2 human review closes the gap in two
  clicks.
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
  (Reading the archive pHash index and archive thumbnails is local file I/O and
  does not breach this — it never writes anything under `ARCHIVE_DIR`.)
- Show a clear "all reviewed" state when no `pending_review` entries remain.
### Duplicate detection (`imagemeta.py`)
Post-ID dedup can't catch the same artwork saved from two different subreddits;
a perceptual hash can. `imagemeta.py` owns the pHash/dimension cache and the
lookup; hashing/distance primitives are imported from `hash_index.py` and never
reimplemented.
- The comparison corpus spans two locations, because an `archived` entry's
  staging file no longer exists: the archive half comes from `hash_index.py`'s
  index (`data/archive_index.db`), joined back to the manifest via
  `archive_path` ↔ the index's `rel_path` — both are POSIX and relative to
  `ARCHIVE_DIR`, so they match exactly. The staging half is hashed into the
  manifest entries themselves.
- Thresholds are `calibrate.py`'s numbers for THIS archive: `<= 8` is the same
  artwork (red banner), `9..11` is an uncertain band (amber), `12+` is noise.
  Duplicated in `imagemeta.py` rather than imported from `backfill.py`, whose
  import pulls in network setup the review UI must not touch.
- The banner also surfaces `backfill_uncertain`, which backfill.py has always
  written but nothing ever displayed. It renders amber by definition — that
  flag *is* the uncertain band, so it must never render as a certain match.
- **One duplicate = one banner.** The corpus holds several ROWS per artwork
  (an archived image plus its `Wallpaper/PC` / `Wallpaper/<phone>` copies, or
  plain duplicate files in the archive), so `find_duplicates` collapses them:
  one match per `post_id`/path, then any candidate within `DUPLICATE_MAX` of
  an already-kept one is the same artwork again. Ties in distance prefer the
  copy that still HAS a file, since that one carries both the preview and the
  reject action.
- **Gallery siblings are never duplicates.** A sibling is a different image of
  the same post. The check must use the `post_id` FIELD — sibling entries are
  keyed `t3_..._1`, `t3_..._2`, ... under one shared `post_id`, so comparing
  dict keys silently lets every sibling through and offers a one-click reject
  against a distinct image.
- The review UI hides a `pending_review` twin it hasn't reached yet (later in
  the queue, or not in it at all): the decision belongs on the SECOND copy,
  where the first has been seen. It is passed to `find_duplicates` as its
  `exclude` predicate so those matches are dropped BEFORE the result limit —
  otherwise they crowd out an actionable archived match. The resulting
  invariant is that every certain match shown is actionable, so the one-click
  reject is always offered (the sole exception: a twin whose file is already
  gone, which has no keeper to defer to).
- The banner shows ONE thumbnail; the line it belongs to is marked "shown
  below". It is not always the nearest match — a match whose file is gone has
  nothing to preview — and an unmarked preview under a "distance 0" line reads
  as a claim about the wrong image.
- Reject hashes the file BEFORE deleting it; that is the last moment it
  exists. Entries rejected before this existed can never be compared again.
- Hashing is a real cost (~0.2s per high-res image), so `imagemeta.py warm`
  runs as the last step of the maintenance cycle, right after ingest. The
  review UI's startup pass is only a safety net for images that arrived some
  other way — it must stay incremental and must never re-hash.
- **The index must not be allowed to go stale — a file missing from it is
  compared against NOTHING.** An archived entry has no staging file, so the
  staging half can't cover it, and it was invisible to detection between
  `build` runs. Three things keep that closed, and all three are needed:
  `archive.py --apply` records each file it files (and each wallpaper copy)
  via `hash_index.record_indexed_file`, reusing the hash already cached on the
  entry — free, no image re-read; maintenance step 5 re-runs
  `hash_index.py build`, which is the only thing that catches files added to
  the archive by hand (most of the archive); and `find_duplicates` takes
  `indexed_paths` so an archived entry the index doesn't have yet is still
  compared from its cached manifest hash. All index writes go through
  `hash_index.py` — nothing else opens that database, and nothing writes
  inside `ARCHIVE_DIR`.
### Wallpaper suggestion (review UI)
Dimensions are shown next to the Wallpaper control ALWAYS, so a borderline
image can be judged by eye. Thresholds come from `layout.json`'s optional
`wallpaper_rules` (see `layout.example.json`), merged per-key over defaults in
`shortname.load_wallpaper_rules`. A suggested target is marked with a ★ in the
radio label. **Never auto-select** — the suggestion is advisory, the choice is
the human's. `both` is only ever suggested when the image independently
satisfies both rule sets, which the disjoint default aspect bands make
essentially impossible; that is intentional.
 
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
  - **Character-alias learning (review UI, on Accept)** — the character-level
    twin of the series-alias prompt. When a typed character name resolves to no
    subfolder, Accept opens a confirm panel offering to remember it; the target
    comes from the franchise's `characters` roster (filterable dropdown), and
    "just this once" writes nothing. Explicit confirm only, atomic write via
    `shortname.save_character_alias` → `save_layout`, and Undo reverts the
    `layout.json` write and the accept together (one `layout_snapshot` on
    `last_action`) because they were one user action.
    - Fires only for a **nested** franchise with a non-empty roster: for
      flat/shortname styles the character name never reaches the path, so the
      prompt would be unactionable.
    - **Chained after** the subreddit-map panel, never merged with it — they
      are independent questions, and answering one must not swallow the other.
      Both stages share the one `pending_accept` deferral (`stage` field), so
      the accept still commits at the single `_finalize_accept` point.
    - Deliberately overrides the older "learning alternates is resolve.py's
      job" rule for CHARACTER names only: the misspelling happens in the review
      box, and by the time `archive.py` flags it the reviewer is several screens
      away from the image that would say which character it is. Series
      alternates are still learned in `resolve.py`.
  - **File as Known Series** checkbox (in the Slice 2 review UI itself, alongside
    crossover/group/wallpaper) — forces the CURRENT image to file shortname-style
    in `Others/Known Series/`, overriding its franchise's normal `layout.json`
    routing. On Accept, reuses an existing shortname-file entry for the
    franchise if one matches (see the shortname-matching rules below), else
    proposes and saves a new collision-checked code. Sets a per-image
    `archive_override: "known_series"` manifest field — never touches the
    franchise's global `layout.json` mapping. Undo reverts both the manifest
    field and any new shortname-file line it wrote. **Keep this control even
    though it looks redundant:** routing's own shortname fallback (precedence
    6) is only reached when franchise resolution FAILS, so this checkbox is
    the only way to file an image under `Others/Known Series` when its
    franchise *does* have a folder — such an entry routes cleanly and never
    flags, so `resolve.py` can't reach it either.
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
  (`"Raiden Shogun"` → `"Raiden"`). Unmatched → flag, never guess. Scoped per
  franchise because two franchises can have different characters sharing one
  short name. **Learned, not only hand-written** — see "Character-alias
  learning" below. Character-name comparison uses `normalize_name_key`
  (casefold + drop every non-alphanumeric), NOT `lookup_ci`'s series
  normalizer, so `"Hutao"`/`"hu-tao"` resolve to a `"Hu Tao"` folder with no
  alias recorded. Only genuinely different names ever need an entry.
- **Name-order tolerance:** if a character name doesn't resolve as given, a
  name of EXACTLY two tokens is retried in reversed token order through the
  same alias-then-roster path, so `"Kuki Shinobu"` matches a `"Shinobu Kuki"`
  folder and vice versa (`shortname.reversed_name_variant`). Match-time only —
  the name stored in the manifest is never rewritten. Deliberately capped at
  two tokens: with 3+ the permutations stop being a name-order question and
  become guesses, which this must never do. The retry is strictly additive —
  it can only turn a former flag into a match, never change an existing match.
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