# Puts the CRM relay token into gateway\.env on the HOME PC.
#
# Two machines need the same secret and neither should ever display it:
#
#   office PC : gateway\.env.crm_relay  CRM_RELAY_TOKEN  (the relay checks it)
#   home PC   : gateway\.env            CRM_RELAY_TOKEN  (the gateway sends it)
#
# setup_crm_token.ps1 is the office half -- it lifts the value out of the
# CRM's own config.json. This is the home half, and it cannot read that
# file, so the value is typed in once into a hidden field. Nothing is
# echoed; only a four-byte fingerprint is printed, which is enough to
# confirm both machines hold the same string and useless to anyone who
# reads it over a shoulder.
#
#   powershell -File gateway\setup_crm_relay_token.ps1
#
# On the office PC, print the fingerprint to compare against with:
#
#   powershell -File gateway\setup_crm_token.ps1        (it prints one too)

[CmdletBinding()]
param()

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

Write-Host "Paste the token from the office PC's gateway\.env.crm_relay (CRM_RELAY_TOKEN)."
Write-Host "It will not be shown as you type."
$secure = Read-Host "CRM_RELAY_TOKEN" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $token = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }

if (-not $token) { Write-Error "Nothing was entered." }
$token = $token.Trim()

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

Write-Host ""
Write-Host "CRM_RELAY_TOKEN written to gateway\.env"
Write-Host "fingerprint: $fingerprint    (must match the office PC's)"
Write-Host ""
Write-Host "Restart the gateway, then ask Tachikoma: 「◯◯さんの墓所どこ？」"
