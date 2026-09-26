; Pocket TTS SAPI5 installer
;
; Expects these to exist before compiling (see build_installer.bat):
;   output\PocketTTSSAPI.dll, output\x64\PocketTTSSAPI.dll,
;   output\PocketTTSVoiceManager.exe        (build_all.bat)
;   runtime\**                              (installer\prepare_runtime.ps1)
;   installer\staging\models\**, installer\staging\voices\**
;                                           (installer\prepare_voices.py)

#define MyAppName "Pocket TTS SAPI5"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "Josh Kennedy"

[Setup]
AppId={{B4A7C2F1-9E63-4C58-A1D0-7F2A45E8B9D3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\PocketTTS SAPI5
DisableProgramGroupPage=yes
OutputDir=..\output
OutputBaseFilename=PocketTTS_SAPI5_Setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern
UninstallDisplayName={#MyAppName}
VersionInfoVersion={#MyAppVersion}
SetupLogging=yes
; A .pttsvoices association is added, so Explorer is told to reread it.
ChangesAssociations=yes

[Tasks]
Name: "desktopicon"; Description: "Put the &Voice Manager on the desktop"
Name: "startupengine"; Description: "Start the Pocket TTS &engine automatically at sign-in (recommended: speech starts instantly)"

[Files]
Source: "..\output\PocketTTSSAPI.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\output\x64\PocketTTSSAPI.dll"; DestDir: "{app}\x64"; Flags: ignoreversion
Source: "..\output\PocketTTSVoiceManager.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\host\pockettts_host.py"; DestDir: "{app}\host"; Flags: ignoreversion
Source: "..\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs
; AI model (large). Overwritten on upgrade; the Voice Manager can update it later.
Source: "staging\models\*"; DestDir: "{commonappdata}\PocketTTS\models"; Flags: ignoreversion recursesubdirs
; Default voices. The voice list itself is only written on a fresh install so
; upgrades never clobber the user's cloned voices.
Source: "staging\voices\*"; DestDir: "{commonappdata}\PocketTTS\voices"; Excludes: "voices.ini"; Flags: ignoreversion recursesubdirs
Source: "staging\voices\voices.ini"; DestDir: "{commonappdata}\PocketTTS\voices"; Flags: onlyifdoesntexist uninsneveruninstall

[Registry]
; Voice packages open in the Voice Manager, so a shared file can simply be
; double-clicked. PrivilegesRequired=admin makes HKA resolve to HKLM.
Root: HKA; Subkey: "Software\Classes\.pttsvoices"; ValueType: string; ValueName: ""; ValueData: "PocketTTS.VoicePackage"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\PocketTTS.VoicePackage"; ValueType: string; ValueName: ""; ValueData: "Pocket TTS voice package"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\PocketTTS.VoicePackage\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\PocketTTSVoiceManager.exe"" ""%1"""

[Dirs]
; Voice cloning and model updates run as the signed-in user.
Name: "{commonappdata}\PocketTTS"; Permissions: users-modify
Name: "{commonappdata}\PocketTTS\models"; Permissions: users-modify
Name: "{commonappdata}\PocketTTS\voices"; Permissions: users-modify

[Icons]
Name: "{autoprograms}\Pocket TTS Voice Manager"; Filename: "{app}\PocketTTSVoiceManager.exe"
Name: "{autodesktop}\Pocket TTS Voice Manager"; Filename: "{app}\PocketTTSVoiceManager.exe"; Tasks: desktopicon
Name: "{commonstartup}\Pocket TTS Engine"; Filename: "{app}\runtime\PocketTTSHost.exe"; Parameters: """{app}\host\pockettts_host.py"""; Tasks: startupengine

[Run]
Filename: "{sys}\regsvr32.exe"; Parameters: "/s ""{app}\x64\PocketTTSSAPI.dll"""; StatusMsg: "Registering the 64-bit SAPI5 voice..."; Flags: runhidden
Filename: "{syswow64}\regsvr32.exe"; Parameters: "/s ""{app}\PocketTTSSAPI.dll"""; StatusMsg: "Registering the 32-bit SAPI5 voice..."; Flags: runhidden
Filename: "{app}\runtime\PocketTTSHost.exe"; Parameters: """{app}\host\pockettts_host.py"""; StatusMsg: "Starting the Pocket TTS engine..."; Flags: runasoriginaluser nowait
Filename: "{app}\PocketTTSVoiceManager.exe"; Description: "Open the Voice Manager now"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c taskkill /f /im PocketTTSHost.exe"; Flags: runhidden; RunOnceId: "KillHost"
Filename: "{cmd}"; Parameters: "/c taskkill /f /im PocketTTSVoiceManager.exe"; Flags: runhidden; RunOnceId: "KillManager"
Filename: "{sys}\regsvr32.exe"; Parameters: "/s /u ""{app}\x64\PocketTTSSAPI.dll"""; Flags: runhidden; RunOnceId: "UnregX64"
Filename: "{syswow64}\regsvr32.exe"; Parameters: "/s /u ""{app}\PocketTTSSAPI.dll"""; Flags: runhidden; RunOnceId: "UnregX86"

[UninstallDelete]
; The model is large and re-downloadable; user-cloned voices are kept.
Type: filesandordirs; Name: "{commonappdata}\PocketTTS\models"

[Code]
{ The engine host keeps the bundled Python runtime open and the Voice
  Manager keeps its own binary open, so both are stopped before any file
  is replaced. The host restarts on demand. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  Exec(ExpandConstant('{cmd}'), '/c taskkill /f /im PocketTTSHost.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{cmd}'), '/c taskkill /f /im PocketTTSVoiceManager.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1500);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    Sleep(1500); { give clients a moment to drop the DLLs }
end;

{ Keep the detailed setup log next to the engine logs so support requests
  can include everything from one folder. }
procedure DeinitializeSetup();
var
  LogPath, DestDir: string;
begin
  LogPath := ExpandConstant('{log}');
  if LogPath <> '' then
  begin
    DestDir := ExpandConstant('{commonappdata}\PocketTTS\logs');
    if ForceDirectories(DestDir) then
      CopyFile(LogPath, DestDir + '\setup.log', False);
  end;
end;
