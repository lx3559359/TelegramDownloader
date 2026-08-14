#ifndef AppVersion
  #define AppVersion "0.3.1"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\TelegramDownloader"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\release"
#endif
#ifnexist SourceDir + "\TelegramDownloader.exe"
  #error Packaged runtime is missing TelegramDownloader.exe
#endif
#ifnexist SourceDir + "\UpdateHelper.exe"
  #error Packaged runtime is missing UpdateHelper.exe
#endif
#ifnexist SourceDir + "\runtime-manifest.json"
  #error Packaged runtime is missing runtime-manifest.json
#endif

[Setup]
AppId={{B19D534A-A414-4D17-9BB6-CE9A60D8243C}
AppName=Telegram 下载器
AppVersion={#AppVersion}
AppPublisher=lx3559359
AppPublisherURL=https://github.com/lx3559359/TelegramDownloader
AppSupportURL=https://github.com/lx3559359/TelegramDownloader/issues
DefaultDirName={code:GetDefaultInstallDir}
DefaultGroupName=Telegram 下载器
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
AllowUNCPath=no
OutputDir={#OutputDir}
OutputBaseFilename=TelegramDownloader-{#AppVersion}-win-x64-setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern dark
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
Uninstallable=yes
UninstallDisplayIcon={app}\TelegramDownloader.exe
UsePreviousAppDir=yes
UsePreviousLanguage=no
ChangesAssociations=no
ChangesEnvironment=no

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "data\*,downloads\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{app}\data"; Flags: uninsneveruninstall
Name: "{app}\downloads"; Flags: uninsneveruninstall

[Icons]
Name: "{userprograms}\Telegram 下载器"; Filename: "{app}\TelegramDownloader.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\Telegram 下载器"; Filename: "{app}\TelegramDownloader.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\TelegramDownloader.exe"; Description: "启动 Telegram 下载器"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
const
  DRIVE_FIXED = 3;

var
  RemoveUserData: Boolean;

function GetDriveType(lpRootPathName: string): Integer;
  external 'GetDriveTypeW@kernel32.dll stdcall';

function NormalizedDrive(const Path: string): string;
begin
  Result := Uppercase(AddBackslash(ExtractFileDrive(ExpandFileName(Path))));
end;

function IsForbiddenInstallPath(const Path: string): Boolean;
var
  CandidateDrive: string;
  SystemDrive: string;
begin
  CandidateDrive := NormalizedDrive(Path);
  SystemDrive := Uppercase(AddBackslash(ExtractFileDrive(ExpandConstant('{win}'))));
  Result := (CandidateDrive = '') or (CandidateDrive = 'C:\') or
    (CandidateDrive = SystemDrive);
end;

function GetDefaultInstallDir(Param: string): string;
var
  DriveNumber: Integer;
  Root: string;
  SystemDrive: string;
begin
  SystemDrive := Uppercase(AddBackslash(ExtractFileDrive(ExpandConstant('{win}'))));
  for DriveNumber := Ord('D') to Ord('Z') do
  begin
    Root := Chr(DriveNumber) + ':\';
    if (Uppercase(Root) <> SystemDrive) and (GetDriveType(Root) = DRIVE_FIXED) then
    begin
      Result := Root + 'TelegramDownloader';
      Exit;
    end;
  end;
  Result := 'D:\TelegramDownloader';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and IsForbiddenInstallPath(WizardDirValue) then
  begin
    if not WizardSilent then
      SuppressibleMsgBox(
        '为确保所有数据都保存在非系统盘，请选择 C 盘和 Windows 系统盘以外的固定磁盘。',
        mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): string;
begin
  Result := '';
  if IsForbiddenInstallPath(WizardDirValue) then
    Result := '安装已阻止：目标目录不能位于 C 盘或 Windows 系统盘。';
end;

function InitializeUninstall(): Boolean;
begin
  RemoveUserData := SuppressibleMsgBox(
    '是否同时删除 data 和 downloads 中的账号配置、任务记录及已下载文件？' + #13#10 +
    '选择“否”将保留全部用户数据。',
    mbConfirmation, MB_YESNO, IDNO) = IDYES;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and RemoveUserData then
  begin
    DelTree(ExpandConstant('{app}\data'), True, True, True);
    DelTree(ExpandConstant('{app}\downloads'), True, True, True);
  end;
end;
