@echo off
rem Oshiire one-shot installer (Windows). Builds .venv-win (the path Oshiire.bat
rem invokes by name), installs requirements, THEN upgrades numpy past the
rem numpy<2 pin dghs-imgutils carries -- on Python 3.14 that pin has no wheel and
rem pip builds an experimental MinGW numpy that crashes Oshiire on startup. The
rem numpy upgrade is NOT optional; this script does it for you. See docs\SETUP.md.
setlocal
cd /d "%~dp0"

if not exist ".venv-win\Scripts\python.exe" (
    echo Creating virtualenv .venv-win ...
    python -m venv .venv-win
    if errorlevel 1 (
        echo.
        echo Could not create .venv-win. Is Python 3.14 installed and on PATH?
        pause
        exit /b 1
    )
)

echo.
echo [1/2] Installing requirements ...
".venv-win\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Requirements install failed -- see the output above.
    pause
    exit /b 1
)

echo.
echo [2/2] Upgrading numpy past the numpy^<2 pin ...
".venv-win\Scripts\python.exe" -m pip install --upgrade numpy
if errorlevel 1 (
    echo.
    echo numpy upgrade failed -- see the output above.
    echo Oshiire will not start until numpy is 2.x; retry:
    echo     .venv-win\Scripts\pip install --upgrade numpy
    pause
    exit /b 1
)

echo.
echo Installed numpy version:
".venv-win\Scripts\python.exe" -c "import numpy; print('  numpy', numpy.__version__)"

echo.
echo Done. Double-click Oshiire.bat to start.
pause
