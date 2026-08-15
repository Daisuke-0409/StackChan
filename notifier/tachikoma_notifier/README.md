# Tachikoma 共通イベント通知基盤

作業時の確認ポリシー（確認回数の上限、git push/実機flash/シークレット
コミット前は必ず確認する例外）はリポジトリ直下の`CLAUDE.md`を参照。

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

`SpeechSink`が出力境界です。`notifier.py`の`_build_sink()`が起動時に以下から選びます。

- `LogSpeechSink`: `--log-only`指定時、または実機TTS失敗時のログフォールバック
- `StackChanSpeechSink`: `TACHIKOMA_STACKCHAN_SPEAK_URL` / `TACHIKOMA_STACKCHAN_DEVICE_TOKEN` /
  `TACHIKOMA_STACKCHAN_DEVICE_ID`の3つが**すべて**環境変数に設定されている場合のみ選択され、
  タチコマ実機のスピーカーへ出力します（Gatewayの`/v1/speak`経由）。失敗時は
  `WindowsSpeechSink`へフォールバックします
- `WindowsSpeechSink`: 上記3変数が未設定、または一部のみ設定の場合の既定値。
  PC自身のWindows PowerShell/System.Speechによる日本語TTS

イベント変換、認証、デデュープは出力先の選択に関わらず変更しません。

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

## Step 2.1: approval internal models

`approvals.py` adds provider-neutral `ApprovalRequest` and
`ApprovalDecision` models without adding a Hook transport or permission
relay. The models are frozen dataclasses with strict JSON round-tripping,
UUID4 identifiers for approval and decision records, UTC ISO-8601 timestamps, bounded safe summaries, and
fail-closed enum and field validation.

`ApprovalStatus` exposes pending, announcement, confirmation, terminal, and
relay-failure states. `ApprovalRequest.transition_to()` enforces the allowed
state graph, while `expire_if_needed()` uses an injected UTC time for
deterministic timeout checks. Initial choices are only `approve_once` and
`reject`; persistent or session-wide approval is intentionally absent.

`safe_summary` is normalized and limited to 240 characters. This keeps voice
prompts and future relay payloads short while preventing accidental retention
of a whole command, file body, or conversation.

`make_approval_dedupe_key()` hashes source, session, action, tool, and the
normalized safe summary. It never uses `approval_id` or an event ID.
`DecisionReplayGuard` is an in-memory one-shot guard for a future relay; it
stores only opaque IDs and does not persist requests.

Metadata is recursively bounded and removes sensitive keys such as tokens,
authorization headers, transcript paths, tool input, commands, and file
contents. Non-finite numbers and non-JSON metadata values are rejected. Raw
Hook JSON, transcripts, commands, and credentials are not accepted as model
data. Unknown JSON fields are rejected to avoid silently accepting a future
permission-control field.

Step 2.1 does not modify `settings.json`, `install_hooks.ps1`, the existing
HTTP API, `TachikomaEvent`, Claude adapters, voice input, StackChan, or any
permission result transport.

## Step 2.2: in-memory approval request store

`approval_store.py` adds `ApprovalRequestStore`, a bounded process-local
store. The default capacity is 1000 requests and the default terminal
dedupe-retention window is 300 seconds; both are configurable. No SQLite,
Redis, external database, filesystem persistence, network I/O, or logging is
used.

The public operations are `register`, `get`, `require`, `list_pending`,
`list_by_status`, `apply_decision`, `expire_due`, `remove_terminal_before`,
`cleanup`, and `count`. Registration accepts validated active requests only.
An active duplicate `dedupe_key` returns the existing request. A terminal
duplicate is reused within the retention window and can be replaced after
that window or explicit cleanup. `approval_id` remains unique.

`expire_due()` uses `now >= expires_at`, accepts an injected clock for tests,
updates only active requests, and is idempotent. `apply_decision()` performs
validation, state transition, and one-shot decision consumption atomically.
`approve_once` requires `awaiting_confirmation`; `invalid` is system-only.
Replay identifiers are bounded to the configured store capacity multiplier.

All operations are protected by a process-local `RLock`. Internal
`stored_at`, `updated_at`, and `terminal_at` timestamps are store metadata and
are not added to `ApprovalRequest` or exposed as Hook data.

