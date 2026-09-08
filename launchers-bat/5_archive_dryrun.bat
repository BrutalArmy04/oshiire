@echo off
setlocal
cd /d "%~dp0.."

".venv-win\Scripts\python.exe" archive.py
if errorlevel 1 (
    echo.
    echo Archive dry-run FAILED - see error above.
    pause
    exit /b 1
)

echo.
pause
