@echo off
setlocal
cd /d "%~dp0.."

set /p CONFIRM=Apply these archive moves now? (y/n):
if /i not "%CONFIRM%"=="y" (
    echo Cancelled - no changes made.
    pause
    exit /b 0
)

".venv-win\Scripts\python.exe" archive.py --apply
if errorlevel 1 (
    echo.
    echo Archive apply FAILED - see error above.
    pause
    exit /b 1
)

echo.
echo Archive apply complete.
pause
