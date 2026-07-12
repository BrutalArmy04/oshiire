# Oshiire

A local, single-user pipeline that archives saved anime-style artwork from a
Reddit account. It reads a private Reddit "saved" RSS feed, downloads the
images, guesses the character(s) via post metadata and a local AI tagger as a
fallback, and presents each image in a small web UI for human approve/edit/
reject before filing it into a sorted personal archive.

Everything runs locally. No image, title, or metadata is ever sent to a third
party — the only outbound network calls are to Reddit itself (to read the
feed and download images).

## How it works

```
ingest → auto-tag (metadata) → AI fallback → human review → resolve (flags) → archive
```

A few design decisions matter more than the individual scripts:

- **`manifest.json` is the single source of truth.** Every stage reads and
  writes it; no stage calls another directly. All writes are atomic
  (write to a temp file, then `os.replace()`) so a crash mid-write can't
  corrupt it.
- **`ingest.py` is the only Reddit-specific component.** Everything
  downstream — tagging, review, archiving — works off the manifest schema,
  not off Reddit's data shape. Swapping in a different source (a CSV
  backfill, a different platform) only means writing a new ingester.
- **The AI tagger sits behind one seam.** A single function,
  `guess_character(image_path, post_metadata) -> Guess`, is the only thing
  downstream code depends on. Until the AI step runs it returns
  `Guess("Unknown", 0.0, "stub")`; the WD14 model lives entirely behind that
  seam, so nothing else needs to import `torch`/`imgutils`.
- **The AI is a fallback, never a replacement for review.** Guess precedence
  is manual edit > metadata match > AI guess > `Unknown`. Nothing reaches the
  final archive without a human clicking Accept.
- **Filing is conservative by default.** A new archive folder is only ever
  created by an explicit click in the review UI — the router never
  auto-creates one from a guess, and never overwrites an existing file.
  `archive.py` is dry-run by default; you have to pass `--apply` to actually
  move anything, and the dry-run table is meant to be read before you do.

## Screenshots

*(placeholders — to be added)*

- `review.py`, mid-review: the image alongside its title/subreddit/permalink
  and the editable character/franchise/crossover fields.
- `review.py`, the "Known Series" / same-series-group confirm panel — shows
  the human-in-the-loop guardrail before a non-obvious filing decision.
- `archive.py` dry-run output in the terminal — the planned-moves table,
  before anything is applied.
- `resolve.py`, one flagged entry showing the map/create-folder resolution
  options.

## Tech stack

- **Python 3.14**
- **Ingestion:** `feedparser` (parses the saved-posts RSS/Atom feed) +
  `requests` (image downloads). Reddit disabled self-serve API app creation,
  so this project deliberately does not use `praw` or the Data API — the
  private saved-posts RSS feed is the only way left to read a saved list
  without going through Reddit's third-party-app approval process.
- **AI tagger:** `dghs-imgutils` (WD14) + `onnxruntime`, local inference only.
- **Review/resolve UI:** `gradio`.
- **Config:** `python-dotenv`.

## Install & setup

1. Clone the repo, create a virtualenv, and install dependencies:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Numpy gotcha — do this right after step 1, it will bite you otherwise:**
   `dghs-imgutils` pins `numpy<2`, but numpy 1.x has no prebuilt wheel for
   Python 3.14 — pip falls back to building from source, which segfaults on
   import. `onnxruntime`/`opencv`/`imgutils` all work fine with numpy 2.x
   despite the stale pin. Run:
   ```
   pip install --upgrade numpy
   ```
   (tested working: numpy 2.5.1).

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
   - `ARCHIVE_DIR` — the folder your sorted archive should live in (can be a
     cloud-synced folder like Google Drive; this project only ever moves
     files into it, the sync client handles the upload).
   - `REDDIT_USERNAME` — optional, included in the request User-Agent per
     Reddit's API etiquette guidelines. Leave it blank to use a generic value.

