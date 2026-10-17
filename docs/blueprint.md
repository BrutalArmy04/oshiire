# Project Blueprint: Oshiire (Reddit Anime Auto-Archiver)

## Executive Summary
A 100% Python-based, end-to-end automated pipeline that extracts saved
anime-style artwork from a user's Reddit account, intelligently guesses the
characters in the images using a combination of metadata and AI, and presents
them in a local web interface for quick human approval before sending them to a
final, sorted local or cloud archive.

> **Ingestion note (updated):** Reddit disabled self-serve API app creation, so
> this project reads the account's **private saved-posts RSS feed** (via
> `feedparser`) instead of the Data API / `praw`. The feed is read-only and
> returns only recent saves. See `CLAUDE.md` for the binding technical rules;
> the phases below describe the original design intent.

## 1. System Architecture & Workflow
The system is designed around a Human-in-the-Loop (HITL) Staging Architecture,
meaning automated processing and manual review are separated to ensure 100%
accuracy in the final archive.

### Phase 1: Ingestion (The Downloader)
- **Action:** The script reads the user's private saved RSS feed and extracts
  direct image URLs.
- **Storage:** Images are downloaded into a temporary Staging Folder (the Inbox).
- **Ledger:** A `manifest.json` file is created/updated in the Staging folder to
  track the status of every downloaded image (e.g., "pending review").

### Phase 2: Intelligence (The Sorting Brain)
Before asking the user to review an image, the system attempts to guess the
character using a two-step "Fast-Track" process:
1. **Metadata Scraping (Primary):** The script checks the Reddit post's title
   and subreddit name. If it finds obvious keywords (e.g., subreddit
   `r/HatsuneMiku`, title "Spike Spiegel fanart"), it assigns the tag
   immediately.
2. **AI Tagging (Fallback):** If the metadata is vague, the image is passed to a
   localized AI model trained specifically on anime art. The AI analyzes the
   image and returns a character guess with a confidence score. This guess is
   saved to the `manifest.json`.

### Phase 3: The Review (The Web UI)
- **Action:** The user launches a lightweight, browser-based dashboard.
- **Experience:** The UI reads the `manifest.json` and loads pending images one
  by one. It displays the image alongside the AI/Metadata's best guess in an
  editable text box.
- **Interaction:** The user clicks "Approve" (accepting the guess), edits the
  text box and then approves, or clicks "Reject/Delete".

### Phase 4: Archiving (Storage)
- **Action:** Upon approval in the UI, the script physically moves the image out
  of the Staging Folder and into the final Archive Directory, sorting it into a
  subfolder named after the approved character.
- **Cloud Integration:** The final Archive Directory can be situated inside a
  local sync folder (like Google Drive or OneDrive) for automatic cloud backup,
  or the script can be configured to use APIs (like Rclone) to beam the file
  directly to the cloud and delete the local copy to save hard drive space.

## 2. The Technology Stack
- **Reddit Extraction:** private saved-posts RSS feed via `feedparser` +
  `requests` for image downloads. (The original `praw`/Data-API plan is no
  longer viable due to Reddit's app-creation lockdown.)
- **AI Vision Model:** WD14 Tagger (Waifu Diffusion), accessed via `imgutils`.
- **User Interface:** `gradio` (build web interfaces in pure Python).
- **Distribution/Packaging:** `PyInstaller` — optional, final step only.

## 3. Known Edge Cases & Challenges to Design For
- **Group Shots:** The AI will detect multiple characters. *Solution:* decide on
  a routing rule (duplicate the file, create a combo folder, or route to a
  generic "Group" folder).
- **Original Characters (OCs) & New IP:** The AI cannot recognize characters it
  wasn't trained on. *Solution:* rely on metadata scraping, or route to an
  "Unknown" folder for manual tagging in the UI.
- **Feed Limitations:** The saved RSS feed returns only recent saves, not full
  history, and cannot be used to unsave posts. *Solution:* poll incrementally
  and rely on manifest-based dedup so re-seen posts are skipped.
- **Non-image posts:** Not every saved post is an image (text, links, galleries).
  *Solution:* handle/skip gracefully rather than crashing.
- **Background vs. Foreground:** The UI script and the Download script should not
  talk directly. *Solution:* both scripts communicate only by reading/writing
  `manifest.json`, with atomic writes to prevent file-locking errors.
