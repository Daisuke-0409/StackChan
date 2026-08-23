# Starts the local speech recogniser on the machine that holds the gateway.
#
# The robot's hearing is done today by a general model asked to transcribe.
# This replaces it with a recogniser. See gateway\local_stt.py for why, and
# for what changes as a result.
#
#   powershell -ExecutionPolicy Bypass -File gateway\run_local_stt.ps1
#
# First run only:
#   pip install faster-whisper
# The model downloads on first start (a few hundred MB) and is cached.
#
# Settings come from gateway\.env.local_stt if it exists (gitignored).
# Nothing in it is a credential: this process holds no keys, reaches no
# network, and answers only to this machine.

$ErrorActionPreference = "Stop"
$gatewayDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$firmwareDir = Split-Path -Parent $gatewayDir
$envFile = Join-Path $gatewayDir ".env.local_stt"

if (Test-Path $envFile) {
    # -Encoding UTF8, or PowerShell 5.1 reads the file as ANSI and the
    # Japanese vocabulary arrives as mojibake -- which, fed to the decoder
    # as its bias, silenced every transcription on 2026-08-21.
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
        $trimmed = $line.Trim()
        if ($trimmed -eq "" -or $trimmed.StartsWith("#")) { continue }
        $split = $trimmed.IndexOf("=")
        if ($split -lt 1) { continue }
        $name = $trimmed.Substring(0, $split).Trim()
        $value = $trimmed.Substring($split + 1).Trim().Trim('"')
        Set-Item -Path "Env:$name" -Value $value
    }
    Write-Host "settings: $envFile"
} else {
    Write-Host "settings: none ($envFile not found; using defaults)"
}

# The same interpreter rule as run_gateway.ps1, and for the same reason:
# two pythons live on this machine, and the bare name resolves to the one
# without the packages (relearned 2026-08-21, the launcher's first run at
# home). The probe answers on stdout because stderr becomes an ErrorRecord
# under Stop and kills the launcher before it can try the next candidate.
$pythonCandidates = @()
if ($env:TACHIKOMA_PYTHON) { $pythonCandidates += $env:TACHIKOMA_PYTHON }
$pythonCandidates += (Get-Command python.exe -All -ErrorAction SilentlyContinue |
                      Select-Object -ExpandProperty Source)

$probe = @"
try:
    import faster_whisper
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
    Write-Error "No python with faster-whisper found. Run: pip install faster-whisper (checked: $($pythonCandidates -join ', '))"
}
Write-Host "python: $python"

Set-Location $firmwareDir
$ErrorActionPreference = "Continue"
& $python -u gateway/local_stt.py
