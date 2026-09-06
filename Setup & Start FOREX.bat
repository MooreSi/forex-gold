@echo off
setlocal EnableDelayedExpansion
title FOREX Trader - Starting Up

echo.
echo  ==========================================
echo            FOREX Trader
echo  ==========================================
echo.

set "SCRIPT_DIR=%~dp0"
:: pushd (not cd /d) - if this .bat is launched from a network share (a UNC
:: path like \\host\share\...), cmd.exe can't set the current directory to
:: it directly ("UNC paths are not supported") and silently falls back to
:: the Windows directory instead. pushd maps the UNC path to a temporary
:: drive letter first, which works from any launch location.
pushd "%SCRIPT_DIR%"

set "VENV_PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "REQ_FILE=%SCRIPT_DIR%requirements.txt"
set "MARKER=%SCRIPT_DIR%.venv\req_hash.txt"
set "USER_DATA=%APPDATA%\ForexTrader"

:: -- Python / venv check -------------------------------------------------------
:: If venv already exists skip straight to the hash check
if exist "%VENV_PYTHON%" goto :check_deps

:: No venv - find a system Python 3.11+
echo  Locating Python 3.11+...

set "SYS_PYTHON="

:: -- 1. Try Windows Python Launcher (py.exe) ------------------------------------
where py >nul 2>&1
if !errorlevel! == 0 (
    for /f "tokens=*" %%v in ('py --version 2^>^&1') do set "_pyver=%%v"
    echo !_pyver! | findstr /i "Python 3\." >nul
    if !errorlevel! == 0 (
        echo  Found: !_pyver! via py launcher
        set "SYS_PYTHON=py"
        goto :check_version
    )
)

:: -- 2. Try python / python3 in PATH - reject Windows Store stubs ---------------
::    Store stubs output a redirect message instead of "Python 3.x.y"
for %%C in (python python3) do (
    where %%C >nul 2>&1
    if !errorlevel! == 0 (
        for /f "tokens=*" %%v in ('%%C --version 2^>^&1') do set "_pyver=%%v"
        echo !_pyver! | findstr /i "Python 3\." >nul
        if !errorlevel! == 0 (
            echo  Found: !_pyver! via %%C
            set "SYS_PYTHON=%%C"
            goto :check_version
        )
    )
)

:: -- 3. Scan per-user install paths directly (bypasses Store stubs) -------------
if exist "%LOCALAPPDATA%\Programs\Python\" (
    for /f "delims=" %%d in ('dir /b /ad "%LOCALAPPDATA%\Programs\Python\" 2^>nul ^| findstr /ri "Python3[0-9]" ^| sort /r') do (
        if exist "%LOCALAPPDATA%\Programs\Python\%%d\python.exe" (
            for /f "tokens=*" %%v in ('"%LOCALAPPDATA%\Programs\Python\%%d\python.exe" --version 2^>^&1') do set "_pyver=%%v"
            echo !_pyver! | findstr /i "Python 3\." >nul
            if !errorlevel! == 0 (
                echo  Found: !_pyver! at %%d
                set "SYS_PYTHON=%LOCALAPPDATA%\Programs\Python\%%d\python.exe"
                goto :check_version
            )
        )
    )
)

:: -- 4. Nothing found - auto-install Python 3.11 --------------------------------
goto :auto_install_python

:check_version
:: Reject anything older than 3.11
for /f "tokens=2 delims= " %%v in ('!SYS_PYTHON! --version 2^>^&1') do set "_ver=%%v"
for /f "tokens=1,2 delims=." %%a in ("!_ver!") do (
    set "_vmaj=%%a"
    set "_vmin=%%b"
)
if !_vmaj! lss 3 goto :auto_install_python
if !_vmaj!==3 if !_vmin! lss 11 (
    echo  !_pyver! is too old - need 3.11+. Installing newer version...
    goto :auto_install_python
)
goto :create_venv

:auto_install_python
echo.
echo  Python 3.11 not found. Downloading installer - please wait...
echo  (One-time setup, takes about 30 seconds.)
echo.

set "_PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
set "_PY_INST=%TEMP%\python-3.11.9-amd64.exe"
set "_PY_DIR=%LOCALAPPDATA%\Programs\Python\Python311"

powershell -NoProfile -Command ^
  "try { Invoke-WebRequest -Uri '%_PY_URL%' -OutFile '%_PY_INST%' -UseBasicParsing } catch { Write-Host ('Download error: ' + $_.Exception.Message); exit 1 }"

if !errorlevel! neq 0 (
    echo  ERROR: Download failed. Check your internet connection and try again.
    pause
    exit /b 1
)
if not exist "%_PY_INST%" (
    echo  ERROR: Installer file missing after download.
    pause
    exit /b 1
)

