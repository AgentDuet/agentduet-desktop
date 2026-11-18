[Setup]
AppId=AgentDuet.Recorder
AppName=AgentDuet Recorder
SetupIconFile=..\..\windows\Recorder\Recorder.ico
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
Source: "..\..\dist-bin\window\*"; DestDir: "{app}\window"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\NOTICE.recorder"; DestDir: "{app}"; DestName: "NOTICE"
Source: "..\..\LICENSE"; DestDir: "{app}"

[Icons]
Name: "{userprograms}\AgentDuet Recorder"; Filename: "{app}\AgentDuet Recorder.exe"

[UninstallDelete]
Type: files; Name: "{userstartup}\agentduet-recorder.cmd"
