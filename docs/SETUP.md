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

   Windows users: `Oshiire.bat` invokes `.venv-win\Scripts\pythonw.exe` **by
   name** (and the pipeline steps use the sibling `python.exe`), so build your
   virtualenv at that exact path or edit `Oshiire.bat`.

1. Clone the repo, then run the installer. It builds the virtualenv at the path
   the launcher expects, installs dependencies, **and upgrades numpy** (step 2
   — the one step people miss) in one go:

   - **Windows:** double-click **`install.bat`** (or run it from a terminal). It
     creates `.venv-win` — the exact path `Oshiire.bat` invokes by name.
   - **Linux/macOS:** run **`./install.sh`**. It creates `.venv`, the path
     `oshiire.sh` uses. (Override the interpreter with `PYTHON=python3.14
     ./install.sh` if `python3` isn't 3.14.)

   The installer prints the final numpy version so you can confirm it's 2.x.

   **Manual alternative** (if you'd rather do it by hand — this is exactly what
   the installer runs). Windows, to match `Oshiire.bat`:
   ```
   python -m venv .venv-win
   .venv-win\Scripts\activate
   pip install -r requirements.txt
   pip install --upgrade numpy      <- step 2 below; do NOT skip it
   ```
   Linux/macOS (`oshiire.sh` expects `.venv`):
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   pip install --upgrade numpy      <- step 2 below; do NOT skip it
   ```
   `requirements.txt` + the numpy upgrade is all you need to run Oshiire.
   **Contributors** who want to run the opt-in end-to-end test or rebuild the
   bundled fonts also install `requirements-dev.txt` (Playwright + fonttools) —
   not needed for normal use.

   One of those dependencies is worth knowing about before it surprises you:
   `curl_cffi` is pinned (`curl_cffi==0.16.2`) and is the transport Oshiire
   uses for `old.reddit.com` HTML pages. old.reddit login-walls anything whose
   TLS/HTTP2 fingerprint isn't a real browser's, so plain `requests` gets the
   login page even carrying a valid session cookie and full browser headers;
   `curl_cffi` impersonates Chrome's fingerprint and gets real pages back. The
   pin is deliberate — the impersonation targets are tied to the bundled
   libcurl build, so an unreviewed upgrade can change what "chrome" means on
   the wire. Nothing else in the project imports it; it lives behind
   `redditclient.py`.

2. **Numpy gotcha — the installer already does this; do it yourself only if you
   installed manually.** This step is not optional, and it is the single most
   common way a setup breaks:

   `dghs-imgutils` pins `numpy<2`, but numpy 1.x has no prebuilt wheel for
   Python **3.13+** — pip falls back to the experimental MinGW build, which
   crashes on import. `onnxruntime`/`opencv`/`imgutils` all work fine with numpy
   2.x despite the stale pin, so override it:
   ```
   pip install --upgrade numpy
   ```
   (tested working: numpy 2.5.1 on 3.14.6).

   If you ever re-run `pip install -r requirements.txt` later, it silently
   downgrades numpy again — re-run the upgrade afterwards. Oshiire now
   **checks numpy at startup** and, if it's too old or the broken MinGW build,
   refuses to launch with a visible window telling you to run
   `.venv-win\Scripts\pip install --upgrade numpy` (the full log is in
   `data\oshiire.log`) — so a silent black window is no longer how you find
   out.

   The 3.13+ scoping is why this step exists rather than being a `requirements.txt`
   pin: on 3.12 and earlier the constraint resolves to a real wheel and this
   override would be unnecessary. Since the supported version is 3.14, in
   practice you always need it — but if you are experimenting on an older
   interpreter, that is the line that tells you whether to bother.

3. Get your Reddit saved-posts feed URL: go to
   `old.reddit.com/prefs/feeds`, find "your saved links", and copy its RSS
   URL. Reddit hands it to you on the `old.reddit.com` host — **change that to
   `www.reddit.com`**, keeping everything from `/saved.rss?` onward exactly as
   copied, so what you save looks like
   `https://www.reddit.com/saved.rss?feed=<token>&user=<your-username>`.

   That swap matters: since the end of July 2026 `old.reddit.com` answers
   logged-out requests with its login page, so the old.reddit form of this URL
   returns the login wall instead of your saves. The `www` host still serves
   the feed with no cookie at all. (Ingest rewrites an old.reddit feed host to
   `www` for you, but store the `www` form anyway — it is what actually gets
   used, and it keeps the file honest about where the request goes.)

   **This URL is a secret** — it has a per-account access token embedded
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
   - `REDDIT_SESSION_COOKIE` — **the second secret in this file.** A logged-in
     cookie string copied out of your browser. See below.

   **About `REDDIT_SESSION_COOKIE`.** Three parts of Oshiire read
   server-rendered `old.reddit.com` HTML rather than the feed: ingest's gallery
   permalink fetch (galleries have no per-image data in the RSS), the backfill
   CSV sweep, and `calibrate.py`. Since the end of July 2026 old.reddit
   requires a logged-in session for those pages, and serves its login page —
   as a normal HTTP 200 — to anyone without one. This cookie is what makes them
   return real pages. The RSS feed does **not** need it; that is read from
   `www.reddit.com` cookieless.

   To get it: open devtools (F12) → **Network** tab → load any page on
   `old.reddit.com` while logged in → click that request → **Headers** →
   **Request Headers** → copy the entire `Cookie:` value. Paste it into `.env`
   as one line:

   ```
   REDDIT_SESSION_COOKIE=reddit_session=<value>; token_v2=<value>; ...
   ```

   Treat it like a password — worse, in fact: whoever holds it is logged in as
   you, with none of the feed token's read-only limits. It is gitignored along
   with the rest of `.env`; never paste it into an issue, a chat, or a
   screenshot. Leave it blank if you only ingest single-image posts from the
   feed and never run backfill.

   The cookie is necessary but not sufficient — those fetches also go out over
   `curl_cffi`'s Chrome-impersonated transport (see step 1). You don't
   configure that; it just explains why a hand-rolled `curl` with the same
   cookie still hits the wall.

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
| `.env.example` | `.env` | Feed URL, session cookie, and archive path. **Holds two secrets** — see steps 3 and 4. |
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

Sometimes the answer to "which folder does this name belong in?" is *neither* —
so that prompt has two more buttons, and both write to `layout.json` too:

- **`character_group_route`** — "Always file to Others_Group". A per-franchise
  list of names that skip the roster entirely and file into the group
  subfolder, for a tag that is really a pairing or a crowd rather than one
  character. It beats a same-named subfolder, so it is a decision, not a
  fallback.
- **`character_alias_dismissed`** — "Stop asking about this name". Silences
  the prompt for that name and does nothing else. The image is **not** routed
  by this: an unmatched name still gets flagged at archive time unless the
  franchise sets `"fallback": "root"`. Use it for a cameo or a mistagged
  string you don't want a folder for and don't want to be asked about again.

Both keys are optional — omit them entirely and nothing changes. Both are
matched by the same lenient name rule as `character_aliases`, so an answer
given for `Hu Tao` still holds when the next post tags it `hutao`, and Undo
reverts the write along with the accept that made it.

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
subreddit it doesn't recognize, it asks you right there (the subreddit-mapping
confirm screenshot in the README) whether and how to remember it. You don't
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
2. **Double-click `Oshiire.bat`** (Windows) or **`oshiire.sh`** (Linux/macOS).
   Oshiire opens in your web browser. Everything below is a door on the home
   menu — no terminal.
