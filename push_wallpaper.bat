@echo off
setlocal
cd /d "%~dp0"

set "EXE=%~dp0dist\WallpaperController.exe"
if not exist "%EXE%" set "EXE=%~dp0WallpaperController.exe"

if not exist "%EXE%" (
    echo [ERROR] WallpaperController.exe not found.
    echo         Run build.bat first, or put this file next to the exe.
    echo.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo ============================================================
    echo   Push a wallpaper from the command line
    echo ============================================================
    echo.
    echo   Usage:
    echo       push_wallpaper.bat "D:\path\to\image.jpg" [style]
    echo.
    echo   style is optional, default is fill:
    echo       fill     stretch the image to fill the screen
    echo       fit      fit inside the screen
    echo       stretch  stretch, may distort
    echo       tile     repeat
    echo       center   center without scaling
    echo       span     span across multiple monitors
    echo.
    echo   Example:
    echo       push_wallpaper.bat "D:\wallpapers\newyear.jpg" fill
    echo.
    echo   Tip: schedule this with Task Scheduler to rotate wallpapers.
    echo ============================================================
    echo.
    pause
    exit /b 2
)

set "STYLE=%~2"
if "%STYLE%"=="" set "STYLE=fill"

echo Pushing "%~1"  [style=%STYLE%]
echo Waiting for agents to confirm...
echo.

rem The exe is built without a console window, and cmd.exe does not wait for
rem windowed applications. "start /wait" makes it wait so we get a real exit code.
start /wait "" "%EXE%" --push "%~1" --style "%STYLE%" --wait 12

set "RC=%ERRORLEVEL%"
echo.
echo ------------------------------------------------------------
echo   Exit code: %RC%
if "%RC%"=="0" echo   OK    - at least one machine confirmed the change.
if "%RC%"=="2" echo   WARN  - agents are online but none confirmed. Check the firewall.
if "%RC%"=="3" echo   ERROR - no online agent found. Agent running? Same subnet?
echo ------------------------------------------------------------
echo.
exit /b %RC%
