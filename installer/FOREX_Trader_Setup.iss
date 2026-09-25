; FOREX Trader — Windows installer (a bootstrapper)
; Built with Inno Setup 6 (https://jrsoftware.org/isinfo.php)
;
; This .exe carries NO app files. It installs the Visual C++ runtime, puts
; portable Git where "Setup & Start FOREX.bat" looks for it, and pulls the
; current app from GitHub (MooreSi/forex-gold, branch main) into the install
; folder. It then runs "Setup & Start FOREX.bat", which installs Python if
; needed, builds the venv, installs requirements.txt, and starts the app.
;
; So an app change NEVER needs a new .exe (owner, 2026-09-25: "i dont want to
; have to keep on compiling the iss and reshipping it"). Every install is a
; git checkout from its first second, so Settings > Update and the admin
; console see its commit without any matching. Recompile only when THIS file
; changes, and bump InstallerVersion when you do.
;
; HOW TO BUILD: open this file in Inno Setup Compiler and press F9. The .exe
; appears at the repo root (OutputDir below).

#define AppName          "FOREX Trader"
; The installer's own version, deliberately NOT the app's (VERSION): the app
; version moves with every release and this file should not.
#define InstallerVersion "2.0"
#define AppPublisher     "FOREX Trader"
#define AppURL           "http://localhost:8888"

; Where the app comes from. Must match core_app_update._GITHUB_REPO_URL and
; _BRANCH, which the in-app updater pulls from (pinned by
; tests/refactor/test_installer_is_a_bootstrapper.py).
#define RepoUrl          "https://github.com/MooreSi/forex-gold.git"
#define Branch           "main"
; The same portable Git build "Setup & Start FOREX.bat" downloads, into the
; same folder, so the app finds the git this installer used.
#define PortableGitUrl   "https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.3/PortableGit-2.55.0.3-64-bit.7z.exe"

[Setup]
AppId                    = {{9B4E8C3A-2F71-4D8B-A6E1-3C9D5F7B2E4A}
AppName                  = {#AppName}
AppVersion               = {#InstallerVersion}
AppVerName               = {#AppName}
AppPublisher             = {#AppPublisher}
AppPublisherURL          = {#AppURL}
AppSupportURL            = {#AppURL}
AppUpdatesURL            = {#AppURL}
DefaultDirName           = {localappdata}\FOREX Trader
; Always this folder: the uninstaller deletes {app} whole (the app files come
; from git, so it cannot list them), which must never be a folder the user
; picked, such as Documents.
DisableDirPage           = yes
UsePreviousAppDir        = no
DefaultGroupName         = {#AppName}
AllowNoIcons             = yes
LicenseFile              =
; Per-user install; the firewall steps need admin and fail quietly without it.
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
VersionInfoVersion       = 2.0.0.0
VersionInfoCompany       = {#AppPublisher}
VersionInfoDescription   = {#AppName} Installer
SetupIconFile            = ..\frontend\static\gold_bag.ico
DisableProgramGroupPage  = yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Dirs]
; Must match config.py's _APP_DATA_FOLDER exactly (plain "ForexTrader").
Name: "{userappdata}\ForexTrader\data\sessions"

[Icons]
Name: "{group}\FOREX Trader";          Filename: "{app}\Setup & Start FOREX.bat"; WorkingDir: "{app}"; IconFilename: "{app}\frontend\static\gold_bag.ico"
Name: "{group}\Stop FOREX Trader";     Filename: "{app}\Stop FOREX.bat";           WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\FOREX Trader";    Filename: "{app}\Setup & Start FOREX.bat"; WorkingDir: "{app}"; Tasks: desktopicon; IconFilename: "{app}\frontend\static\gold_bag.ico"

[Run]
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

; The launcher does the rest on its first run: Python, the venv,
; requirements.txt, then the app.
Filename: "{app}\Setup & Start FOREX.bat"; \
    Description: "Launch FOREX Trader now"; \
    Flags: postinstall shellexec skipifsilent

[UninstallRun]
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""FOREX Trader UI (port 8888)""";    Flags: runhidden; RunOnceId: "DelFW8888"
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""FOREX Trader Bridge (port 9000)"""; Flags: runhidden; RunOnceId: "DelFW9000"
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""FOREX Trader Sync (port 8765)""";   Flags: runhidden; RunOnceId: "DelFW8765"

[UninstallDelete]
; The app is a git checkout plus its venv, none of it installed from [Files],
; so the uninstaller must remove the folder itself. Settings, databases and
; logs live in %APPDATA%\ForexTrader and are untouched.
Type: filesandordirs; Name: "{app}"

[Code]

var
  ProgressPage: TOutputProgressWizardPage;
  Bootstrapped: Boolean;

function AppDir(): String;
begin
  Result := WizardDirValue();
end;

function GitDir(): String;
begin
  Result := ExpandConstant('{localappdata}\Programs\PortableGit');
end;

function GitExe(): String;
begin
  Result := GitDir() + '\cmd\git.exe';
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

// Smart launch: a machine that already has the app as a git checkout with a
// venv is simply started. Updates reach it through Settings > Update (git
// pull), not through this .exe, so there is nothing for a re-run to install.
// To reinstall from scratch, uninstall first.
function InitializeSetup(): Boolean;
var
  AppPath: String;
  ErrorCode: Integer;
begin
  Result := True;
  AppPath := ExpandConstant('{localappdata}\FOREX Trader');

  if DirExists(AppPath + '\.git') and
     FileExists(AppPath + '\Setup & Start FOREX.bat') and
     FileExists(AppPath + '\.venv\Scripts\python.exe') then
  begin
    ShellExec('open', AppPath + '\Setup & Start FOREX.bat', '',
              AppPath, SW_SHOWNORMAL, ewNoWait, ErrorCode);
    Result := False; // Abort installer; app is now launching
    Exit;
  end;

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

procedure InitializeWizard();
begin
  ProgressPage := CreateOutputProgressPage('Downloading FOREX Trader',
    'Fetching the app and what it needs from the internet.');
  Bootstrapped := False;
end;

// Runs a PowerShell -Command snippet hidden and returns True on a clean exit.
// TLS 1.2 is forced: some Windows 10 builds do not negotiate it by default.
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

function Download(const Url, Dest: String): Boolean;
begin
  RunPowerShell('Invoke-WebRequest -Uri ''' + Url + ''' -OutFile ''' + Dest + ''' -UseBasicParsing');
  Result := FileExists(Dest);
end;

// lightgbm's lib_lightgbm.dll links the MSVC runtime, which a bare Windows
// image (a fresh VPS) lacks; the app then crash-looped at startup (bugs/066).
// aka.ms/vs/17/release/vc_redist.x64.exe is Microsoft's stable redirect.
// Exit codes 0 (installed), 1638 (newer present) and 3010 (reboot wanted, not
// needed for the DLL to load) are all success.
function InstallVCRedist(): Boolean;
var
  ExePath: String;
  ResultCode: Integer;
begin
  ExePath := ExpandConstant('{tmp}\vc_redist.x64.exe');
  Result := Download('https://aka.ms/vs/17/release/vc_redist.x64.exe', ExePath);
  if not Result then Exit;
  Result := Exec(ExePath, '/install /quiet /norestart', '', SW_HIDE,
                 ewWaitUntilTerminated, ResultCode)
            and ((ResultCode = 0) or (ResultCode = 1638) or (ResultCode = 3010));
end;

function EnsureGit(): Boolean;
var
  Archive: String;
  ResultCode: Integer;
begin
  Result := FileExists(GitExe());
  if Result then Exit;
  Archive := ExpandConstant('{tmp}\PortableGit.7z.exe');
  if not Download('{#PortableGitUrl}', Archive) then Exit;
  ForceDirectories(GitDir());
  Exec(Archive, '-y -o"' + GitDir() + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := FileExists(GitExe());
end;

function Git(const Args: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(GitExe(), Args, AppDir(), SW_HIDE, ewWaitUntilTerminated, ResultCode)
            and (ResultCode = 0);
end;

// The app, as a checkout of origin/main. Also turns an older copied install
// (the pre-2.0 installer shipped files, no .git) into a checkout in place:
// tracked files are replaced, the venv and anything untracked are kept.
// --depth=1: the full history is ~45 MB and nothing on a client reads it;
// later `git fetch`es from Settings > Update deepen it as needed.
function FetchApp(): Boolean;
begin
  ForceDirectories(AppDir());
  Result := DirExists(AppDir() + '\.git') or Git('init -q');
  if not Result then Exit;
  if not Git('remote set-url origin {#RepoUrl}') then
    if not Git('remote add origin {#RepoUrl}') then begin Result := False; Exit; end;
  Result := Git('fetch --depth=1 origin {#Branch}')
            and Git('checkout -f -B {#Branch} --track origin/{#Branch}')
            and FileExists(AppDir() + '\Setup & Start FOREX.bat');
end;

// Runs when the user presses Install, before anything is written. A failure
// keeps the wizard on this page with the reason, so nothing is half-installed
// and the user can retry once the connection is back.
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID <> wpReady) or Bootstrapped then Exit;

  ProgressPage.Show;
  try
    ProgressPage.SetText('Installing the Visual C++ runtime...', '');
    ProgressPage.SetProgress(1, 4);
    if not InstallVCRedist() then
      MsgBox('Could not install the Visual C++ Redistributable, which the app''s ' +
        'LightGBM models need.' + #13#10 + #13#10 + 'Setup will continue. If the ' +
        'app then fails to start with a "lib_lightgbm.dll" error, install it from ' +
        'https://aka.ms/vs/17/release/vc_redist.x64.exe.', mbError, MB_OK);

    ProgressPage.SetText('Getting Git...', '');
    ProgressPage.SetProgress(2, 4);
    if not EnsureGit() then
    begin
      MsgBox('Could not download Git from github.com, which setup uses to fetch ' +
        'the app. Check the internet connection and press Install again.',
        mbError, MB_OK);
      Result := False;
      Exit;
    end;

    ProgressPage.SetText('Downloading FOREX Trader from GitHub...', '{#RepoUrl}');
    ProgressPage.SetProgress(3, 4);
    if not FetchApp() then
    begin
      MsgBox('Could not download FOREX Trader from GitHub ({#RepoUrl}).' + #13#10 + #13#10 +
        'Check the internet connection and press Install again.',
        mbError, MB_OK);
      Result := False;
      Exit;
    end;

    // The first start after this install opens the dashboard even on a VPS,
    // where every other start skips it (run.py's _should_open_browser).
    SaveStringToFile(AppDir() + '\open_browser_once', '', False);
    ProgressPage.SetProgress(4, 4);
    Bootstrapped := True;
  finally
    ProgressPage.Hide;
  end;
end;
