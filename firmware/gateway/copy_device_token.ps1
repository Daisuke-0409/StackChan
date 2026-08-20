# Puts this machine's robot token on the clipboard, without showing it.
#
# The office robot is flashed with its own DEVICE_TOKEN, and the home
# gateway has to know it or the office body gets 401 to every poll -- which
# is exactly what it has been getting. Carrying it home is the last step,
# and the value should not pass through a terminal, a chat window or a log
# on the way.
#
# So it goes to the clipboard and nowhere else. Paste it into NordPass,
# carry it home, and the home session appends it to DEVICE_TOKEN after a
# comma. A fingerprint is printed instead, which is safe to read aloud,
# photograph or compare across machines.
#
#   powershell -File gateway\copy_device_token.ps1
#
# The clipboard keeps what it is given. Clear it once it is in NordPass:
#   Set-Clipboard -Value ""

[CmdletBinding()]
param(
    [string]$CMakeCache
)

$ErrorActionPreference = "Stop"
$gatewayDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$firmwareDir = Split-Path -Parent $gatewayDir

if (-not $CMakeCache) { $CMakeCache = Join-Path $firmwareDir "build\CMakeCache.txt" }
if (-not (Test-Path $CMakeCache)) {
    Write-Error "Not found: $CMakeCache. This machine has not built the firmware, so it holds no robot token."
}

$line = Select-String -Path $CMakeCache -Pattern '^TACHIKOMA_DEVICE_TOKEN:STRING=' |
        Select-Object -First 1
if (-not $line) {
    Write-Error "CMakeCache.txt has no TACHIKOMA_DEVICE_TOKEN. Was this build provisioned with one?"
}

$token = ($line.Line -replace '^TACHIKOMA_DEVICE_TOKEN:STRING=', '').Trim()
if (-not $token) { Write-Error "The token line is empty." }

$sha = [System.Security.Cryptography.SHA256]::Create()
$bytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($token))
$fingerprint = ($bytes[0..3] | ForEach-Object { $_.ToString("x2") }) -join ""

Set-Clipboard -Value $token
$length = $token.Length
$token = $null

Write-Host ""
Write-Host "This robot's token is on the clipboard. It was not displayed."
Write-Host "  fingerprint: $fingerprint"
Write-Host "  length:      $length"
Write-Host ""
Write-Host "Paste it into NordPass now, then clear the clipboard:"
Write-Host "  Set-Clipboard -Value `"`""
Write-Host ""
Write-Host "At home, it goes on the end of DEVICE_TOKEN in gateway\.env, after a comma."
Write-Host "Both bodies are then admitted; removing one entry later revokes one body."