`ApprovalRequestStore.advance_status()` was added alongside Step 2.3 to move a
request through the non-decision relay lifecycle (`announced`,
`awaiting_confirmation`, `relay_failed`). Decision outcomes (`approved`,
`rejected`, `cancelled`, `expired`, `invalid`) still only happen through
`apply_decision`, so every terminal state stays decision-audited.
`approvals.py`'s transition graph was extended so `relay_failed` is reachable
from `pending` and `announced`, not only `awaiting_confirmation`, since a
relay can fail before any confirmation is ever awaited.

## Step 2.3: simulated PermissionRelay

`permission_relay.py` adds `SimulatedPermissionRelay`, which drives one
`ApprovalRequest` through `submit -> announce -> begin_confirmation ->
resolve` on top of `ApprovalRequestStore`. It takes two caller-supplied
callables: an announcer (`ApprovalRequest -> bool`, e.g. a TTS `speak()`) and
a decision provider (`ApprovalRequest -> ApprovalChoice`) that stands in for a
future confirmation source.

This is a mock for internal testing, not a production relay: there is no
voice/microphone input, no StackChan or Even G2 transport, and no Claude Code
Hook response transport. Nothing here sends a decision back to Claude Code.

Fail-closed behavior:

- An announcer that raises or returns a falsy value marks the request
  `relay_failed` and the decision provider is never called.
- A decision provider that raises, or returns anything other than
  `ApprovalChoice.APPROVE_ONCE` / `ApprovalChoice.REJECT`, resolves to a
  system-issued `reject` (`ApprovalActor.SYSTEM`), never an implicit approval.
- `resolve()` always tags its `ApprovalDecision` with
  `ConfirmationMethod.SIMULATED`, so simulated decisions are distinguishable
  from a real relay's output in any future audit trail.
- Expiry, replay, and already-finalized checks are enforced by
  `ApprovalRequestStore`; `SimulatedPermissionRelay` does not duplicate or
  weaken them.

Step 2.3 does not implement `approve_session`, "always allow", or any other
persistent-approval choice, and does not modify `settings.json`,
`install_hooks.ps1`, the existing HTTP API, `TachikomaEvent`, Claude adapters,
voice input, or StackChan.

## Step 2.4: does an official Claude Code approval channel exist? (investigation only)

Yes. Claude Code's `PreToolUse` and `PermissionRequest` hooks can return a
real permission decision by printing JSON to stdout and exiting 0:

- `PreToolUse`: `{"hookSpecificOutput": {"permissionDecision": "allow" | "deny" | "ask", "permissionDecisionReason": "..."}}`
- `PermissionRequest`: `{"hookSpecificOutput": {"decision": {"behavior": "allow" | "deny"}}}`

registered under `hooks.PreToolUse` (with a `matcher`) in `settings.json`.
Exit codes alone cannot carry a decision; exit 2 only signals a block, not a
choice.

The important constraint: this channel is synchronous. The hook process must
return its decision before the tool call proceeds, bounded by that hook
entry's `timeout`. There is no official "pause and receive a decision from an
external async source" primitive — a hook command could itself block while
polling an external system for a decision, but that is a property of the
hook script, not something Claude Code provides.

This step is investigation only. No hook script, `settings.json` change, or
real connection between this relay and Claude Code's permission flow has been
made.

## Step 2.5: PC manual approval CLI

`manual_approval_cli.py` adds `ManualApprovalCli`, a small interactive
front end over `ApprovalRequestStore`: it lists the single pending request
(or asks the caller to pick one by id when several are pending), shows
`approve_once` / `reject` as the only two choices, and applies exactly one
decision through `apply_decision`. A second decision on the same request
raises `ApprovalAlreadyFinalizedError` or `DecisionReplayError`, the same as
everywhere else in the store — the CLI does not add its own replay
tracking, since duplicating it would risk drifting out of sync with the
store's. `input_fn` / `output_fn` are injectable so the whole flow is
testable without a real terminal.

## Step 2.6: StackChan-voiced approval announcement

`voice_announce.py` adds `build_speech_announcer()`, which adapts an
existing `SpeechSink.speak`-shaped callable (the same `WindowsSpeechSink` /
`LogSpeechSink` already used by `notifier.py`) into the `Announcer` shape
`SimulatedPermissionRelay` expects. It only speaks one formatted sentence
naming the tool and safe summary of a pending request. No microphone, no
response channel, and no StackChan/Even G2 hardware connection are added —
this is the same PC-side TTS voice the notifier already uses for ordinary
status events.

## Step 2.7: cloud STT dry run

