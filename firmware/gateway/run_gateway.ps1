# Starts the Tachikoma Gateway with settings from gateway/.env (gitignored).
#
# The device reaches this process over the LAN, so GATEWAY_HOST must be
# 0.0.0.0 -- server.py's built-in default (127.0.0.1) accepts only local
# connections and leaves the device with "Connection reset by peer".
#
#   powershell -File gateway\run_gateway.ps1
#
# DEVICE_TOKEN must match the TACHIKOMA_DEVICE_TOKEN the firmware was
# provisioned with, otherwise every request comes back 401. If .env does not
# set it, it is read from the build's CMakeCache.txt.

$ErrorActionPreference = "Stop"
$gatewayDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$firmwareDir = Split-Path -Parent $gatewayDir
$envFile = Join-Path $gatewayDir ".env"

if (-not (Test-Path $envFile)) {
    Write-Error "$envFile not found. Copy .env.example to .env and fill it in."
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

if (-not $env:DEVICE_TOKEN) {
    $cache = Join-Path $firmwareDir "build\CMakeCache.txt"
    if (Test-Path $cache) {
        $match = Get-Content $cache | Where-Object { $_ -match '^TACHIKOMA_DEVICE_TOKEN:STRING=' }
        if ($match) { $env:DEVICE_TOKEN = ($match -replace '^TACHIKOMA_DEVICE_TOKEN:STRING=', '') }
    }
}

if (-not $env:GATEWAY_HOST) { $env:GATEWAY_HOST = "0.0.0.0" }

# Fail loudly here rather than letting every device request 502 at runtime.
if ($env:AI_PROVIDER -eq "gemini" -and -not $env:AI_PROVIDER_API_KEY) {
    Write-Error "AI_PROVIDER=gemini but AI_PROVIDER_API_KEY is empty in $envFile."
}
if ($env:STT_PROVIDER -eq "gemini" -and -not $env:AI_PROVIDER_API_KEY) {
    Write-Error "STT_PROVIDER=gemini but AI_PROVIDER_API_KEY is empty in $envFile (Gemini STT reuses the chat key)."
}
if (-not $env:DEVICE_TOKEN -and $env:ALLOW_INSECURE_DEV -ne "1") {
    Write-Error "No DEVICE_TOKEN and ALLOW_INSECURE_DEV is not 1: every request would be rejected with 401."
}

$addresses = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress) -join ", "
Write-Host "LAN addresses: $addresses"
Write-Host "The firmware's provisioned gateway URL must point at one of these."

Set-Location $firmwareDir
python -u gateway/server.py
