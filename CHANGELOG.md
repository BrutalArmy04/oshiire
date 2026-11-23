# Changelog

All notable changes to Oshiire are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-11-23

**Action required:** Reddit ended RSS feeds on 13 November 2026, so Oshiire now reads your saved list while logged in. Enter your Reddit username and login cookie in Manage → Settings → App → Change setup. Your old feed link is no longer used. Oshiire opens the setup window once on its own if you are coming from an earlier version.

### Added
- Ingest reads your whole saved list (up to the ~1,000 newest saves Reddit shows), not just the last ~100 the feed gave.
- A login cookie field in the setup window, with a "Test it" button and a guide to copying the cookie.
- If a job has several steps and a later one fails, the message now says which steps finished first.
- `data/jobs.log` records each job's output (secrets redacted) with start and end times and exit codes, to help work out what ran when.
- Crossposts remember the subreddit they were originally posted in.

### Changed
- Ingest walks your saved list newest-first and stops where it reaches posts it already has, saving as it goes.
- The login cookie expires. When it does, Ingest stops and says so; copy a fresh one in Manage → Settings → App → Change setup.
- The cookie lives in the setup window instead of a hand-made `.env` file (a `.env` still works and takes priority).
- If you are upgrading from an earlier version, the setup window reopens once, with a note explaining the change, if your username or cookie is missing.
- Only one job runs at a time. A command-line run is refused while Oshiire is open.

### Fixed
- Two Ingest runs at the same time could crash with a "manifest.json.tmp" error and overwrite each other's results. Each save of the manifest now uses its own temporary file.
- The cookie guide now tells you to open old.reddit.com, and "Test it" no longer reads like a total ("Logged in. Your saved list loads.").
- In Resolve, an image with no character could not be filed under an existing character folder ("This entry has no character to alias"); the button now files it there.

Some things don't work yet; see Known issues in the README.

## [1.0.1] - 2026-10-17

### Changed
- Tidier download folder and GitHub page.
- If you added Oshiire to the Start menu with 1.0.0, open Manage > Settings > App > Change setup and tick it again.

## [1.0.0] - 2026-10-17

### Added
- Download-and-run copies for Windows and Linux: unzip, double-click, no Python to install.
- A first-run setup window that asks for your saved-posts feed link and your archive folder, with a "Test it" button and help finding the link.
- Change the feed link, archive folder or username later from Manage → Settings → App.
- Optional Start menu (Windows) or app menu (Linux) entry, offered on first run.
- Optional one-time download of the AI tagging model from the setup window, so the first tagging run doesn't stall.
- Your data lives in its own folder, kept separate from the app, so updating Oshiire never touches it; open it from the Home help panel.
- Ingest new saves from your Reddit feed, and work through your older saves from Reddit's data export in batches you can stop at any time.
- Review each image one at a time: accept, fix the character or series, or reject, with undo.
- Automatic character and series suggestions from the post, with a local AI tagger as a fallback.
- A warning when an image looks like one you already have, before you file it twice.
- Preview where every accepted image will go, then file them into your sorted archive in one step.
- Wallpaper suggestions for PC and phone, based on the image's size.
- Free up room in Reddit's capped saved list by un-saving only posts Oshiire has already captured.
- Opens in your web browser or in its own window, in light, dark or your system's theme.
