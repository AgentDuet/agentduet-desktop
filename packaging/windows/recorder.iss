[Setup]
AppId=AgentDuet.Recorder
AppName=AgentDuet Recorder
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\AgentDuet Recorder
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
MinVersion=10.0.19045
DisableProgramGroupPage=yes
DefaultGroupName=AgentDuet Recorder
OutputDir=..\..\dist-installer
OutputBaseFilename=agentduet-recorder-windows-x86_64-setup
Compression=lzma2
SolidCompression=yes
CloseApplications=yes

[Files]
Source: "..\..\dist-bin\agentduet-desktop.exe"; DestDir: "{app}"; DestName: "AgentDuet Recorder.exe"; Flags: ignoreversion
Source: "..\..\dist-bin\MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy
Source: "..\NOTICE.recorder"; DestDir: "{app}"; DestName: "NOTICE"
Source: "..\..\LICENSE"; DestDir: "{app}"

[Icons]
Name: "{userprograms}\AgentDuet Recorder"; Filename: "{app}\AgentDuet Recorder.exe"

[UninstallDelete]
Type: files; Name: "{userstartup}\agentduet-recorder.cmd"

[Code]
function HasRuntime(Root: Integer): Boolean;
var Version: String;
begin
  Result := RegQueryStringValue(Root, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version)
    and (Version <> '') and (Version <> '0.0.0.0');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var ExitCode: Integer;
begin
  Result := '';
  if HasRuntime(HKCU) or HasRuntime(HKLM32) then Exit;
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/install', '', SW_SHOW,
    ewWaitUntilTerminated, ExitCode) or not (HasRuntime(HKCU) or HasRuntime(HKLM32)) then
    Result := 'Install Microsoft Edge WebView2 Runtime and retry setup.';
end;
