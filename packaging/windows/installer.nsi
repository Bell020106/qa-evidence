Unicode True
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!define APPKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\QAEvidence"
Var ValidationReason
Name "QA Evidence"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\QA Evidence"
ShowInstDetails show
ShowUninstDetails show
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Korean"

Function .onInit
  SetShellVarContext current
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Windows x64가 필요합니다." /SD IDOK
    SetErrorLevel 2
    Abort
  ${EndIf}
  ReadRegStr $0 HKCU "${APPKEY}" "InstallLocation"
  StrCmp $0 "" 0 existing
  IfFileExists "$SMPROGRAMS\QA Evidence.lnk" foreign_shortcut end_init
  existing:
    StrCpy $INSTDIR $0
    Goto end_init
  foreign_shortcut:
    MessageBox MB_ICONSTOP "기존 QA Evidence 바로가기가 있습니다. 다른 설치의 파일을 덮어쓰지 않습니다." /SD IDOK
    SetErrorLevel 2
    Abort
  end_init:
FunctionEnd

Function ValidateTarget
  StrCpy $ValidationReason "드라이브 루트에는 설치할 수 없습니다. 전용 하위 폴더를 선택하세요."
  ${GetRoot} "$INSTDIR" $0
  StrCmp $INSTDIR $0 unsafe
  StrCmp $INSTDIR "$0\" unsafe
  ; NSIS GetFullPathName requires an existing path. The Win32 API also
  ; normalizes a not-yet-created installation directory without creating it.
  StrCpy $ValidationReason "설치 경로를 해석할 수 없습니다. 유효한 절대 경로를 선택하세요."
  System::Call 'kernel32::GetFullPathNameW(w "$INSTDIR", i ${NSIS_MAX_STRLEN}, w .r0, p 0) i.r1'
  IntCmp $1 0 unsafe
  IntCmp $1 ${NSIS_MAX_STRLEN} unsafe normalized unsafe
  normalized:
  StrCmp $0 "" unsafe
  StrCpy $INSTDIR $0
  trim_separator:
    StrLen $1 $INSTDIR
    IntCmp $1 3 normalized_done normalized_done
    StrCpy $0 $INSTDIR 1 -1
    StrCmp $0 "\" 0 normalized_done
    StrCpy $INSTDIR $INSTDIR -1
    Goto trim_separator
  normalized_done:
  StrCpy $ValidationReason "시스템 또는 사용자 기본 폴더 자체에는 설치할 수 없습니다. 전용 하위 폴더를 선택하세요."
  ${GetRoot} "$INSTDIR" $0
  StrCmp $INSTDIR $0 unsafe
  StrCmp $INSTDIR "$0\" unsafe
  StrCmp $INSTDIR "$PROFILE" unsafe
  StrCmp $INSTDIR "$DOCUMENTS" unsafe
  StrCmp $INSTDIR "$LOCALAPPDATA" unsafe
  StrCmp $INSTDIR "$APPDATA" unsafe
  StrCmp $INSTDIR "$PROGRAMFILES" unsafe
  StrCmp $INSTDIR "$PROGRAMFILES64" unsafe
  StrCmp $INSTDIR "$WINDIR" unsafe
  StrCmp $INSTDIR "$SYSDIR" unsafe
  StrCpy $ValidationReason "설치 경로가 140자를 초과합니다. 짧은 전용 폴더를 선택하세요."
  StrLen $0 $INSTDIR
  IntCmp $0 140 path_ok path_ok unsafe
  path_ok:
    StrCpy $ValidationReason "다른 설치 경로가 등록되어 있습니다. 기존 설치 위치를 확인하세요."
    ReadRegStr $0 HKCU "${APPKEY}" "InstallLocation"
    StrCmp $0 "" owner_check
    StrCmp $0 $INSTDIR owner_check unsafe
  owner_check:
    IfFileExists "$INSTDIR\.qa-evidence-owner" known_owner new_owner
  known_owner:
    StrCpy $ValidationReason "설치 폴더의 소유 표시가 이 제품과 일치하지 않습니다. 다른 전용 폴더를 선택하세요."
    FileOpen $0 "$INSTDIR\.qa-evidence-owner" r
    FileRead $0 $1
    FileClose $0
    StrCmp $1 "QA-EVIDENCE-PACKAGE-1" reparse_check unsafe
  new_owner:
    StrCpy $ValidationReason "설치 대상에 기존 Uninstall.exe 또는 제품 파일이 있습니다. 덮어쓰지 않으므로 다른 전용 폴더를 선택하세요."
    IfFileExists "$INSTDIR\Uninstall.exe" unsafe
    !include "${COLLISION_CHECKS}"
  reparse_check:
    StrCpy $ValidationReason "설치 대상에 링크 또는 재분석 지점이 있습니다. 일반 전용 폴더를 선택하세요."
    !include "${REPARSE_CHECKS}"
    Return
  unsafe:
    MessageBox MB_ICONSTOP "$ValidationReason$\r$\n$\r$\n경로: $INSTDIR" /SD IDOK
    SetErrorLevel 2
    Abort
