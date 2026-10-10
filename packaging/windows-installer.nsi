!include "FileFunc.nsh"
!include "LogicLib.nsh"
Unicode true
Name "ZuckerMixer"
OutFile "..\dist\ZuckerMixer-${VERSION}-Windows.exe"
InstallDir "$LOCALAPPDATA\Programs\ZuckerMixer"
RequestExecutionLevel user
SilentInstall silent
SetCompressor /SOLID lzma
Section
  SetOutPath "$INSTDIR"
  ClearErrors
  File /r "..\dist\ZuckerMixer\*.*"
  ${If} ${Errors}
    MessageBox MB_OK|MB_ICONSTOP "Close ZuckerMixer and run this installer again. Installation could not finish."
    SetErrorLevel 1
    Quit
  ${EndIf}
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File "..\build\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
  ExecWait '"$PLUGINSDIR\MicrosoftEdgeWebView2RuntimeInstallerX64.exe" /silent /install' $0
  ; The frozen UI check below verifies the actual renderer, including an
  ; existing WebView2 installation when Microsoft's installer returns nonzero.
  ExecWait '"$INSTDIR\ZuckerMixer.exe" --windows-ui-check' $0
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "ZuckerMixer could not open its Windows interface. Please report this error together with your Windows version."
    SetErrorLevel 1
    Quit
  ${EndIf}
  CreateShortcut "$SMPROGRAMS\ZuckerMixer.lnk" "$INSTDIR\ZuckerMixer.exe"
  ${GetParameters} $1
  ClearErrors
  ${GetOptions} $1 "/NOLAUNCH" $2
  ${If} ${Errors}
    Exec '"$INSTDIR\ZuckerMixer.exe"'
  ${EndIf}
SectionEnd
