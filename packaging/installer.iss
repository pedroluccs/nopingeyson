; Instalador do NoPingeyson (Inno Setup 6).
; Compilar:  ISCC packaging\installer.iss   (depois do PyInstaller; veja packaging\build.ps1)

#define AppName "NoPingeyson"
#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#define AppExe "NoPingeyson.exe"

[Setup]
AppId={{6E1B6D3C-2B7A-4E3F-9C51-4F0D2A7B9E10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=pedroluccs
AppPublisherURL=https://github.com/pedroluccs/nopingeyson
AppSupportURL=https://github.com/pedroluccs/nopingeyson/issues
AppUpdatesURL=https://github.com/pedroluccs/nopingeyson/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\build\installer
OutputBaseFilename=NoPingeyson-Setup-{#AppVersion}-x64
SetupIconFile=nopingeyson.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
MinVersion=10.0

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\build\dist\NoPingeyson\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent shellexec

[UninstallRun]
; desfaz o FPS Boost antes de remover (volta o Windows ao original)
Filename: "{app}\{#AppExe}"; Parameters: "boost restore"; Flags: runhidden waituntilterminated; RunOnceId: "RestoreBoost"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
