"""The PC's ear: a desk microphone that speaks through Tachikoma.

The robot's own microphone is its weakest part. This module listens on a USB
microphone attached to the PC, segments utterances by silence, and hands them
to the same gateway endpoints the robot uses:

    mic -> /v1/transcribe (speaker identification included)
        -> /v1/chat with device_id = the robot
        -> the reply lands in the robot's speak_queue

so the answer comes out of Tachikoma's mouth, with its voice and its motions,
and lands in the same shared memory. The firmware is not involved and not
changed. conversation_pipeline.py's docstring left "how audio becomes text"
deliberately outside its boundary; this is that missing edge.

Every constant here is shaped by a bug the robot's own ear taught us the hard
way in August 2026:

  * The silence clock keeps running *while* an utterance is being recorded.
    Freezing it at speech onset made every utterance measure 0ms and get
    discarded as too short. (voice_input_controller.cpp, fixed 08-13)
  * Thresholds are calibrated from the room actually being listened to,
    never hard-coded. The robot shipped with a silence threshold below its
    own room tone and recorded 30 seconds per exchange.
  * The ear goes deaf while the robot is speaking. The robot hears itself
    at 5-16x its speech threshold; a desk mic will too. We cannot see the
    robot's playback state from here, so the mute window is estimated from
    the reply's length and refreshed if the queue is known to be deep.
  * Utterance lengths are logged, because the numbers are what let any of
    the above be tuned by reading instead of guessing.

Run via run_pc_ear.ps1, which loads gateway/.env for the device token.
"""
from __future__ import annotations

import json
import math
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, Optional

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000

# Silence that ends an utterance / speech shorter than this is a cough or a
# chair. Same reasoning as the firmware's kFollowUp* pair.
END_SILENCE_MS = 800
MIN_SPEECH_MS = 300
MAX_UTTERANCE_MS = 15000
PREROLL_MS = 400

CALIBRATION_SECONDS = 1.2
# Multipliers over measured ambient RMS. The gap between room tone and speech
# at a desk mic is wide (measured 3-10x on the robot's mic); these sit inside
# it. Overridable via env for a loud room.
SPEECH_OVER_AMBIENT = 4.0
SILENCE_OVER_AMBIENT = 2.2
SPEECH_RMS_FLOOR = 700   # below this the mic is effectively muted; refuse to
                         # arm on a threshold that noise alone can cross


def _rms(frame: bytes) -> int:
    count = len(frame) // 2
    if count == 0:
        return 0
    samples = struct.unpack(f"<{count}h", frame)
    return int(math.sqrt(sum(s * s for s in samples) / count))


