; cADRe Windows installer (Inno Setup 6). Built by scripts\build_release.py, which passes:
;   /DAppVersion=0.1.0  /DStage=<build\windows\stage>  /DOutDir=<dist>  /DIcon=<cadre\static\tray.ico>
;
; Per-user, no admin rights: everything goes to %LOCALAPPDATA%\cADRe, the same folder
; `cadre setup` uses, so a release and a checkout install share one layout:
;   python\    embeddable Python + psutil (cADRe's own interpreter, see cadre.paths)
;   release\   cadre_release.py + the cadre package of this release
;   app\       written by `cadre setup` (run by cadre_release.py install)
; Uninstall removes the hooks and the autostart entry first, then the files. The event log
; (~\.cadre) is kept.

#ifndef AppVersion
  #error AppVersion is required (build with scripts\build_release.py)
#endif

[Setup]
AppId={{6F3B2C1D-8A4E-4F5B-9C7D-2E1A3B4C5D6E}
AppName=cADRe
AppVersion={#AppVersion}
AppVerName=cADRe {#AppVersion}
AppPublisher=cADRe
AppComments=Agentic Detection & Response: which AI coding agents run on this machine, where, doing what.
DefaultDirName={localappdata}\cADRe
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
PrivilegesRequired=lowest
UsedUserAreasWarning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename=cADRe-{#AppVersion}-windows-x64-setup
SetupIconFile={#Icon}
UninstallDisplayIcon={app}\release\cadre\static\tray.ico
UninstallDisplayName=cADRe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; our own "stop" step below ends the dashboard and the tray icon before files are replaced
CloseApplications=no

[Files]
Source: "{#Stage}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#Stage}\release\*"; DestDir: "{app}\release"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{userprograms}\cADRe"; Filename: "{app}\python\pythonw.exe"; \
  Parameters: """{app}\release\cadre_release.py"" open"; WorkingDir: "{app}"; \
  IconFilename: "{app}\release\cadre\static\tray.ico"; Comment: "Open the cADRe dashboard"

[Run]
Filename: "http://127.0.0.1:8765/"; Description: "Open the cADRe dashboard"; \
  Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "{app}\python\python.exe"; Parameters: """{app}\release\cadre_release.py"" uninstall"; \
  WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "cadre-uninstall"

[UninstallDelete]
; files `cadre setup` and Python wrote at run time (the installer only tracks its own)
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\release"
Type: filesandordirs; Name: "{app}\python"

[Code]
function Bootstrap(Cmd: String; var ResultCode: Integer): Boolean;
var
  Py, Script: String;
begin
  Py := ExpandConstant('{app}\python\python.exe');
  Script := ExpandConstant('{app}\release\cadre_release.py');
  Result := FileExists(Py) and FileExists(Script) and
    Exec(Py, '"' + Script + '" ' + Cmd, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  // an earlier release may be running from the python folder: stop it so its files can be replaced
  Bootstrap('stop', Code);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := 'Installing hooks and starting cADRe...';
    if not Bootstrap('install', Code) or (Code <> 0) then
      SuppressibleMsgBox('cADRe was copied, but its setup step reported a problem.' + #13#10 +
        'Details: ' + ExpandConstant('{app}\release\setup.log'), mbError, MB_OK, IDOK);
  end;
end;
