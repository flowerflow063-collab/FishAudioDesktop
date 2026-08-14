[Setup]
AppName=Fish Audio Desktop
AppVersion=1.0.0
DefaultDirName={autopf}\Fish Audio Desktop
DefaultGroupName=Fish Audio Desktop
OutputDir=installer
OutputBaseFilename=FishAudioDesktop-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin

[Files]
Source: "dist\FishAudioDesktop.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Fish Audio Desktop"; Filename: "{app}\FishAudioDesktop.exe"
Name: "{autodesktop}\Fish Audio Desktop"; Filename: "{app}\FishAudioDesktop.exe"

[Run]
Filename: "{app}\FishAudioDesktop.exe"; Description: "Abrir Fish Audio Desktop"; Flags: nowait postinstall skipifsilent
