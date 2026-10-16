@echo off
cd /d "%~dp0"
if not exist ".venv-win\Scripts\pythonw.exe" (
    echo Oshiire isn't set up yet... Follow docs\SETUP.md
    pause
    exit /b 1
)
start "" ".venv-win\Scripts\pythonw.exe" app.py
