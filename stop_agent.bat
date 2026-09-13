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
echo   Stopping Win Wallpaper Push Agent
echo ============================================================
echo.

rem Graceful stop through the agent's own control channel (same user session).
"%EXE%" --stop
echo.

rem Anything still alive is in ANOTHER user session - that needs administrator.
tasklist /FI "IMAGENAME eq WallpaperAgent.exe" 2>nul | find /i "WallpaperAgent.exe" >nul
if errorlevel 1 goto done

net session >nul 2>&1
if errorlevel 1 (
    echo   NOTE: WallpaperAgent.exe is still in the process list.
    echo         It is probably running in another user session.
    echo         Right-click this file and run as administrator to end them all,
    echo         or run:  taskkill /IM WallpaperAgent.exe /F /T
    echo.
    pause
    exit /b 1
)

echo   Ending the remaining processes in all sessions ...
taskkill /IM WallpaperAgent.exe /F /T
echo.

:done
echo   ------------------------------------------------------------
echo   Reminder: the agent is also listed in Task Manager -^> Startup
echo   ^(name WinWallpaperAgent / WinWallpaperPushAgent^). It will start
echo   again at the next logon unless you remove that entry with
echo   disable_autostart.bat or by uninstalling the MSI.
echo   ------------------------------------------------------------
echo.
pause
exit /b 0
