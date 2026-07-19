# Tachikoma Claude Code notifier (Step 1)

This component is one-way only: Claude Code hooks send status events to a
local authenticated HTTP endpoint, which normalizes them and speaks a short
Japanese phrase on the PC. It never approves, denies, or modifies Claude Code
tool calls. Even Terminal is not used.

## Start on Windows

Open PowerShell and set a local-only token. Do not put the token in a settings
file or commit it:

```powershell
$env:TACHIKOMA_NOTIFY_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python tachikoma_notifier/notifier.py
```

Use `--log-only` when testing without a Japanese SAPI voice:

```powershell
python tachikoma_notifier/notifier.py --log-only
```

The server listens on `127.0.0.1:8787`. `/health` is a read-only liveness
check; `/events` requires `Authorization: Bearer <token>`.

## Claude Code hooks

Copy `settings.example.json` into the user-level Claude settings file:

```text
C:\Users\mylit\.claude\settings.json
```

Merge its `hooks` object with existing settings; do not overwrite unrelated
settings. Claude Code expands `$TACHIKOMA_NOTIFY_TOKEN` only because the
example explicitly lists it under `allowedEnvVars`.

The payloads are normalized as follows:

| Hook | Condition | State | Speech |
|---|---|---|---|
| Notification | `notification_type=permission_prompt` | approval_needed | Claude Codeが承認待ちだよ |
| Stop | no background task/cron remains | completed | タスクが終わったよ |
| PostToolUseFailure / StopFailure | failure | error | エラーが出たみたい |

Repeated events for the same session/state are suppressed for five seconds.
Stop events with active background tasks or scheduled wakeups are ignored to
avoid announcing completion too early.

## Future StackChan output

The output boundary is `SpeechSink`. Replace `WindowsSpeechSink` with a
StackChan sink that forwards a normalized event to the existing authenticated
Gateway. Keep normalization, authentication, and deduplication unchanged.

Permission relay and voice approval are intentionally not implemented in this
Step.
