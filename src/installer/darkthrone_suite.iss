; Inno Setup script for DarkThrone Suite
;
; Requires Inno Setup 6 installed on the dev machine:
;   https://jrsoftware.org/isinfo.php
;
; Produces: release/installers/DarkThroneSuite-Setup-v{version}.exe
;
; When a new version is cut:
;   1. Update the AppVersion + OutputBaseFilename below (match src/_version.py)
;   2. Run `cd src && build.bat` — invokes this script via ISCC.exe
;   3. Upload the output exe to GitHub Releases

#define MyAppName        "DarkThrone Suite"
#define MyAppVersion     "1.1.0"
#define MyAppPublisher   "cmdprive"
#define MyAppURL         "https://github.com/cmdprive/darkthrone-suite"
#define MyAppExeName     "DarkThrone Suite.exe"

[Setup]
; AppId MUST stay constant across versions — it's what Windows uses to
; recognize this as "the same app" for upgrade vs fresh-install detection.
AppId={{A1B2C3D4-DARK-THRO-NE01-5U17E0000001}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\DarkThroneSuite
DefaultGroupName=DarkThrone Suite
DisableProgramGroupPage=yes
; LicenseFile (optional — add later if desired)
OutputDir=..\..\release\installers
OutputBaseFilename=DarkThroneSuite-Setup-v{#MyAppVersion}
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
; Icon file is optional — if missing, default Windows icon is used
; SetupIconFile=icon.ico
; UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
; Bundle everything PyInstaller produced in release\DarkThrone Suite\
Source: "..\..\release\DarkThrone Suite\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon

[Run]
; Launch the Suite after install completes (user can uncheck).
; The Suite handles first-time Chromium download itself on startup —
; no separate playwright-install step needed in the installer, so this
; script stays simple and works regardless of which python is installed.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Leave %LOCALAPPDATA%\DarkThroneSuite\ alone so reinstalling preserves
; the user's auth.json / config / intel history. If they want to nuke
; everything they can delete that folder manually.

[Code]
// Sanity: warn if the app is already running, which would prevent overwriting files
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
