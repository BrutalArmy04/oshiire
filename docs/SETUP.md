# Setup & usage

This is the hands-on guide: install it, configure it, and run it for the
first time. For the pitch, architecture, and screenshots, see the main
[README](../README.md).

## Install & setup

0. **Python version — use 3.14.**

   That is what this project is developed and run on, and it is the only
   interpreter the AI tagger, the perceptual-hash tooling and the review UI have
   ever been exercised against. Older versions are **untested and unsupported**.

   To be clear about what that does and doesn't mean: nothing in the *syntax*
   requires 3.14 — the highest requirement anywhere in the codebase is
   `int.bit_count()`, which is 3.10+. So an older Python will very likely import
   and run. It is the dependency stack that is unverified there: `dghs-imgutils`,
   `onnxruntime`, `Pillow`, `imagehash` and `gradio` have not been installed or
   run below 3.14 in this project. If you try it and it works, good — but you are
   the first, and a failure is a bug report about your setup, not about the code.

   Windows users: the `.bat` launchers all invoke `.venv-win\Scripts\python.exe`
   **by name**, so build your virtualenv at that exact path or edit the
   launchers.

1. Clone the repo, create a virtualenv, and install dependencies. Windows, to
   match the launchers:
   ```
   python -m venv .venv-win
   .venv-win\Scripts\activate
   pip install -r requirements.txt
   ```
   Linux/macOS (nothing invokes this by name, so call it what you like):
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Numpy gotcha — do this right after step 1.**
   `dghs-imgutils` pins `numpy<2`, but numpy 1.x has no prebuilt wheel for
   Python **3.13+** — pip falls back to building from source, which segfaults on
   import. `onnxruntime`/`opencv`/`imgutils` all work fine with numpy 2.x
   despite the stale pin, so override it:
   ```
   pip install --upgrade numpy
   ```
   (tested working: numpy 2.5.1 on 3.14.6).

   The 3.13+ scoping is why this step exists rather than being a `requirements.txt`
   pin: on 3.12 and earlier the constraint resolves to a real wheel and this
   override would be unnecessary. Since the supported version is 3.14, in
   practice you always need it — but if you are experimenting on an older
   interpreter, that is the line that tells you whether to bother.

3. Get your Reddit saved-posts feed URL: go to
   `old.reddit.com/prefs/feeds`, find "your saved links", and copy its RSS
   URL. **This URL is a secret** — it has a per-account access token embedded
   in it, so treat it like a password, never commit it or share it. It stops
   working if you change your Reddit password (that's what invalidates it if
   it ever leaks — password change is also your recovery step if it does).
   The feed returns your ~25 most recent saves by default; appending
   `&limit=100` widens that to ~100.

4. Copy `.env.example` to `.env` and fill in:
   - `REDDIT_SAVED_FEED_URL` — the URL from step 3.
   - `ARCHIVE_DIR` — the folder your sorted archive should live in. **This
     does not need to exist as a real archive already** — see "Your first
     run" below. It can be a cloud-synced folder like Google Drive; this
     project only ever moves files into it, the sync client handles the
     upload.
   - `REDDIT_USERNAME` — optional, included in the request User-Agent per
     Reddit's API etiquette guidelines. Leave it blank to use a generic value.

5. Copy the example config files to their real (gitignored) names. Every
   template ships as `<name>.example.<ext>`; the copy is yours, stays local,
   and the template stays committed as documentation of the format:

   ```
   copy layout.example.json             layout.json
   copy known_series_names.example.txt  known_series_names.txt
   copy subreddit_map.example.json      subreddit_map.json
   copy data\series_aliases.example.json data\series_aliases.json
   ```

   (`.env.example` → `.env` you already did in step 4.)

   You don't need to fill these in completely before your first run — see
   below.

### Every template in the repo

