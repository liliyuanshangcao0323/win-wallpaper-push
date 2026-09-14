@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Win Wallpaper Push - build two standalone exe files
echo ============================================================
echo.

rem NOTE: keep this file pure ASCII and CRLF - cmd.exe seeks through .bat
rem files by byte offset and multi-byte characters corrupt later lines.

python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller not found. Install it first:
    echo         python -m pip install pyinstaller
    echo.
    pause
    exit /b 1
)

rem Bundle the BurntToast module INSIDE both exe files, so a client machine
rem only needs the single exe: no internet, no PSGallery, no extra folder.
rem The module is ~1 MB because prepare_toast_module.bat trims the 20 MB WinRT
rem projection DLL (Microsoft.Windows.SDK.NET.dll) that this tool never uses.
set "TOASTDATA="
if exist "%~dp0BurntToast\BurntToast.psd1" set TOASTDATA=--add-data "BurntToast;BurntToast"
if exist "%~dp0BurntToast\1.1.0\BurntToast.psd1" set TOASTDATA=--add-data "BurntToast;BurntToast"
if defined TOASTDATA (
    echo Toast module: bundled INSIDE the exe
) else (
    echo Toast module: NOT bundled - run prepare_toast_module.bat first,
    echo                otherwise clients need internet to install it once.
)
echo.

echo [1/2] Building CONTROLLER  --^>  dist\WallpaperController.exe
echo ------------------------------------------------------------
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name WallpaperController ^
    --hidden-import PIL.Image ^
    --hidden-import PIL.ImageTk ^
    --hidden-import PIL.ImageDraw ^
    --hidden-import PIL._tkinter_finder ^
    --hidden-import toastui ^
    --hidden-import toastspec ^
    --hidden-import sshui ^
    --hidden-import sshcmd ^
    --hidden-import ui ^
    --hidden-import netutil ^
    --hidden-import protocol ^
    --exclude-module numpy ^
    --exclude-module matplotlib ^
    --exclude-module scipy ^
    --exclude-module PyQt5 ^
    --exclude-module PySide2 ^
    %TOASTDATA% ^
    --distpath dist --workpath build ^
    controller.py
if errorlevel 1 goto fail

echo.
echo [2/2] Building AGENT  --^>  dist\WallpaperAgent.exe
echo ------------------------------------------------------------
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name WallpaperAgent ^
    --hidden-import PIL.Image ^
    --hidden-import toast ^
    --hidden-import toastspec ^
    --hidden-import agentauth ^
    --hidden-import winipc ^
    --hidden-import netutil ^
    --hidden-import protocol ^
    --hidden-import wallpaper ^
    --hidden-import ui ^
    --exclude-module numpy ^
    --exclude-module matplotlib ^
    --exclude-module scipy ^
    --exclude-module PyQt5 ^
    --exclude-module PySide2 ^
    %TOASTDATA% ^
    --distpath dist --workpath build ^
    agent.py
if errorlevel 1 goto fail

echo.
echo [3/3] Copying the BurntToast module folder next to the exe (optional)
echo ------------------------------------------------------------
echo   The module is already INSIDE both exe files, so the .exe alone is enough.
echo   This copy is only an override slot: a BurntToast folder next to the exe
echo   wins over the built-in one (use it to upgrade the module without a rebuild).
if exist "%~dp0BurntToast\BurntToast.psd1" (
    xcopy "%~dp0BurntToast" "%~dp0dist\BurntToast\" /E /I /Y /Q >nul
    echo   dist\BurntToast copied as an override.
) else (
    if exist "%~dp0BurntToast" (
        xcopy "%~dp0BurntToast" "%~dp0dist\BurntToast\" /E /I /Y /Q >nul
        echo   dist\BurntToast copied as an override.
    ) else (
        echo   no .\BurntToast folder - the built-in module is still used, and
        echo   notifications keep working. Run prepare_toast_module.bat only if
        echo   you want to bundle or ship a module folder as well.
    )
)

echo.
echo ============================================================
echo   BUILD OK - output is in the dist folder:
echo.
echo     dist\WallpaperController.exe   (controller)
echo     dist\WallpaperAgent.exe        (agent)
echo.
echo   Both exe files already contain the BurntToast module, so copying
echo   the single exe to a client machine is enough for notifications.
echo ============================================================
echo.
dir /b dist\*.exe
echo.
exit /b 0

:fail
echo.
echo ============================================================
echo   BUILD FAILED - see the log above.
echo ============================================================
exit /b 1
