@echo off
setlocal
cd /d "%~dp0.."

".venv-win\Scripts\python.exe" resolve.py
if errorlevel 1 (
    echo.
    echo Flag-resolution UI exited with an error - see above.
    pause
    exit /b 1
)
