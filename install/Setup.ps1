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
    foreach ($line in $fix -split "`n") { Write-Host "      $($line.TrimEnd())" -ForegroundColor Yellow }
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

# Runs one of the app's own commands and returns the code it finished with,
# leaving everything it printed in $script:StepOutput.
#
# This exists because of a trap that stopped a real install. Python tools
# write their ordinary progress messages to the "error" channel - alembic
# announces every database step that way - and none of it is an error. With
# $ErrorActionPreference = "Stop" set above, PowerShell treats a single line
# on that channel as a fatal fault and kills the script, so a database step
# that had just succeeded looked like a crash.
#
# So: capture both channels as plain text, take no notice of which channel a
# line arrived on, and judge the command only on the exit code it returned.
# That is the number that actually says whether it worked.
function Invoke-Step {
    param([string]$Exe, [string[]]$Arguments)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $lines = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
        $script:StepOutput = ($lines -join [Environment]::NewLine)
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

#: The last few lines a failed step printed - enough to diagnose it, short
#: enough to read on screen and paste into a chat.
function Step-Tail([int]$count = 15) {
    if ([string]::IsNullOrWhiteSpace($script:StepOutput)) { return "(it printed nothing)" }
    $lines = $script:StepOutput -split "`r?`n"
    if ($lines.Count -le $count) { return $script:StepOutput }
    return ($lines[-$count..-1] -join [Environment]::NewLine)
}

Write-Host ""
Write-Host "=============================================================="
Write-Host "  EDM Zone quoting app - setup"
Write-Host "=============================================================="
Write-Host ""

# --- 0. Where the app has been put ----------------------------------------
# On a work PC the Desktop and Documents folders are very often redirected to
# a server, so a folder that looks like it is on this machine is really on
# \\SOMESERVER\something. Both Python's private folder and the app's database
# rely on file locking that network drives do not provide, so installing there
# produces failures later that are almost impossible to read. Say so now,
# while it costs nothing but a drag-and-drop.
Say "Checking where the app has been put..."
$onNetwork = $root.StartsWith("\\") -or $root.StartsWith("//")
if (-not $onNetwork) {
    try {
        $letter = Split-Path -Qualifier $root
        $drive = New-Object -TypeName System.IO.DriveInfo -ArgumentList "$letter\"
        $onNetwork = ($drive.DriveType -eq [System.IO.DriveType]::Network)
    } catch { }
}
if ($onNetwork) {
    Stop-With "The app is in a network folder, and it cannot run from there." @"
        This folder is on a server, not on this PC:

          $root

        At work the Desktop and Documents folders are usually kept on a
        server even though they look like they are on your own machine.
        The app needs to be on the PC itself.

        Move it, then run setup again:

          1. Open File Explorer, click "This PC", open the C: drive
          2. Make a new folder there called  EDMZone
          3. Inside that, make another called  Quoting
          4. Drag this whole folder into C:\EDMZone\Quoting
          5. Open its install folder and double-click START-HERE-Setup.bat
"@
}
Good "Installed on this PC"

# --- 1. Python ------------------------------------------------------------
Say "Checking for Python..."
$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        if ((Invoke-Step $candidate @("--version")) -eq 0 -and $script:StepOutput -match "Python 3\.(\d+)") {
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
Invoke-Step $python @("--version") | Out-Null
Good "Found $($script:StepOutput.Trim())"

# --- 2. Virtual environment ----------------------------------------------
$venv = Join-Path $backend ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

if (Test-Path $venvPython) {
    Good "Using the existing installation folder"
} else {
    Say "Creating a private Python folder for the app (about 30 seconds)..."
    Invoke-Step $python @("-m", "venv", $venv) | Out-Null
    if (-not (Test-Path $venvPython)) {
        Stop-With "Could not create the Python folder." @"
        Check you have permission to write to $backend

        What it printed:
$(Step-Tail)
"@
    }
    Good "Created"
}

# --- 3. Dependencies ------------------------------------------------------
Say "Installing the parts the app needs (a few minutes the first time)..."
Invoke-Step $venvPython @("-m", "pip", "install", "--quiet", "--upgrade", "pip") | Out-Null
$code = Invoke-Step $venvPython @("-m", "pip", "install", "--quiet", "-r", (Join-Path $backend "requirements.txt"))
if ($code -ne 0) {
    Stop-With "Installing the app's parts failed." @"
        This is nearly always the internet connection or a firewall.
        Try again; if it keeps failing, send the lines below to Claude.

$(Step-Tail)
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
    # Alembic narrates what it is doing on the error channel even when all is
    # well, which is exactly why this goes through Invoke-Step. Lines like
    # "INFO [alembic.runtime.migration] Context impl SQLiteImpl" are the
    # sound of it working.
    $code = Invoke-Step $venvPython @("-m", "alembic", "upgrade", "head")
    if ($code -ne 0) {
        Stop-With "Could not set up the database." @"
        What it printed:

$(Step-Tail)

        Send those lines to Claude.
"@
    }
    Good "Database ready"

    # Only the market sources. Deliberately not the development seed: its
    # rates and prices are invented placeholders, and putting invented rates
    # into the machine that quotes customers is the one thing this whole
    # system is built to prevent. Real rates get typed in on the Rates screen.
    $code = Invoke-Step $venvPython @("-m", "scripts.seed", "--sources-only")
    if ($code -ne 0) {
        Stop-With "Could not list the market data sources." @"
        The database itself is fine - only this last step failed, and the
        app will still start. What it printed:

$(Step-Tail)

        Send those lines to Claude.
"@
    }
    Good "Market data sources listed (switched off until given a web address)"
} finally { Pop-Location }

# --- 6. Desktop shortcut --------------------------------------------------
Say "Making a Start shortcut on the Desktop..."
$startBat = Join-Path $PSScriptRoot "Start.bat"
try {
    $shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "EDM Zone Quoting.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($shortcut)
    $link.TargetPath = $startBat
    $link.WorkingDirectory = $PSScriptRoot
    $link.Description = "Start the EDM Zone quoting app"
    $link.Save()
    Good "Added 'EDM Zone Quoting' to the Desktop"
} catch {
    # The app is installed and working at this point; a missing shortcut is a
    # convenience, not a failure, so it must not undo everything above.
    Warn "Could not put a shortcut on the Desktop - everything else is done."
    Say "   Start the app by double-clicking Start.bat in the install folder."
}

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
