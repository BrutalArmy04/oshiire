@echo off
setlocal
cd /d "%~dp0"

"%~dp0.venv-win\Scripts\python.exe" review.py
if errorlevel 1 (
    echo.
    echo Review UI exited with an error - see above.
    pause
    exit /b 1
)
