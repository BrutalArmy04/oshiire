@echo off
setlocal
cd /d "%~dp0.."

".venv-win\Scripts\python.exe" ingest.py
if errorlevel 1 (
    echo.
    echo Ingest FAILED - see error above.
    pause
    exit /b 1
)

echo.
echo Ingest complete.
pause
