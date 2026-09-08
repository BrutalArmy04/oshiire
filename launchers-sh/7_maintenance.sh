#!/usr/bin/env bash
# Post-cap maintenance cycle:
#   backfill -> export -> ingest -> hash staging -> index archive.
# Run from a terminal, read the five summaries, refresh the drain
# list in the browser userscript, done.
cd "$(dirname "$(readlink -f "$0")")/.."
PY=".venv/bin/python"
[ -x "$PY" ] || { echo "No venv at $PY"; exit 1; }

# LIMIT = rows of saved_posts.csv back-catalogue to sweep this run. Bump for a bigger batch.
LIMIT=500
# COOLDOWN = seconds to idle between backfill and ingest. Both hit old.reddit hard;
# running them back-to-back arrives already rate-limited. Bump it if you still see 429s.
COOLDOWN=120

echo "============================================================"
echo " Oshiire maintenance cycle  (backfill limit ${LIMIT})"
echo "============================================================"

echo; echo "=== [1/5] Backfill ==="
if ! "$PY" backfill.py --limit "$LIMIT"; then echo; echo "Backfill FAILED - see error above."; exit 1; fi

echo; echo "=== [2/5] Export unsave whitelist ==="
if ! "$PY" export_unsave_list.py; then echo; echo "Export FAILED - see error above."; exit 1; fi

echo; echo "=== Cooling down ${COOLDOWN}s before ingest (avoid arriving rate-limited) ==="
sleep "$COOLDOWN"

echo; echo "=== [3/5] RSS ingest ==="
if ! "$PY" ingest.py; then echo; echo "Ingest FAILED - see error above."; exit 1; fi

# Hash the images ingest just downloaded, so the review UI launches
# instantly instead of hashing them itself on startup.
echo; echo "=== [4/5] Hash new images (duplicate detection) ==="
if ! "$PY" imagemeta.py warm; then
  echo; echo "Hashing FAILED - see error above. Review still works; it will hash on startup instead."
fi

# Refresh the archive-side half of duplicate detection. Resumable and
# incremental, but it stats every file in ARCHIVE_DIR -- over the rclone mount
# the FIRST build reads the whole archive, so give it a few minutes.
echo; echo "=== [5/5] Index newly archived files (duplicate detection) ==="
if ! "$PY" hash_index.py build; then
  echo; echo "Archive indexing FAILED - see error above. Review still works;"
  echo "entries filed since the last successful build are compared from their cached hash instead."
fi

echo
echo "============================================================"
echo " Maintenance cycle complete. See the summaries above:"
echo "   [1] backfill bucket counts (owned / new / dead / ...)"
echo "   [2] whitelist TOTAL + NEW since last export = posts to drain"
echo "   [3] ingest downloaded=... line"
echo "   [4] images hashed for duplicate detection"
echo "   [5] archive files newly indexed (the \"indexed N\" number)"
echo
echo " NEXT: Load the updated data/unsave_list.json into the browser"
echo " userscript to drain newly-captured posts."
echo "============================================================"
