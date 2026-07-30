@echo off
setlocal
cd /d "%~dp0.."

".venv-win\Scripts\python.exe" ai_tag.py
if errorlevel 1 (
    echo.
    echo AI tagging FAILED - see error above.
    pause
    exit /b 1
)

echo.
echo AI tagging complete.
pause
