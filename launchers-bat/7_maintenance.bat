@echo off
setlocal
cd /d "%~dp0.."

REM ============================================================
REM  Post-cap maintenance cycle:
REM    backfill -> export -> ingest -> hash staging -> index archive.
REM  Double-click, read the five summaries, refresh the drain
REM  list in the browser userscript, done.
REM
REM  LIMIT = rows of the saved_posts.csv back-catalogue to sweep
REM  this run. Bump it for a bigger batch.
REM ============================================================
set LIMIT=500

REM  COOLDOWN = seconds to idle between the backfill and ingest steps. Both hit
REM  old.reddit hard; running them back-to-back arrives already rate-limited, so
REM  let the throttle drain before ingest starts. Bump it if you still see 429s.
set COOLDOWN=120

echo ============================================================
echo  Oshiire maintenance cycle  (backfill limit %LIMIT%)
echo ============================================================

echo.
echo === [1/5] Backfill ===
".venv-win\Scripts\python.exe" backfill.py --limit %LIMIT%
if errorlevel 1 (
    echo.
    echo Backfill FAILED - see error above.
    pause
    exit /b 1
)

echo.
echo === [2/5] Export unsave whitelist ===
".venv-win\Scripts\python.exe" export_unsave_list.py
if errorlevel 1 (
    echo.
    echo Export FAILED - see error above.
    pause
    exit /b 1
)

echo.
echo === Cooling down %COOLDOWN%s before ingest (avoid arriving rate-limited) ===
timeout /t %COOLDOWN% /nobreak

echo.
echo === [3/5] RSS ingest ===
".venv-win\Scripts\python.exe" ingest.py
if errorlevel 1 (
    echo.
    echo Ingest FAILED - see error above.
    pause
    exit /b 1
)

REM Hash the images ingest just downloaded, so the review UI launches
REM instantly instead of hashing them itself on startup.
echo.
echo === [4/5] Hash new images (duplicate detection) ===
".venv-win\Scripts\python.exe" imagemeta.py warm
if errorlevel 1 (
    echo.
    echo Hashing FAILED - see error above. Review still works; it will
    echo hash on startup instead.
    pause
)

REM Refresh the archive-side half of duplicate detection. Everything filed
REM since the last build is missing from the index, and a missing file is
REM compared against nothing - which is how real duplicates slip through.
REM Resumable and incremental: it only hashes files that are new or changed,
REM but it does stat every file in ARCHIVE_DIR, so give it a few minutes.
echo.
echo === [5/5] Index newly archived files (duplicate detection) ===
".venv-win\Scripts\python.exe" hash_index.py build
if errorlevel 1 (
    echo.
    echo Archive indexing FAILED - see error above. Review still works;
    echo entries filed since the last successful build are compared from
    echo their cached hash instead.
    pause
)

echo.
echo ============================================================
echo  Maintenance cycle complete. See the summaries above:
echo    [1] backfill bucket counts (owned / new / dead / ...)
echo    [2] whitelist TOTAL + NEW since last export = posts to drain
echo    [3] ingest downloaded=... line
echo    [4] images hashed for duplicate detection
echo    [5] archive files newly indexed (the "indexed N" number)
echo.
echo  NEXT: Load the updated data\unsave_list.json into the browser
echo  userscript to drain newly-captured posts.
echo ============================================================
pause
