@echo off
setlocal
cd /d "%~dp0"

rem NOTE: keep this file pure ASCII - cmd.exe seeks through .bat files by byte
rem offset and multi-byte characters corrupt the following lines.

rem ---------------------------------------------------------------- elevate
net session >nul 2>&1
if errorlevel 1 (
    echo Administrator privileges are required.
    echo A UAC prompt will appear - click Yes.
    echo.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)

echo ============================================================
echo   Uninstalling Win Wallpaper Push Agent  ^(silent^)
echo ============================================================
echo.

rem The .wxs uses <Product Id="*">, so the ProductCode changes on EVERY rebuild
rem while the UpgradeCode stays fixed. Therefore the installed product is looked
rem up by Publisher. The lookup lives in a separate .ps1 because cmd's for /f
rem parser gets confused by parentheses and pipes inside the backtick command.
set "PRODUCTCODE="
for /f "usebackq delims=" %%G in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\find_installed.ps1"`) do set "PRODUCTCODE=%%G"

if defined PRODUCTCODE (
    echo   Found installed product: %PRODUCTCODE%
    echo.
    msiexec /x "%PRODUCTCODE%" /qn /norestart
) else (
    set "MSI=%~dp0dist\WallpaperAgent.msi"
    if exist "%MSI%" (
        echo   No installed product found by Publisher - trying the local MSI.
        echo.
        msiexec /x "%MSI%" /qn /norestart
    ) else (
        echo   Nothing to uninstall: no product with Publisher WinWallpaperPush
        echo   is registered, and no local MSI was found either.
        echo.
        pause
        exit /b 1
    )
)

set "RC=%ERRORLEVEL%"
echo.
echo   msiexec exit code: %RC%
if "%RC%"=="0"    echo   OK      uninstalled successfully.
if "%RC%"=="1605" echo   OK      product was not installed.
if "%RC%"=="3010" echo   OK      uninstalled, a reboot is required.
echo.
echo   Removed : program files, HKLM Run entry, firewall rule.
echo   Kept    : %%LOCALAPPDATA%%\WinWallpaperPush\wallpapers  ^(per-user data^)
echo             C:\ProgramData\WinWallpaperPush\  ^(may remain if it was edited^)
echo.
pause
exit /b %RC%
