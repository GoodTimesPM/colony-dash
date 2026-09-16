@echo off
rem Double-click this after extracting the zip.
rem
rem It does two things install.ps1 cannot do for itself. Windows marks every
rem file that came out of a downloaded zip as blocked, and PowerShell refuses to
rem run a blocked script, so the files are unblocked first. And a .ps1 is not
rem double-clickable at all by default, which is the whole reason this wrapper
rem is a .cmd.
rem
rem The window stays open at the end so you can read what happened. Everything
rem past the script name is forwarded, so this still works from a terminal:
rem
rem     Install.cmd -Port 9000
rem     Install.cmd -NoShortcut

setlocal
set "here=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -LiteralPath '%here%' -Recurse | Unblock-File -ErrorAction SilentlyContinue; & '%here%install.ps1' %*"

echo.
echo Press any key to close this window...
pause >nul
