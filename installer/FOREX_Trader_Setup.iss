; FOREX Trader — Windows Installer Script
; Built with Inno Setup 6 (https://jrsoftware.org/isinfo.php)
;
; HOW TO BUILD:
;   1. Install Inno Setup 6 on Windows.
;   2. Open this file in Inno Setup Compiler.
;   3. Press F9 (or Build → Compile).
;   4. Installer .exe appears at the repo root (see OutputDir below).
;
; BEFORE BUILDING:
;   a. Adjust AppVersion and VersionInfoVersion below to match the release --
;      always bump both, even for a same-day rebuild (see [Code]'s
;      InitializeSetup: a same-numbered rebuild skips reinstalling on any
;      machine that already has that version).
;   That's it -- the embedded Python runtime, get-pip.py, and the Visual C++
;   Redistributable (lightgbm needs its runtime DLLs) are no longer bundled at
;   compile time; they're downloaded fresh during install (see [Code]'s
;   CurStepChanged) using PowerShell's Invoke-WebRequest/Expand-Archive, both
;   built into every Windows 10+ target this installer already requires. No
;   third-party download plugin, no local prerequisite files.

#define AppName      "FOREX Trader"
#define AppVersion   "6.11"
#define AppPublisher "FOREX Trader"
#define AppURL       "http://localhost:8888"
#define AppExeName   "Setup && Start FOREX.bat"