3. **Ingest** (Coming in) — fetches your saved feed and downloads new images
   into `staging/`. AI tagging runs automatically when the fetch finishes.
   Nothing is reviewed or filed yet; check `staging/` and you'll see the images.
4. **Review** (the big door) — go through a few images: Accept the ones tagged
   correctly, fix the character/franchise fields and then Accept the ones that
   aren't, Skip anything you're unsure about (it comes back next time). Nothing
   outside `staging/` is touched yet.
5. **Preview filing** (Putting away) — shows a table of where each accepted
   image *would* go, without moving anything. **Read this table.** If a
   franchise or character isn't in `layout.json` yet, it shows up flagged
   instead of guessed — open **Resolve** to map it to an existing folder, create
   a new one, or file it under Others.
6. **File images** (Putting away) — re-runs the same routing, this time actually
   moving files into `ARCHIVE_DIR` and creating any subfolders it needs.
7. Look in `ARCHIVE_DIR`. Your accepted images are now filed into it, in the
   shape you described in `layout.json`.

**Nothing writes to `ARCHIVE_DIR` until step 6.** Step 1 only reads it, and
steps 3–5 are entirely read/stage-only, so it's safe to stop after Preview
filing, adjust `layout.json` based on what got flagged, and re-run Preview
filing as many times as you want before ever filing.

Once you're comfortable with the loop, see Usage below for the full daily
workflow, including Backfill and the flag-resolution screen.

If **Ingest** finds nothing new even though you've saved things, nothing is
broken — you've hit Reddit's saved-listing ceiling. See
[BACKFILL.md](BACKFILL.md), which is the whole story and the fix.

If instead a run stops with **`Reddit returned its login wall`**, or ingest
warns that a gallery fetch hit it, your `REDDIT_SESSION_COOKIE` has expired —
a password change or a logout invalidates it, and they expire on their own
besides. Re-copy it from the browser (step 4) and re-run. Nothing is lost:
backfill rewinds its cursor to the row it was on, and a walled gallery is
recorded as a retryable `skipped` entry that the next ingest picks up. If the
*feed* is the thing returning the wall, check `REDDIT_SAVED_FEED_URL` uses the
`www.reddit.com` host (step 3) before assuming the token is dead.

