# Changelog

All notable changes to Oshiire are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Reddit retired the saved-posts RSS feed on 2026-11-13, so Ingest now reads your saved list while logged in. It needs your Reddit username and a login cookie copied from your browser (the setup window explains how and has a "Test it" button). The old feed link is no longer used.
- The login cookie expires. When it does, Ingest stops and says so; copy a fresh one in Manage → Settings → App → Change setup.
- Ingest walks your saved list newest-first and stops where it reaches posts it already has, saving as it goes.
- The cookie now lives in the setup window instead of a hand-made `.env` file (a `.env` still works and takes priority).

### Added
- Crossposts remember the subreddit they were originally posted in.

### Fixed
- The cookie guide now tells you to open old.reddit.com, and "Test it" no longer reads like a total ("Logged in. Your saved list loads.").
- In Resolve, an image with no character could not be filed under an existing character folder ("This entry has no character to alias"); the button now files it there.

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
