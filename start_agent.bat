@echo off
setlocal
cd /d "%~dp0"

rem NOTE: keep this file pure ASCII - cmd.exe seeks through .bat files by byte
rem offset and multi-byte characters corrupt the following lines.

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

echo ============================================================
echo   Starting Win Wallpaper Push Agent  ^(silent, background^)
echo ============================================================
echo   %EXE%
echo.

rem If an instance is already running, the exe exits immediately by itself
rem (single instance), so start.exe returning "nothing" here is normal.
start "" "%EXE%" --silent

rem Give it a moment, then show the real status
ping -n 4 127.0.0.1 >nul
"%EXE%" --status

echo.
echo   Show its window : "%EXE%" --show
echo   Stop it         : stop_agent.bat
echo   Autostart       : enable_autostart.bat  (start at every logon)
echo.
pause
exit /b 0