echo  Installing Python 3.11 silently (no admin rights needed)...
"%_PY_INST%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_test=0 Include_launcher=1
:: Wait for MSI to flush
ping -n 4 127.0.0.1 >nul 2>&1

:: Check the expected install location first
if exist "%_PY_DIR%\python.exe" (
    set "SYS_PYTHON=%_PY_DIR%\python.exe"
    for /f "tokens=*" %%v in ('"%_PY_DIR%\python.exe" --version 2^>^&1') do echo  Installed: %%v
    goto :create_venv
)

:: Scan in case the installer chose a different folder
if exist "%LOCALAPPDATA%\Programs\Python\" (
    for /f "delims=" %%d in ('dir /b /ad "%LOCALAPPDATA%\Programs\Python\" 2^>nul ^| findstr /ri "Python3[0-9]" ^| sort /r') do (
        if exist "%LOCALAPPDATA%\Programs\Python\%%d\python.exe" (
            set "SYS_PYTHON=%LOCALAPPDATA%\Programs\Python\%%d\python.exe"
            for /f "tokens=*" %%v in ('"%LOCALAPPDATA%\Programs\Python\%%d\python.exe" --version 2^>^&1') do echo  Installed: %%v
            goto :create_venv
        )
    )
)

echo.
echo  ERROR: Python installation did not complete. Please install Python 3.11
echo  manually from https://www.python.org/downloads/ (tick "Add to PATH"),
echo  then run this file again.
pause
exit /b 1

:create_venv
echo  Creating virtual environment...
%SYS_PYTHON% -m venv "%SCRIPT_DIR%.venv"
if !errorlevel! neq 0 (
    echo.
    echo  ERROR: Could not create virtual environment.
    echo  Make sure the project folder path contains no special characters.
    pause
    exit /b 1
)
echo  Done.
echo.

:: -- Dependency install / update -----------------------------------------------
:check_deps
:: Compare MD5 of requirements.txt with last-run hash
:: certutil is available on all Windows versions without extra installs
certutil -hashfile "%REQ_FILE%" MD5 2>nul | findstr /v "CertUtil\|hash" > "%TEMP%\forex_req_new.txt" 2>nul

set "DO_INSTALL=1"
if exist "%MARKER%" (
    fc /b "%MARKER%" "%TEMP%\forex_req_new.txt" >nul 2>&1
    if !errorlevel! == 0 (
        set "DO_INSTALL=0"
    )
)

if "!DO_INSTALL!"=="1" (
    echo  Installing / updating dependencies - please wait...
    echo  (This only runs when requirements change or on first launch.)
    echo.
    "%VENV_PYTHON%" -m pip install --quiet --upgrade pip
    "%VENV_PYTHON%" -m pip install --quiet --upgrade -r "%REQ_FILE%"
    if !errorlevel! neq 0 (
        echo.
        echo  ERROR: Failed to install dependencies.
        echo  Check your internet connection and try again.
        pause
        exit /b 1
    )
    copy /y "%TEMP%\forex_req_new.txt" "%MARKER%" >nul
    echo  Dependencies installed successfully.
    echo.
) else (
    echo  Dependencies are up to date.
)

