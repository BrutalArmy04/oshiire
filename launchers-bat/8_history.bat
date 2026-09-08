@echo off
setlocal
cd /d "%~dp0.."

REM Read-only history browser. Looks things up; changes nothing.
".venv-win\Scripts\python.exe" history.py
if errorlevel 1 (
    echo.
    echo History viewer exited with an error - see above.
    pause
    exit /b 1
)
