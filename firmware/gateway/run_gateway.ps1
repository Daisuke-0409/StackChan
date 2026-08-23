# Starts the Tachikoma Gateway with settings from gateway/.env (gitignored).
#
# The device reaches this process over the LAN, so GATEWAY_HOST must be
# 0.0.0.0 -- server.py's built-in default (127.0.0.1) accepts only local
# connections and leaves the device with "Connection reset by peer".
#
#   powershell -ExecutionPolicy Bypass -File gateway\run_gateway.ps1
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

# Which python, decided here rather than left to PATH order. Speaker
# identification needs torch, librosa and numpy; the gateway itself does not,
# so a python without them starts perfectly well and simply stops recognising
# anybody -- and then Tachikoma cannot reach the master-only memories and
# appears to have forgotten who it is talking to. That is what happened on
# 2026-08-14: a second interpreter (3.9) arrived ahead of the one the packages
# are installed in (3.11), and the only sign was `voice=False` in one log line.
#
# Set TACHIKOMA_PYTHON in .env to override.
$pythonCandidates = @()
if ($env:TACHIKOMA_PYTHON) { $pythonCandidates += $env:TACHIKOMA_PYTHON }
$pythonCandidates += (Get-Command python.exe -All -ErrorAction SilentlyContinue |
                      Select-Object -ExpandProperty Source)

# The probe catches its own ImportError and answers on stdout. Letting python
# fail normally would write to stderr, and PowerShell turns a native command's
# stderr into an ErrorRecord -- which, with $ErrorActionPreference = "Stop" at
# the top of this file, aborts the launcher instead of trying the next
# interpreter.
$probe = @"
try:
    import numpy, torch, librosa
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
    # Still start: a gateway that talks without recognising faces beats no
    # gateway at all, and the alternative is silence with no explanation.
    $python = if ($pythonCandidates.Count -gt 0) { $pythonCandidates[0] } else { "python" }
    Write-Warning "No python with numpy/torch/librosa found. Speaker identification will be OFF,"
    Write-Warning "which means master-only memories stay hidden and Tachikoma will not know who"
    Write-Warning "it is speaking to. Checked: $($pythonCandidates -join ', ')"
    Write-Warning "Install them there, or set TACHIKOMA_PYTHON in gateway\.env."
}

Write-Host "python: $python"

# "Stop" is right for the configuration checks above -- a missing token should
# not start a gateway that rejects everything. It is wrong for the server
# itself. PowerShell turns a native command's stderr into an ErrorRecord, and
# under "Stop" the first such line kills the launcher: OpenCV writes a harmless
# setPreferableTarget notice to stderr while the face model loads, and that one
# line was enough to take the whole gateway down a second or two after it had
# started listening. It only appeared once the interpreter with OpenCV in it
# was the one being chosen.
$ErrorActionPreference = "Continue"
& $python -u gateway/server.py
