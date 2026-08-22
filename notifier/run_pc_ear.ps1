# Starts the PC ear: a desk microphone that speaks through Tachikoma.
#
#   powershell -File notifier\run_pc_ear.ps1
#
# Loads DEVICE_TOKEN from firmware\gateway\.env (same parsing as
# run_gateway.ps1), then runs pc_ear.py on a python that has sounddevice.
# The gateway must be running; the robot must be on for the reply to be heard.
#
# Overrides (set in gateway\.env or the environment):
#   TACHIKOMA_EAR_INPUT_DEVICE  substring of the mic name   (default UGREEN)
#   TACHIKOMA_EAR_GATEWAY       gateway URL                 (default http://127.0.0.1:8080)
#   TACHIKOMA_EAR_DEVICE_ID     whose mouth answers         (default 80456B4DE03C)
#   TACHIKOMA_EAR_SPEECH_RMS / TACHIKOMA_EAR_SILENCE_RMS    skip calibration

$ErrorActionPreference = "Stop"
$notifierDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $notifierDir
$envFile = Join-Path $repoRoot "firmware\gateway\.env"

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

# Same probe as run_gateway.ps1, for the same reason: PATH order changed once
# and silently picked an interpreter without the audio stack.
$probe = @"
try:
    import sounddevice
    print('HAVE_DEPS')
except Exception:
    print('NO_DEPS')
"@

$python = $null
foreach ($candidate in (Get-Command python.exe -All -ErrorAction SilentlyContinue |
                        Select-Object -ExpandProperty Source)) {
    if ((& $candidate -c $probe) -contains 'HAVE_DEPS') { $python = $candidate; break }
}
if (-not $python) {
    Write-Error "No python with sounddevice found. Install it: python -m pip install sounddevice"
}

Write-Host "python: $python"
Set-Location $repoRoot
$env:PYTHONPATH = Join-Path $repoRoot "notifier"
$ErrorActionPreference = "Continue"   # native stderr must not kill the ear
& $python -u -m tachikoma_notifier.pc_ear
