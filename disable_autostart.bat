@echo off
setlocal
cd /d "%~dp0"

rem NOTE: keep this file pure ASCII and CRLF - cmd.exe seeks through .bat files
rem by byte offset, and multi-byte characters corrupt the following lines.

rem ============================================================
rem   Disable autostart (portable deployment).
rem
rem   Thin wrapper: WallpaperAgent.exe --uninstall does the work -
rem   it stops the running agent, removes the HKCU Run entry and the
rem   logon scheduled task. Program files and firewall rules stay.
rem
rem   The MSI writes its own HKLM entry; remove that by uninstalling
rem   the MSI (uninstall_agent.bat) or, as administrator:
rem     reg delete "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" /v WinWallpaperPushAgent /f
rem ============================================================

set "EXE=%~dp0dist\WallpaperAgent.exe"
if not exist "%EXE%" set "EXE=%~dp0WallpaperAgent.exe"

set "FOUND="

if exist "%EXE%" (
    "%EXE%" --uninstall
    set "FOUND=1"
) else (
    echo WallpaperAgent.exe not found next to this script - cleaning the
    echo autostart entries directly instead.
    echo.
    schtasks /delete /tn "WinWallpaperPushAgent" /f >nul 2>&1
    if not errorlevel 1 (
        echo Removed logon scheduled task: WinWallpaperPushAgent
        set "FOUND=1"
    )
    reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v WinWallpaperAgent /f >nul 2>&1
    if not errorlevel 1 (
        echo Removed HKCU Run entry: WinWallpaperAgent
        set "FOUND=1"
    )
)

reg query "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" /v WinWallpaperPushAgent >nul 2>&1
if not errorlevel 1 (
    echo.
    echo NOTE: this machine also has the MSI autostart entry
    echo       HKLM\...\Run\WinWallpaperPushAgent
    net session >nul 2>&1
    if errorlevel 1 (
        echo       Removing it needs administrator rights - run this file as
        echo       administrator, or uninstall the MSI: uninstall_agent.bat
    ) else (
        reg delete "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" /v WinWallpaperPushAgent /f >nul 2>&1
        if not errorlevel 1 echo       Removed HKLM Run entry: WinWallpaperPushAgent
    )
)

if not defined FOUND echo Nothing to remove - no autostart entry was found.

echo.
echo Tip: to remove the firewall rule as well: remove_firewall_rules.bat
echo.
pause
exit /b 0
