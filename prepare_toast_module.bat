@echo off
setlocal
cd /d "%~dp0"

rem NOTE: keep this file pure ASCII and CRLF - cmd.exe seeks through .bat files
rem by byte offset, and multi-byte characters corrupt the following lines.

rem ============================================================
rem   Prepare the BurntToast module for OFFLINE deployment.
rem
rem   Run this ONCE on a machine that has internet. It puts the module into
rem   .\BurntToast\ and trims it down to ~1 MB (drops a 20 MB DLL that only
rem   the Microsoft Toolkit "compat" path needs and this tool never uses).
rem
rem   After that, build.bat packs the folder INSIDE WallpaperAgent.exe, so a
rem   client machine needs nothing but the single exe: no internet, no
rem   PSGallery, no extra files, nothing installed system wide.
rem
rem   The agent looks for the module in this order: configured path,
rem   BurntToast\ next to the exe, the copy built into the exe, then the
rem   per-user cache (see README, "client needs the BurntToast module").
rem ============================================================

set "DEST=%~dp0BurntToast"

echo ============================================================
echo   Prepare BurntToast for offline deployment
echo ============================================================
echo.

if exist "%DEST%\BurntToast.psd1" goto already
if exist "%DEST%\1.1.0\BurntToast.psd1" goto already

echo [1/2] trying to copy it from this machine if it is already installed ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$m = Get-Module -ListAvailable BurntToast | Select-Object -First 1; if (-not $m) { exit 1 }; $src = Split-Path $m.Path -Parent; Copy-Item $src '%DEST%' -Recurse -Force; if (Test-Path (Join-Path '%DEST%' 'BurntToast.psd1')) { exit 0 } else { exit 1 }"
if not errorlevel 1 goto trim

echo       not installed here - downloading from PSGallery ...
echo [2/2] Save-Module -Name BurntToast
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Save-Module -Name BurntToast -Path '%~dp0' -Force"
if errorlevel 1 goto fail

if exist "%DEST%\BurntToast.psd1" goto trim
if exist "%DEST%\1.1.0\BurntToast.psd1" goto trim
for /d %%D in ("%DEST%\*") do (
    if exist "%%D\BurntToast.psd1" (
        echo       flattening version folder ...
        xcopy "%%D\*" "%DEST%\" /E /I /Y >nul
        rmdir /s /q "%%D"
    )
)

:already
if not exist "%DEST%\BurntToast.psd1" if not exist "%DEST%\1.1.0\BurntToast.psd1" goto fail

:trim
rem Remove the 20 MB WinRT projection DLL. It is only used by the Microsoft
rem Toolkit "compat" submission path (ToastNotificationManagerCompat); this
rem tool submits through WinRT with an explicit AppId instead, so the DLL is
rem never loaded. Trimming takes the module from ~21.8 MB to ~1 MB, which is
rem what lets build.bat bundle it INSIDE the exe.
set "BIGDLL=%DEST%\lib\Microsoft.Windows.SDK.NET\Microsoft.Windows.SDK.NET.dll"
if exist "%BIGDLL%" (
    del /f /q "%BIGDLL%" >nul 2>&1
    if exist "%BIGDLL%" (
        echo   NOTE: could not remove Microsoft.Windows.SDK.NET.dll - module
        echo         still works, it is just ~21 MB instead of ~1 MB.
    ) else (
        echo   trimmed lib\Microsoft.Windows.SDK.NET\Microsoft.Windows.SDK.NET.dll
    )
)

:done
echo.
echo ============================================================
echo   OK - module ready:
echo.
dir /b "%DEST%"
echo.
echo   Module size:
powershell -NoProfile -Command ^
  "'    {0:N2} MB' -f ((Get-ChildItem '%DEST%' -Recurse -File | Measure-Object Length -Sum).Sum/1MB)"
echo.
echo   Option A (recommended): just run build.bat - it packs this folder
echo            INSIDE WallpaperAgent.exe, so one exe file is enough on
echo            every client.
echo   Option B: keep it as a folder and copy BOTH of these to the client
echo            (same folder):  WallpaperAgent.exe  +  BurntToast\
echo.
echo   Either way the agent picks it up automatically at startup - no
echo   internet, no admin rights, nothing installed system wide.
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo ============================================================
echo   FAILED - could not obtain the BurntToast module.
echo.
echo   Do it by hand on a machine with internet:
echo     powershell -Command "Save-Module BurntToast -Path .\"
echo   then copy the generated BurntToast folder next to the agent exe.
echo ============================================================
echo.
pause
exit /b 1