| Template | Copy it to | What it is |
|---|---|---|
| `.env.example` | `.env` | Feed URL and archive path. **Holds a secret** — see step 4. |
| `layout.example.json` | `layout.json` | Your archive's filing rules: per-franchise style, aliases, special folders, wallpaper thresholds. |
| `known_series_names.example.txt` | `known_series_names.txt` | `SHORTNAME = Full Series Name`, for series filed into `Others/Known Series/` rather than a folder of their own. |
| `subreddit_map.example.json` | `subreddit_map.json` | subreddit → franchise (+ character). Grows as you review; you don't need to pre-fill it. |
| `data/series_aliases.example.json` | `data/series_aliases.json` | Alternate series names → the canonical one. Normally written for you by `resolve.py`. |
| `data/saved_posts.example.csv` | `data/saved_posts.csv` | Shape of Reddit's data export. Copy it only to dry-run the backfill sweep before your real export arrives — then replace it. See [BACKFILL.md](BACKFILL.md). |
| `manifest.example.json` | *(do not copy)* | Reference only: a real-shaped `manifest.json` with one entry per status and per routing case. The real one is created for you by the first ingest run. Field-by-field prose lives in [CLAUDE.md](../CLAUDE.md) — see "Manifest schema"; this file is the worked example of it. |
| `data/unsave_list.example.json` | *(do not copy)* | Reference only: the whitelist shape the bulk-unsave userscript consumes. Generated by `export_unsave_list.py`. |
| `data/backfill/state.example.json` | *(do not copy)* | Reference only: the backfill cursor format, for diagnosing a stalled sweep. Copying it would fast-forward the cursor past unprocessed rows. |
| `data/backfill/backfill_log.example.jsonl` | *(do not copy)* | Reference only: the run-log format. Copying it would authorise unsaving posts that were never captured. |

`data/tombstones.json` is **not** a template — it is committed, real, shared
reference data (perceptual hashes of image-host "removed" placeholder cards,
which are identical for everyone). Use it as-is.

### Where the manifest schema is written down

`manifest.json` is the single source of truth every stage reads and writes, so
it is worth knowing before you change anything:

- **The schema itself** — every field, which stage writes it, and the rules that
  look like typos but aren't (`character_guess` is `[]` and never `"Unknown"`;
  `skip_reason` and `reason` are not interchangeable; gallery entries key as
  `t3_..._1` while `post_id` stays the parent) — is in
  [CLAUDE.md](../CLAUDE.md), under **Manifest schema**. That is the definition.
- **A worked example of it** is `manifest.example.json`: sixteen entries covering
  all six statuses and every routing case, each with an inline `_note`
  explaining what it demonstrates. It is a valid manifest, not a commented
  one — no invented fields, no invented statuses — so it loads through
  `manifest.py` and routes through `archive.py` exactly as a real one does.
  `tests/test_example_configs.py` asserts that it still does.

## layout.json: a worked example

`layout.json` tells the archiver how to file each franchise: as one flat
folder, as a folder with per-character subfolders, or as a shared
"lightly-collected series" bucket. Here's a concrete one, using two real
franchises, so the format maps onto an actual collection instead of an
abstract placeholder:

```json
{
  "franchise_aliases": {},
  "character_aliases": {
    "Genshin Impact": {
      "Raiden Shogun": "Raiden"
    }
  },
  "franchises": {
    "Genshin Impact": {
      "style": "nested",
      "characters": ["Ayaka", "Raiden"],
      "fallback": "root"
    },
    "Blue Archive": {
      "style": "flat"
    }
  }
}
```

That, plus a shortname entry `BtR = Bocchi the Rock` in
`known_series_names.txt`, produces this folder tree on disk:

```
ArchiveDir/
  Genshin Impact/
    Ayaka/
    Raiden/
  Blue Archive/          <- flat: everything here
  Others/Known Series/   <- shortname: art_BtR.jpg
```

What each style means:

- **`flat`** — every image for that franchise goes straight into
  `ARCHIVE_DIR/<Franchise>/`, no character subfolders. Use this for a
  franchise you don't care to split by character (`Blue Archive` above).
- **`nested`** — `ARCHIVE_DIR/<Franchise>/<Character>/`. A character with no
  matching subfolder gets flagged for review, unless the franchise sets
  `"fallback": "root"` (as `Genshin Impact` does above), in which case it
  routes to the franchise's top-level folder instead of flagging — handy
  while you're still adding character subfolders one at a time.
- **`shortname`** — for a series you've only saved a handful of images from
  and don't want a folder for at all: files go into `Others/Known Series/`
  with a `_SHORTNAME` suffix on the filename, decoded by
  `known_series_names.txt`. No entry needed in `franchises` — this is what
  happens by default for a recognized-but-unmapped series.
- **`character_aliases`** maps the name the tagger produces to the folder
  name on disk, when they differ — above, art tagged `"Raiden Shogun"` files
  into the `Raiden` subfolder. Keyed per franchise, because two franchises can
  have different characters sharing one short name.

  You mostly don't have to write these by hand. Spacing, casing, punctuation
  and two-token name order are already tolerated (`Hutao`, `hu-tao` and
  `Hu Tao` all find the `Hu Tao` folder; `Kuki Shinobu` finds `Shinobu Kuki`),
  so only genuinely different names need an entry. When you type one of those
  in the review UI and Accept, it offers to remember it — pick the folder it
  belongs to and it is saved here for you. Nothing is written unless you
  confirm, and Undo removes it again.
