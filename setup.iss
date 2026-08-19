[Setup]
AppName=My Activity Monitor
AppVersion=1.0
DefaultDirName={userappdata}\WindowsMonitor
DefaultGroupName=My Activity Monitor
UninstallDisplayIcon={app}\watchdog.exe
OutputDir=user_installer
OutputBaseFilename=Setup_Monitor
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest

; Disable all wizard pages for silent single-click installation
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes

[Files]
Source: "dist\watchdog.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "app.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "watchdog.py"; DestDir: "{app}"; Flags: ignoreversion
Source: ".env"; DestDir: "{app}"; Flags: ignoreversion
Source: "install.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "uninstall.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userstartup}\My Activity Monitor"; Filename: "{app}\watchdog.exe"; WorkingDir: "{app}"

[Run]
; Run the dependency checker and Python installer silently in the background
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -Silent"; Flags: runhidden waituntilterminated

; Run the program immediately after installation completes
Filename: "{app}\watchdog.exe"; Description: "Launch Monitor"; Flags: nowait postinstall skipifsilent