`cloud_transcriber.py` adds `CloudTranscriber`, a stdlib-only (`urllib`),
OpenAI-compatible cloud speech-to-text client, and `run_dry_run()`, which
reads one recorded audio file, transcribes it, and logs the recognized text
through `DryRunTranscriptionLog` — nothing else. The API key is read only
from `TACHIKOMA_STT_API_KEY` (or passed explicitly) and is never logged or
included in exception messages; `TranscriptionError` always carries a fixed,
safe message instead of the raw response body. The HTTP transport is
injectable (`opener`), so tests exercise request construction, error
handling, and the dry-run log entirely without real network calls. This
step does not send anything to Claude Code and does not apply any approval
decision — recognized text only reaches a log line, and, separately, the
strictly-gated path in Step 2.8.

**On the cloud-STT choice:** this was a deliberate, informed choice by the
user, made with an understanding of the tradeoff against an offline/local
model. As of Step 2.7, no real microphone input and no real recorded voice
is ever sent anywhere — only a dry run against recorded/mock audio. When
Phase 5 wires an actual microphone into StackChan, that microphone may pick
up voice from an in-progress customer interaction, not just approval
confirmations, so the choice of cloud STT must be revisited at that point,
not carried forward automatically.

## Step 2.8: strictly-gated voice `approve_once`

`voice_approval_gate.py` adds `VoiceApprovalGate.try_approve()`, which
applies a voice `approve_once` decision only when **every** one of these
holds, otherwise raising `VoiceApprovalDenied` and applying nothing:

1. Exactly one request is currently pending.
2. The caller-supplied `approval_id` matches that single pending request.
3. `risk_level` is `low`.
4. The request is already `awaiting_confirmation` (already announced).
5. `tool_name` is on the explicit allowlist `SAFE_VOICE_TOOL_NAMES`
   (`Read`, `Glob`, `Grep`). This is how "no file deletion, no git push, no
   external send, no credential access, no system config change, no
   arbitrary command execution" is enforced mechanically: those categories
   are simply never on the allowlist, rather than detected by inspecting
   free-text tool input, which is deliberately stripped from metadata
   elsewhere and can't be reliably classified after the fact.
6. The request has not expired — enforced by `list_pending()` itself (see
   Step 2.3/2.2 notes above), not a separate check here.
7. The recognized utterance, after NFKC normalization and trimming of
   whitespace/trailing punctuation, exactly matches one of a small fixed
   set in `EXPLICIT_APPROVAL_PHRASES`. This is an exact-match allowlist, not
   substring/keyword matching, so a misrecognized negation
   (e.g. `"承認しません"`) cannot match just because it contains `"承認"`.

There is no voice-driven `reject` path — rejection still only happens
through expiry or the manual CLI (Step 2.5). One-shot application (replay
guard + terminal transition) is still entirely `ApprovalRequestStore`'s
job; this module only decides whether to call `apply_decision` at all.

None of Steps 2.5–2.8 implement `approve_session`, "always allow", or any
persistent-approval choice; none modify `settings.json`,
`install_hooks.ps1`, the existing HTTP API, or send anything to Claude Code;
none connect to a real microphone or StackChan/Even G2 hardware.

## Step 3: CodexAdapter

`adapters/codex.py` adds `CodexAdapter`, following `ClaudeCodeAdapter`'s
`can_handle` / `normalize` shape. OpenAI Codex CLI's hook system (v0.114+)
sends one JSON object per hook on stdin using a `hook_event_name` /
`session_id` envelope that closely parallels Claude Code's own. Confirmed
hook event names: `SessionStart`, `SubagentStart`, `PreToolUse`,
`PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`,
`UserPromptSubmit`, `SubagentStop`, `Stop`.

Only two events have a confidently-confirmed, unambiguous mapping and are
implemented: `PermissionRequest` → `approval_needed`, `Stop` →
`task_completed`. `PreToolUse` / `PostToolUse` tool-failure detection is
**not** implemented — public documentation doesn't confirm a stable
success/failure field shape for `tool_response`, and guessing at an
unconfirmed field risks silently misclassifying events. Extend this once
verified against a live Codex instance. Everything else is safely ignored.

## Step 4: GeminiCliAdapter

`adapters/gemini_cli.py` adds `GeminiCliAdapter`, same shape again. Gemini
CLI's hook envelope (`session_id`, `transcript_path`, `cwd`,
`hook_event_name`, `timestamp`) and its `Notification` hook's fields
(`notification_type`, `message`, `details`) also parallel Claude Code's.