- **`franchise_aliases`** does the same thing one level up (franchise name →
  folder name), e.g. `"Azure Lane": "Azur Lane"`. Leave it empty (as above)
  when your tag names already match your folder names.

## Your first run

**The key thing to know going in: `ARCHIVE_DIR` does not need to be an
existing, already-organized archive.** Point it at any empty folder — the
first `--apply` run creates the franchise/character subfolders it needs as
it files images. You are not modeling a collection that already exists; you
are describing where new folders should go as they're needed.

Likewise, `layout.json` does not need to describe your whole collection on
day one. Start with one or two franchises you actually save art of — copy
the worked example above and swap in your own. Anything not yet in
`layout.json` either falls back to `Others/Known Series/` (if you give it a
shortname) or gets flagged in the dry-run table for you to resolve, so
nothing is lost — you just add franchises to the config as you encounter
them. `subreddit_map.json` works the same way: it starts seeded with a
handful of example subs, and grows as you go — every time review hits a
subreddit it doesn't recognize, it asks you right there (the confirm panel
in the second README screenshot) whether and how to remember it. You don't
need to pre-populate a full subreddit map before starting.

With that in mind, the first end-to-end run looks like this:

1. **Build the archive index** — once, before anything else:
   ```
   .venv-win\Scripts\python.exe hash_index.py build
   ```
   This walks `ARCHIVE_DIR` and computes a perceptual hash of every image it
   finds, into a local SQLite index under `data/`. It is strictly read-only
   over your archive — it never moves, renames, writes or deletes anything
   there.

   **Do it first**, because it is what lets review tell you an image is one you
   already have. If your `ARCHIVE_DIR` is an empty folder this finishes
   instantly and does nothing useful yet, which is fine — re-run it once you
   have art in there. If you're pointing Oshiire at an archive you have already
   built up by hand, this is the step that makes all of it visible to duplicate
   detection, and it's worth the wait (roughly 0.2s per image, so a large
   archive is a coffee break; it's resumable and incremental afterwards).

   It is also a hard prerequisite for the back-catalogue sweep — see
   [BACKFILL.md](BACKFILL.md).
2. **`launchers/1_ingest.bat`** — fetches your saved feed and downloads new
   images into `staging/`. Nothing is reviewed or filed yet; check `staging/`
   and you'll see the downloaded images.
3. **`launchers/3_review.bat`** — opens the review UI in your browser. Go
   through a few images: Accept the ones tagged correctly, fix the
   character/franchise fields and then Accept for the ones that aren't, Skip
   anything you're unsure about (it'll come back next time). Nothing outside
   `staging/` is touched yet.
4. **`launchers/5_archive_dryrun.bat`** — prints a table of where each accepted
   image *would* go, without moving anything. **Read this table.** If a
   franchise or character isn't in `layout.json` yet, it shows up here as
   flagged instead of guessed. If the dry-run flags an entry, it means the
   franchise or character isn't in your layout yet — run
   `launchers/4_resolve.bat` to map it to an existing folder, create a new one,
   or file it under Others.
5. **`launchers/6_archive_apply.bat`** — re-runs the same routing, this time
   actually moving files into `ARCHIVE_DIR` and creating any subfolders it needs.
6. Look in `ARCHIVE_DIR`. Your accepted images are now filed into it, in the
   shape you described in `layout.json`.

**Nothing writes to `ARCHIVE_DIR` until step 5.** Step 1 only reads it, and
steps 2–4 are entirely read/stage-only, so it's safe to stop after the dry-run,
adjust `layout.json` based on what got flagged, and re-run the dry-run as many
times as you want before ever applying.

Once you're comfortable with the loop, see Usage below for the full daily
workflow, including the AI tagging pass and the flag-resolution screen.

If `1_ingest.bat` reports `downloaded=0` even though you've saved new things,
nothing is broken — you've hit Reddit's saved-listing ceiling. See
[BACKFILL.md](BACKFILL.md), which is the whole story and the fix.

## Usage

The daily workflow runs through the numbered `.bat` launchers in the
`launchers/` folder. Double-click them in order — the filename numbering now
matches the actual workflow order:

- **`launchers/0_open_saved_page.bat`** *(optional)* — opens your Reddit saved
  page in the browser, so the drain step is one double-click away. Reads your
  username from `.env`'s `REDDIT_USERNAME`; edit the placeholder in the file if
  you leave that blank.
