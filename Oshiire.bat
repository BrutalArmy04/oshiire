@echo off
setlocal
cd /d "%~dp0"
rem A release ships its own Python in runtime\; a git checkout uses .venv-win.
rem app.py is passed by ABSOLUTE path so "Stop it and start fresh" can verify
rem the running instance whatever its working folder is.
set "OSHIIRE_PYW="
if exist "%~dp0runtime\pythonw.exe" set "OSHIIRE_PYW=%~dp0runtime\pythonw.exe"
if not defined OSHIIRE_PYW if exist "%~dp0.venv-win\Scripts\pythonw.exe" set "OSHIIRE_PYW=%~dp0.venv-win\Scripts\pythonw.exe"
if defined OSHIIRE_PYW goto run
rem Opened straight from the zip, Windows runs it from a temp folder.
set "OSHIIRE_HERE=%~dp0"
if /i not "%OSHIIRE_HERE:\AppData\Local\Temp\=%"=="%OSHIIRE_HERE%" goto inzip
echo Oshiire isn't set up yet... Follow docs\SETUP.md
pause
exit /b 1
:inzip
echo Oshiire is still inside the zip. Right-click the zip, choose Extract All, then open Oshiire.bat in the extracted folder.
pause
exit /b 1
:run
start "" "%OSHIIRE_PYW%" "%~dp0app.py"
