param(
    [string]$SettingsPath = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.claude\settings.json')
)

$ErrorActionPreference = 'Stop'
$hooks = @'
{
  "Notification": [{"matcher":"permission_prompt","hooks":[{"type":"http","url":"http://127.0.0.1:8787/events","headers":{"Authorization":"Bearer $TACHIKOMA_NOTIFY_TOKEN"},"allowedEnvVars":["TACHIKOMA_NOTIFY_TOKEN"],"timeout":5}]}],
  "Stop": [{"hooks":[{"type":"http","url":"http://127.0.0.1:8787/events","headers":{"Authorization":"Bearer $TACHIKOMA_NOTIFY_TOKEN"},"allowedEnvVars":["TACHIKOMA_NOTIFY_TOKEN"],"timeout":5}]}],
  "PostToolUseFailure": [{"hooks":[{"type":"http","url":"http://127.0.0.1:8787/events","headers":{"Authorization":"Bearer $TACHIKOMA_NOTIFY_TOKEN"},"allowedEnvVars":["TACHIKOMA_NOTIFY_TOKEN"],"timeout":5}]}],
  "StopFailure": [{"hooks":[{"type":"http","url":"http://127.0.0.1:8787/events","headers":{"Authorization":"Bearer $TACHIKOMA_NOTIFY_TOKEN"},"allowedEnvVars":["TACHIKOMA_NOTIFY_TOKEN"],"timeout":5}]}]
}
'@ | ConvertFrom-Json

$directory = Split-Path -Parent $SettingsPath
New-Item -ItemType Directory -Force -Path $directory | Out-Null
if (Test-Path -LiteralPath $SettingsPath) {
    $settings = Get-Content -Raw -LiteralPath $SettingsPath | ConvertFrom-Json
    if ($null -ne $settings.hooks) {
        throw "hooks already exists; merge manually to avoid overwriting existing hooks"
    }
    $backup = "$SettingsPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item -LiteralPath $SettingsPath -Destination $backup -Force
} else {
    $settings = [pscustomobject]@{}
    $backup = $null
}

$settings | Add-Member -NotePropertyName hooks -NotePropertyValue $hooks
$temp = "$SettingsPath.tmp-$PID"
$settings | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $temp -Encoding UTF8
Move-Item -LiteralPath $temp -Destination $SettingsPath -Force

if ($backup) {
    Write-Output "Claude hooks installed. Backup created: $backup"
} else {
    Write-Output "Claude hooks installed: $SettingsPath"
}
Write-Output "Set TACHIKOMA_NOTIFY_TOKEN in the environment before launching Claude Code."