1. **`launchers/1_ingest.bat`** — fetches the saved feed, downloads new images
   into `staging/`, runs metadata-based tagging, records everything in
   `manifest.json`.
   Saved posts with no importable image (text, links, video, a gallery whose
   page won't parse) are recorded as `skipped` and never reach review, since
   there's no picture to judge. To see them:
   `.venv-win\Scripts\python.exe ingest.py --list-skipped` — it says which are
   still being retried automatically, and offers `--retry-skipped KEY` to force
   a re-fetch or `--reject-skipped KEY` to close one out for good.
2. **`launchers/2_ai_tag.bat`** — runs the local WD14 tagger over whatever
   metadata tagging left unidentified. This runs *before* review — it's cheaper
   to let the AI fill gaps first than to hand-tag them, so unidentified images
   arrive at review already tagged.
3. **`launchers/3_review.bat`** — opens the Gradio review UI. One image at a
   time, chronological order: Accept, Reject, or Skip, with character/franchise/
   crossover fields editable before accepting. Two things show up here
   automatically: a **duplicate warning** when the image perceptually matches
   something already archived or still in staging (red = near-certain, amber =
   possibly related), and the image's **pixel dimensions** next to the
   Wallpaper control, with a ★ marking any wallpaper target its size suits.
   The suggestion is never auto-selected — you always pick.
4. **`launchers/4_resolve.bat`** — only if the dry-run (step 5) flagged anything
   (an unmapped franchise/character, etc.). Walks through each flagged entry and
   lets you resolve it (map to an existing folder, create a new one, propose a
   shortname).
5. **`launchers/5_archive_dryrun.bat`** — prints the planned moves for every
   `approved` entry, without touching any files. **Always read this output**
   before applying — it's also where routing conflicts get flagged. If it flags
   anything, run `4_resolve.bat` and dry-run again.
6. **`launchers/6_archive_apply.bat`** — re-runs the same routing logic as the
   dry-run, this time actually moving files into `ARCHIVE_DIR` and updating the
   manifest.
7. **`launchers/7_maintenance.bat`** *(periodic)* — the post-cap maintenance
   cycle, in five steps: sweeps the back-catalogue (`backfill`), exports the
   unsave whitelist, runs a fresh RSS ingest, hashes the newly downloaded
   images, and refreshes the archive index so newly filed art is included in
   duplicate detection. It prints a summary of each. Run it when you want to
   reach past the RSS feed's rolling window — and after a big archiving run,
   for step 5 alone. The first time, read [BACKFILL.md](BACKFILL.md) before
   running it: step 1 needs `data/saved_posts.csv` from a Reddit data export,
   and the thresholds it sweeps with are worth calibrating to your own archive.
   Draining the listing afterwards (loading the refreshed whitelist into the
   browser userscript) is documented there too — including why it counts by
   default and unsaves only when explicitly told to.
8. **`launchers/8_history.bat`** *(anytime)* — a read-only browser of already
   processed entries, newest first, filterable by status and showing where
   each archived image was filed. Purely for looking things up: it has no edit
   controls and never writes anything. (To revert your most recent decision,
   use **Undo** in the review UI instead.)

### Duplicate detection needs the archive index

The duplicate warning in step 3 compares against two things: images still in
`staging/` (hashed automatically) and everything already filed in your archive.
The archive half needs the perceptual-hash index built once — this is step 1 of
"Your first run" above:

```
.venv-win\Scripts\python.exe hash_index.py build
```

Without it, review still works and still catches staging-vs-staging duplicates
— it just can't tell you that an image is already filed in your archive. The
index is read-only over `ARCHIVE_DIR` and lives in `data/`, which is
gitignored.

**Keep it fresh — a stale index silently stops catching duplicates.** The index
is a snapshot: art filed after it was built isn't in it, and an image that
isn't in it is compared against nothing. Two things keep it current:

- `6_archive_apply.bat` records every file it files (and every wallpaper copy)
  in the index as it goes, reusing the hash review already computed — free, and
  it means art you archive is comparable immediately.
- `7_maintenance.bat` step 5 re-runs `hash_index.py build`, which is what
  catches images you added to the archive **by hand**, outside the pipeline.

Run `build` yourself after a large manual import if you don't want to wait for
the next maintenance cycle. It's resumable and only hashes files that are new
or changed, but it does stat every file under `ARCHIVE_DIR`, so allow a few
minutes on a cloud-synced folder. If review reports that some archived entries
aren't in the index yet, it falls back to their cached hashes — correct, but
narrower than a real refresh, since it can't cover files the pipeline never
handled.
