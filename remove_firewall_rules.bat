@echo off
setlocal

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Administrator privileges required.
    echo         Right-click this file and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

echo Removing Win Wallpaper Push firewall rules...
echo.

rem Portable deployment (add_firewall_rules.bat)
netsh advfirewall firewall delete rule name="WinWallpaperPush-UDP-Broadcast"
netsh advfirewall firewall delete rule name="WinWallpaperPush-UDP-Reply"
netsh advfirewall firewall delete rule name="WinWallpaperPush-TCP-Transfer"
rem Written by "WallpaperAgent.exe --install" / install_agent.bat
netsh advfirewall firewall delete rule name="WinWallpaperPush-Agent-UDP-38571"
rem Written by the MSI
netsh advfirewall firewall delete rule name="Win Wallpaper Push Agent (UDP 38571)"

echo.
echo Done.
echo.
pause