Only one mapping is implemented: `hook_event_name == "Notification"` with
`notification_type == "ToolPermission"` → `approval_needed`. Public
documentation references a separate "session complete" notification
concept, but does not confirm its exact `notification_type` value (or
whether it arrives via a hook at all, versus Gemini CLI's separate
experimental terminal-notification feature) — rather than guess,
`task_completed` / `task_failed` mappings are intentionally left
unimplemented pending verification against a live instance.

## Step 5: GitHub Actions / build notifications

`adapters/github_actions.py` adds `GitHubActionsAdapter`. GitHub delivers
its webhook event name via the `X-GitHub-Event` HTTP header, not inside the
JSON body, so this adapter expects whatever receives the raw webhook to
merge that header's value into the body as `github_event` before calling
it — that receiver is out of scope here.

Supported `github_event` values, using GitHub's standard (stable,
well-documented) webhook JSON shapes:

| `github_event` | condition | common event | title |
|---|---|---|---|
| `workflow_run` | `action=="completed"`, `conclusion=="success"` | `task_completed` | ビルド成功 |
| `workflow_run` | `action=="completed"`, `conclusion` in `{"failure","timed_out"}` | `task_failed` | ビルド失敗 |
| `check_run` | `action=="completed"`, `conclusion=="failure"` | `tool_failed` | テスト失敗 |
| `pull_request` | `action=="review_requested"` | `waiting` | レビュー待ち |
| `deployment_status` | `state=="success"` | `task_completed` | デプロイ完了 |
| `deployment_status` | `state` in `{"failure","error"}` | `task_failed` | デプロイ失敗 |

`repository.full_name` becomes `project_id`. Since these events have no
`session_id`, `project_id` is passed into `make_dedupe_key`'s session_id
slot instead, so the same message from two different repositories doesn't
collapse into one deduplicated event. Anything else, or a payload missing
the expected nested fields, is safely ignored — never raises.

## Step 6: multi-PC / multi-session tracking

`session_registry.py` adds `SessionRegistry`, a bounded, thread-safe,
process-local tracker, separate from every approval-related module (it
does not import `approval_store.py`, `permission_relay.py`, or
`voice_approval_gate.py`, and implements no approval/rejection logic).

