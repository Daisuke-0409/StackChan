# Puts the CRM relay token into gateway\.env on the HOME PC.
#
# Two machines need the same secret and neither should ever display it:
#
#   office PC : gateway\.env.crm_relay  CRM_RELAY_TOKEN  (the relay checks it)
#   home PC   : gateway\.env            CRM_RELAY_TOKEN  (the gateway sends it)
#
# setup_crm_token.ps1 is the office half -- it lifts the value out of the
# CRM's own config.json. This is the home half, and it cannot read that
# file, so the value arrives by clipboard: copy it from the password
# manager, run this, and it is written and the clipboard wiped.
#
#   powershell -File gateway\setup_crm_relay_token.ps1
#
# Clipboard rather than a prompt because a hidden prompt refuses pastes in
# some consoles, and a token nobody can paste gets retyped -- or worse,
# pasted somewhere visible first. Use -Prompt to type it instead.
#
# Nothing is echoed either way. Both halves print the same four-byte
# fingerprint, which proves the two machines match and tells a shoulder
# nothing.

[CmdletBinding()]
param(
    [switch]$Prompt,
    [switch]$KeepClipboard
)

$ErrorActionPreference = "Stop"
$gatewayDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $gatewayDir ".env"

if (-not (Test-Path $envFile)) {
    Write-Error "$envFile not found. This script is for the home PC, where the gateway runs."
}

function Get-Fingerprint([string]$value) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $bytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($value))
    return ($bytes[0..3] | ForEach-Object { $_.ToString("x2") }) -join ""
}

$token = $null
if ($Prompt) {
    $secure = Read-Host "CRM_RELAY_TOKEN (typed, not shown)" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $token = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
} else {
    Write-Host "Reading the token from the clipboard (never displayed)."
    Add-Type -AssemblyName System.Windows.Forms
    $token = [System.Windows.Forms.Clipboard]::GetText()
    if (-not $token) {
        Write-Error "The clipboard is empty. Copy the token from the office PC's gateway\.env.crm_relay (or the password manager) and run this again. To type it instead: -Prompt"
    }
}

if (-not $token) { Write-Error "No token was provided." }
$token = $token.Trim()

# A pasted line often arrives as the whole assignment. Take the value.
if ($token -match '^\s*CRM_RELAY_TOKEN\s*=\s*(.+)$') { $token = $Matches[1].Trim() }
$token = $token.Trim('"').Trim("'")

if ($token.Length -lt 16) {
    Write-Error "That does not look like the token (only $($token.Length) characters). Nothing was written."
}

$lines = [System.IO.File]::ReadAllLines($envFile)
$updated = @()
$seen = $false
foreach ($line in $lines) {
    if ($line -match '^\s*CRM_RELAY_TOKEN\s*=') {
        $updated += "CRM_RELAY_TOKEN=$token"
        $seen = $true
    } else {
        $updated += $line
    }
}
if (-not $seen) { $updated += "CRM_RELAY_TOKEN=$token" }

# No BOM: python reads this file with a plain utf-8 open.
$noBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($envFile, $updated, $noBom)

$fingerprint = Get-Fingerprint $token
$token = $null

if (-not $Prompt -and -not $KeepClipboard) {
    # The secret has landed where it belongs; leaving a copy in the
    # clipboard for the next paste is how it ends up in a chat window.
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.Clipboard]::Clear()
    Write-Host "clipboard cleared"
}

Write-Host ""
Write-Host "CRM_RELAY_TOKEN written to gateway\.env"
Write-Host "fingerprint: $fingerprint    (the office PC prints the same one)"
Write-Host ""
Write-Host "Restart the gateway, then ask Tachikoma about a grave location."
