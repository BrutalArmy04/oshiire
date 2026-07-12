# Oshiire

A local, single-user pipeline that archives saved anime-style artwork from a
Reddit account. It reads a private Reddit "saved" RSS feed, downloads the
images, guesses the character(s) via post metadata and a local AI tagger as a
fallback, and presents each image in a small web UI for human approve/edit/
reject before filing it into a sorted personal archive.

Everything runs locally. No image, title, or metadata is ever sent to a third
party — the only outbound network calls are to Reddit itself (to read the
feed and download images).

**Setup & usage:** see [docs/SETUP.md](docs/SETUP.md).

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
  seam, so nothing else needs to import `onnxruntime`/`imgutils`.
- **The AI is a fallback, never a replacement for review.** Guess precedence
  is manual edit > metadata match > AI guess > `Unknown`. Nothing reaches the
  final archive without a human clicking Accept.
- **Filing is conservative by default.** A new archive folder is only ever
  created by an explicit click in the review UI — the router never
  auto-creates one from a guess, and never overwrites an existing file.
  `archive.py` is dry-run by default; you have to pass `--apply` to actually
  move anything, and the dry-run table is meant to be read before you do.

## Screenshots

![review.py mid-review](screenshots/review.jpg)
`review.py` mid-review: a cleanly auto-tagged entry — character and franchise
filled in from the subreddit map at high confidence, no edits needed.

![Subreddit-mapping confirm panel](screenshots/confirm-panel.jpg)
The subreddit-mapping confirm panel: a new subreddit→franchise/character
mapping is never learned silently — Accept is blocked (note the greyed-out
action buttons) until the user confirms whether, and how, to remember it.

![archive.py dry-run output](screenshots/dry-run.jpg)
`archive.py` dry-run output: nothing has moved yet — the planned-moves table
shows nested/flat/shortname routing, alias resolution, a wallpaper copy, and
the special `Others/` destinations, all before `--apply` touches a file.

![resolve.py flagged entry](screenshots/resolve.jpg)
`resolve.py`, one flagged entry, with the resolution options (map to an
existing folder, create a new one, or route to Unknown Sauce) scoped to why
it was flagged.

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

## Status

The core pipeline — ingest, tag, review, archive — works end to end and is in
daily use archiving real saves.

## Known limitations

- The RSS feed is a rolling window of recent saves (~25 by default, ~100
  with `&limit=100`) with no pagination. Older saves that have already
  scrolled out of that window aren't reachable through this ingester.
- Windows-first: the launchers are `.bat` files, no `.sh` equivalents yet.

## Roadmap

- **Known issue — OC detection only recognises the exact `[original]` title
  tag.** Other phrasings (e.g. a real post titled "Pearl Clutching [Artist's
  Original]") get misread as a franchise name, which then fails to resolve
  and flags as `unresolved_franchise` instead of routing to
  `Others/Artist's Original/`. Planned fix: recognise common OC phrasings in
  the title parser, and add an explicit "Original character" checkbox to the
  review UI so a human can assert OC status regardless of title wording,
  mirroring the existing crossover / same-series-group checkboxes.
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