[Setup]
AppId                    = {{9B4E8C3A-2F71-4D8B-A6E1-3C9D5F7B2E4A}
AppName                  = {#AppName}
AppVersion               = {#AppVersion}
AppPublisher             = {#AppPublisher}
AppPublisherURL          = {#AppURL}
AppSupportURL            = {#AppURL}
AppUpdatesURL            = {#AppURL}
DefaultDirName           = {localappdata}\FOREX Trader
DefaultGroupName         = {#AppName}
AllowNoIcons             = yes
LicenseFile              =
; Install to per-user localappdata; firewall steps self-elevate via netsh.
PrivilegesRequired            = lowest
PrivilegesRequiredOverridesAllowed = commandline
OutputDir                = ..
OutputBaseFilename       = FOREX_Trader_Setup
Compression              = lzma2/ultra64
SolidCompression         = yes
WizardStyle              = modern
; WizardImageFile uses the built-in default (compiler: prefix fails under Wine)
UninstallDisplayIcon     = {app}\frontend\static\gold_bag.ico
ArchitecturesInstallIn64BitMode = x64compatible
MinVersion               = 10.0.17763
; Windows 10 1809+ required (needed for Python 3.11 + modern TLS)
VersionInfoVersion       = 1.6.0.0
VersionInfoCompany       = {#AppPublisher}
VersionInfoDescription   = {#AppName} Installer
SetupIconFile            = ..\frontend\static\gold_bag.ico
DisableProgramGroupPage  = yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Dirs]
; Ensure the user data directory tree exists (config.py also creates it, but
; having it here means the shortcuts and first-run paths are valid immediately).
; Must match config.py's _APP_DATA_FOLDER exactly (plain "ForexTrader" --
; the "-Refactor2" suffix was a leftover fork-isolation default, reverted
; now that this checkout is the only app, not a fork running alongside a
; separate original).
Name: "{userappdata}\ForexTrader\data\sessions"

[Files]
; ── Application source files ──────────────────────────────────────────────────
; Exclude Mac-only scripts, macOS-specific Wine setup, and build artefacts
; backend/ and frontend/ replaced forex_trader/ in the 2026 restructure.
; Packaging the old path made this script fail at COMPILE time (Inno errors
; on a [Files] entry that matches nothing), so the installer had been
; unbuildable since then -- see tests/refactor/test_installer_packages_the_real_tree.py.
; Build caches and Finder files are excluded: the .exe is built from a working
; tree, and a file no commit has stops the install linking to GitHub (the v6.11
; VPS said "Not linked" over one frontend/tsconfig.tsbuildinfo, 2026-09-25).
Source: "..\backend\*";           DestDir: "{app}\backend";           Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__,*.pyc,*.pyo,*.tsbuildinfo,.DS_Store"
; node_modules is developer-only (160+ MB); the app serves the committed dist/.
Source: "..\frontend\*";          DestDir: "{app}\frontend";          Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__,*.pyc,*.pyo,node_modules,*.tsbuildinfo,.DS_Store"
Source: "..\run.py";               DestDir: "{app}";                    Flags: ignoreversion
Source: "..\mt5_bridge.py";        DestDir: "{app}";                    Flags: ignoreversion
Source: "..\requirements.txt";     DestDir: "{app}";                    Flags: ignoreversion
Source: "..\pyproject.toml";       DestDir: "{app}";                    Flags: ignoreversion
Source: "..\config.yaml.example";  DestDir: "{app}";                    Flags: ignoreversion
Source: "..\Setup & Start FOREX.bat"; DestDir: "{app}";                Flags: ignoreversion
Source: "..\Stop FOREX.bat";       DestDir: "{app}";                    Flags: ignoreversion
; The in-app updater reads both of these at runtime; without them the
; Update tab reports no version and an empty changelog.
; The repo's own ignore rules, so linking to GitHub on the target ignores what
; git ignores (core_app_update.link_checkout).
Source: "..\.gitignore";           DestDir: "{app}";                    Flags: ignoreversion
Source: "..\VERSION";              DestDir: "{app}";                    Flags: ignoreversion
Source: "..\CHANGELOG.md";         DestDir: "{app}";                    Flags: ignoreversion
; Read by the running app from the install directory (2026-09-23):
; the EA the Install button copies into MetaTrader (ea_deploy.py), and the
; script the keep-alive Scheduled Task runs (core_autostart.watchdog_script).
Source: "..\mql5\*";               DestDir: "{app}\mql5";               Flags: ignoreversion
Source: "..\tools\watchdog.py";    DestDir: "{app}\tools";              Flags: ignoreversion

; ── install_deps.py only -- the embedded Python runtime + get-pip.py are no
; longer bundled here; CurStepChanged (below) downloads both fresh into
; {app}\python_embed at install time instead.
Source: "install_deps.py"; DestDir: "{app}\installer";  Flags: ignoreversion

[Icons]
Name: "{group}\FOREX Trader";          Filename: "{app}\Setup & Start FOREX.bat"; WorkingDir: "{app}"; IconFilename: "{app}\frontend\static\gold_bag.ico"
Name: "{group}\Stop FOREX Trader";     Filename: "{app}\Stop FOREX.bat";           WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\FOREX Trader";    Filename: "{app}\Setup & Start FOREX.bat"; WorkingDir: "{app}"; Tasks: desktopicon; IconFilename: "{app}\frontend\static\gold_bag.ico"

[Run]
; ── Step 1: Bootstrap pip into the embedded Python ────────────────────────────
Filename: "{app}\python_embed\python.exe"; \
    Parameters: "{app}\python_embed\get-pip.py --no-warn-script-location"; \
    WorkingDir: "{app}"; \
    StatusMsg: "Bootstrapping pip..."; \
    Flags: runhidden waituntilterminated

; ── Step 2: Install virtualenv into the embedded Python ───────────────────────
Filename: "{app}\python_embed\python.exe"; \
    Parameters: "-m pip install --quiet virtualenv"; \
    WorkingDir: "{app}"; \
    StatusMsg: "Installing virtualenv..."; \
    Flags: runhidden waituntilterminated

; ── Step 3: Create the app virtual environment and install all packages ────────
; install_deps.py handles venv creation, pip upgrade, and requirements install.
Filename: "{app}\python_embed\python.exe"; \
    Parameters: "{app}\installer\install_deps.py ""{app}"""; \
    WorkingDir: "{app}"; \
    StatusMsg: "Installing FOREX Trader dependencies (this may take a few minutes)..."; \
    Flags: runhidden waituntilterminated

; ── Step 4: Add Windows Firewall rules (admin context) ────────────────────────
; Port 8888 — the dashboard (browser access)
Filename: "netsh"; \
    Parameters: "advfirewall firewall add rule name=""FOREX Trader UI (port 8888)"" dir=in action=allow protocol=TCP localport=8888 profile=private"; \
    StatusMsg: "Adding firewall rule for port 8888..."; \
    Flags: runhidden waituntilterminated

; Port 9000 — MT5 Bridge (local-only, restrict to 127.0.0.1)
Filename: "netsh"; \
    Parameters: "advfirewall firewall add rule name=""FOREX Trader Bridge (port 9000)"" dir=in action=allow protocol=TCP localport=9000 localip=127.0.0.1 profile=private"; \
    StatusMsg: "Adding firewall rule for port 9000..."; \
    Flags: runhidden waituntilterminated

; Port 8765 (the Remote-node sync server) is deliberately NOT opened here: most
; Windows installs are someone's main PC, not a VPS, and should accept nothing
; inbound. Settings > Remote node > "Make this node a VPS" opens it, and the
; uninstaller below removes it.

; ── Step 5: Open the app after install (optional) ─────────────────────────────
Filename: "{app}\Setup & Start FOREX.bat"; \
    Description: "Launch FOREX Trader now"; \
    Flags: postinstall shellexec skipifsilent

[UninstallRun]
; Remove the firewall rules on uninstall
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""FOREX Trader UI (port 8888)""";    Flags: runhidden; RunOnceId: "DelFW8888"
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""FOREX Trader Bridge (port 9000)"""; Flags: runhidden; RunOnceId: "DelFW9000"
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""FOREX Trader Sync (port 8765)""";   Flags: runhidden; RunOnceId: "DelFW8765"

[Code]

// Reads the version written to installed_version.txt on the previous install.
// Returns empty string if not installed.
function GetInstalledVersion(): String;
var
  Lines: TArrayOfString;
  VersionFile: String;
begin
  Result := '';
  VersionFile := ExpandConstant('{localappdata}\FOREX Trader\installed_version.txt');
  if FileExists(VersionFile) then
    if LoadStringsFromFile(VersionFile, Lines) then
      if GetArrayLength(Lines) > 0 then
        Result := Trim(Lines[0]);
end;

// Returns True if MetaTrader 5 terminal64.exe is registered in the system.
function MetaTraderInstalled(): Boolean;
var
  RegValue: String;
begin
  Result := RegQueryStringValue(HKEY_LOCAL_MACHINE,
    'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\terminal64.exe',
    '', RegValue);
  if not Result then
    Result := RegQueryStringValue(HKEY_CURRENT_USER,
      'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\terminal64.exe',
      '', RegValue);
end;

// Smart launch: if this exact version is already installed, skip the wizard
// and launch the app directly.  Users keep one .exe and double-click it every
// time -- first run installs, subsequent runs launch, new version upgrades.
function InitializeSetup(): Boolean;
var
  AppPath: String;
  InstalledVersion: String;
  ErrorCode: Integer;
begin
  Result := True;
  AppPath := ExpandConstant('{localappdata}\FOREX Trader');
  InstalledVersion := GetInstalledVersion();

  // Same version already installed -- skip wizard and launch immediately
  if (InstalledVersion = '{#AppVersion}') and
     FileExists(AppPath + '\.venv\Scripts\python.exe') then
  begin
    ShellExec('open', AppPath + '\Setup & Start FOREX.bat', '',
              AppPath, SW_SHOWNORMAL, ewNoWait, ErrorCode);
    Result := False; // Abort installer; app is now launching
    Exit;
  end;

  // Not installed or new version -- warn if MetaTrader 5 is missing
  if not MetaTraderInstalled() then
  begin
    if MsgBox(
      'MetaTrader 5 does not appear to be installed.' + #13#10 + #13#10 +
      'FOREX Trader requires MetaTrader 5 (Vantage Markets edition) to be ' +
      'installed and logged in before the bridge can connect.' + #13#10 + #13#10 +
      'You can install MetaTrader 5 later and the app will still work, but ' +
      'trading will not function until MT5 is set up.' + #13#10 + #13#10 +
      'Continue with the FOREX Trader installation?',
      mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;

// Runs a PowerShell -Command snippet hidden, waits for it to finish, and
// returns True on a clean exit. TLS 1.2 is forced explicitly since some
// Windows 10 builds don't negotiate it by default, which would otherwise
// fail silently against python.org/bootstrap.pypa.io.
function RunPowerShell(const Cmd: String): Boolean;
var
  ResultCode: Integer;
  FullCmd: String;
begin
  FullCmd := '-NoProfile -ExecutionPolicy Bypass -Command ' +
    '"[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; ' + Cmd + '"';
  Result := Exec('powershell.exe', FullCmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
            and (ResultCode = 0);
end;

// Downloads the Microsoft Visual C++ Redistributable (x64) and installs it
// silently. lightgbm's compiled lib_lightgbm.dll links against the MSVC
// runtime (vcruntime140.dll, msvcp140.dll, vcruntime140_1.dll); a bare/minimal
// Windows image -- Server Core, a fresh VPS, a Windows install that skipped
// optional updates -- does not have these, and the DLL load then fails with
// "Could not find module ... (or one of its dependencies)", which crash-loops
// the app before it can render the traceback usefully (bugs, 2026-09-25: a
// fresh VPS install died 5x in 30s on this exact error before auto-restart
// gave up). The Windows 10 SDK does not ship it either -- only the
// redistributable does. aka.ms/vs/17/release/vc_redist.x64.exe is Microsoft's
// own stable, versionless redirect for the latest VC++ redist; used the same
// way FetchPythonEmbed uses python.org/bootstrap.pypa.io below.
function FetchAndInstallVCRedist(): Boolean;
var
  ExePath: String;
  ResultCode: Integer;
begin
  ExePath := ExpandConstant('{app}\vc_redist.x64.exe');

  WizardForm.StatusLabel.Caption := 'Downloading Visual C++ Redistributable...';
  WizardForm.Update;
  RunPowerShell(
    'Invoke-WebRequest -Uri ''https://aka.ms/vs/17/release/vc_redist.x64.exe'' ' +
    '-OutFile ''' + ExePath + ''' -UseBasicParsing'
  );

  if not FileExists(ExePath) then
  begin
    Result := False;
    Exit;
  end;

  WizardForm.StatusLabel.Caption := 'Installing Visual C++ Redistributable...';
  WizardForm.Update;
  // /install /quiet /norestart: silent, no forced reboot. Exit codes 0 (fresh
  // install), 1638 (equal/newer already present) and 3010 (installed, reboot
  // wanted but not required for the DLL to load) all mean lightgbm's runtime
  // deps are satisfied; anything else is a real failure.
  Result := Exec(ExePath, '/install /quiet /norestart', '', SW_HIDE,
                  ewWaitUntilTerminated, ResultCode);
  if Result then
    Result := (ResultCode = 0) or (ResultCode = 1638) or (ResultCode = 3010);

  DeleteFile(ExePath);
end;

// Downloads the Python 3.11.9 embeddable runtime + get-pip.py fresh at
// install time (2026-07-24 -- previously bundled via [Files], which meant
// every builder needed to manually pre-stage installer\python_embed\ before
// compiling; see the file header). Uses only PowerShell's Invoke-WebRequest/
// Expand-Archive -- both ship with every Windows 10+ target this installer
// already requires (MinVersion above), so no third-party download plugin is
// needed. Returns True if both the runtime and get-pip.py end up in place.
function FetchPythonEmbed(): Boolean;
var
  ZipPath, EmbedDir: String;
begin
  EmbedDir := ExpandConstant('{app}\python_embed');
  ZipPath  := ExpandConstant('{app}\python_embed.zip');
  ForceDirectories(EmbedDir);

  WizardForm.StatusLabel.Caption := 'Downloading Python runtime...';
  WizardForm.Update;
  RunPowerShell(
    'Invoke-WebRequest -Uri ''https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip'' ' +
    '-OutFile ''' + ZipPath + ''' -UseBasicParsing'
  );

  if not FileExists(ZipPath) then
  begin
    Result := False;
    Exit;
  end;

  WizardForm.StatusLabel.Caption := 'Extracting Python runtime...';
  WizardForm.Update;
  RunPowerShell(
    'Expand-Archive -Path ''' + ZipPath + ''' -DestinationPath ''' + EmbedDir + ''' -Force'
  );
  DeleteFile(ZipPath);

  WizardForm.StatusLabel.Caption := 'Downloading pip bootstrap...';
  WizardForm.Update;
  RunPowerShell(
    'Invoke-WebRequest -Uri ''https://bootstrap.pypa.io/get-pip.py'' ' +
    '-OutFile ''' + EmbedDir + '\get-pip.py'' -UseBasicParsing'
  );

  Result := FileExists(EmbedDir + '\python.exe') and FileExists(EmbedDir + '\get-pip.py');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Must run before install_deps.py's pip install ([Run], below) -- lightgbm
    // (a hard dependency, imported at app startup by the breakout/reversal
    // engines) needs the MSVC runtime this installs to load its DLL. Warn but
    // don't abort the whole install on failure: a network hiccup here
    // shouldn't block someone who already has the redist from a prior VS/
    // driver install, and the app tells the user plainly if lightgbm still
    // can't load.
    if not FetchAndInstallVCRedist() then
      MsgBox(
        'Could not install the Visual C++ Redistributable needed by one of ' +
        'FOREX Trader''s dependencies (lightgbm).' + #13#10 + #13#10 +
        'Setup will continue, but the app may fail to start with a ' +
        '"lib_lightgbm.dll" error. If that happens, install the redistributable ' +
        'manually from https://aka.ms/vs/17/release/vc_redist.x64.exe and try again.',
        mbError, MB_OK
      );

    if not FetchPythonEmbed() then
    begin
      MsgBox(
        'Could not download the Python runtime needed to finish setup.' + #13#10 + #13#10 +
        'This requires an internet connection to python.org and bootstrap.pypa.io. ' +
        'Check your connection and re-run this installer.',
        mbError, MB_OK
      );
      Exit;
    end;

    // Patch embedded Python ._pth to allow full site-packages access
    if FileExists(ExpandConstant('{app}\python_embed\python311._pth')) then
      SaveStringToFile(
        ExpandConstant('{app}\python_embed\python311._pth'),
        'python311.zip' + #13#10 + '.' + #13#10 + #13#10 + 'import site' + #13#10,
        False
      );

    // The first start after this install opens the dashboard even on a VPS,
    // where every other start skips it (run.py's _should_open_browser).
    SaveStringToFile(ExpandConstant('{app}\open_browser_once'), '', False);

    // Write version marker so smart-launch detects this version on next run
    SaveStringToFile(
      ExpandConstant('{app}\installed_version.txt'),
      '{#AppVersion}',
      False
    );
  end;
end;

procedure DeinitializeSetup();
begin
end;
