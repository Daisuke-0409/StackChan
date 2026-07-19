"""Synthesize speech to raw PCM bytes using Windows SAPI (System.Speech).

This reuses the exact TTS engine notifier.py's WindowsSpeechSink already
uses, but captures the output to a WAV file instead of the default audio
device, so the resulting PCM bytes can be sent elsewhere (e.g. to
StackChanSpeechSink) instead of playing on the PC's own speakers.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import wave
from typing import Callable, Optional

DEFAULT_SAMPLE_RATE = 24000
DEFAULT_TIMEOUT_SECONDS = 20.0

PowerShellRunner = Callable[[str, dict], "subprocess.CompletedProcess"]


class SpeechSynthesisError(Exception):
    """Safe, non-sensitive error. Never includes the synthesized text."""


def _default_runner(script: str, env: dict) -> "subprocess.CompletedProcess":
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        check=False,
    )


_SYNTH_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $voice = $env:TACHIKOMA_TTS_VOICE
  if ($voice) { $s.SelectVoice($voice) }
  else { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet,
                               [System.Speech.Synthesis.VoiceAge]::NotSet, 0, 'ja-JP') }
} catch { }
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    [int]$env:TACHIKOMA_TTS_SAMPLE_RATE,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($env:TACHIKOMA_TTS_OUT, $format)
$s.Speak($env:TACHIKOMA_TTS_TEXT)
$s.Dispose()
"""


def synthesize_wav_pcm(
    text: str,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    voice: Optional[str] = None,
    runner: PowerShellRunner = _default_runner,
) -> bytes:
    """Synthesize text to raw 16-bit mono PCM bytes at sample_rate.

    Raises SpeechSynthesisError on any failure (missing voice engine,
    PowerShell failure, or a produced file that doesn't match the requested
    format) rather than returning possibly-wrong-format audio.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValueError("sample_rate must be a positive integer")

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = os.path.join(tmp_dir, "speech.wav")
        env = os.environ.copy()
        env["TACHIKOMA_TTS_TEXT"] = text
        env["TACHIKOMA_TTS_OUT"] = out_path
        env["TACHIKOMA_TTS_SAMPLE_RATE"] = str(sample_rate)
        if voice:
            env["TACHIKOMA_TTS_VOICE"] = voice
        elif "TACHIKOMA_TTS_VOICE" in env:
            del env["TACHIKOMA_TTS_VOICE"]

        try:
            completed = runner(_SYNTH_SCRIPT, env)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SpeechSynthesisError("speech synthesis process failed") from exc
        if completed.returncode != 0 or not os.path.isfile(out_path):
            raise SpeechSynthesisError("speech synthesis did not produce an output file")

        try:
            with wave.open(out_path, "rb") as wav_file:
                if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
                    raise SpeechSynthesisError("synthesized audio is not 16-bit mono PCM")
                if wav_file.getframerate() != sample_rate:
                    raise SpeechSynthesisError("synthesized audio sample rate does not match request")
                pcm = wav_file.readframes(wav_file.getnframes())
        except wave.Error as exc:
            raise SpeechSynthesisError("synthesized file is not a valid WAV") from exc

    if not pcm:
        raise SpeechSynthesisError("synthesized audio is empty")
    return pcm


__all__ = ["DEFAULT_SAMPLE_RATE", "SpeechSynthesisError", "synthesize_wav_pcm"]
