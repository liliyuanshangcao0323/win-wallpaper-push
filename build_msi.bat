@echo off
setlocal
cd /d "%~dp0"

rem NOTE: this file must stay pure ASCII.
rem cmd.exe seeks through a .bat file by BYTE OFFSET. Multi-byte characters
rem (e.g. UTF-8 Chinese in a rem comment) make it land in the middle of a
rem character and it then eats the first characters of the following lines.
rem That is why every .bat in this project is English-only.

set "ROOT=%~dp0"
set "WIXDIR=%ROOT%.tools\wix3"
set "CANDLE=%WIXDIR%\candle.exe"
set "LIGHT=%WIXDIR%\light.exe"
set "EXTF=%WIXDIR%\WixFirewallExtension.dll"
set "EXTU=%WIXDIR%\WixUtilExtension.dll"
set "EXTUI=%WIXDIR%\WixUIExtension.dll"
set "MSI=%ROOT%dist\WallpaperAgent.msi"

rem ---------------------------------------------------------- 0. python check
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. It is required for the license generator
    echo         and for fetching the WiX toolset.
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo   Build the AGENT .msi installer
echo ============================================================
echo.

rem ---------------------------------------------------------- 1. WiX toolset
rem The download is done by Python on purpose: on locked down networks the
rem PowerShell / curl HTTP stacks cannot reach github.com while Python can.
if not exist "%CANDLE%" (
    echo [1/4] WiX toolset not found - downloading ^(about 40 MB^)...
    python "%ROOT%installer\get_wix.py" --dest "%WIXDIR%"
    if errorlevel 1 (
        echo.
        echo [ERROR] Could not obtain the WiX toolset.
        echo         Follow the manual download instructions printed above,
        echo         then run build_msi.bat again.
        echo.
        pause
        exit /b 1
    )
) else (
    echo [1/4] WiX toolset found.
)

rem ---------------------------------------------------------- 2. agent exe
if not exist "%ROOT%dist\WallpaperAgent.exe" (
    echo.
    echo [ERROR] dist\WallpaperAgent.exe not found.
    echo         Run build.bat first to produce the exe.
    echo.
    pause
    exit /b 1
)
echo [2/4] Agent executable found.

rem ---------------------------------------------------------- 3. license rtf
echo [3/4] Generating License.rtf from License.txt ...
python "%ROOT%installer\make_license_rtf.py"
if errorlevel 1 goto fail

rem ---------------------------------------------------------- 4. compile link
rem Switch into the installer folder first: every relative path inside the
rem .wxs file (..\dist\... , README-AGENT.md, WixUILicenseRtf) is resolved
rem against the .wxs file location.
cd /d "%ROOT%installer"

if exist Agent.wixobj del /q Agent.wixobj
if exist "%MSI%" del /q "%MSI%"

echo [4/4] Compiling and linking ...
echo.

"%CANDLE%" -nologo -arch x64 -ext "%EXTF%" -ext "%EXTU%" -ext "%EXTUI%" -out Agent.wixobj Agent.wxs
if errorlevel 1 goto fail

rem -cultures takes a priority list. WixUIExtension has zh-CN strings but
rem WixFirewallExtension only ships en-US, so zh-CN alone makes light fail
rem with "localization variable is unknown". Listing both fixes that: light
rem uses zh-CN where available and falls back to en-US for the rest.
rem
rem Silenced ICE checks, each one intentional:
rem   ICE61  we set AllowSameVersionUpgrades="yes" on purpose so the same
rem          version can be reinstalled (repair / re-push by GPO)
rem   ICE43  wants non-advertised shortcuts to use a HKCU keypath. That rule
rem   ICE57  targets per-user "just me" installs; this package is per-machine
rem          (ALLUSERS), where HKCU during a SYSTEM/GPO install would land in
rem          the wrong hive. Our shortcuts live in per-machine components whose
rem          KeyPath is the installed file, and ProgramMenuFolder resolves to
rem          the All Users start menu - which is exactly what we want.
rem
rem Note: light.exe has no flag for relocating the .wixpdb it emits. Its -pdb
rem option takes an INPUT pdb (used when building patches), so passing an
rem output path there fails with LGHT0103. The file is moved afterwards instead.
"%LIGHT%" -nologo -sice:ICE61 -sice:ICE43 -sice:ICE57 -ext "%EXTF%" -ext "%EXTU%" -ext "%EXTUI%" -cultures:zh-CN;en-US -out "%MSI%" Agent.wixobj
if errorlevel 1 goto fail

rem Keep the debug symbols with the source so dist\ only ever holds deliverables.
if exist "%ROOT%dist\WallpaperAgent.wixpdb" move /y "%ROOT%dist\WallpaperAgent.wixpdb" "%ROOT%installer\Agent.wixpdb" >nul

cd /d "%ROOT%"
echo.
echo ============================================================
echo   BUILD OK
echo.
echo     dist\WallpaperAgent.msi
echo.
echo   Silent install   ^(as administrator^):
echo     msiexec /i "dist\WallpaperAgent.msi" /qn
echo.
echo   Silent uninstall ^(as administrator^):
echo     msiexec /x "dist\WallpaperAgent.msi" /qn
echo.
echo   Install with full logging:
echo     msiexec /i "dist\WallpaperAgent.msi" /qn /l*v install.log
echo ============================================================
echo.
dir /b /-c "%ROOT%dist\*.msi"
echo.
exit /b 0

:fail
cd /d "%ROOT%"
echo.
echo ============================================================
echo   BUILD FAILED - see the compiler output above.
echo ============================================================
exit /b 1