5. Copy the example config files to their real (gitignored) names and edit
   them for your own archive:
   - `layout.example.json` → `layout.json`
   - `known_series_names.example.txt` → `known_series_names.txt`
   - `subreddit_map.example.json` → `subreddit_map.json`

6. A worked `layout.json` snippet, to make the format concrete:
   ```json
   {
     "franchise_aliases": {
       "Example Franchise Full Name": "Example Folder Name"
     },
     "character_aliases": {
       "Example Nested Franchise": {
         "Tagged Character Name": "Folder Character Name"
       }
     },
     "franchises": {
       "Example Nested Franchise": {
         "style": "nested",
         "characters": ["Character A", "Character B", "Character C"],
         "fallback": "root"
       },
       "Example Flat Franchise": {
         "style": "flat"
       }
     }
   }
   ```
   - `flat` — every image for that franchise goes straight into
     `ARCHIVE_DIR/<Franchise>/`, no character subfolders.
   - `nested` — `ARCHIVE_DIR/<Franchise>/<Character>/`. A character with no
     matching subfolder gets flagged for review, unless the franchise sets
     `"fallback": "root"`, in which case it routes to the franchise's
     top-level folder instead.
   - `shortname` — for a lightly-collected series with no folder of its own:
     files go into `Others/Known Series/` with a `_SHORTNAME` suffix, decoded
     by `known_series_names.txt`.
   - `franchise_aliases` / `character_aliases` map the names the tagger
     produces to your actual folder names (e.g. `"Azure Lane"` →
     `"Azur Lane"`).

## Usage

The daily workflow runs through the numbered `.bat` launchers — but note the
effective order isn't the filename order:

1. **`1_ingest.bat`** — fetches the saved feed, downloads new images into
   `staging/`, runs metadata-based tagging, records everything in
   `manifest.json`.
2. **`5_ai_tag.bat`** — runs the local WD14 tagger over whatever metadata
   tagging left as `Unknown`. Run this *before* review, not after — it's
   cheaper to let the AI fill gaps first than to hand-tag them.
3. **`2_review.bat`** — opens the Gradio review UI. One image at a time,
   chronological order: Accept, Reject, or Skip, with character/franchise/
   crossover fields editable before accepting.
4. **`3_archive_dryrun.bat`** — prints the planned moves for every `approved`
   entry, without touching any files. **Always read this output** before
   applying — it's also where routing conflicts get flagged.
5. **`resolve.bat`** — only if the dry-run flagged anything (an unmapped
   franchise/character, etc.). Walks through each flagged entry and lets you
   resolve it (map to an existing folder, create a new one, propose a
   shortname).
6. **`4_archive_apply.bat`** — re-runs the same routing logic as step 4, this
   time actually moving files into `ARCHIVE_DIR` and updating the manifest.

## Status

The core pipeline — ingest, tag, review, archive — works end to end and is in
daily use archiving real saves.

## Known limitations

- The RSS feed is a rolling window of recent saves (~25 by default, ~100
  with `&limit=100`) with no pagination. Older saves that have already
  scrolled out of that window aren't reachable through this ingester.
- Windows-first: the launchers are `.bat` files, no `.sh` equivalents yet.

## Roadmap

- CSV backfill ingester — a one-time sweep of full saved history via
  Reddit's data export, to get past the RSS feed's rolling-window limit.
- Live character/folder validation in the review UI, so a mismatch (e.g.
  tagging "Kuki Shinobu" when the archive folder is named "Shinobu") is
  caught at review time instead of at dry-run time.
- A read-only layout/map viewer.
- Additional ingesters (local folder import, Pixiv bookmarks).
- `.sh` launchers for Linux/macOS.

## Design doc

[`CLAUDE.md`](CLAUDE.md) is the project's full spec — architecture
invariants, the manifest schema, routing precedence, and the safety rules
above, written out in detail. It's also the file that drove this project's
AI-assisted development.
