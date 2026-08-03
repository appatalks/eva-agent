!macro customInstall
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\runtime\install-windows-prereqs.ps1" -LaunchLogin'
  Pop $0
  StrCmp $0 "0" eva_bootstrap_complete
  MessageBox MB_ICONSTOP|MB_OK "Eva runtime setup failed. Review $LOCALAPPDATA\Eva Standalone\runtime\bootstrap.log, then run the installer again."
  Abort
eva_bootstrap_complete:
!macroend
