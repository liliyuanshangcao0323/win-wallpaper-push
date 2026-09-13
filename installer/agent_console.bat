@echo off
rem ============================================================
rem  Win Wallpaper Push Agent - text console
rem
rem  The agent runs silently in the background with no window and
rem  no tray icon. This console is the place where a user can see
rem  whether it is running, stop it, or open its log file.
rem
rem  NOTE: keep this file pure ASCII. cmd.exe seeks through .bat
rem  files by byte offset and multi-byte characters corrupt the
rem  following lines. The agent itself prints Chinese text into
rem  this window; that is fine because it is output, not source.
rem ============================================================
setlocal
cd /d "%~dp0"
set "AGENT=%~dp0WallpaperAgent.exe"
set "LOGFILE=%LOCALAPPDATA%\WinWallpaperPush\agent.log"

if not exist "%AGENT%" (
    echo [ERROR] WallpaperAgent.exe not found next to this script.
    echo         %AGENT%
    echo.
    pause
    exit /b 1
)

:menu
cls
echo ============================================================
echo   Win Wallpaper Push - Agent console
echo ============================================================
echo.
echo    1  Show status        (is it running? autostart, password, log)
echo    2  Show window        (asks for the panel password)
echo    3  Stop agent         (asks for the panel password)
echo    4  Open log file
echo    5  Start agent        (silent, in this session)
echo    6  Exit
echo.
echo   Note: the panel is password protected. The password is set the first
echo   time the window is opened (or by WallpaperAgent.exe --set-password).
echo.
set "CHOICE="
set /p "CHOICE=Choose 1-6 and press Enter: "

if "%CHOICE%"=="1" goto status
if "%CHOICE%"=="2" goto show
if "%CHOICE%"=="3" goto stop
if "%CHOICE%"=="4" goto log
if "%CHOICE%"=="5" goto start
if "%CHOICE%"=="6" goto end
goto menu

:status
echo.
"%AGENT%" --status
echo.
pause
goto menu

:show
echo.
"%AGENT%" --show
echo.
pause
goto menu

:stop
echo.
"%AGENT%" --stop
echo.
pause
goto menu

:log
echo.
if not exist "%LOGFILE%" (
    echo No log file yet: %LOGFILE%
    echo ^(it is created the first time the agent starts^)
    echo.
    pause
    goto menu
)
start "" notepad "%LOGFILE%"
goto menu

:start
echo.
"%AGENT%" --silent
echo.
echo   Note: --silent returns immediately when an instance is already
echo   running, so "nothing happened" here is normal.
echo.
pause
goto menu

:end
endlocal
exit /b 0