Each session's identity is the tuple `(source, device_id, session_id,
project_id, workspace)` — `workspace` isn't part of `TachikomaEvent` itself,
so it's supplied by the caller alongside the event. `upsert_from_event()`
records the latest `status` (taken directly from `event.event_type.value`,
so no separate status vocabulary is invented) and `last_updated_at` (taken
from `event.occurred_at`). An event older than the currently recorded state
is ignored, so an out-of-order delivery can't regress a session's tracked
status. `priority` is caller-supplied and persists across updates unless
explicitly overridden on a later call.

Public operations: `upsert_from_event`, `get`, `list_all`,
`list_by_priority` (highest priority first, ties broken by most-recently-
updated), `remove_stale`, and `count`. Registering a genuinely new session
past `max_sessions` (default 500) raises `SessionRegistryCapacityError`;
updating an already-tracked session never does.

Steps 3–6 only generate and convert notifications. None of them touch
`ApprovalRequest`, `ApprovalDecision`, `ApprovalRequestStore`,
`SimulatedPermissionRelay`, or any voice-approval logic, and none modify
`notifier.py`, `routing.py`, `events.py`, `adapters/claude_code.py`, or
`install_hooks.ps1`.

## Step 7: Gemini + Voicebox conversational reply pipeline

**Gemini・Voiceboxとも実サービスで動作確認済み（2026-07-23）。**
Voiceboxは`localhost:17493`起動後に`/docs`・`/openapi.json`で実仕様を照合し、
`voicebox_synth.py`を実際のAPI形状（3ステップの非同期フロー、下記参照）に
合わせて書き直し済み。実プロファイル「タチコマ」
（`a0715b38-0a0c-487a-917f-255139f1ea1e`）でGemini応答→音声合成の
end-to-endも実行し、WAVファイル生成まで確認済み。

Three new, independent PC-side modules chain STT-recognized text into a
spoken reply on the physical StackChan, reusing the existing
`speak_queue`/`StackChanSpeechSink` delivery path already verified against
real hardware — no device-side (NVS/flash) changes were needed or made.

- `gemini_responder.py` — `GeminiResponder.generate_reply(user_text)` calls
  the Gemini `generateContent` REST API (stdlib `urllib` only, matching
  `cloud_transcriber.py`'s style) and returns a short Japanese reply, using
  a system prompt that gives it the Tachikoma personality (childlike
  curiosity + genuine intelligence, opinions that drift day to day, replies
  capped at two sentences). Requires `TACHIKOMA_GEMINI_API_KEY`; model is
  `TACHIKOMA_GEMINI_MODEL` (default `gemini-flash-latest` — `gemini-2.0-flash`,
  this file's original default, returned a 404 "no longer available" when
  verified live on 2026-07-23; `-latest` aliases track whatever Google
  currently recommends instead of a version number that will age out).
  The key is sent only via the `x-goog-api-key` header, never in the URL or
  body. **Verified live on 2026-07-23** with a real API key: returned a
  correctly-styled Japanese reply on the first call.
- `voicebox_synth.py` — `VoiceboxSynthesizer.synthesize(text)` drives the
  real (verified live) three-step Voicebox flow: `POST {base_url}/generate`
  with `{"text", "profile_id", "language", "engine"}` returns a JSON
  `GenerationResponse` (generation is asynchronous — status goes
  `loading_model` → `generating` → `completed`/`failed`, ~35s on a cold
  model); poll `GET {base_url}/history/{id}` until `completed`/`failed`;
  then `GET {base_url}/audio/{id}` returns the audio as WAV bytes directly
  (`Content-Type: audio/wav`, confirmed mono/16-bit/24000Hz — matches
  `windows_wave_synth.py`'s rate, so no resampling is needed). Raises the
  *same* `SpeechSynthesisError` type on any failure (network, `failed`
  status, or poll timeout) so it drops straight into
  `StackChanSpeechSink`'s `synthesizer` parameter unchanged. Requires
  `TACHIKOMA_VOICEBOX_BASE_URL` and `TACHIKOMA_VOICEBOX_PROFILE_ID`;
  `TACHIKOMA_VOICEBOX_API_KEY` is optional (omit for an unauthenticated
  local engine). Valid `engine` values are `qwen` (default),
  `qwen_custom_voice`, `luxtts`, `chatterbox`, `chatterbox_turbo`, `tada`,
  `kokoro` — not `qwen3-tts`, an earlier incorrect guess.
- `voicevox_synth.py` — `VoicevoxSynthesizer.synthesize(text)` drives a
  local VOICEVOX ENGINE instance (`localhost:50021` by default; the same
  two-step `audio_query`/`synthesis` REST flow, verified live). Fast
  (well under a second per call, CPU-only) and free. Default speaker is
  Zundamon / Normal (style id `3`; override with
  `TACHIKOMA_VOICEVOX_SPEAKER_ID`).
- `gemini_tts_synth.py` — `GeminiTtsSynthesizer.synthesize(text)` calls a
  Gemini native-TTS model (`generateContent` with
  `responseModalities: ["AUDIO"]`; default model
  `gemini-2.5-flash-preview-tts`, default voice `Zephyr` — chosen by ear
  after comparing 8 candidate voices against real Japanese text). No local service
  to run. Verified live: ~4s per call, ~91 audio + ~21 text tokens for a
  short two-sentence reply (see cost note below). A bare short phrase can
  make the model answer conversationally in text instead of speaking it —
  worked around by wrapping the input in an explicit "read this verbatim"
  instruction before sending. Response audio is raw 16-bit PCM
  (`audio/L16;codec=pcm;rate=24000`, base64-encoded) — no WAV header, so
  no `wave` parsing needed, just a straight base64 decode.
  **Cost estimate (unverified against Google's live pricing page — this
  session had no way to check it, treat as a rough order of magnitude
  only):** at roughly published Gemini 2.5 Flash Preview TTS rates, a
  ~100-token reply costs a small fraction of a US cent (well under ¥1).
  Confirm current pricing before relying on this for volume use.
- `conversation_pipeline.py` — `ConversationReplyPipeline.handle_utterance(user_text)`
  calls `GeminiResponder` then `sink.speak(reply.text)`; never raises
  (reply-generation failures are logged safely and return `False` without
  touching the sink). `build_tts_stackchan_sink()` returns the same
  `StackChanSpeechSink` class already proven end-to-end on real hardware,
  with the synthesizer picked by `TACHIKOMA_TTS_ENGINE`
  (`gemini` default, `voicevox`, or `voicebox`) injected in place of
  `windows_wave_synth.synthesize_wav_pcm` — endpoint/token/device_id still
  resolve from the same `TACHIKOMA_STACKCHAN_*` env vars `notifier.py`
  already uses, so nothing about the verified delivery path changes.
  `build_voicebox_stackchan_sink()` (Voicebox-only, no engine switch) is
  kept unchanged for anything still calling it directly.

What this step deliberately does **not** do: it does not decide how
recorded audio becomes STT text (that boundary stays at
`cloud_transcriber.TranscriptionResult.text`, or any other `str`), it does
not add a CLI entrypoint tying microphone capture to a reply, and it does
not modify `stackchan_speech_sink.py`, `windows_wave_synth.py`, or
`notifier.py`.

## R4: 承認リレーの配線 (approval_daemon + claude_hook_approval)

Claude Code がツールの許可を求める → タチコマが読み上げる → 大輔が
「はい/いいえ」で答える → 判定が Claude Code に返る。Step 2 で作った
承認基盤 (`approval_store` / `voice_approval_gate` ほか) を実フローに
繋いだのがこの2ファイル。

- `approval_daemon.py` — PC 常駐。`http://127.0.0.1:8378/approval`
  (ローカルホスト限定・変更不可) で PreToolUse hook JSON を受け、
  ApprovalRequest 登録 → 既存 SpeechSink 経路で読み上げ →
  デスクマイクで返事を聞く (pc_ear の区切り・キャリブレーション・
  `/v1/transcribe` を再利用) → `{"decision": "allow"|"deny"|"ask"}` を返す
