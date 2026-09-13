@echo off
setlocal
cd /d "%~dp0"

rem NOTE: keep this file pure ASCII - cmd.exe seeks through .bat files by byte
rem offset and multi-byte characters corrupt the following lines.

rem ============================================================
rem   Unattended install of the Win Wallpaper Push agent (MSI)
rem
rem   Usage:
rem     install_agent.bat                     interactive (elevates via UAC, waits)
rem     install_agent.bat /quiet              fully unattended: no UAC prompt,
rem                                          no pause, exit code = msiexec's
rem     install_agent.bat /quiet D:\x.msi     ... and install that MSI instead of
rem                                          the one sitting next to this script
rem
rem   What it does, in order:
rem     1. elevate (UAC) if needed          [/quiet: fail instead]
rem     2. msiexec /i ... /qn   - silent, logs to <msi folder>\install.log
rem     3. make sure UDP 38571 inbound is allowed
rem        (the MSI adds a rule too; this is a safety net, duplicates are harmless)
rem     4. start the agent right now, in THIS user session, silently
rem        [skipped when running as SYSTEM - there is no user session then]
rem     5. verify: firewall rule, running process, agent status
rem
rem   For GPO / SCCM / Intune you can also just push the MSI itself:
rem     msiexec /i "WallpaperAgent.msi" /qn /norestart
rem   the agent then starts by itself at the next logon (HKLM Run entry).
rem ============================================================

set "QUIET="
if /i "%~1"=="/quiet"  set "QUIET=1"
if /i "%~1"=="-quiet"  set "QUIET=1"
if /i "%~1"=="--quiet" set "QUIET=1"
if /i "%~1"=="/q"      set "QUIET=1"

rem MSI: use the .msi given on the command line, else next to this script / dist
set "MSI=%~dp0dist\WallpaperAgent.msi"
if not exist "%MSI%" set "MSI=%~dp0WallpaperAgent.msi"
for %%A in (%*) do (
    if /i "%%~xA"==".msi" set "MSI=%%~A"
)
for %%A in ("%MSI%") do set "MSIDIR=%%~dpA"
for %%A in ("%MSI%") do set "MSINAME=%%~nxA"

set "AGENT=C:\Program Files\WinWallpaperPush\WallpaperAgent.exe"
set "LOG=%MSIDIR%install.log"
set "FWRULE=WinWallpaperPush-Agent-UDP-38571"
set "STARTTASK=WinWallpaperPushAgentStartNow"

if not exist "%MSI%" (
    echo [ERROR] WallpaperAgent.msi not found:
    echo         %MSI%
    echo.
    echo   Run build_msi.bat first to produce it.
    echo.
    if not defined QUIET pause
    exit /b 1
)

rem ---------------------------------------------------------------- elevate
net session >nul 2>&1
if errorlevel 1 (
    if defined QUIET (
        echo [ERROR] Administrator privileges are required.
        echo         Run this from an elevated context ^(SCCM / GPO run as SYSTEM^),
        echo         or use: msiexec /i "WallpaperAgent.msi" /qn /norestart
        exit /b 1603
    )
    echo Administrator privileges are required.
    echo A UAC prompt will appear - click Yes.
    echo.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)

echo ============================================================
echo   Installing Win Wallpaper Push Agent  ^(silent^)
echo ============================================================
echo.
echo   MSI : %MSI%
echo   Log : %LOG%
echo.

rem ---------------------------------------------------------------- 1. install
msiexec /i "%MSI%" /qn /norestart /l*v "%LOG%"
set "RC=%ERRORLEVEL%"

echo   msiexec exit code: %RC%
if "%RC%"=="0"    echo   OK      installed successfully.
if "%RC%"=="3010" echo   OK      installed, a reboot is required.
if "%RC%"=="1603" echo   FAILED  fatal error - check %LOG%
if "%RC%"=="1618" echo   FAILED  another installation is already in progress.
if "%RC%"=="1619" echo   FAILED  the MSI package could not be opened.
echo.

