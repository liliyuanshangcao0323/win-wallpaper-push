@echo off
setlocal
cd /d "%~dp0"

rem NOTE: keep this file pure ASCII and CRLF - cmd.exe seeks through .bat files
rem by byte offset, and multi-byte characters corrupt the following lines.

rem ============================================================
rem   Enable autostart for the PORTABLE exe deployment.
rem
rem   This is a thin wrapper: the agent exe does the real work now.
rem
rem     enable_autostart.bat          -> WallpaperAgent.exe --install
rem                                      (writes HKCU Run + opens UDP 38571;
rem                                       the firewall part shows one UAC prompt)
rem     enable_autostart.bat /task    -> logon scheduled task instead
rem                                      (for machines where policy or a
rem                                       "cleaner" tool disables Run entries)
rem
rem   You can skip this file entirely:
rem     WallpaperAgent.exe --install
rem   or just tick "run automatically at logon" in the agent window.
rem ============================================================

set "MODE=install"
if /i "%~1"=="/task"  set "MODE=task"
if /i "%~1"=="-task"  set "MODE=task"
if /i "%~1"=="--task" set "MODE=task"

set "EXE=%~dp0dist\WallpaperAgent.exe"
if not exist "%EXE%" set "EXE=%~dp0WallpaperAgent.exe"

if not exist "%EXE%" (
    echo [ERROR] WallpaperAgent.exe not found.
    echo.
    echo   Looked in:
    echo     %~dp0dist\WallpaperAgent.exe
    echo     %~dp0WallpaperAgent.exe
    echo.
    echo   Put this .bat next to the exe, or run build.bat first.
    echo.
    pause
    exit /b 1
)

if /i "%MODE%"=="task" goto maketask

rem ---------------------------------------------------------------- normal mode
"%EXE%" --install
set "RC=%ERRORLEVEL%"
echo.
echo   To undo: disable_autostart.bat
echo.
pause
exit /b %RC%

rem ---------------------------------------------------------------- logon task
:maketask
rem A logon trigger is machine wide, so creating that task needs admin rights.
net session >nul 2>&1
if errorlevel 1 (
    echo Administrator rights are required for the logon task mode.
    echo A UAC prompt will appear - click Yes.
    echo.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '/task' -Verb RunAs"
    exit /b 0
)

"%EXE%" --install --task
set "RC=%ERRORLEVEL%"
echo.
echo   To undo: disable_autostart.bat
echo.
pause
exit /b %RC%
