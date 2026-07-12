# Setup & usage

This is the hands-on guide: install it, configure it, and run it for the
first time. For the pitch, architecture, and screenshots, see the main
[README](../README.md).

## Install & setup

1. Clone the repo, create a virtualenv, and install dependencies:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Numpy gotcha — do this right after step 1**
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
   - `ARCHIVE_DIR` — the folder your sorted archive should live in. **This
     does not need to exist as a real archive already** — see "Your first
     run" below. It can be a cloud-synced folder like Google Drive; this
     project only ever moves files into it, the sync client handles the
     upload.
   - `REDDIT_USERNAME` — optional, included in the request User-Agent per
     Reddit's API etiquette guidelines. Leave it blank to use a generic value.

5. Copy the example config files to their real (gitignored) names:
   - `layout.example.json` → `layout.json`
   - `known_series_names.example.txt` → `known_series_names.txt`
   - `subreddit_map.example.json` → `subreddit_map.json`

   You don't need to fill these in completely before your first run — see
   below.

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
  into the `Raiden` subfolder.
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

1. **`1_ingest.bat`** — fetches your saved feed and downloads new images into
   `staging/`. Nothing is reviewed or filed yet; check `staging/` and you'll
   see the downloaded images.
2. **`2_review.bat`** — opens the review UI in your browser. Go through a few
   images: Accept the ones tagged correctly, fix the character/franchise
   fields and then Accept for the ones that aren't, Skip anything you're
   unsure about (it'll come back next time). Nothing outside `staging/` is
   touched yet.
3. **`3_archive_dryrun.bat`** — prints a table of where each accepted image
   *would* go, without moving anything. **Read this table.** If a franchise
   or character isn't in `layout.json` yet, it shows up here as flagged
   instead of guessed. If the dry-run flags an entry, it means the franchise or character isn't in your layout yet — run resolve.bat to map it to an existing folder, create a new one, or file it under Others
4. **`4_archive_apply.bat`** — re-runs the same routing, this time actually
   moving files into `ARCHIVE_DIR` and creating any subfolders it needs.
5. Look in `ARCHIVE_DIR`. Your accepted images are now filed into it, in the
   shape you described in `layout.json`.

**Nothing touches `ARCHIVE_DIR` until step 4.** Steps 1–3 are entirely
read/stage-only, so it's safe to stop after the dry-run, adjust
`layout.json` based on what got flagged, and re-run the dry-run as many
times as you want before ever applying.

Once you're comfortable with the loop, see Usage below for the full daily
workflow, including the AI tagging pass and the flag-resolution screen.

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
