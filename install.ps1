<#
.SYNOPSIS
    Set up Colony Dash on this machine.

.DESCRIPTION
    Everything between a fresh `git clone` and a working dashboard: a private
    Python environment, the dependencies, a config file, the ledger, and a
    Desktop shortcut.

    Nothing here touches your system Python. The dependencies go into a `.venv`
    folder inside this checkout, which means uninstalling is deleting a folder,
    and a version pinned here can never collide with a version some other
    project needs.

    Safe to run twice. Every step checks for its own result first and says so
    instead of redoing it, so this doubles as a repair when something has gone
    missing.

.PARAMETER Port
    The port the shortcut opens the dashboard on. Default 8787.

.PARAMETER NoShortcut
    Skip the Desktop shortcut. Everything else still happens.

.PARAMETER Uninstall
    Remove the environment, the shortcut and the scheduled tasks. Your ledger
    and your `.env` are deliberately left where they are; the script prints
    both paths so you can delete them yourself if that is what you meant.
    Asks first, unless -Force.

.PARAMETER Force
    Skip the confirmation prompt on -Uninstall.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
#>

[CmdletBinding()]
param(
    [int]$Port = 8787,
    [switch]$NoShortcut,
    [switch]$Uninstall,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$Tasks = @("Colony Dash Server", "Colony Dash Pulse")

# `python -m colony` finds the package on the working directory, not on the path
# to this script, so every command below has to run from the checkout being
# installed. Without this the installer reaches into whatever checkout the shell
# happened to be sitting in and builds *that* one's ledger -- which is exactly
# what it did the first time it was run, from one folder over.
Push-Location $Root

function Done([int]$Code) {
    Pop-Location
    exit $Code
}

# --- output ------------------------------------------------------------------
# A first-run installer is read, not skimmed. Each step says what it is about to
# do before it does it, so a failure lands under a heading that names the thing
# that failed.

$script:StepNumber = 0

function Step([string]$Text) {
    $script:StepNumber++
    Write-Host ""
    Write-Host "[$script:StepNumber] $Text" -ForegroundColor Cyan
}

function Ok([string]$Text)    { Write-Host "    $Text" -ForegroundColor Green }
function Note([string]$Text)  { Write-Host "    $Text" -ForegroundColor DarkGray }
function Warn([string]$Text)  { Write-Host "    $Text" -ForegroundColor Yellow }

function Fail([string]$Text, [string]$Fix) {
    Write-Host ""
    Write-Host "  $Text" -ForegroundColor Red
    if ($Fix) {
        Write-Host ""
        Write-Host "  $Fix" -ForegroundColor Yellow
    }
    Write-Host ""
    Done 1
}

# --- uninstall ---------------------------------------------------------------

if ($Uninstall) {
    Write-Host ""
    Write-Host "This removes, from this machine:" -ForegroundColor Cyan
    Write-Host "    the scheduled tasks `"$($Tasks -join '", "')`""
    Write-Host "    the Colony Dash shortcut on your Desktop"
    Write-Host "    $Venv"
    Write-Host ""
    Write-Host "Your ledger and your .env are left alone." -ForegroundColor DarkGray
    Write-Host ""

    # The tasks and the Desktop shortcut are machine-wide, not folder-scoped, so
    # an uninstall run from a second checkout by mistake would take out the one
    # you actually use. Cheap question, expensive mistake.
    if (-not $Force) {
        $answer = Read-Host "Go ahead? [y/N]"
        if ($answer -notmatch '^[Yy]') {
            Write-Host "Nothing was removed." -ForegroundColor Yellow
            Done 0
        }
    }

    Step "Scheduled tasks"
    foreach ($name in $Tasks) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Ok "removed `"$name`""
        } else {
            Note "`"$name`" was not registered"
        }
    }

    Step "Desktop shortcut"
    $desktop = [Environment]::GetFolderPath("Desktop")
    $link = Join-Path $desktop "Colony Dash.lnk"
    if (Test-Path $link) {
        Remove-Item $link -Force
        Ok "removed $link"
    } else {
        Note "no shortcut on the Desktop"
    }

    Step "Python environment"
    if (Test-Path $Venv) {
        Remove-Item $Venv -Recurse -Force
        Ok "removed $Venv"
    } else {
        Note "no .venv to remove"
    }

    Write-Host ""
    Write-Host "Left alone, on purpose:" -ForegroundColor Yellow
    Write-Host "    $(Join-Path $Root '.colony')   the ledger, every story and run transcript"
    Write-Host "    $(Join-Path $Root '.env')      your config, including any access token"
    Write-Host ""
    Write-Host "Delete those yourself if you meant to start over. Deleting the"
    Write-Host "ledger cannot be undone."
    Write-Host ""
    Done 0
}

Write-Host ""
Write-Host "Colony Dash" -ForegroundColor Cyan
Write-Host "Installing into $Root" -ForegroundColor DarkGray

# --- 1. python ---------------------------------------------------------------
# 3.12 is what this has been built and run against. 3.11 and below are a hard
# no: the code uses `X | None` annotations at runtime and `tomllib`.

Step "Looking for Python 3.12 or newer"

$Candidates = @(
    @("py", "-3.13"),
    @("py", "-3.12"),
    @("py", "-3"),
    @("python"),
    @("py")
)

function Invoke-Candidate($Candidate, [string[]]$Arguments) {
    $prefix = @()
    if ($Candidate.Count -gt 1) { $prefix = $Candidate[1..($Candidate.Count - 1)] }
    & $Candidate[0] ($prefix + $Arguments) 2>$null
}

$Python = $null
foreach ($candidate in $Candidates) {
    if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
    try {
        $version = Invoke-Candidate $candidate @("-c", "import sys;print('%d.%d' % sys.version_info[:2])")
    } catch {
        continue
    }
    if ($LASTEXITCODE -ne 0 -or -not $version) { continue }
    $parts = "$version".Trim().Split(".")
    if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 12) {
        $Python = $candidate
        Ok "found Python $version via `"$($candidate -join ' ')`""
        break
    }
    Note "skipped Python $version at `"$($candidate -join ' ')`", too old"
}

if (-not $Python) {
    Fail "No Python 3.12 or newer on this machine." @"
Install it, then run this script again:

    winget install Python.Python.3.12

Or download it from https://www.python.org/downloads/ and tick
"Add python.exe to PATH" on the first screen of the installer.
"@
}

# --- 2. the environment ------------------------------------------------------

Step "Creating the private environment"

if (Test-Path $VenvPy) {
    Note "already there, reusing $Venv"
} else {
    Invoke-Candidate $Python @("-m", "venv", $Venv) | Out-Null
    if (-not (Test-Path $VenvPy)) {
        Fail "Could not create a virtual environment in $Venv." @"
This usually means the Python install is missing the `venv` module, which
happens with some Microsoft Store builds. Install Python from python.org
instead and run this script again.
"@
    }
    Ok "created $Venv"
}

# --- 3. dependencies ---------------------------------------------------------
# Four packages, all pinned. This is the only step that needs the internet, and
# the only one that takes real time.

Step "Installing dependencies (this is the slow part)"
Note "four packages and what they depend on, about a minute on a first run"

& $VenvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $VenvPy -m pip install -r (Join-Path $Root "requirements.txt") --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    Fail "pip could not install the dependencies." @"
The output above says why. The usual causes are no internet connection, or a
corporate proxy that needs configuring. Nothing has been half-installed: run
this script again once it is sorted.
"@
}
Ok "fastapi, uvicorn, pywebview and pillow are in"

# --- 4. config ---------------------------------------------------------------
# Every key in .env.example is optional, so this is a convenience rather than a
# requirement. Copying it means the file is sitting there with its comments when
# you want to turn something on, instead of being something you have to go find.

Step "Config file"

$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    Note ".env already exists, leaving it alone"
} else {
    Copy-Item (Join-Path $Root ".env.example") $EnvFile
    Ok "wrote .env from the example"
    Note "every setting in it is optional; the dashboard runs with none of them"
}

# --- 5. the ledger -----------------------------------------------------------

Step "Building the ledger"

& $VenvPy -m colony init
if ($LASTEXITCODE -ne 0) {
    Fail "`colony init` failed. The output above says why." ""
}

# --- 6. shortcut -------------------------------------------------------------
# `colony shortcut` targets the pythonw.exe beside whichever interpreter runs
# it, so running it through the venv is what points the shortcut at the venv.

if (-not $NoShortcut) {
    Step "Desktop shortcut"
    & $VenvPy -m colony shortcut --port $Port
    if ($LASTEXITCODE -ne 0) {
        Warn "could not create the shortcut; everything else is fine"
        Warn "start the dashboard with:  .\colony-dash.cmd dash"
    }
}

# --- 7. what is still missing ------------------------------------------------
# Neither of these stops the install, and neither can be fixed from in here, so
# they are reported at the end rather than raised as failures partway through.

Step "Checking what else this needs"

if (Get-Command claude -ErrorAction SilentlyContinue) {
    Ok "the claude CLI is on your PATH"
} else {
    Warn "the claude CLI is not on your PATH"
    Warn "the dashboard, the board and the ledger all work without it, but no"
    Warn "agent can run until it is there and signed in:"
    Warn "    https://claude.com/claude-code"
}

$WebView2 = "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
if (Test-Path $WebView2) {
    Ok "the WebView2 runtime is installed, so the desktop window will open"
} else {
    Warn "no WebView2 runtime found, so there will be no desktop window"
    Warn "the server still runs; open http://127.0.0.1:$Port in a browser"
    Warn "or install the runtime: winget install Microsoft.EdgeWebView2Runtime"
}

# --- done --------------------------------------------------------------------

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host ""
Write-Host "Start it:" -ForegroundColor Cyan
Write-Host "    .\colony-dash.cmd dash        the desktop window"
Write-Host "    .\colony-dash.cmd status      the same thing as text"
Write-Host "    .\colony-dash.cmd --help      everything else"
Write-Host ""
Write-Host "Or double-click `"Colony Dash`" on your Desktop."
Write-Host ""
Write-Host "The colony reads the folder this checkout sits in, so it expects"
Write-Host "your other projects to be siblings of this one. Point it elsewhere"
Write-Host "with COLONY_PROJECTS_ROOT in .env."
Write-Host ""

Done 0