if not "%RC%"=="0" if not "%RC%"=="3010" (
    echo   Install did not succeed - stopping here.
    echo.
    if not defined QUIET pause
    exit /b %RC%
)

rem ---------------------------------------------------------------- 2. firewall
rem The MSI installs a port based rule for UDP 38571. Firewall custom actions can
rem fail silently (they are IgnoreFailure=yes on purpose), and a missing rule
rem looks exactly like "installed but never receives anything", so check and add.
echo   [firewall] checking inbound UDP 38571 ...
netsh advfirewall firewall show rule name=all dir=in 2>nul | findstr /c:"38571" >nul
if errorlevel 1 (
    echo   [firewall] rule NOT found - adding "%FWRULE%"
    netsh advfirewall firewall delete rule name="%FWRULE%" >nul 2>&1
    netsh advfirewall firewall add rule name="%FWRULE%" dir=in action=allow protocol=UDP localport=38571 >nul
    if errorlevel 1 (
        echo   [firewall] FAILED to add the rule - the agent will not receive
        echo              broadcasts until this port is allowed.
    ) else (
        echo   [firewall] OK
    )
) else (
    echo   [firewall] rule present
)

rem ---------------------------------------------------------------- 3. start now
rem The MSI is installed as SYSTEM, so it cannot start a process inside the
rem logged on user's session. A one-shot scheduled task with /IT does exactly
rem that, and runs at normal (non elevated) integrity - which matters because
rem an elevated agent's control objects cannot be opened by a normal --stop.
rem
rem Running as SYSTEM (SCCM / GPO script) there is no user session at all, so
rem starting the agent here would just create a useless session-0 process.
echo.
for /f "delims=" %%U in ('whoami 2^>nul') do set "WHOAMI=%%U"
echo %WHOAMI% | find /i "system" >nul
if not errorlevel 1 (
    echo   [start] running as SYSTEM - no user session to start in.
    echo           The agent will start by itself at the next user logon.
    echo           To start it right away in a user session, push
    echo             "C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --silent
    echo           as a user-context step ^(SCCM: run with user's rights /
    echo           Intune: platform script with "run using logged on credentials"^).
    goto verify
)
if not exist "%AGENT%" (
    echo   [start] WARNING: %AGENT% not found - skipping the immediate start.
    goto verify
)
echo   [start] starting the agent in this user session (silent) ...
schtasks /create /tn "%STARTTASK%" /tr "\"%AGENT%\" --silent" /sc once /st 00:00 /it /f >nul 2>&1
if errorlevel 1 (
    echo   [start] scheduled task not available - starting directly instead
    start "" "%AGENT%" --silent
) else (
    schtasks /run /tn "%STARTTASK%" >nul 2>&1
    schtasks /delete /tn "%STARTTASK%" /f >nul 2>&1
)
ping -n 5 127.0.0.1 >nul

:verify
rem ---------------------------------------------------------------- 4. verify
echo.
echo   ------------------------------------------------------------
echo   Verify
echo   ------------------------------------------------------------
tasklist /FI "IMAGENAME eq WallpaperAgent.exe" 2>nul | find /i "WallpaperAgent.exe" >nul
if errorlevel 1 (
    echo   process: NOT running yet
    echo            Wallpaper is a per-user setting, so the agent runs inside
    echo            the user session and is started from HKLM Run at logon.
    echo            Log off and on again, or run start_agent.bat / the agent exe.
) else (
    echo   process: running
)
if exist "%AGENT%" (
    "%AGENT%" --status
)
echo.

echo   Next steps
echo     - it starts by itself at every logon ^(HKLM Run^)
echo     - show its window : Start menu -^> "Win Wallpaper Push" -^> Show window
echo     - status / stop / log : Start menu -^> Agent console
echo     - uninstall : uninstall_agent.bat
echo.
if not defined QUIET pause
exit /b 0