class UtteranceSegmenter:
    """Frames in, whole utterances out. Pure logic, no audio APIs, testable."""

    def __init__(self, *, speech_rms: int, silence_rms: int,
                 end_silence_ms: int = END_SILENCE_MS,
                 min_speech_ms: int = MIN_SPEECH_MS,
                 max_utterance_ms: int = MAX_UTTERANCE_MS,
                 preroll_ms: int = PREROLL_MS,
                 log: Callable[[str], None] = print) -> None:
        self.speech_rms = speech_rms
        self.silence_rms = silence_rms
        self.end_silence_ms = end_silence_ms
        self.min_speech_ms = min_speech_ms
        self.max_utterance_ms = max_utterance_ms
        self._preroll_frames = max(1, preroll_ms // FRAME_MS)
        self._log = log
        self._preroll: list[bytes] = []
        self._in_speech = False
        self._buffer = bytearray()
        self._speech_start_ms = 0
        self._quiet_since_ms = 0

    def feed(self, frame: bytes, now_ms: int) -> Optional[bytes]:
        """Returns a complete utterance's PCM when one just ended, else None."""
        level = _rms(frame)

        if not self._in_speech:
            self._preroll.append(frame)
            if len(self._preroll) > self._preroll_frames:
                self._preroll.pop(0)
            if level >= self.speech_rms:
                self._in_speech = True
                self._buffer = bytearray().join([b"".join(self._preroll)])
                self._buffer += frame
                self._speech_start_ms = now_ms
                self._quiet_since_ms = now_ms
                self._log(f"[ear] speech detected (rms={level})")
            return None

        self._buffer += frame
        # The clock that must never freeze: quiet_since advances on every
        # loud-enough frame, and only the gap since the last one ends the
        # utterance.
        if level >= self.silence_rms:
            self._quiet_since_ms = now_ms

        ran_long = now_ms - self._speech_start_ms >= self.max_utterance_ms
        went_quiet = now_ms - self._quiet_since_ms >= self.end_silence_ms
        if not ran_long and not went_quiet:
            return None

        self._in_speech = False
        spoken_ms = self._quiet_since_ms - self._speech_start_ms
        pcm = bytes(self._buffer)
        self._buffer = bytearray()
        self._preroll.clear()
        if spoken_ms < self.min_speech_ms:
            self._log(f"[ear] too short ({spoken_ms}ms), ignoring")
            return None
        self._log(f"[ear] utterance ended ({spoken_ms}ms"
                  f"{', hit max' if ran_long else ''}), sending")
        return pcm


class GatewayClient:
    """The two calls the robot's firmware makes, made from the PC instead."""

    def __init__(self, base_url: str, token: str, device_id: str,
                 log: Callable[[str], None] = print) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._device_id = device_id
        self._session = f"pc-ear-{uuid.uuid4().hex[:8]}"
        self._log = log

    def transcribe(self, pcm: bytes) -> Optional[str]:
        request = urllib.request.Request(
            f"{self._base}/v1/transcribe", data=pcm, method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/octet-stream",
                # The gateway keys "who is speaking right now" by this header,
                # and the chat call that follows looks the speaker up under
                # the same id. It must be the robot's id, or the identified
                # role would be filed where the chat never looks.
                "X-Device-Id": self._device_id,
                "X-Sample-Rate": str(SAMPLE_RATE),
            })
        body = self._post(request, what="transcribe", timeout=60)
        if body is None:
            return None
        text = body.get("text", "")
        return text or None

    def chat(self, text: str) -> Optional[str]:
        payload = json.dumps({
            "device_id": self._device_id,
            "session_id": self._session,
            "request_id": f"r-{uuid.uuid4().hex[:8]}",
            "text": text,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base}/v1/chat", data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            })
        body = self._post(request, what="chat", timeout=90)
        if body is None:
            return None
        return body.get("text")

    def _post(self, request: urllib.request.Request, *, what: str,
              timeout: float) -> Optional[dict]:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self._log(f"[ear] {what} failed: HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            self._log(f"[ear] {what} failed: {type(exc).__name__}")
        return None


def reply_mute_seconds(reply: str) -> float:
    """How long to go deaf while the robot says this.

    We cannot see the robot's playback from the PC, and polling speak_queue
    would *steal* the audio the robot is about to fetch -- dequeue is
    one-shot. So estimate: VOICEVOX Japanese lands near 7 chars/second, plus
    fixed cost for the queue poll (up to 2s) and the speaker's tail.
    """
    return min(3.0 + len(reply) * 0.15, 25.0)


def main() -> int:
    try:
        import sounddevice as sd
    except ImportError:
        print("sounddevice is not installed for this python. "
              "Run: python -m pip install sounddevice", file=sys.stderr)
        return 2

    base_url = os.environ.get("TACHIKOMA_EAR_GATEWAY", "http://127.0.0.1:8080")
    token = os.environ.get("DEVICE_TOKEN", "")
    device_id = os.environ.get("TACHIKOMA_EAR_DEVICE_ID", "80456B4DE03C")
    wanted = os.environ.get("TACHIKOMA_EAR_INPUT_DEVICE", "UGREEN")
    if not token:
        print("DEVICE_TOKEN is empty; run via run_pc_ear.ps1 so gateway/.env "
              "is loaded.", file=sys.stderr)
        return 2

    device: Optional[int] = None
    for index, info in enumerate(sd.query_devices()):
        if info["max_input_channels"] > 0 and wanted.lower() in info["name"].lower():
            device = index
            break
    label = sd.query_devices(device)["name"] if device is not None else "system default"
    if device is None and wanted:
        print(f"[ear] no input device matching '{wanted}'; using the system default")

    # Calibrate against the actual room. Hard-coded thresholds under the room
    # tone are how the robot recorded 30 seconds per utterance.
    print(f"[ear] mic: {label}")
    print(f"[ear] calibrating room tone for {CALIBRATION_SECONDS:.0f}s -- stay quiet...")
    ambient_levels: list[int] = []
    with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                           blocksize=FRAME_SAMPLES, device=device) as stream:
        for _ in range(int(CALIBRATION_SECONDS * 1000 / FRAME_MS)):
            frame, _overflowed = stream.read(FRAME_SAMPLES)
            ambient_levels.append(_rms(bytes(frame)))
    ambient = sorted(ambient_levels)[len(ambient_levels) // 2]

    speech_rms = int(os.environ.get("TACHIKOMA_EAR_SPEECH_RMS", "0")) or \
        max(int(ambient * SPEECH_OVER_AMBIENT), SPEECH_RMS_FLOOR)
    silence_rms = int(os.environ.get("TACHIKOMA_EAR_SILENCE_RMS", "0")) or \
        max(int(ambient * SILENCE_OVER_AMBIENT), int(SPEECH_RMS_FLOOR * 0.6))
    print(f"[ear] ambient rms={ambient} -> speech>={speech_rms} silence>={silence_rms}")
    print(f"[ear] gateway={base_url} device_id={device_id}")
    print("[ear] listening. Speak; Tachikoma answers. Ctrl+C to stop.")

    segmenter = UtteranceSegmenter(speech_rms=speech_rms, silence_rms=silence_rms)
    client = GatewayClient(base_url, token, device_id)

    with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                           blocksize=FRAME_SAMPLES, device=device) as stream:
        while True:
            frame, _overflowed = stream.read(FRAME_SAMPLES)
            utterance = segmenter.feed(bytes(frame), int(time.monotonic() * 1000))
            if utterance is None:
                continue

            text = client.transcribe(utterance)
            if not text:
                print("[ear] (nothing recognized)")
                continue
            print(f"[ear] you : {text}")
            reply = client.chat(text)
            if not reply:
                print("[ear] (no reply)")
                continue
            print(f"[ear] tachikoma: {reply}")

            # Deaf while the robot speaks, so the desk mic does not hand the
            # robot's own words back to it. The stream keeps running; its
            # buffer is drained and discarded afterwards so stale audio from
            # during the reply cannot leak into the next utterance.
            mute = reply_mute_seconds(reply)
            print(f"[ear] muted {mute:.1f}s while the robot replies")
            time.sleep(mute)
            stream.read(stream.read_available)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[ear] stopped")
        raise SystemExit(0)
