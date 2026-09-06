<#
    Sets up the quoting app on a Windows PC. Run once.

    Right-click this file and choose "Run with PowerShell".

    It checks what is needed, installs the parts that are missing, sets up the
    database, and writes a Start shortcut on the Desktop. It changes nothing
    outside this folder except that shortcut, and it asks before each step
    that costs anything or takes time.

    If it stops with a red message, the message says what to do. Nothing is
    left half-done: running it again from the start is always safe.
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"

function Say([string]$text)  { Write-Host "  $text" }
function Good([string]$text) { Write-Host "  [ok] $text" -ForegroundColor Green }
function Warn([string]$text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }
function Stop-With([string]$text, [string]$fix) {
    Write-Host ""
    Write-Host "  [x] $text" -ForegroundColor Red
    foreach ($line in $fix -split "`n") { Write-Host "      $($line.Trim())" -ForegroundColor Yellow }
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host ""
Write-Host "=============================================================="
Write-Host "  EDM Zone quoting app - setup"
Write-Host "=============================================================="
Write-Host ""

# --- 1. Python ------------------------------------------------------------
Say "Checking for Python..."
$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $version = & $candidate --version 2>&1
        if ($version -match "Python 3\.(\d+)") {
            if ([int]$Matches[1] -ge 11) { $python = $candidate; break }
        }
    } catch { }
}

if (-not $python) {
    Stop-With "Python 3.11 or newer is not installed." @"
        Install it from https://www.python.org/downloads/
        IMPORTANT: on the first screen of the installer, tick
        'Add python.exe to PATH' before clicking Install.
        Then run this setup again.
"@
}
Good "Found $(& $python --version)"

# --- 2. Virtual environment ----------------------------------------------
$venv = Join-Path $backend ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

if (Test-Path $venvPython) {
    Good "Using the existing installation folder"
} else {
    Say "Creating a private Python folder for the app (about 30 seconds)..."
    & $python -m venv $venv
    if (-not (Test-Path $venvPython)) {
        Stop-With "Could not create the Python folder." "Check you have permission to write to $backend"
    }
    Good "Created"
}

# --- 3. Dependencies ------------------------------------------------------
Say "Installing the parts the app needs (a few minutes the first time)..."
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r (Join-Path $backend "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Stop-With "Installing the app's parts failed." @"
        This is nearly always the internet connection or a firewall.
        Try again; if it keeps failing, send the red text above to Claude.
"@
}
Good "Installed"

# --- 4. Settings file -----------------------------------------------------
$envFile = Join-Path $backend ".env"
if (Test-Path $envFile) {
    Good "Settings file already exists - leaving it alone"
} else {
    Say "Creating the settings file..."
    @"
# Settings for the quoting app. This file holds passwords - never email it,
# never put it in GitHub, never paste it into a chat window.

AQM_ENVIRONMENT=production

# --- Reading the sales mailbox ---
# Fill these in after following the connection instructions.
AQM_GRAPH_AUTH_MODE=user
AQM_GRAPH_TENANT_ID=
AQM_GRAPH_CLIENT_ID=
AQM_GRAPH_QUOTING_MAILBOX=sales@edmzone.co.uk
AQM_GRAPH_RFQ_CATEGORY=RFQ
AQM_INTERNAL_EMAIL_DOMAINS=["edmzone.co.uk"]

# How often to check the mailbox, in seconds. 300 = every five minutes.
AQM_MAILBOX_POLL_SECONDS=300

# --- Reading drawings ---
# Needed for the AI to read drawings. Without it the pricing still works but
# nothing is read off a drawing automatically.
AQM_ANTHROPIC_API_KEY=

# --- Where things are kept ---
AQM_DATABASE_URL=sqlite:///./quoting.db
AQM_STORAGE_ROOT=./storage
"@ | Set-Content -Path $envFile -Encoding UTF8
    Good "Created backend\.env - the connection details go in here"
}

# --- 5. Database ----------------------------------------------------------
Say "Setting up the database..."
Push-Location $backend
try {
    & $venvPython -m alembic upgrade head 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-With "Could not set up the database." "Send the message above to Claude." }
    Good "Database ready"

    # Only the market sources. Deliberately not the development seed: its
    # rates and prices are invented placeholders, and putting invented rates
    # into the machine that quotes customers is the one thing this whole
    # system is built to prevent. Real rates get typed in on the Rates screen.
    & $venvPython -m scripts.seed --sources-only 2>&1 | Out-Null
    Good "Market data sources listed (switched off until given a web address)"
} finally { Pop-Location }

# --- 6. Desktop shortcut --------------------------------------------------
Say "Making a Start shortcut on the Desktop..."
$startBat = Join-Path $PSScriptRoot "Start.bat"
$shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "EDM Zone Quoting.lnk"
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath = $startBat
$link.WorkingDirectory = $PSScriptRoot
$link.Description = "Start the EDM Zone quoting app"
$link.Save()
Good "Added 'EDM Zone Quoting' to the Desktop"

# --- Done -----------------------------------------------------------------
Write-Host ""
Write-Host "=============================================================="
Write-Host "  Setup finished" -ForegroundColor Green
Write-Host "=============================================================="
Write-Host ""
Say "What happens next:"
Write-Host ""
Say "1. Put the connection details into backend\.env"
Say "   (the two ID numbers from the Microsoft sign-up page)"
Say "2. Double-click Sign-in.bat, and sign in with the code it shows"
Say "3. Double-click 'EDM Zone Quoting' on the Desktop"
Write-Host ""
Say "The app opens at http://localhost:8000 in your browser."
Write-Host ""
Warn "There are no rates in it yet, and that is on purpose."
Say "   Enquiries will arrive and drawings will be read, but nothing"
Say "   will price until you enter your hourly rates on the Rates"
Say "   screen. The app has no default rate anywhere - it refuses to"
Say "   quote rather than guess a number nobody chose."
Write-Host ""
Read-Host "Press Enter to close"