FunctionEnd

Section "QA Evidence" SEC_MAIN
  SetShellVarContext current
  Call ValidateTarget
  SetOutPath "$INSTDIR"
  ClearErrors
  GetTempFileName $1 "$INSTDIR"
  IfErrors write_failed
  FileOpen $0 $1 w
  IfErrors write_failed
  FileClose $0
  Delete $1
  !include "${INSTALL_FILES}"
  FileOpen $0 "$INSTDIR\.qa-evidence-owner" w
  FileWrite $0 "QA-EVIDENCE-PACKAGE-1"
  FileClose $0
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortcut "$SMPROGRAMS\QA Evidence.lnk" "$INSTDIR\QA Evidence.exe"
  WriteRegStr HKCU "${APPKEY}" "DisplayName" "QA Evidence"
  WriteRegStr HKCU "${APPKEY}" "DisplayVersion" "0.1.0"
  WriteRegStr HKCU "${APPKEY}" "Publisher" "QA Evidence local build"
  WriteRegStr HKCU "${APPKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${APPKEY}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegDWORD HKCU "${APPKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${APPKEY}" "NoRepair" 1
  Goto installed
  write_failed:
    MessageBox MB_ICONSTOP "설치 폴더에 쓸 수 없습니다. 쓰기 가능한 전용 사용자 폴더를 선택하세요." /SD IDOK
    SetErrorLevel 2
    Abort
  installed:
SectionEnd

Function un.onInit
  SetShellVarContext current
  GetFullPathName $INSTDIR "$INSTDIR"
  ReadRegStr $0 HKCU "${APPKEY}" "InstallLocation"
  StrCmp $0 $INSTDIR registry_ok unsafe
  registry_ok:
    FileOpen $0 "$INSTDIR\.qa-evidence-owner" r
    IfErrors unsafe
    FileRead $0 $1
    FileClose $0
    StrCmp $1 "QA-EVIDENCE-PACKAGE-1" reparse_check unsafe
  reparse_check:
    !include "${REPARSE_CHECKS}"
    Return
  unsafe:
    MessageBox MB_ICONSTOP "이 설치의 소유 경로를 확인할 수 없어 제거하지 않았습니다." /SD IDOK
    SetErrorLevel 2
    Abort
FunctionEnd

Section "Uninstall"
  SetShellVarContext current
  !include "${REMOVE_FILES}"
  Delete "$INSTDIR\.qa-evidence-owner"
  Delete "$INSTDIR\Uninstall.exe"
  Delete "$SMPROGRAMS\QA Evidence.lnk"
  DeleteRegKey HKCU "${APPKEY}"
  RMDir "$INSTDIR"
SectionEnd
