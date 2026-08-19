# Puts the CRM's lookup token into gateway\.env.crm_relay without showing it.
#
# The token already exists: the CRM generates it into its own config.json.
# Nobody needs to read it, retype it, or paste it into a chat window to get
# it here -- this moves it from one file to the other and prints only a
# fingerprint, so you can confirm both ends match without either end being
# displayed.
#
# That restraint is the point. On 2026-07-28 a Gmail app password leaked by
# being printed while showing that same config.json, because the masking
# filter did not reach a nested key. The safe way to handle a secret is not
# a better filter; it is never rendering it.
#
#   powershell -File gateway\setup_crm_token.ps1
#
# If the CRM is somewhere this machine cannot read -- after the move to the
# NAS, most likely -- use -Prompt and paste it into a hidden field instead:
#
#   powershell -File gateway\setup_crm_token.ps1 -Prompt

# Note the absence of a Japanese literal for the CRM folder. A .ps1 saved as
# UTF-8 without a BOM is read as Shift-JIS by Windows PowerShell 5.1, which
# mangles one -- and a mangled default path fails as "not found", which is a
# confusing way to learn about file encodings. The folder happens to contain
# "CRM" in ASCII, so it can be found without spelling its name.
[CmdletBinding()]
param(
    [string]$CrmConfig,
    [switch]$Prompt
)

$ErrorActionPreference = "Stop"
$gatewayDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $gatewayDir ".env.crm_relay"
$example = Join-Path $gatewayDir ".env.crm_relay.example"

function Get-Fingerprint([string]$value) {
    # Enough to compare two machines, useless to anyone who sees it.
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $bytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($value))
    return ($bytes[0..3] | ForEach-Object { $_.ToString("x2") }) -join ""
}

# --- get the token -------------------------------------------------------
$token = $null

if ($Prompt) {
    $secure = Read-Host "Paste the CRM tachikoma_token" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $token = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
} else {
    if (-not $CrmConfig) {
        $found = Get-ChildItem $env:USERPROFILE -Directory -Filter "*CRM*" -ErrorAction SilentlyContinue |
                 ForEach-Object { Join-Path $_.FullName "config.json" } |
                 Where-Object { Test-Path $_ } |
                 Select-Object -First 1
        if (-not $found) {
            Write-Error "Could not find the CRM's config.json under $env:USERPROFILE. Pass -CrmConfig <path>, or use -Prompt to paste the token instead."
        }
        $CrmConfig = $found
    }
    if (-not (Test-Path $CrmConfig)) {
        Write-Error "CRM config not found at $CrmConfig. Pass -CrmConfig <path>, or use -Prompt to paste the token instead."
    }
    Write-Host "reading the token from the CRM's config.json (never displayed)"
    # utf-8-sig: PowerShell writes UTF-8 with a BOM, and a plain UTF-8 read
    # chokes on it at the first character.
    $raw = [System.IO.File]::ReadAllText($CrmConfig, [System.Text.Encoding]::UTF8)
    try { $config = $raw | ConvertFrom-Json } catch { Write-Error "Could not parse $CrmConfig as JSON." }
    $token = $config.tachikoma_token
    if (-not $token) {
        Write-Error "config.json has no 'tachikoma_token'. Start the CRM once so it generates one, or use -Prompt."
    }
}

if (-not $token) { Write-Error "No token was provided." }

# --- write it ------------------------------------------------------------
if (-not (Test-Path $envFile)) {
    if (-not (Test-Path $example)) { Write-Error "$example is missing; cannot create $envFile." }
    Copy-Item $example $envFile
    Write-Host "created .env.crm_relay from the example"
}

$lines = [System.IO.File]::ReadAllLines($envFile)
$updated = @()
$seen = @{}
foreach ($line in $lines) {
    $matched = $false
    foreach ($key in @("CRM_RELAY_TOKEN", "CRM_TOKEN")) {
        if ($line -match "^\s*$key\s*=") {
            $updated += "$key=$token"
            $seen[$key] = $true
            $matched = $true
            break
        }
    }
    if (-not $matched) { $updated += $line }
}
foreach ($key in @("CRM_RELAY_TOKEN", "CRM_TOKEN")) {
    if (-not $seen[$key]) { $updated += "$key=$token" }
}

# No BOM: this file is read by both PowerShell and Python, and Python's
# plain utf-8 read fails on one.
$noBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($envFile, $updated, $noBom)

$fingerprint = Get-Fingerprint $token
$token = $null

Write-Host ""
Write-Host "CRM_RELAY_TOKEN and CRM_TOKEN written to gateway\.env.crm_relay"
Write-Host "fingerprint: $fingerprint    (compare with the CRM side; the token itself is never printed)"
Write-Host ""
Write-Host "gateway\.env.crm_relay is gitignored. Start the relay with:"
Write-Host "  powershell -File gateway\run_crm_relay.ps1"
