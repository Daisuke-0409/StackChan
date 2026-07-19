# Tachikoma 共通イベント通知基盤

Step 1.5では、Claude Code固有のHook JSONを、将来のCodex・CI・StackChan
などでも利用できる`TachikomaEvent`へ変換します。現在実装している入力は
Claude Codeだけです。通知は片方向で、Permission relayや音声承認は行いません。

```text
Claude Code Hook JSON
  -> ClaudeCodeAdapter
  -> TachikomaEvent
  -> EventRouter / Deduplicator
  -> SpeechFormatter
  -> SpeechSink
```

## Windowsでの起動

トークンはプロセス環境にだけ設定し、settings.jsonやソースへ保存しません。

```powershell
$env:TACHIKOMA_NOTIFY_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python tachikoma_notifier/notifier.py
```

音声を使わずログだけ確認する場合:

```powershell
python tachikoma_notifier/notifier.py --log-only
```

サーバーは`127.0.0.1:8787`だけで待ち受けます。`GET /health`は認証不要の
死活確認、`POST /events`は`Authorization: Bearer <token>`が必要です。

## 共通イベント

`TachikomaEvent`は`schema_version="1.0"`を必須とし、UUIDの`event_id`とUTC
ISO 8601の`occurred_at`を持ちます。`to_dict()`と`to_json()`で安全にJSON化できます。

```json
{
  "schema_version": "1.0",
  "event_id": "uuid",
  "source": "claude_code",
  "event_type": "approval_needed",
  "severity": "warning",
  "occurred_at": "2026-07-19T04:30:00.000Z",
  "session_id": "session-or-null",
  "project_id": null,
  "device_id": null,
  "title": "承認待ち",
  "message": "承認待ち",
  "raw_event_name": "Notification",
  "requires_action": true,
  "dedupe_key": "v1:sha256",
  "metadata": {"notification_type": "permission_prompt"}
}
```

### EventSource

`claude_code`、`codex`、`gemini_cli`、`github_actions`、`build_system`、
`stackchan`、`system`を定義しています。現時点で実装済みのAdapterは
`ClaudeCodeAdapter`だけです。

### EventType

`approval_needed`、`task_started`、`task_completed`、`task_failed`、
`tool_started`、`tool_completed`、`tool_failed`、`blocked`、`waiting`、
`error`、`info`を定義しています。

### EventSeverity

`info`、`warning`、`error`、`critical`を定義しています。

## Claude Code変換表

| Hook | 条件 | 共通イベント | 音声 |
|---|---|---|---|
| Notification | `notification_type=permission_prompt` | `approval_needed` / `warning` | Claude Codeが承認待ちだよ |
| Stop | background task/cronなし | `task_completed` / `info` | タスクが終わったよ |
| PostToolUseFailure | 失敗 | `tool_failed` / `error` | ツールの実行でエラーが出たみたい |
| StopFailure | 失敗 | `task_failed` / `error` | タスクが失敗したみたい |
| Notification | 明示的なエラー種別 | `error` / `error` | エラーが出たみたい |

background taskまたはsession cronが残るStopは、完了通知に変換しません。
未知のイベントは安全に無視します。

## デデュープ

`event_id`ではなく、`source`、`session_id`、`event_type`、正規化済みmessageから
SHA-256の`dedupe_key`を生成します。同じキーは5秒以内に1回だけ読み上げ、
5秒経過後は再通知します。

## 出力先

`SpeechSink`が出力境界です。現在は以下を維持しています。

- `WindowsSpeechSink`: Windows PowerShell/System.Speechによる日本語TTS
- `LogSpeechSink`: TTS失敗時または`--log-only`のログフォールバック

将来StackChanへ出力する場合は、`StackChanSpeechSink`を追加し、
`EventRouter`へ渡すだけで差し替えられます。イベント変換、認証、デデュープは
変更しません。

新しい入力元は`adapters/base.py`のAdapter契約に沿って、例えば
`adapters/codex.py`を追加します。Adapterは共通`TachikomaEvent`を返し、
Router以降は共有します。

## セキュリティと入力検証

- JSONはオブジェクトであること、`hook_event_name`、session_id、message長を検証
- `last_assistant_message`は長さだけ検証し、内容を読み込まない
- transcript_pathの内容を読み込まない
- metadataは許可された小さな値だけに制限し、token、API key、認証情報、transcript等のキーを除外
- 生Hook JSON、会話全文、Bearer token、APIキーをログへ出さない
- 外部公開、Tailscale、クラウド送信を行わない

## Claude Code Hooks

既存の`C:\Users\mylit\.claude\settings.json`と`install_hooks.ps1`の仕様は変更しません。
Hook設定は`$TACHIKOMA_NOTIFY_TOKEN`を`allowedEnvVars`経由で参照します。

Permission relay、声による承認、Claude CodeへのYes/No送信は未実装です。
