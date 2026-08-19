# Starts the Tachikoma CRM relay on the PC that can reach the CRM.
#
# Run this at the site where the CRM lives -- today the office PC. The gateway
# at the other site asks this process for grave lookups over Tailscale; this
# process asks the CRM over localhost and hands back only the fields the robot
# can say aloud.
#
#   powershell -File gateway\run_crm_relay.ps1
#
# Settings come from gateway\.env.crm_relay (gitignored). CRM_RELAY_TOKEN is
# required: this listens on the tailnet, and an open one would serve the
# customer ledger to anything that can reach this PC.

$ErrorActionPreference = "Stop"
$gatewayDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$firmwareDir = Split-Path -Parent $gatewayDir
$envFile = Join-Path $gatewayDir ".env.crm_relay"

if (-not (Test-Path $envFile)) {
    Write-Error "$envFile not found. Copy .env.crm_relay.example to .env.crm_relay and fill it in."
}

foreach ($line in Get-Content $envFile) {
    $trimmed = $line.Trim()
    if ($trimmed -eq "" -or $trimmed.StartsWith("#")) { continue }
    $split = $trimmed.IndexOf("=")
    if ($split -lt 1) { continue }
    $name = $trimmed.Substring(0, $split).Trim()
    $value = $trimmed.Substring($split + 1).Trim().Trim('"')
    Set-Item -Path "Env:$name" -Value $value
}

if (-not $env:CRM_RELAY_TOKEN) {
    Write-Error "CRM_RELAY_TOKEN is empty in $envFile. Generate one with: python -c ""import secrets; print(secrets.token_urlsafe(32))"""
}

# A relay that cannot reach the CRM looks exactly like a robot that has
# forgotten how to answer. Say which one it is here, at startup, rather than
# leaving it to be diagnosed from the far end.
$crmBase = if ($env:CRM_BASE_URL) { $env:CRM_BASE_URL } else { "http://127.0.0.1:8765" }
try {
    $null = Invoke-WebRequest "$crmBase/" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
    Write-Host "CRM: reachable at $crmBase"
} catch {
    Write-Warning "CRM did not answer at $crmBase. Start it (起動.bat) or fix CRM_BASE_URL."
}

$addresses = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress) -join ", "
Write-Host "This PC's addresses: $addresses"
Write-Host "The gateway's CRM_RELAY_URL must point at the tailnet one."

Set-Location $firmwareDir
python -u gateway/crm_relay.py
