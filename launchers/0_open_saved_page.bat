@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0.."

REM Default placeholder - edit if .env has no REDDIT_USERNAME.
set "REDDIT_USERNAME=YOUR_REDDIT_USERNAME"

REM Pull REDDIT_USERNAME out of .env if it's set there (non-blank wins).
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if /i "%%A"=="REDDIT_USERNAME" if not "%%B"=="" set "REDDIT_USERNAME=%%B"
    )
)

start "" "https://old.reddit.com/user/!REDDIT_USERNAME!/saved"
