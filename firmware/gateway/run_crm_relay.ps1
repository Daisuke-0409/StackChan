# Starts the Tachikoma CRM relay on the PC that can reach the CRM.
#
# Run this at the site where the CRM lives -- today the office PC. The gateway
# at the other site asks this process for grave lookups over Tailscale; this
# process asks the CRM over localhost and hands back only the fields the robot
# can say aloud.
#
#   powershell -ExecutionPolicy Bypass -File gateway\run_crm_relay.ps1
#
# Settings come from gateway\.env.crm_relay (gitignored). CRM_RELAY_TOKEN is
# required: this listens on the tailnet, and an open one would serve the
# customer ledger to anything that can reach this PC.

$ErrorActionPreference = "Stop"
$gatewayDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$firmwareDir = Split-Path -Parent $gatewayDir
$envFile = Join-Path $gatewayDir ".env.crm_relay"

# Same reasoning as run_forwarder.ps1: the launcher's lines go where the
# server's do, and not through PowerShell's `*>>`, which writes UTF-16 and
# leaves a log no tool can read straight through.
function Say([string]$text) {
    if ($env:TACHIKOMA_LOG_FILE) {
        $stamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
        # Add-Content, not [System.IO.File]::AppendAllText: a script calling
        # .NET to write files is a shape behaviour-monitoring antivirus
        # flags, and Avast blocked this very file for it on 2026-08-24
        # (IDP.Generic). The cmdlet does the same job and looks like what it
        # is. -Encoding UTF8 adds a BOM only when creating the file; the
        # server writes one too and skips it when the file already exists.
        try {
            Add-Content -Path $env:TACHIKOMA_LOG_FILE -Value "[$stamp] $text" `
                        -Encoding UTF8 -ErrorAction Stop
            return
        } catch {
            # Fall through to the console rather than lose the line.
        }
    }
    Write-Host $text
}

if (-not (Test-Path $envFile)) {
    Write-Error "$envFile not found. Copy .env.crm_relay.example to .env.crm_relay and fill it in."
}

# -Encoding UTF8: PS 5.1 reads a BOM-less file as ANSI, which turns the
# Japanese in .env (STT_VOCABULARY and friends) into mojibake before it
# ever reaches the process. The file is UTF-8; say so.
foreach ($line in Get-Content $envFile -Encoding UTF8) {
    $trimmed = $line.Trim()
    if ($trimmed -eq "" -or $trimmed.StartsWith("#")) { continue }
    $split = $trimmed.IndexOf("=")
    if ($split -lt 1) { continue }
    $name = $trimmed.Substring(0, $split).Trim()
    $value = $trimmed.Substring($split + 1).Trim().Trim('"')
    Set-Item -Path "Env:$name" -Value $value
}

if (-not $env:CRM_RELAY_TOKEN) {
    Say "FATAL: CRM_RELAY_TOKEN is empty in $envFile."
    Write-Error "CRM_RELAY_TOKEN is empty in $envFile. Generate one with: python -c ""import secrets; print(secrets.token_urlsafe(32))"""
}

# A relay that cannot reach the CRM looks exactly like a robot that has
# forgotten how to answer. Say which one it is here, at startup, rather than
# leaving it to be diagnosed from the far end.
$crmBase = if ($env:CRM_BASE_URL) { $env:CRM_BASE_URL } else { "http://127.0.0.1:8765" }
try {
    $null = Invoke-WebRequest "$crmBase/" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
    Say "CRM: reachable at $crmBase"
} catch {
    Say "WARNING: CRM did not answer at $crmBase. Start it (起動.bat) or fix CRM_BASE_URL."
}

$addresses = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress) -join ", "
Say "This PC's addresses: $addresses"
Say "The gateway's CRM_RELAY_URL must point at the tailnet one."

Set-Location $firmwareDir

# Which python, decided here rather than left to PATH order -- the same rule
# as run_gateway.ps1, for the same reason (PATH order changed once and picked
# a broken interpreter; the Microsoft Store stub python.exe also lives on
# PATH and exits without running anything). crm_relay.py needs only the
# standard library, so the probe just asks the candidate to actually run.
# Set TACHIKOMA_PYTHON in the env file to override.
$pythonCandidates = @()
if ($env:TACHIKOMA_PYTHON) { $pythonCandidates += $env:TACHIKOMA_PYTHON }
$pythonCandidates += (Get-Command python.exe -All -ErrorAction SilentlyContinue |
                      Select-Object -ExpandProperty Source)

$python = $null
foreach ($candidate in $pythonCandidates) {
    if (-not (Test-Path $candidate)) { continue }
    if ((& $candidate -c "print('OK')" 2>$null) -contains 'OK') { $python = $candidate; break }
}
if (-not $python) {
    Say "FATAL: no working python.exe found on PATH. Install Python or set TACHIKOMA_PYTHON."
    Write-Error "No working python.exe found on PATH. Install Python or set TACHIKOMA_PYTHON."
}
Say "python: $python"

# The configuration checks above should stop the launcher; a line on the
# server's stderr should not (PowerShell turns native stderr into an
# ErrorRecord, and under "Stop" one harmless warning kills the process --
# run_gateway.ps1 learned this the hard way).
$ErrorActionPreference = "Continue"

& $python -u gateway/crm_relay.py
