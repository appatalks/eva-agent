!macro customInstall
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\runtime\install-windows-prereqs.ps1" -LaunchLogin'
  Pop $0
!macroend
