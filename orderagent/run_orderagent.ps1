# Starts the mobile-order agent (orderagent/server.py) on 127.0.0.1:8766.
#
#   powershell -File orderagent\run_orderagent.ps1
#
# Settings come from firmware\gateway\.env (the same file the gateway uses)
# so DEVICE_TOKEN never needs to exist twice. Payment stays a dry run unless
# ORDER_PAYMENT_ENABLED=1 is set there explicitly -- do not set it until the
# checkout flow has been walked through together on a real 少額 order.

$ErrorActionPreference = "Stop"
$agentDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoDir = Split-Path -Parent $agentDir
$envFile = Join-Path $repoDir "firmware\gateway\.env"

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
} else {
    Write-Warning "$envFile not found -- announcements will be rejected (no DEVICE_TOKEN)."
}

# The same interpreter rule as the gateway launcher: playwright must import,
# otherwise every job would fail at the cart step with no clear reason.
$pythonCandidates = @()
if ($env:TACHIKOMA_PYTHON) { $pythonCandidates += $env:TACHIKOMA_PYTHON }
$pythonCandidates += (Get-Command python.exe -All -ErrorAction SilentlyContinue |
                      Select-Object -ExpandProperty Source)

$probe = @"
try:
    import playwright
    print('HAVE_DEPS')
except Exception:
    print('NO_DEPS')
"@

$python = $null
foreach ($candidate in $pythonCandidates) {
    if (-not (Test-Path $candidate)) { continue }
    if ((& $candidate -c $probe) -contains 'HAVE_DEPS') { $python = $candidate; break }
}
if (-not $python) {
    Write-Error "No python with playwright found. pip install playwright && playwright install chromium"
}

Write-Host "python: $python"
Write-Host "payment_enabled: $($env:ORDER_PAYMENT_ENABLED -eq '1')"

Set-Location $repoDir
$ErrorActionPreference = "Continue"
& $python -u -m orderagent.server
