@echo off
rem Double-click to remove the environment, the Desktop shortcut and the two
rem scheduled tasks. It asks first.
rem
rem Your ledger and your .env are left where they are. The script prints both
rem paths so you can delete them yourself if that is what you meant.

setlocal
set "here=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -LiteralPath '%here%' -Recurse | Unblock-File -ErrorAction SilentlyContinue; & '%here%install.ps1' -Uninstall %*"

echo.
echo Press any key to close this window...
pause >nul