## Usage

The daily loop is: **double-click Oshiire, then work through the home menu.**

**Opening and closing.** `Oshiire.bat` (Windows) / `oshiire.sh` (Linux/macOS)
opens it in your **web browser** by default. Prefer its own **window**, or set
**Light / Dark / Match system**, under **Manage → Settings → App** — both are
stored in `data/settings.json` and take effect next launch (appearance also
applies to the open tab at once). In browser mode, **Quit Oshiire** in the header
stops the server (then you can close the tab); in window mode, closing the window
quits. **Closing the browser tab is fine** — you needn't Quit first: the server
keeps running in the background but **stops itself after 10 idle minutes** (never
while a job is running). A **second double-click** while it's running never
starts a second copy — in browser mode it opens another tab at the running one,
and otherwise (or in window mode) it shows a small window offering **Open it**
(reopen the running Oshiire) or **Stop it and start fresh** (stop the background
instance — it warns first if a job might be running — and start over).

If the **archive folder isn't reachable** (e.g. a Google Drive folder and Drive
isn't running), the home board warns and the Putting-away doors wait — start
Drive, then click **Check again**. Review, Ingest, Backfill, History and
Settings keep working meanwhile.

The menu, in workflow order:

- **Ingest** (Coming in) — fetches the saved feed, downloads new images into
  `staging/`, runs metadata tagging, then **AI tagging runs automatically** (the
  local WD14 tagger fills whatever metadata left unidentified) — all recorded in
  `manifest.json`. Saved posts with no importable image (text, links, video, an
  unparseable gallery) are recorded as `skipped` and never reach review. To
  inspect those from a terminal:
  `.venv-win\Scripts\python.exe ingest.py --list-skipped` (with
  `--retry-skipped KEY` to force a re-fetch, `--reject-skipped KEY` to close one
  out for good).
- **Backfill** (Coming in) — works through older saves from your Reddit data
  export, a batch at a time: set the batch size, and **Stop** halts at the next
  checkpoint (every finished batch is kept). It then exports the unsave
  whitelist, runs a fresh ingest, hashes the new images and rebuilds the archive
  index — the whole post-cap maintenance cycle. Read [BACKFILL.md](BACKFILL.md)
  before the first run: it needs `data/saved_posts.csv` from a Reddit data
  export, and the thresholds are worth calibrating to your own archive. The
  listing-drain step (loading the refreshed whitelist into the browser
  userscript) is documented there too.
- **Review** — one image at a time, chronological: Accept / Reject / Skip, with
  character / franchise / crossover editable before accepting. Two things show
  automatically: a **duplicate warning** when the image perceptually matches
  something already archived or still in staging (red = near-certain, amber =
  possibly related), and the image's **pixel dimensions** next to the Wallpaper
  control, with a ★ marking any wallpaper target its size suits (never
  auto-selected — you always pick).
- **Preview filing** (Putting away) — prints the planned moves for every
  `approved` entry without touching files. **Always read it** before File
  images; routing conflicts get flagged here.
- **Resolve** (Putting away) — only if Preview filing flagged anything (an
  unmapped franchise/character, etc.). Walk through each flagged entry and
  resolve it (map to an existing folder, create a new one, propose a shortname).
- **File images** (Putting away) — re-runs the same routing, this time moving
  files into `ARCHIVE_DIR` and updating the manifest. Afterwards it refreshes
  the duplicate index and the unsave list.
- **History** (header) — a read-only browser of processed entries, newest
  first, filterable by status, showing where each archived image was filed. No
  edit controls and it never writes anything. (To revert your most recent
  decision, use **Undo** in Review instead.)
- **Manage** (header) — **Character Folders** (promote a Known-Series series to
  its own folder, or create a new one), **Sync** (reconcile the archive against
  the manifest), and **Settings** (the five config editors plus the **App** tab
  for window/appearance).

**Where things live.** App preferences in `data/settings.json` (window mode and
appearance — never a secret; `.env` is never written). History thumbnails cache
under `data/thumbs/`. The run log (useful if something goes wrong in window
mode, where there's no console) is `data/oshiire.log`, rotated to
`data/oshiire.prev.log`. All of `data/` is gitignored.

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

- **File images** records every file it files (and every wallpaper copy) in the
  index as it goes, reusing the hash review already computed — free, and it means
  art you archive is comparable immediately.
- **Backfill**'s final step re-runs `hash_index.py build`, which is what catches
  images you added to the archive **by hand**, outside the pipeline.

Run `build` yourself after a large manual import if you don't want to wait for
the next maintenance cycle. It's resumable and only hashes files that are new
or changed, but it does stat every file under `ARCHIVE_DIR`, so allow a few
minutes on a cloud-synced folder. If review reports that some archived entries
aren't in the index yet, it falls back to their cached hashes — correct, but
narrower than a real refresh, since it can't cover files the pipeline never
handled.
