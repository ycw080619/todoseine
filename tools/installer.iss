; TodoSeine 一键安装包（Inno Setup 6）
; 用法：ISCC.exe installer.iss
; 产物：dist\TodoSeine-Setup-0.4.1.exe

#define MyAppName "TodoSeine"
#define MyAppVersion "0.4.1"
#define MyAppPublisher "TodoSeine"
#define MyAppExeName "todoseine_qt.exe"
#define MyAppIcon "..\assets\todoseine.ico"
#define MyAppExePath "..\dist\todoseine_qt.exe"

[Setup]
AppId={{8A7B3C1D-2E4F-5A6B-9C8D-7E6F5A4B3C2D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/ycw080619/todoseine
AppSupportURL=https://github.com/ycw080619/todoseine
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
; 单文件安装包：所有内容打包进一个 exe
OutputDir=..\dist
OutputBaseFilename=TodoSeine-Setup-{#MyAppVersion}
SetupIconFile={#MyAppIcon}
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
; 中文界面
LanguageDetectionMethod=none
; 不需要管理员权限（安装到当前用户目录）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; 安装前关闭正在运行的实例
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: checkedonce
Name: "startupicon"; Description: "开机自启（也可在应用设置里更改）"; GroupDescription: "开机自启:"

[Files]
; 主程序（onefile，已包含所有依赖）
Source: "{#MyAppExePath}"; DestDir: "{app}"; Flags: ignoreversion
; 应用图标（供快捷方式使用）
Source: "{#MyAppIcon}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\todoseine.ico"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\todoseine.ico"; Tasks: desktopicon

[Registry]
; 开机自启：写入 HKCU\Software\Microsoft\Windows\CurrentVersion\Run
; 与应用内 tray.set_autostart 用同一个键名，互相兼容
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "TodoSeine"; ValueData: """{app}\{#MyAppExeName}"""; \
  Tasks: startupicon; Flags: uninsdeletevalue

[Run]
; 安装完成后可选启动
Filename: "{app}\{#MyAppExeName}"; \
  Description: "启动 {#MyAppName}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时清理用户数据（可选，默认保留用户配置）
; Type: filesandordirs; Name: "{userappdata}\.chat2todo"
