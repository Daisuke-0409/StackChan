# Starts the Tachikoma forwarder on the PC the robot can reach.
#
# Run this at the site that has a body but no gateway (the office). The robot
# keeps pointing at this PC's LAN address; this process carries the traffic to
# the gateway at the other site over Tailscale.
#
#   powershell -File gateway\run_forwarder.ps1
#
# FORWARDER_TARGET is the gateway's Tailscale address, not its LAN address:
# the LAN address belongs to the other site's network and means nothing here.
# Settings come from gateway\.env.forwarder if present, so the office PC needs
# no API keys and no DEVICE_TOKEN -- it never reads what it carries.

$ErrorActionPreference = "Stop"
$gatewayDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$firmwareDir = Split-Path -Parent $gatewayDir
$envFile = Join-Path $gatewayDir ".env.forwarder"

if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
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
    Write-Error "FORWARDER_TARGET is not set. Put it in $envFile, e.g. FORWARDER_TARGET=http://100.x.y.z:8080"
}

# Tailscale is the link this depends on, and a silent logged-out client is the
# failure that looks like a broken robot. Say so here instead.
$tailscale = "C:\Program Files\Tailscale\tailscale.exe"
if (Test-Path $tailscale) {
    $status = & $tailscale status 2>$null | Select-Object -First 1
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Tailscale is installed but not logged in. Run 'tailscale up' and open the URL it prints."
    } else {
        Write-Host "Tailscale: $status"
    }
} else {
    Write-Warning "Tailscale not found at $tailscale. The target address will not resolve without it."
}

$addresses = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress) -join ", "
Write-Host "This PC's addresses: $addresses"
Write-Host "The robot's provisioned gateway URL must point at one of these."
Write-Host "Forwarding to: $env:FORWARDER_TARGET"

Set-Location $firmwareDir
python -u gateway/forwarder.py