:: -- Git check / portable install -------------------------------------------
:: Needed so the Settings > Update page's GitHub self-update panel, and the
:: git-checkout bootstrap that runs after every admin-console push (see
:: remote/client.py's _apply_update()), have a git binary to call. Installs
:: the official portable build (no installer, no admin rights, no registry
:: changes) into a user-writable folder and adds it to PATH for this session
:: -- later relaunches inherit the same PATH since restarts spawn from
:: within this same running process tree.
git --version >nul 2>&1
if !errorlevel! neq 0 (
    set "PORTABLE_GIT_DIR=%LOCALAPPDATA%\Programs\PortableGit"
    if exist "!PORTABLE_GIT_DIR!\bin\git.exe" (
        set "PATH=!PORTABLE_GIT_DIR!\bin;!PORTABLE_GIT_DIR!\cmd;%PATH%"
    ) else (
        echo  Git not found - downloading portable Git ^(one-time setup^)...
        set "_GIT_URL=https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.3/PortableGit-2.55.0.3-64-bit.7z.exe"
        set "_GIT_INST=%TEMP%\PortableGit-64-bit.7z.exe"
        powershell -NoProfile -Command ^
          "try { Invoke-WebRequest -Uri '!_GIT_URL!' -OutFile '!_GIT_INST!' -UseBasicParsing } catch { Write-Host ('Download error: ' + $_.Exception.Message); exit 1 }"
        if !errorlevel! == 0 if exist "!_GIT_INST!" (
            mkdir "!PORTABLE_GIT_DIR!" 2>nul
            "!_GIT_INST!" -y -o"!PORTABLE_GIT_DIR!" >nul
            del /f /q "!_GIT_INST!" 2>nul
            if exist "!PORTABLE_GIT_DIR!\bin\git.exe" (
                set "PATH=!PORTABLE_GIT_DIR!\bin;!PORTABLE_GIT_DIR!\cmd;%PATH%"
                echo  Git installed.
            ) else (
                echo  WARNING: Git install did not complete - Settings ^> Update
                echo  will say git is not installed. Re-run this script to retry.
            )
        ) else (
            echo  WARNING: Git download failed ^(no internet?^) - Settings ^> Update
            echo  will say git is not installed. Re-run this script to retry.
        )
    )
    echo.
)

:: -- Create user data directories ----------------------------------------------
if not exist "%USER_DATA%\data\sessions" mkdir "%USER_DATA%\data\sessions" 2>nul

:: -- Copy default config for new users -----------------------------------------
if not exist "%USER_DATA%\config.yaml" (
    if exist "%SCRIPT_DIR%config.yaml.example" (
        copy /y "%SCRIPT_DIR%config.yaml.example" "%USER_DATA%\config.yaml" >nul
        echo  Default config created. Enter your credentials in Settings after the app opens.
        echo.
    )
)

:: -- Launch MetaTrader 5 terminal if not already running -------------------------
:: Mirrors "Start MT5 Bridge.command" on macOS: check bridge_credentials.json
:: (written by the app once an MT5 terminal path is saved in Settings) and, if
:: MT5 isn't already running, launch it and give it a few seconds to initialise
:: before the bridge tries to attach.
set "CREDS_FILE=%USER_DATA%\bridge_credentials.json"
set "MT5_TERM_PATH="
if exist "%CREDS_FILE%" (
    for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command ^
        "try { (Get-Content -Raw '%CREDS_FILE%' | ConvertFrom-Json).terminal_path } catch {}"`) do (
        set "MT5_TERM_PATH=%%p"
    )
)

if defined MT5_TERM_PATH (
    if exist "!MT5_TERM_PATH!" (
        tasklist /fi "imagename eq terminal64.exe" 2>nul | findstr /i "terminal64.exe" >nul
        if !errorlevel! neq 0 (
            echo  Launching MetaTrader 5 terminal...
            start "" "!MT5_TERM_PATH!"
            echo  Waiting 10 seconds for MT5 to initialise...
            ping -n 11 127.0.0.1 >nul 2>&1
        ) else (
            echo  MetaTrader 5 is already running.
        )
    )
)
echo.

:: -- Launch ---------------------------------------------------------------------
echo  Starting FOREX Trader on http://localhost:8888
echo  Your browser will open automatically.
echo.
echo  Leave this window open while the app is running.
echo  To stop the app, close this window or press Ctrl+C.
echo.

:: First launch — open the browser automatically.
set "_FAILSTREAK=0"
for /f %%t in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "_LASTSTART=%%t"
"%VENV_PYTHON%" "%SCRIPT_DIR%run.py"
set "_EXIT=!errorlevel!"

:restart_check
:: Exit code 0   = user deliberately stopped the app (Power dialog "Stop") - respect it, don't relaunch.
:: Exit code 42  = clean restart requested (licence activated, update applied, etc.).
:: Any other non-zero exit is an unexpected crash — on an unattended VPS there is
:: nobody to see the "stopped" message or press a key at `pause`, so the app would
:: otherwise sit dead until someone happens to notice and manually relaunch it.
:: Relaunch on 42 or a crash, but track how many times it's died within 30s of
:: starting (a real crash loop) — after 5 such rapid failures in a row, stop
:: auto-relaunching and fall through to the pause prompt so it doesn't spin
:: silently forever; a single slow/normal run always resets the streak.
if "!_EXIT!"=="0" goto :stopped

for /f %%t in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "_NOW=%%t"
set /a "_UPTIME=_NOW-_LASTSTART"
if !_UPTIME! lss 30 (
    set /a "_FAILSTREAK+=1"
) else (
    set "_FAILSTREAK=0"
)

if !_FAILSTREAK! geq 5 (
    echo.
    echo  FOREX Trader crashed !_FAILSTREAK! times within 30 seconds of starting - stopping auto-restart.
    goto :stopped
)

echo  Restarting FOREX Trader...
echo.
for /f %%t in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "_LASTSTART=%%t"
"%VENV_PYTHON%" "%SCRIPT_DIR%run.py" --no-browser
set "_EXIT=!errorlevel!"
goto :restart_check

:stopped
:: Show message and wait so an interactive user can read any errors. On an
:: unattended VPS this just idles harmlessly - it only reaches here after
:: repeated rapid crashes, which by then needs a human to look at logs anyway.
echo.
echo  FOREX Trader has stopped.
popd
pause
