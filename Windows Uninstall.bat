@echo off
setlocal EnableDelayedExpansion
title FOREX Trader - Uninstall

:: FOREX Trader — Complete Uninstall
:: Removes the app folder (code + .venv), all local trading data (databases,
:: logs, sessions, config.yaml), the licence activation, and the Desktop
:: shortcut. Requires two separate confirmations before deleting anything.
::
:: This is for the manually-deployed setup (Setup & Start FOREX.bat /
:: git checkout). If FOREX Trader was installed via FOREX_Trader_Setup.exe
:: instead, use Windows Settings > Apps > Uninstall for that install —
:: this script does not touch its registry entries or Start Menu shortcuts.
::
:: A batch file cannot reliably delete the folder it is currently running
:: from, so this copies itself to %TEMP% and relaunches from there, passing
:: the real app folder as an argument, before doing any deleting.

if "%~2"=="RELAUNCHED" goto :main

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

set "SELF_COPY=%TEMP%\FOREX_Uninstall_%RANDOM%.bat"
copy /y "%~f0" "%SELF_COPY%" >nul
if errorlevel 1 (
    echo.
    echo  ERROR: Could not prepare the uninstaller. Try running as Administrator.
    pause
    exit /b 1
)
start "" cmd /c ""%SELF_COPY%" "%APP_DIR%" RELAUNCHED"
exit /b


:main
set "APP_DIR=%~1"
cls
echo.
echo  ==========================================================
echo    FOREX Trader — COMPLETE UNINSTALL
echo  ==========================================================
echo.
echo  This will PERMANENTLY DELETE:
echo.
echo    - The entire app folder, including the Python environment
echo      and any local git history:
echo        %APP_DIR%
echo.
echo    - All local trading data and settings:
echo        %APPDATA%\ForexTrader-Refactor2
echo        %APPDATA%\ForexTrader
echo      (trade databases, history, logs, session data, config.yaml)
echo.
echo    - Your licence activation:
echo        %USERPROFILE%\.forex_trader_licence
echo.
echo    - The Desktop shortcut, if present
echo.
echo  THIS CANNOT BE UNDONE. There is no backup and no recovery —
echo  trade history, logs, settings and the licence activation will
echo  be gone for good.
echo.
echo  If FOREX Trader is running right now, it will be force-stopped
echo  as part of this uninstall. Any trade currently open at your
echo  broker stays open, but this app will no longer be monitoring
echo  or managing it (stop-loss trailing, TP handling, etc.) once
echo  it's stopped — close or hand off any open trades first if that
echo  matters to you.
echo.
echo  Note: Portable Git (if this app installed it) is left in place,
echo  since other tools on this machine may depend on it. Remove it
echo  manually from %LOCALAPPDATA%\Programs\PortableGit if you don't
echo  need it.
echo.
echo  ==========================================================
echo.

set "APP_RUNNING=0"
set "RUNNING_PID="
for %%P in (8888 8890) do (
    for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":%%P " ^| findstr "LISTENING"') do (
        set "APP_RUNNING=1"
        set "RUNNING_PID=%%p"
    )
)
if "!APP_RUNNING!"=="1" (
    echo  WARNING: FOREX Trader appears to be running right now [PID !RUNNING_PID!].
    echo.
)

set "CONFIRM1="
set /p "CONFIRM1=Type DELETE (all caps) to continue, or press Enter to cancel: "
if not "!CONFIRM1!"=="DELETE" (
    echo.
    echo  Cancelled — nothing was deleted.
    pause
    exit /b 0
)

echo.
echo  Last chance. This is permanent and cannot be undone.
set "CONFIRM2="
set /p "CONFIRM2=Type YES to uninstall now: "
if not "!CONFIRM2!"=="YES" (
    echo.
    echo  Cancelled — nothing was deleted.
    pause
    exit /b 0
)

echo.
echo  Uninstalling...
echo.

for %%P in (8888 8890) do (
    for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":%%P " ^| findstr "LISTENING"') do (
        taskkill /PID %%p /F >nul 2>&1
        echo  Stopped running instance [PID %%p]
    )
)
timeout /t 2 /nobreak >nul

if exist "%USERPROFILE%\.forex_trader_licence" (
    del /f /q "%USERPROFILE%\.forex_trader_licence" >nul 2>&1
    echo  Removed licence activation
)

if exist "%USERPROFILE%\Desktop\FOREX Trader.lnk" (
    del /f /q "%USERPROFILE%\Desktop\FOREX Trader.lnk" >nul 2>&1
    echo  Removed Desktop shortcut
)
if exist "%PUBLIC%\Desktop\FOREX Trader.lnk" (
    del /f /q "%PUBLIC%\Desktop\FOREX Trader.lnk" >nul 2>&1
)

if exist "%APPDATA%\ForexTrader-Refactor2" (
    rmdir /s /q "%APPDATA%\ForexTrader-Refactor2" >nul 2>&1
    echo  Removed %APPDATA%\ForexTrader-Refactor2 (databases, logs, settings)
)
if exist "%APPDATA%\ForexTrader" (
    rmdir /s /q "%APPDATA%\ForexTrader" >nul 2>&1
    echo  Removed %APPDATA%\ForexTrader
)

echo  Removing app folder: %APP_DIR%
rmdir /s /q "%APP_DIR%" >nul 2>&1
if exist "%APP_DIR%" (
    echo.
    echo  WARNING: Could not fully remove %APP_DIR%
    echo  Some files may still be in use — close any open windows or
    echo  terminals in that folder and delete it manually.
) else (
    echo  App folder removed.
)

echo.
echo  ==========================================================
echo    Uninstall complete.
echo  ==========================================================
echo.
pause

del /f /q "%~f0" >nul 2>&1
