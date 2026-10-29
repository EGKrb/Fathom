; Fathom installer (NSIS). Built by tools\build-installer.ps1, which passes the
; release payload dir, version, icon and license on the command line:
;   makensis /DVERSION=1.0.0 /DSRCDIR=<release\release> /DICON=<fathom.ico>
;            /DLICENSE=<LICENSE> /DOUTFILE=<dist\Fathom_1.0.0_setup.exe> fathom-installer.nsi
;
; Fathom is GPL-3.0, based on x64dbg. The installer only packages the portable
; distribution into Program Files, adds shortcuts, and registers an uninstaller.

Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"

!ifndef VERSION
  !define VERSION "1.0.0"
!endif
!ifndef SRCDIR
  !error "pass /DSRCDIR=<release payload dir> (the folder that holds Fathom.exe)"
!endif
!ifndef ICON
  !define ICON "..\src\launcher\fathom.ico"
!endif
!ifndef LICENSE
  !define LICENSE "..\LICENSE"
!endif
!ifndef OUTFILE
  !define OUTFILE "Fathom_${VERSION}_setup.exe"
!endif

!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Fathom"

Name "Fathom ${VERSION}"
OutFile "${OUTFILE}"
; Per-user install: Fathom (x64dbg) keeps its config (x64dbg.ini) and db\ next to
; the executables, so the install dir must stay writable. %LOCALAPPDATA% is
; writable and needs no admin/UAC.
InstallDir "$LOCALAPPDATA\Programs\Fathom"
InstallDirRegKey HKCU "Software\Fathom" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "Fathom"
VIAddVersionKey "FileDescription" "Fathom installer"
VIAddVersionKey "LegalCopyright" "Fathom (GPL-3.0), based on x64dbg"
VIAddVersionKey "FileVersion" "${VERSION}.0"
VIAddVersionKey "ProductVersion" "${VERSION}"

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\Fathom.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Launch Fathom"
!define MUI_FINISHPAGE_LINK "Fathom on GitHub"
!define MUI_FINISHPAGE_LINK_LOCATION "https://github.com/EGKrb/Fathom"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${LICENSE}"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Section "Fathom" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  File /r "${SRCDIR}\*"

  CreateDirectory "$SMPROGRAMS\Fathom"
  CreateShortcut "$SMPROGRAMS\Fathom\Fathom.lnk" "$INSTDIR\Fathom.exe" "" "$INSTDIR\Fathom.exe" 0
  CreateShortcut "$SMPROGRAMS\Fathom\Uninstall Fathom.lnk" "$INSTDIR\uninstall.exe"
  CreateShortcut "$DESKTOP\Fathom.lnk" "$INSTDIR\Fathom.exe" "" "$INSTDIR\Fathom.exe" 0

  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "Software\Fathom" "InstallDir" "$INSTDIR"

  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "Fathom"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "EGKrb"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\Fathom.exe"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" "$\"$INSTDIR\uninstall.exe$\" /S"
  WriteRegStr HKCU "${UNINST_KEY}" "URLInfoAbout" "https://github.com/EGKrb/Fathom"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\Fathom.lnk"
  RMDir /r "$SMPROGRAMS\Fathom"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\Fathom"
SectionEnd
