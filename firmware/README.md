
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

### Known issue (unresolved, 2026-07-24): recognized/spoken audio is garbled and fast

While investigating the above, push-to-talk audio sent to STT was found to
be garbled -- and speech played back through the (now-working) audio path
sounds fast/garbled too.

**What was found and fixed today**: `TACHIKOMA_DEBUG_LOGGING=1` (`78b4557`)
saved an uploaded recording as WAV; analyzing it showed even-indexed
samples at ~13x the RMS of odd-indexed samples -- two different signals
interleaved, not one noisy mono channel. Traced to
`AUDIO_INPUT_REFERENCE=true` (`hal/board/config.h`), which makes
`AudioCodec::input_channels()` 2 (real mic + AEC reference), while
`VoiceInputController` has declared `channels=1` and never checked
`input_channels()` since the original Phase 5 implementation (`b76f04a`,
2026-07-20). `DownmixToChannel0()` (`0f35305`) now extracts channel 0
before the recording buffer is built.

**Not resolved**: after flashing the downmix fix, speech is still
fast/garbled. This is not yet understood -- possibilities, none confirmed:

- The downmix picked the wrong channel (channel 0 assumed to be the real
  mic; if the hardware/driver actually interleaves reference-first, this
  needs to extract index 1, not index 0, from each channel-pair).
- A separate, still-unidentified sample-rate or frame-size handling bug
  independent of the channel count (e.g. `kFrameSamples` / recording
  buffer arithmetic assuming a rate or frame layout that doesn't match
  `AUDIO_INPUT_SAMPLE_RATE=24000` in practice).

**First thing to do next session**: re-analyze the WAV files already saved
in `TACHIKOMA_DEBUG_AUDIO_DIR` (default
`%TEMP%\tachikoma_debug_audio`) -- both the pre-downmix ones from today
and, ideally, a fresh one recorded after the downmix fix -- to reconfirm
which of the interleaved channels (even-indexed vs. odd-indexed samples)
is actually the coherent speech signal, not just assume channel 0. Re-check
the sample-rate and frame-size handling in `voice_input_controller.cpp`
against the codec's actual behavior at the same time, in case the channel
mixup was only ever part of the problem.
