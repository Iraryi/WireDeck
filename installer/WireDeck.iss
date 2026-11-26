; WireDeck Inno Setup 6 Installer Script
; Single source of truth macros supplied by scripts/build.ps1:
;   /DAppVersion=0.1.0
;   /DAppId={8E4F7A21-6C3D-4B9E-9A12-5D8C1F4E2B70}
;   /DSourceDir=d:\New\WireDeck\dist\WireDeck
;   /DOutputDir=d:\New\WireDeck\release

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef AppId
  #define AppId "{8E4F7A21-6C3D-4B9E-9A12-5D8C1F4E2B70}"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\WireDeck"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif

#define AppName "WireDeck"
#define AppPublisher "WireDeck Contributors"
#define AppURL "https://github.com/wiredeck/WireDeck"
#define AppIdEscaped "{" + AppId

[Setup]
AppId={#AppIdEscaped}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=WireDeck-v{#AppVersion}-win64-setup
SetupIconFile=..\assets\wiredeck.ico
UninstallDisplayIcon={app}\WireDeck.exe
UninstallDisplayName={#AppName} v{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=115
ShowLanguageDialog=no
UsePreviousLanguage=yes
ChangesEnvironment=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "zh_CN"; MessagesFile: "compiler:Default.isl,ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
zh_CN.TaskDesktop=创建桌面快捷方式
english.TaskDesktop=Create a desktop shortcut
zh_CN.TaskPath=将 wiredeck-cli.exe 加入用户 PATH（推荐，供终端与 AI 调用）
english.TaskPath=Add wiredeck-cli.exe to user PATH (recommended for CLI / AI)
zh_CN.TaskPortable=便携数据模式（配置与扩展保存在安装目录 data\ 下）
english.TaskPortable=Portable data mode (store config and extensions under install\data\)
zh_CN.LaunchApp=立即启动 WireDeck
english.LaunchApp=Launch WireDeck now
zh_CN.ActiveNatPrompt=检测到 WireDeck 有线共享（WireDeckNat）当前仍处于开启状态。是否先通过管理员权限恢复日常上网，再继续卸载？选择“否”将中止卸载以避免残留网络设置。
english.ActiveNatPrompt=WireDeck direct sharing (WireDeckNat) is still active. Clean it up with administrator permission before uninstalling? Choosing No cancels uninstall so your network is not left dirty.
zh_CN.ActiveNatAbort=未能清理活跃的 WireDeckNat 共享（可能取消了 UAC 授权）。已中止卸载，请先在 WireDeck 中点击“恢复日常上网”后再卸载。
english.ActiveNatAbort=Could not clean up active WireDeckNat (UAC may have been cancelled). Uninstall was cancelled; please click "Restore Normal Net" in WireDeck first.

[Tasks]
Name: "desktopicon"; Description: "{cm:TaskDesktop}"
Name: "addtopath"; Description: "{cm:TaskPath}"
Name: "portablemode"; Description: "{cm:TaskPortable}"; Flags: unchecked

[Files]
; Strict ownership: copy packaged app files, explicitly excluding any stray portable.mode
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "portable.mode"; Flags: ignoreversion recursesubdirs createallsubdirs
; Install clean portable.mode ONLY when user explicitly opts into Portable mode
Source: "portable.mode.template"; DestDir: "{app}"; DestName: "portable.mode"; Tasks: portablemode; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\WireDeck.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\WireDeck.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\WireDeck.exe"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent

[Code]
const
  EnvKey = 'Environment';

procedure AddToUserPath(const DirToAdd: string);
var
  OldPath, NewPath, CheckOld, CheckAdd: string;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', OldPath) then
    OldPath := '';
  CheckOld := ';' + Uppercase(OldPath) + ';';
  CheckAdd := ';' + Uppercase(DirToAdd) + ';';
  if Pos(CheckAdd, CheckOld) > 0 then
    Exit;
  if OldPath = '' then
    NewPath := DirToAdd
  else if Copy(OldPath, Length(OldPath), 1) = ';' then
    NewPath := OldPath + DirToAdd
  else
    NewPath := OldPath + ';' + DirToAdd;
  RegWriteExpandStringValue(HKCU, EnvKey, 'Path', NewPath);
end;

procedure RemoveFromUserPath(const DirToRemove: string);
var
  OldPath, NewPath, Part, TargetUpper: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', OldPath) then
    Exit;
  TargetUpper := Uppercase(DirToRemove);
  NewPath := '';
  while OldPath <> '' do
  begin
    P := Pos(';', OldPath);
    if P > 0 then
    begin
      Part := Copy(OldPath, 1, P - 1);
      Delete(OldPath, 1, P);
    end
    else
    begin
      Part := OldPath;
      OldPath := '';
    end;
    if (Part <> '') and (Uppercase(Part) <> TargetUpper) then
    begin
      if NewPath <> '' then NewPath := NewPath + ';';
      NewPath := NewPath + Part;
    end;
  end;
  RegWriteExpandStringValue(HKCU, EnvKey, 'Path', NewPath);
end;

procedure InitializeWizard();
begin
  { Prevent left/vertical checkbox clipping on high-DPI Windows with Microsoft YaHei UI }
  WizardForm.TasksList.Offset := ScaleX(8);
  WizardForm.TasksList.MinItemHeight := ScaleY(24);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
  begin
    AddToUserPath(ExpandConstant('{app}'));
  end;
end;

function InitializeUninstall(): Boolean;
var
  CliExe: string;
  ResultCode: Integer;
  LogDir, LogFile: string;
begin
  Result := True;
  CliExe := ExpandConstant('{app}\wiredeck-cli.exe');
  if not FileExists(CliExe) then
    Exit;

  { Check if WireDeckNat is currently active (exit code 0 means active) }
  if Exec(CliExe, 'status --check-nat-active', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if ResultCode = 0 then
    begin
      if UninstallSilent then
      begin
        { Silent uninstall cannot safely force interactive UAC; record explicit warning log }
        if FileExists(ExpandConstant('{app}\portable.mode')) then
          LogDir := ExpandConstant('{app}\data\logs')
        else
          LogDir := ExpandConstant('{localappdata}\WireDeck\logs');
        ForceDirectories(LogDir);
        LogFile := LogDir + '\uninstall_warning.log';
        SaveStringToFile(LogFile,
          'WARNING: WireDeckNat was still active during silent uninstall. Run "Get-NetNat WireDeckNat | Remove-NetNat" as Administrator.' + #13#10,
          True);
      end
      else
      begin
        if MsgBox(CustomMessage('ActiveNatPrompt'), mbConfirmation, MB_YESNO) = IDYES then
        begin
          if (not Exec(CliExe, 'apply disable-direct --elevate', '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
          begin
            MsgBox(CustomMessage('ActiveNatAbort'), mbError, MB_OK);
            Result := False;
            Exit;
          end;
        end
        else
        begin
          Result := False;
          Exit;
        end;
      end;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RemoveFromUserPath(ExpandConstant('{app}'));
  end;
end;
