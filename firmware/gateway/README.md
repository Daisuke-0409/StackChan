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

## Who is speaking, and what they may be told

`people.py` holds the people this device has met; `biometrics.py` turns a
voice clip or a camera frame into a comparable embedding. Two things are
kept deliberately apart:

**Identity is a guess.** It comes from a 0.3MP camera and a few seconds of
speech, either of which a photograph or a recording defeats. It is a
convenience signal, never authentication. Anything that must survive a
motivated attacker needs a second factor that is not biometric.

**Policy is not a guess.** Every memory entry carries the widest audience it
may reach, and only entries the current speaker may hear are loaded. The
model composing the answer never sees the rest. Prompting "do not mention
his work" leaves the work in the context, where a sympathetic-sounding
question can still draw it out.

| listener | master-only | household | colleague | everyone |
| --- | --- | --- | --- | --- |
| master | yes | yes | yes | yes |
| household | | yes | yes | yes |
| colleague | | | yes | yes |
| guest / unrecognized | | | | yes |

Until anybody enrols, the device is still the single-user machine it has
always been and everyone is treated as master -- otherwise switching this on
would hide the operator's own name and address from him. Access control
begins the moment there is somebody to tell apart.

### Enrolling

Say so, with a name in the same breath:

```
「自己紹介するね。俺の名前は大輔です」   -> enrols the speaker
「彼女の自己紹介。名前は○○です」        -> role from 彼女/家族/同僚/会社
```

The first person ever to introduce themselves becomes master: nobody exists
yet who could authorize it, and somebody has to be able to grant the rest.
Later introductions default to guest unless the words say otherwise.

An unrecognized face belonging to whoever is currently speaking is
remembered automatically, so faces enrol themselves once a voice is known.

### Marking something private

Keyword-triggered rather than judged by the model, so it is predictable:

```
ここだけの話 / 内緒 / 秘密 / 二人だけ / 俺とお前だけ / オフレコ / 誰にも言わない
```

The cue closes the current turn *and* the preceding exchanges, because it is
nearly always about what was just said. `みんなに言っていい` reopens to
household.

A missed cue would publish something meant to stay in; a false positive only
over-restricts, so the matching leans toward restricting.

### How well it actually works

Measured against the encoder with distinct synthetic voices:

| | cosine |
| --- | --- |
| same speaker | 0.64 - 0.83 |
| clearly different speakers | 0.38 - 0.46 |
| two voices of the same type and register | up to 0.77 |

So one threshold cannot separate everybody. `identify()` also requires the
best match to beat the runner-up by a margin and reports **nobody** when it
cannot tell -- 0.71 against 0.70 is a coin toss, and settling it by taking
the larger number is how the wrong person hears something private. Refusing
degrades to least privilege, which is the safe direction.

`ENABLE_SPEAKER_ID=0` and `ENABLE_FACE_ID=0` turn each off.
`SPEAKER_TTL_SECONDS` (default 180) is how long an identification stands
before the device forgets who it was talking to.

### Models

`gateway/models/` (gitignored, fetched separately):

| file | used for |
| --- | --- |
| `voice_encoder.pt` | resemblyzer GE2E weights, 17 MB |
| `face_detection_yunet.onnx` | face detection, 0.2 MB |
| `face_recognition_sface.onnx` | face embeddings, 39 MB |

The resemblyzer *package* cannot be installed here -- its webrtcvad
dependency needs a C++ toolchain this machine lacks -- so biometrics.py
re-implements the network against the published weights. The face models are
used through cv2's own `FaceDetectorYN` / `FaceRecognizerSF`, already built
into OpenCV 5.

Note for anyone reinstalling: pip fails on this machine with
`CERTIFICATE_VERIFY_FAILED` because its bundled CA list does not include the
VPN's root. Exporting the Windows root store to a PEM and setting `PIP_CERT`
to it fixes that properly, without disabling verification.

## `/v1/vision`

One camera frame in; where to look and who it is, out. Detection and
recognition share a single inference.

```json
{"faces": 1, "look_at": {"x": -0.42, "y": 0.10}, "area_ratio": 0.08,
 "person": {"role": "master", "score": 0.71}}
```

`look_at` is normalized to -1..1 with the origin at the centre of the frame,
which is exactly what `Motion::lookAtNormalized` on the device takes -- the
firmware needs to know nothing about faces, lenses or frame sizes. The
largest face wins: somebody across the room is in frame but is not the
conversation.

A recognized face never raises standing above a voice that is already
identified, so a photograph cannot promote itself over whoever is actually
talking. It only fills in when no voice has been identified.

**The device does not call this yet.** Its camera is currently wired only to
the app's WebSocket stream; an upload path from firmware to this endpoint is
what remains before head-tracking works.

## Conversation memory

Before this, every `/v1/chat` request went to Gemini entirely on its own --
`session_id` was echoed back and never used -- so the device forgot a name
the moment it finished saying it, and asking about local weather meant
saying your address every time.

Two layers, kept under `gateway/memory/<brain_id>.json` (`tachikoma.json` by
default -- see *One persona, several bodies* below):

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
only the number of facts is logged. Delete the store to make it forget.

### One persona, several bodies

The store was originally named after the device, which quietly contradicted
the premise of the project. `people.json` -- who is enrolled, their voice,
their face, what they may be told -- was never per-device, so a second robot
would recognise you on sight and still have no idea what you told the first
one yesterday. Same face, same voice, no shared past.

Every body now reads and writes the same store. The device is still the unit
of *routing* -- which head speaks, which head moves, who it is currently
talking to -- but no longer the unit of memory.

| Variable | Default | Meaning |
|---|---|---|
| `TACHIKOMA_MEMORY_SCOPE` | `shared` | `device` restores the old per-body split |
| `TACHIKOMA_BRAIN_ID` | `tachikoma` | Store filename, i.e. which persona |
| `TACHIKOMA_MEMORY_DIR` | `gateway/memory` | Where the store lives |

A store written before sharing existed is adopted automatically **at startup**
when the shared one is missing: it is *copied*, the original is left alone,
and the adoption is logged next to the store the gateway ended up using. It
happens at boot rather than on the first question so that moving a store
between machines can be confirmed before anyone depends on it. Two such stores are left alone entirely -- two
histories cannot be interleaved without inventing an order for them, and
guessing writes a false past into the one place the robot trusts. Merge those
by hand.

`TACHIKOMA_MEMORY_DIR` may point at a network share so the store outlives any
one PC, but **run only one gateway against a file-backed store**.
`_save_memory()` writes to a temporary file and renames it, which is atomic
on a local filesystem and not across SMB; two gateways would silently drop
each other's turns. Several gateways at once need a database, not a shared
folder -- that is the Phase 8 NAS work, not this.

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
