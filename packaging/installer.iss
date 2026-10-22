#define AppVersion "1.0.0"
[Setup]
AppId={{11F732A0-5B04-49A0-8A6D-C23A72B00F50}
AppName=Codex Quota Widget
AppVersion={#AppVersion}
AppPublisher=HrrToT
AppPublisherURL=https://github.com/HrrToT/codex-quota-widget
AppSupportURL=https://github.com/HrrToT/codex-quota-widget/issues
DefaultDirName={localappdata}\Programs\CodexQuotaWidget
DefaultGroupName=Codex Quota Widget
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\release
OutputBaseFilename=CodexQuotaWidget-{#AppVersion}-windows-x64-setup
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\CodexQuotaWidget.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\CodexQuotaWidget\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Codex Quota Widget"; Filename: "{app}\CodexQuotaWidget.exe"
Name: "{group}\Settings"; Filename: "{app}\CodexQuotaWidget.exe"; Parameters: "--settings"
Name: "{autodesktop}\Codex Quota Widget"; Filename: "{app}\CodexQuotaWidget.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CodexQuotaWidget.exe"; Description: "Launch Codex Quota Widget"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\CodexQuotaWidget.exe"; Parameters: "--quit"; Flags: runhidden waituntilterminated; RunOnceId: "QuitWidget"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var StartupCommand: String;
begin
  if CurUninstallStep = usUninstall then begin
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CodexQuotaWidget', StartupCommand) then
      if Pos(Lowercase(ExpandConstant('{app}\CodexQuotaWidget.exe')), Lowercase(StartupCommand)) > 0 then
        RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CodexQuotaWidget');
  end;
end;
