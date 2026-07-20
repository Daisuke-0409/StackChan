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
