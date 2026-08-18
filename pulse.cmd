@echo off
REM One heartbeat. Invoked hourly by the "Colony Dash Pulse" scheduled task.
REM Costs zero tokens: a tick is pure Python and only escalates to a wake when
REM something actually changed. Output is appended so the log is the audit trail.
cd /d "%~dp0"
if not exist ".colony" mkdir ".colony"
echo. >> ".colony\pulse.log"
echo ===== %DATE% %TIME% ===== >> ".colony\pulse.log"
"C:\Users\jtbal\AppData\Local\Programs\Python\Python312\python.exe" -m colony pulse >> ".colony\pulse.log" 2>&1
