@echo off
rem Run the colony without thinking about which Python.
rem
rem `install.ps1` puts the dependencies in a .venv inside this folder, so a bare
rem `py -m colony` would not find them. This picks the right interpreter, runs
rem from the checkout so `-m colony` resolves, and forwards everything else
rem through untouched:
rem
rem     colony-dash dash
rem     colony-dash status
rem     colony-dash phone --on
rem
rem If there is no .venv it falls back to the system Python, which is what you
rem want if you installed the dependencies yourself instead of running the
rem installer.

setlocal
pushd "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m colony %*
) else (
    py -m colony %*
)

set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%
