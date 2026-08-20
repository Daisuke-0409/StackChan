# Puts this machine's robot token on the clipboard, without showing it.
#
# The office robot is flashed with its own DEVICE_TOKEN, and the home
# gateway has to know it or the office body gets 401 to every poll -- which
# is exactly what it has been getting. Carrying it home is the last step,
# and the value should not pass through a terminal, a chat window or a log
# on the way.
#
# Two ways out, neither of them a screen.
#
#   powershell -File gateway\copy_device_token.ps1 -SendTo oo
#     Sends it straight to the other machine over Taildrop. Encrypted
#     between the two of them, no cloud in the middle, and nothing to
#     copy by hand. The receiving side runs: tailscale file get .
#
#   powershell -File gateway\copy_device_token.ps1
#     Puts it on the clipboard for a password manager. The clipboard keeps
#     what it is given, so overwrite it afterwards: Set-Clipboard "cleared"
#     (an empty string is rejected by Windows PowerShell 5.1).
#
# Either way a fingerprint is printed instead of the token, which is safe
# to read aloud, photograph, or compare between two machines.

[CmdletBinding()]
param(
    [string]$CMakeCache,
    [string]$SendTo
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

$length = $token.Length

if ($SendTo) {
    $tailscale = "C:\Program Files\Tailscale\tailscale.exe"
    if (-not (Test-Path $tailscale)) { Write-Error "Tailscale not found at $tailscale." }
    # A file, briefly, because Taildrop sends files. Written to this user's
    # temp directory and deleted in the same breath.
    $temp = Join-Path $env:TEMP "office-device-token.txt"
    $noBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($temp, $token, $noBom)
    $token = $null
    try {
        & $tailscale file cp $temp "$($SendTo):"
        if ($LASTEXITCODE -ne 0) { Write-Error "Taildrop failed. Is $SendTo online?" }
    } finally {
        Remove-Item $temp -Force -ErrorAction SilentlyContinue
    }
    Write-Host ""
    Write-Host "Sent to $SendTo over Taildrop. It was not displayed and it did not"
    Write-Host "pass through anybody else's server."
    Write-Host "  fingerprint: $fingerprint"
    Write-Host "  length:      $length"
    Write-Host ""
    Write-Host "On that machine:  tailscale file get ."
    Write-Host "Then append it to DEVICE_TOKEN in gateway\.env after a comma, and"
    Write-Host "delete the received file."
} else {
    Set-Clipboard -Value $token
    $token = $null
    Write-Host ""
    Write-Host "This robot's token is on the clipboard. It was not displayed."
    Write-Host "  fingerprint: $fingerprint"
    Write-Host "  length:      $length"
    Write-Host ""
    Write-Host "Paste it into a password manager, then clear the clipboard:"
    Write-Host "  Set-Clipboard `"cleared`""
    Write-Host ""
    Write-Host "At home it goes on the end of DEVICE_TOKEN in gateway\.env, after a comma."
}
Write-Host "Both bodies are then admitted; removing one entry later revokes one body."
