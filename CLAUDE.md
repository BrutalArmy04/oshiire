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
- Python 3.11+
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

## Build order (build vertically, one slice per session)
0. Read `REDDIT_SAVED_FEED_URL`, fetch + parse the saved feed with feedparser,
   extract image URLs and metadata (title, subreddit, link, post id), download
   images to `staging/`, write manifest entries as `pending_review`. Skip posts
   already in the manifest. No AI, no UI, no archiving.
1. Metadata tagging (title/subreddit -> guess). Pure, deterministic.
2. Gradio review UI reading the manifest; wired to the stubbed guesser.
3. Archiving: on approval, move file to `archive/<character>/`.
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
