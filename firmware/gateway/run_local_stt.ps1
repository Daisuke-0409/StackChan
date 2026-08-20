# Starts the local speech recogniser on the machine that holds the gateway.
#
# The robot's hearing is done today by a general model asked to transcribe.
# This replaces it with a recogniser. See gateway\local_stt.py for why, and
# for what changes as a result.
#
#   powershell -File gateway\run_local_stt.ps1
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
    foreach ($line in Get-Content $envFile) {
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

python -c "import faster_whisper" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "faster-whisper is not installed. Run: pip install faster-whisper"
}

Set-Location $firmwareDir
python -u gateway/local_stt.py