- `claude_hook_approval.py` — hook クライアント。標準ライブラリのみで
  自己完結 (PYTHONPATH 不要)。stdin の hook JSON をデーモンへ転送し、
  allow/deny だけを stdout の hook 応答に変換する。ask と**あらゆる失敗**は
  「何も出力せず exit 0」= Claude Code の通常の許可プロンプトに落ちる

### 安全モデル (3文)

音声で許可できるのは `SAFE_VOICE_TOOL_NAMES` (Read / Glob / Grep) の
低リスク読み取り専用ツールだけで、それ以外は読み上げのみ・判定は必ず
PC 側の通常プロンプトに残る (「はい」と言っても `VoiceApprovalGate` が
拒否して "ask" になる)。何かが失敗したら — ゲートウェイ停止・マイク不在・
時間切れ・聞き取り不能・例外 — 結果は必ず "ask" であり、"allow" に
なる失敗経路は存在しない。"deny" は明確な「いいえ/だめ/やめて」、
"allow" は明確な「はい/いいよ/オッケー」かつ既存ゲートの全条件
(単一 pending・risk=low・ツール許可リスト・完全一致フレーズ) を
通過したときだけ適用される。

### 起動

```powershell
powershell -File notifier\run_approval_daemon.ps1
```

`run_pc_ear.ps1` と同じ流儀: `firmware\gateway\.env` を読み込み
(`DEVICE_TOKEN` ほか)、sounddevice を import できる python を探して
起動する。ゲートウェイと VOICEVOX が動いていること。読み上げ先は
notifier と同じ選択則 (`TACHIKOMA_STACKCHAN_*` 3変数が揃えば実機、
なければ Windows TTS)。`--log-only` で音声なしの動作確認ができる。

### Claude Code への hook 登録

`.claude\settings.json` (このリポジトリの設定は**手で**編集する。
スクリプトはいじらない) に PreToolUse hook を足す:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read|Glob|Grep",
        "hooks": [
          {
            "type": "command",
            "command": "C:\\Users\\mylit\\AppData\\Local\\Programs\\Python\\Python311\\python.exe C:\\Users\\mylit\\StackChanDev\\StackChan\\notifier\\tachikoma_notifier\\claude_hook_approval.py",
            "timeout": 35
          }
        ]
      }
    ]
  }
}
```

- `matcher` は声で承認したいツールに絞る。`Read|Glob|Grep` が音声承認の
  全対象。リスクの高いツールも読み上げだけは欲しいなら matcher に足して
  よい (デーモンが announce するが判定は常に "ask" → PC で通常確認)
- `timeout` はクライアントの 30 秒より長くしておく (35 秒)
- デーモンが起動していないときは即座に接続失敗 → 無出力 exit 0 なので、
  hook を登録したまま日常作業をしても害はない (毎回ミリ秒の往復失敗のみ)
- 許可プロンプトが出ないツール (すでに allow 済みのもの) には hook 判定が
  適用されるため、matcher を広げすぎない
