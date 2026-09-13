@echo off
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Administrator privileges required.
    echo         Right-click this file and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo   Win Wallpaper Push - add firewall rules
echo ============================================================
echo.

netsh advfirewall firewall delete rule name="WinWallpaperPush-UDP-Broadcast" >nul 2>&1
netsh advfirewall firewall delete rule name="WinWallpaperPush-UDP-Reply"     >nul 2>&1
netsh advfirewall firewall delete rule name="WinWallpaperPush-TCP-Transfer"  >nul 2>&1

netsh advfirewall firewall add rule name="WinWallpaperPush-UDP-Broadcast" dir=in action=allow protocol=UDP localport=38571
netsh advfirewall firewall add rule name="WinWallpaperPush-UDP-Reply"     dir=in action=allow protocol=UDP localport=38573
netsh advfirewall firewall add rule name="WinWallpaperPush-TCP-Transfer"  dir=in action=allow protocol=TCP localport=38572

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to add one or more rules.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   DONE. Rules added:
echo.
echo     UDP 38571   broadcast receive   
echo                 ^> needed on AGENT machines
echo     UDP 38573   unicast reply       
echo                 ^> needed on the CONTROLLER machine
echo     TCP 38572   wallpaper transfer  
echo                 ^> needed on the CONTROLLER machine
echo.
echo   Run this on EVERY machine (controller and agents).
echo   To undo: remove_firewall_rules.bat
echo ============================================================
echo.
pause
