
## Build

### Fetch Dependencies

```bash
python3 ./fetch_repos.py
```

### Tool Chains

[ESP-IDF v5.5.4](https://docs.espressif.com/projects/esp-idf/en/v5.5.4/esp32s3/index.html)

### Build

```bash
idf.py build
```

### Host-side tests

The motion coordinate helpers can be tested without ESP-IDF hardware:

```bash
cmake -S tests -B build-host-tests
cmake --build build-host-tests
ctest --test-dir build-host-tests --output-on-failure
```

### Flash

```bash
idf.py flash
```

### Recording length

Push-to-talk records up to **30s** (`kMaxRecordingMs`), capped by
`kMaxRecordingSamples` at 30s of 24kHz mono = 1.44 MB. That buffer lives in
PSRAM (`CONFIG_SPIRAM_USE_MALLOC=y`, `ALWAYSINTERNAL=512`) and is reserved in
full at record start so a 1.4 MB vector never doubles mid-capture; the upload
hands `esp_http_client` a pointer to it rather than copying.

Both limits used to be much lower for reasons that no longer applied: the
sample cap was 256 KiB "mirroring SpeechAnnouncer's kMaxAudioBytes" -- a
playback limit unrelated to how long someone may speak -- which cut
recordings off at 5.5s once capture ran at the correct rate.

The gateway's `MAX_TRANSCRIBE_AUDIO_BYTES` must stay >= this in bytes, and
`VoiceInputConfig::response_timeout_ms` (45s) has to cover uploading the
largest clip plus the STT call on it.

### The device is silent / "no reply" -- check the gateway first

The device has no AI of its own: every reply comes from the Tachikoma
Gateway on the PC. If that process is not running, the device is working
perfectly and still says nothing. Symptoms in the device log, all of which
mean "the PC is not listening", not "the firmware is broken":

```
E (nnnnn) esp-tls: [sock=NN] delayed connect error: Connection reset by peer
E (nnnnn) HTTP_CLIENT: Connection failed, sock < 0
[SpeechAnnouncer] speech queue poll failed
TachikomaState: Thinking -> Error by AiRequestFailed
```

Start it with the launcher, which reads `gateway/.env` (gitignored):

```powershell
powershell -ExecutionPolicy Bypass -File gateway\run_gateway.ps1
```

Three things silently break this if set up by hand instead:

- `GATEWAY_HOST` must be `0.0.0.0`. server.py's default is `127.0.0.1`,
  which accepts only PC-local connections -- the device then gets exactly
  the "Connection reset by peer" above. `run_gateway.ps1` forces it.
- `DEVICE_TOKEN` must equal the firmware's provisioned
  `TACHIKOMA_DEVICE_TOKEN`, or every request is 401. The launcher falls
  back to reading it out of `build/CMakeCache.txt`.
- The provisioned URL is a fixed LAN IP (`build/CMakeCache.txt`,
  `TACHIKOMA_GATEWAY_URL`). If the PC's address changes, the device keeps
  dialing the old one; the launcher prints the current LAN addresses so
  this is visible at startup.

`AI_PROVIDER=gemini` needs `AI_PROVIDER_API_KEY` in `.env` -- chat, TTS and
STT all reuse that one key. The launcher refuses to start without it rather
than letting every request fail at runtime.

Verified working end to end 2026-07-25 with `gemini-3.5-flash-lite` (chat),
`gemini-2.5-flash-preview-tts` (TTS) and `gemini-flash-latest` (STT).
Note `gemini-2.5-flash` now 404s with "no longer available to new users" --
the defaults in server.py already avoid it, but a hand-set
`AI_PROVIDER_MODEL` can walk back into it.

### Before flashing any other device running this firmware

2026-07-22 found four bugs on the office device that almost certainly exist
on every other device still running an older build of this same code
(factory NVS, same firmware). Confirm `git log` includes `ef43966`..`483a44c`
(inclusive) before assuming a device is current -- if not, flash it:

- `2c8fd60` -- AiGatewayClient reboot-looped when a real TACHIKOMA_GATEWAY_URL
  was provisioned: the worker called esp_http_client before WiFi/LWIP existed.
- `3ccff6f` -- touching and releasing the head crashed the device (Si12T
  update task's stack lived in PSRAM; it now reads NVS on Release).
- `872e90e` -- LWIP's tcpip task ("tiT") stack-overflowed under concurrent
  MQTT+TLS/OTA/SNTP/HTTP load; raised 3072 -> 6144.
- `483a44c` -- factory NVS ships live xiaozhi-cloud credentials, and the
  stock firmware streams wake-word audio to mqtt.xiaozhi.me /
  api.tenclass.net by default. Disabled and scrubbed from NVS on boot.

`ef43966` is also worth having (fetch_repos.py's CRLF/patch-apply fix) --
without it, a fresh Windows clone silently builds unpatched vendor sources
(the audio_codec_guard fix from `1178b6a` never lands).

### Resolved, 2026-07-24: "no voice from the device with AI_PROVIDER=gemini"

The 2026-07-23 issue below was tracked down to two stacked causes and
fixed: the gateway's `MAX_SPEECH_AUDIO_BYTES` cap (256 KiB) silently
rejected real Gemini TTS output (300-450 KB) with no log line explaining
why (`98d0dec`), and the device's own `SpeechAnnouncer::kMaxAudioBytes`
had the same 256 KiB ceiling on the receiving end (`7cea0b3`). Chat replies
reach the speaker now.

### Resolved, 2026-07-24: recognized/spoken audio was garbled and fast (channel interleaving)

While investigating the "no voice" issue above, push-to-talk audio sent to
STT was found to be garbled -- and speech played back through the audio
path sounded fast/garbled too.

`TACHIKOMA_DEBUG_LOGGING=1` (`78b4557`) saved an uploaded recording as WAV;
analyzing it showed even-indexed samples at ~13x the RMS of odd-indexed
samples -- two different signals interleaved, not one noisy mono channel.
Traced to `AUDIO_INPUT_REFERENCE=true` (`hal/board/config.h`), which makes
`AudioCodec::input_channels()` 2 (real mic + AEC reference), while
`VoiceInputController` had declared `channels=1` and never checked
`input_channels()` since the original Phase 5 implementation (`b76f04a`,
2026-07-20). `DownmixToChannel0()` (`0f35305`) now extracts channel 0
before the recording buffer is built. Confirmed fixed -- no more
interleaved-channel garble in freshly recorded WAVs.

### Resolved, 2026-07-25: Gemini STT hallucinated on near-silent audio

Very short/near-empty recordings (e.g. from the recording-duration bug
below, or a genuine false-trigger) were sent to Gemini STT as-is. Instead
of reporting it couldn't hear anything, Gemini confidently returned a
fluent, plausible-sounding but entirely fabricated Japanese sentence --
worse than no transcription at all, since the device can't distinguish it
from real speech.

Fixed server-side only, no flash needed (`a1c5c84`): `process_transcribe()`
in `gateway/server.py` now skips the STT call entirely when
`len(audio) / 2 / sample_rate < MIN_TRANSCRIBE_AUDIO_SECONDS` (0.5s) and
returns `{"text": ""}` directly. The device already treats an empty `text`
field as `VoiceInputErrorCode::InvalidResponse`
(`VoiceInputController::UploadAndTranscribe`), the same path a real STT
error takes, so this needed no firmware change.

### Resolved, 2026-07-25: recorded audio was much shorter than the actual button-hold (tick starvation)

Following up on the unresolved issue below (kept for context, see the
original writeup at the bottom of this section): `TACHIKOMA_DEBUG_TIMING`
instrumentation (`e56043f`) measured, rather than guessed, both suspects.
`codec->InputData()` itself was never the problem -- 22-64us per call,
negligible, and `got_frame` was never `false` during a recording (the
codec always had a frame ready when asked). The real cause was the shared
`_stackchan_update_task`'s actual tick interval: mean 37.5ms against the
nominal 20ms, with periodic ~78-85ms stall bursts (LVGL/state/motion work
sharing the same loop) eating roughly a third of a hold's elapsed time.
Since `VoiceInputController` drained exactly one fixed 20ms frame per
tick regardless of how much real time had passed, a slow tick meant
permanently lost audio, not delayed catch-up -- captured ~53% of a 4.61s
hold in that first measurement (up from the original ~21% report below,
which was simply a shorter hold with worse luck overlapping the stall
bursts; same mechanism either way).

Fix (`e56043f`): `VoiceInputController::Update()` now only tracks the
post-speech cooldown window. The mic-capture loop moved to its own
dedicated FreeRTOS task (`StartRecordingTask()`, mirroring
`AudioService::AudioInputTask()`'s `CONFIG_USE_AUDIO_PROCESSOR`
configuration -- stack `2048*3`, priority 8, pinned to core 0), polling
independently of the shared task's LVGL/state/motion work. No new
synchronization was needed: `recording_`/`buffer_`/etc. were already
`mutex_`-protected for the existing head-touch-task/shared-task/worker-task
split.

**Implementation pitfall, worth remembering**: the task's poll delay was
first written as `vTaskDelay(pdMS_TO_TICKS(5))`. `CONFIG_FREERTOS_HZ=100`
(10ms tick) makes `pdMS_TO_TICKS(5)` truncate to `0` via integer division,
turning that into `vTaskDelay(0)` -- a same-priority-only yield, not a
real block. At priority 8, pinned to core 0, that starved the core's idle
task and tripped the 10s task watchdog (`task_wdt`) on real hardware
within seconds of a real recording. Fixed (`49234f0`) with `vTaskDelay(1)`
(10ms, the finest non-zero step at this tick rate) -- still 2x tighter
than the shared task's 20ms nominal.

**Re-measured after the fix**: two live recordings (4.61s hold class),
`rec_task_tick_interval_ms` averaged 18.3-18.4ms (slower than the intended
10ms -- likely `TACHIKOMA_DEBUG_TIMING`'s own two `mclog` calls per
iteration adding UART-write overhead; probably tighter still with the
flag off) and `got_frame` was `false` zero times across both recordings.
Captured-audio frame count now meets or slightly exceeds the Press-to-
Release window in both tests (measurement-boundary noise, not a real
overshoot) -- effectively full capture, versus the ~53%/~21% before the
fix.

**Not yet understood**: a second back-to-back recording's upload failed
with `voice input upload/transcription failed` after a ~5.7s delay (looks
timeout-shaped, not an instant auth rejection -- the first recording in
the same session uploaded and transcribed successfully). Capture itself
was unaffected (frame count still tracked the hold correctly). Reproducibility
and root cause are unconfirmed; worth another look if it recurs.

<details>
<summary>Original report (2026-07-25, before the fix above)</summary>

After the channel-interleaving fix above, a controlled live test (button
held for a deliberate ~2.48s Press-to-Release, single clean pair confirmed
via monitor logs) showed the uploaded recording only covers ~0.53s (~21%
of the hold duration). Chattering/multiple Press-Release pairs has been
ruled out -- this is one continuous hold producing one short recording.

**Ruled out**: touch-sensor chattering; the input-side mutual exclusion
between `VoiceInputController::Update()` and `AudioService::AudioInputTask()`
(`cc9f262`, pause on `OnButtonPressed()` / resume on
`StopRecordingAndUpload()` -- confirmed exactly one pause/resume pair per
recording, no leaked pause state); `CoreS3AudioCodec::Read()`/`Write()`
silently reporting success on a failed transfer (`8a332fe`, now returns
actual `esp_codec_dev_read()`/`write()` status).

**Suspected, not yet measured**: `VoiceInputController::Update()` is driven
by the shared `_stackchan_update_task` (`hal.cpp`), a single FreeRTOS task
also running LVGL UI updates, reminders, motion, and other Tachikoma
state -- all behind one `vTaskDelay(pdMS_TO_TICKS(20))` per loop, plus a
conditional extra `vTaskDelay(pdMS_TO_TICKS(100))` when
`!hal_bridge::is_xiaozhi_idle()`. If this task's actual tick interval
during recording is much coarser than the nominal ~20ms (or worse, than
the ~6.67ms figure a smooth audio capture would need), or if
`codec->InputData()` (`esp_codec_dev_read()`) itself blocks for a long time
per call, that alone could explain capturing only ~21% of a hold. Neither
has been measured yet -- this is a hypothesis, not a confirmed cause.

</details>
