
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

### Known issue (unresolved, 2026-07-23): no voice from the device with AI_PROVIDER=gemini

Gateway started with `AI_PROVIDER=gemini` and `GEMINI_TTS_VOICE=Zephyr`
(see `37efaba`, `592d4b0`); held the device's head to trigger push-to-talk
and spoke to it -- no voice came out of the device.

**Not yet checked**: which of the four stages the request actually reaches
before failing -- transcribe (`/v1/transcribe`), chat
(`process_chat()`/`_gemini_chat_response()`), TTS
(`_gemini_tts_pcm()`), or delivery (`enqueue_speech()` /
`/v1/speak_queue` poll). `process_chat()` and `_gemini_tts_pcm()` were
each verified working in isolation via direct Python calls earlier the
same day (see `37efaba`'s commit message), so the bug is likely either in
the transcribe step, in how the device's push-to-talk flow calls
`/v1/chat`, or in the speak_queue poll/playback path -- but this is a
guess, not a finding.

**First thing to do next session**: start the gateway the same way
(`AI_PROVIDER=gemini`, `GEMINI_TTS_VOICE=Zephyr` -- see the startup
command in today's chat log or reconstruct from `server.py`'s env vars),
keep its stdout visible, hold the device's head and speak, and read the
gateway log line by line to find exactly which of the four stages above
the request reaches (or fails at) before investigating further.
