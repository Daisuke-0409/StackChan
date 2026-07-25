# Tachikoma Gateway

The ESP32 client posts JSON to `/v1/chat`; provider credentials stay on this
separate process. The default `mock` provider is offline and requires
`ALLOW_INSECURE_DEV=1` when no device token is configured.

```powershell
$env:AI_PROVIDER='mock'
$env:ALLOW_INSECURE_DEV='1'
python gateway/server.py
curl.exe http://127.0.0.1:8080/health
curl.exe -H 'Content-Type: application/json' -d '{"device_id":"dev","session_id":"s1","request_id":"r1","text":"こんにちは"}' http://127.0.0.1:8080/v1/chat
```

For a real provider set `AI_PROVIDER_URL`, `AI_PROVIDER_API_KEY`, and
`AI_PROVIDER_MODEL`; use HTTPS with certificate verification. Production
deployments should set a strong `DEVICE_TOKEN` and must not enable
`ALLOW_INSECURE_DEV`. Do not commit `.env` files or credentials.

The firmware stores its endpoint and device token in the NVS namespace
`tachi_gateway` (`url`, `device_token`). A development build can provision
these once using local-only CMake cache values:

```powershell
idf.py -D DEVELOPMENT_BUILD=ON `
  -D TACHIKOMA_GATEWAY_URL=https://gateway.example/v1/chat `
  -D TACHIKOMA_DEVICE_TOKEN=<local-token> reconfigure
```

The token is written to NVS on boot and is not printed in logs. Do not commit
the command line or its values.

## Conversation memory

Before this, every `/v1/chat` request went to Gemini entirely on its own --
`session_id` was echoed back and never used -- so the device forgot a name
the moment it finished saying it, and asking about local weather meant
saying your address every time.

Two layers, kept per device under `gateway/memory/<device_id>.json`:

- **turns** -- the last 20 messages (10 exchanges), replayed as conversation
  history so follow-ups resolve against what was just said.
- **profile** -- durable facts (name, where they live, preferences). Gemini
  extracts these **on a background thread after the reply has already been
  sent**, so remembering never adds to the wait. They are injected into the
  system instruction with an explicit note to *use* them without reciting
  them back.

Verified live 2026-07-26 across four separate HTTP requests:

```
> 俺の名前はダイスケ。覚えておいて。      < ダイスケさんですね。しっかり覚えましたよ。
> 横浜市に住んでるよ。これも覚えて。      < 横浜市にお住まいなのですね。
> 俺の名前は何だっけ？                   < ダイスケさんですよ。
> 今日の天気は？                         < 今日の横浜市は雲が広がっていますが…
```

The last line is the point: no location was given in that request; the
profile supplied it and the web search used it.

`ENABLE_MEMORY=0` disables both layers. `TACHIKOMA_MEMORY_DIR` moves the
store.

**This directory holds personal data** (real names, home locations). It is
gitignored, stays on this machine, and is never written to the access log --
only the number of facts is logged. Delete a device's file to make it forget.

## Web search

`/v1/chat` sends Gemini the `google_search` tool, so a question needing
current or external information is answered from a live search rather than
from training data. No extra service or API key is involved -- it reuses
`AI_PROVIDER_API_KEY`.

Gemini decides per request whether to search, and skips it for ordinary
conversation, so this costs nothing when it is not needed. Measured live
2026-07-26 with `gemini-3.5-flash-lite`:

| request | searched | first chunk |
| --- | --- | --- |
| `こんにちは、元気？` | no | 1156 ms |
| `今日の東京の天気を教えて。` | yes (2 queries, 4 sources) | 1719 ms |
| `今の日本の総理大臣は？` | yes (1 query, 1 source) | 1468 ms |

Set `ENABLE_WEB_SEARCH=0` to turn it off.

The queries Gemini ran and the number of sources are logged; the answer text
and page contents are not, matching the rest of the access log.

Two things matter because every reply is spoken aloud:

- The system prompt forbids markdown, bullet lists, URLs and emoji, and asks
  for two or three sentences. A grounded answer defaults to a bulleted
  markdown summary, which is unusable as speech.
- `_speakable()` strips any markup that slips through anyway, before TTS.
  Without it the speaker literally pronounces `**` and reads out URLs.

## Local TTS with VOICEVOX

`TTS_PROVIDER=voicevox` synthesizes on this machine instead of calling
Gemini TTS. Measured on short replies: **755 ms** per sentence against
**2667 ms** for Gemini, which was the largest single component of the delay
before a reply started playing. VOICEVOX's default output (24 kHz, 16-bit,
mono) is exactly what `/v1/speak_queue` serves, so only the WAV header is
removed -- no resampling.

Start the engine before the gateway (the desktop app is not needed):

```powershell
& "$env:LOCALAPPDATA\Programs\VOICEVOX\vv-engine\run.exe" --host 127.0.0.1 --port 50021
```

`VOICEVOX_SPEAKER` selects the voice. If the engine is not reachable the
gateway logs it and falls back to Gemini TTS for that sentence, so a stopped
engine degrades speed rather than breaking replies. Set
`TTS_FALLBACK_TO_GEMINI=0` to fail instead.

## Push-to-talk transcription (`/v1/transcribe`)

The device uploads one raw 16-bit PCM clip (recorded while a push-to-talk
button was held; see `VoiceInputController` in the firmware) as the request
body, with `X-Sample-Rate` set to the mic's actual sample rate. This
endpoint owns the cloud STT provider credentials, exactly like `/v1/chat`
owns the AI provider credentials -- the device never sees an STT API key.
The default `mock` provider returns `MOCK_TRANSCRIPTION` (or a fixed
Japanese phrase) without any network access, mirroring `AI_PROVIDER=mock`.

For a real provider set `STT_PROVIDER=openai` (or any OpenAI-compatible
`/v1/audio/transcriptions` endpoint), `STT_PROVIDER_URL`,
`STT_PROVIDER_API_KEY`, and `STT_PROVIDER_MODEL`.

The firmware stores its endpoint and device token in the NVS namespace
`tachi_stt` (`url`, `device_token`), provisioned the same way:

```powershell
idf.py -D DEVELOPMENT_BUILD=ON `
  -D TACHIKOMA_TRANSCRIBE_QUEUE_URL=https://gateway.example/v1/transcribe `
  -D TACHIKOMA_DEVICE_TOKEN=<local-token> reconfigure
```
