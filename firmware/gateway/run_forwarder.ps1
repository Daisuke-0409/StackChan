# Starts the Tachikoma forwarder on the PC the robot can reach.
#
# Run this at the site that has a body but no gateway (the office). The robot
# keeps pointing at this PC's LAN address; this process carries the traffic to
# the gateway at the other site over Tailscale.
#
#   powershell -ExecutionPolicy Bypass -File gateway\run_forwarder.ps1
#
# FORWARDER_TARGET is the gateway's Tailscale address, not its LAN address:
# the LAN address belongs to the other site's network and means nothing here.
# Settings come from gateway\.env.forwarder if present, so the office PC needs
# no API keys and no DEVICE_TOKEN -- it never reads what it carries.
#
# TACHIKOMA_LOG_FILE, if set, is where this and the server both write. The
# scheduled task sets it; run it by hand and everything goes to the console
# as before.

$ErrorActionPreference = "Stop"
$gatewayDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$firmwareDir = Split-Path -Parent $gatewayDir
$envFile = Join-Path $gatewayDir ".env.forwarder"

# The launcher's own lines belong in the same file the server writes, so
# that "why is the office body silent" is one grep and not two. They cannot
# go through PowerShell's `*>>`, which writes UTF-16 and made forwarder.log
# unreadable to grep, tail and Python all at once (2026-08-24). Appended as
# UTF-8 without a BOM instead: the server put one at the head already.
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

if (Test-Path $envFile) {
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
}

if (-not $env:FORWARDER_TARGET) {
    # Said into the log as well: a launcher that dies of a bad setting
    # under the scheduled task would otherwise leave nothing behind, and
    # the only visible symptom is a robot that never answers.
    Say "FATAL: FORWARDER_TARGET is not set. Put it in $envFile, e.g. FORWARDER_TARGET=http://100.x.y.z:8080"
    Write-Error "FORWARDER_TARGET is not set. Put it in $envFile, e.g. FORWARDER_TARGET=http://100.x.y.z:8080"
}

# Tailscale is the link this depends on, and a silent logged-out client is the
# failure that looks like a broken robot. Say so here instead.
$tailscale = "C:\Program Files\Tailscale\tailscale.exe"
if (Test-Path $tailscale) {
    $status = & $tailscale status 2>$null | Select-Object -First 1
    if ($LASTEXITCODE -ne 0) {
        Say "WARNING: Tailscale is installed but not logged in. Run 'tailscale up' and open the URL it prints."
    } else {
        Say "Tailscale: $status"
    }
} else {
    Say "WARNING: Tailscale not found at $tailscale. The target address will not resolve without it."
}

$addresses = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress) -join ", "
Say "This PC's addresses: $addresses"
Say "The robot's provisioned gateway URL must point at one of these."
Say "Forwarding to: $env:FORWARDER_TARGET"

Set-Location $firmwareDir

# Which python, decided here rather than left to PATH order -- the same rule
# as run_gateway.ps1, for the same reason (PATH order changed once and picked
# a broken interpreter; the Microsoft Store stub python.exe also lives on
# PATH and exits without running anything). forwarder.py needs only the
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

& $python -u gateway/forwarder.py
