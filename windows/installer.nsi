Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!ifndef PAYLOAD
  !error "Pass /DPAYLOAD=... and /DOUTPUT=..."
!endif
Name "Branch Desk"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\Branch Desk"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUninstDetails show
VIProductVersion "2026.11.10.1"
Icon "${__FILEDIR__}\BranchDesk.ico"
UninstallIcon "${__FILEDIR__}\BranchDesk.ico"
VIAddVersionKey /LANG=2052 "ProductName" "Branch Desk"
VIAddVersionKey /LANG=2052 "FileDescription" "Branch Desk Windows x64 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "2026.11.10.1"
VIAddVersionKey /LANG=2052 "LegalCopyright" "Branch Desk"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "安装本地分支与服务工作台。$\r$\n$\r$\n适用 Windows 10/11 x64，按当前用户安装，无需管理员权限。$\r$\n需已安装 Git for Windows 与 WebView2 Runtime。Python 和 .NET 随应用提供。$\r$\n$\r$\n安装后不会自动启动工作台或业务服务。"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TEXT "安装完成。请从桌面或开始菜单打开 Branch Desk，按使用手册添加你自己的项目。$\r$\n$\r$\n实际验证范围请参阅 docs 目录中的发布验证说明。"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  SetShellVarContext current
  SetRegView 64
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\Branch Desk"
  ${IfNot} ${IsNativeAMD64}
    MessageBox MB_ICONSTOP "需要 Windows x64 系统。"
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "需要 Windows 10 或 Windows 11。"
    Abort
  ${EndIf}
FunctionEnd

!macro CheckIdle
  IfFileExists "$INSTDIR\python\python.exe" 0 idle
  IfFileExists "$INSTDIR\app\windows\bridge.py" 0 idle
  nsExec::ExecToStack '"$INSTDIR\python\python.exe" -X utf8 "$INSTDIR\app\windows\bridge.py" install-check'
  Pop $0
  Pop $1
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "工作台仍在运行或状态无法确认。请在窗口菜单关闭工作台后台并退出所有窗口，然后重试。$\r$\n$\r$\n$1"
    Abort
  ${EndIf}
  idle:
!macroend

Section "Branch Desk" SEC_APP
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\Branch Desk"
  !insertmacro CheckIdle
  IfFileExists "$LOCALAPPDATA\Programs\Branch Desk.installing\*.*" staging_exists
  IfFileExists "$LOCALAPPDATA\Programs\Branch Desk.previous\*.*" staging_exists
  SetOutPath "$LOCALAPPDATA\Programs\Branch Desk.installing"
  ClearErrors
  File /r "${PAYLOAD}\*"
  ${If} ${Errors}
    MessageBox MB_ICONSTOP "解包失败，原安装未替换。请检查磁盘空间或安全软件，并联系 Codex 排查。"
    Abort
  ${EndIf}
  SetOutPath "$LOCALAPPDATA\Programs"
  IfFileExists "$INSTDIR\*.*" 0 activate
  ClearErrors
  Rename "$INSTDIR" "$LOCALAPPDATA\Programs\Branch Desk.previous"
  ${If} ${Errors}
    MessageBox MB_ICONSTOP "旧安装被占用，原文件未替换。请关闭工作台后重试。"
    Abort
  ${EndIf}
  activate:
  ClearErrors
  Rename "$LOCALAPPDATA\Programs\Branch Desk.installing" "$INSTDIR"
  ${If} ${Errors}
    Rename "$LOCALAPPDATA\Programs\Branch Desk.previous" "$INSTDIR"
    MessageBox MB_ICONSTOP "安装切换失败，已尝试恢复旧版本。请保留现场交给 Codex 排查。"
    Abort
  ${EndIf}
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\Branch Desk"
  CreateShortCut "$SMPROGRAMS\Branch Desk\Branch Desk.lnk" "$INSTDIR\BranchDesk.exe" "" "$INSTDIR\app\windows\BranchDesk.ico" 0
  CreateShortCut "$SMPROGRAMS\Branch Desk\使用与 Codex 排障手册.lnk" "$INSTDIR\docs\Windows使用与Codex排障手册.html"
  CreateShortCut "$DESKTOP\Branch Desk.lnk" "$INSTDIR\BranchDesk.exe" "" "$INSTDIR\app\windows\BranchDesk.ico" 0
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BranchDesk" "DisplayName" "Branch Desk"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BranchDesk" "DisplayVersion" "2026.11.10.1"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BranchDesk" "DisplayIcon" "$INSTDIR\BranchDesk.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BranchDesk" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BranchDesk" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BranchDesk" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BranchDesk" "NoRepair" 1
  RMDir /r "$LOCALAPPDATA\Programs\Branch Desk.previous"
  Goto finished
  staging_exists:
    MessageBox MB_ICONSTOP "发现上次安装遗留目录 Branch Desk.installing 或 Branch Desk.previous。未覆盖或删除它们，请按手册交给 Codex 检查后重试。"
    Abort
  finished:
SectionEnd

Function un.onInit
  SetShellVarContext current
  SetRegView 64
  ${If} $INSTDIR != "$LOCALAPPDATA\Programs\Branch Desk"
    MessageBox MB_ICONSTOP "安装位置异常，停止卸载以保护文件。"
    Abort
  ${EndIf}
FunctionEnd

Section "Uninstall"
  !insertmacro CheckIdle
  SetOutPath "$TEMP"
  Delete "$INSTDIR\BranchDesk.exe"
  Delete "$INSTDIR\Uninstall.exe"
  Delete "$INSTDIR\install-manifest.json"
  Delete "$INSTDIR\THIRD-PARTY-NOTICES.txt"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\docs"
  RMDir /r "$INSTDIR\licenses"
  RMDir "$INSTDIR"
  Delete "$DESKTOP\Branch Desk.lnk"
  RMDir /r "$SMPROGRAMS\Branch Desk"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BranchDesk"
  MessageBox MB_OK "工作台已卸载。你的代码仓库、开发服务以及 %LOCALAPPDATA%\Branch Desk 中的项目配置和历史记录均保留。"
SectionEnd
