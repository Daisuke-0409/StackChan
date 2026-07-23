import io
import json
import os
import unittest
import wave
from urllib.error import URLError

from tachikoma_notifier.voicevox_synth import DEFAULT_SPEAKER_ID, VoicevoxSynthesizer
from tachikoma_notifier.windows_wave_synth import DEFAULT_SAMPLE_RATE, SpeechSynthesisError


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _make_wav_bytes(*, pcm: bytes = b"\x01\x00\x02\x00", channels: int = 1, sampwidth: int = 2,
                     framerate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sampwidth)
        wav_file.setframerate(framerate)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


def two_step_opener(*, audio_query_payload=None, wav_bytes=None, calls=None):
    audio_query_payload = audio_query_payload if audio_query_payload is not None else {"speedScale": 1.0}
    wav_bytes = wav_bytes if wav_bytes is not None else _make_wav_bytes()

    def opener(request, timeout_seconds):
        if calls is not None:
            calls.append(request)
        if "/audio_query" in request.full_url:
            return FakeResponse(json.dumps(audio_query_payload).encode("utf-8"))
        if "/synthesis" in request.full_url:
            return FakeResponse(wav_bytes)
        raise AssertionError(f"unexpected URL: {request.full_url}")

    return opener


class VoicevoxSynthesizerTests(unittest.TestCase):
    def test_defaults_to_localhost_50021_and_zundamon_normal(self):
        synth = VoicevoxSynthesizer(opener=two_step_opener())
        self.assertEqual(synth._base_url, "http://localhost:50021")
        self.assertEqual(synth._speaker_id, DEFAULT_SPEAKER_ID)
        self.assertEqual(DEFAULT_SPEAKER_ID, "3")

    def test_synthesize_returns_pcm_from_wav_response(self):
        pcm = b"\x01\x00\x02\x00\x03\x00"
        synth = VoicevoxSynthesizer(opener=two_step_opener(wav_bytes=_make_wav_bytes(pcm=pcm)))
        result = synth.synthesize("こんにちは")
        self.assertEqual(result, pcm)

    def test_synthesize_calls_audio_query_then_synthesis_with_speaker(self):
        calls = []
        synth = VoicevoxSynthesizer(speaker_id="7", opener=two_step_opener(calls=calls))
        synth.synthesize("こんにちは")
        self.assertEqual(len(calls), 2)
        self.assertIn("/audio_query", calls[0].full_url)
        self.assertIn("speaker=7", calls[0].full_url)
        self.assertIn("text=", calls[0].full_url)
        self.assertIn("/synthesis", calls[1].full_url)
        self.assertIn("speaker=7", calls[1].full_url)

    def test_synthesize_sends_audio_query_result_as_synthesis_body(self):
        calls = []
        query_payload = {"speedScale": 1.0, "marker": "distinctive-value"}
        synth = VoicevoxSynthesizer(opener=two_step_opener(audio_query_payload=query_payload, calls=calls))
        synth.synthesize("こんにちは")
        sent_body = json.loads(calls[1].data.decode("utf-8"))
        self.assertEqual(sent_body, query_payload)

    def test_rejects_empty_text(self):
        synth = VoicevoxSynthesizer(opener=two_step_opener())
        with self.assertRaises(ValueError):
            synth.synthesize("")

    def test_audio_query_network_failure_raises_speech_synthesis_error(self):
        def opener(request, timeout_seconds):
            raise URLError("boom")

        synth = VoicevoxSynthesizer(opener=opener)
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_synthesis_network_failure_raises_speech_synthesis_error(self):
        def opener(request, timeout_seconds):
            if "/audio_query" in request.full_url:
                return FakeResponse(json.dumps({}).encode("utf-8"))
            raise URLError("boom")

        synth = VoicevoxSynthesizer(opener=opener)
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_non_mono_wav_raises_speech_synthesis_error(self):
        synth = VoicevoxSynthesizer(opener=two_step_opener(wav_bytes=_make_wav_bytes(channels=2)))
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_wrong_sample_rate_raises_speech_synthesis_error(self):
        synth = VoicevoxSynthesizer(opener=two_step_opener(wav_bytes=_make_wav_bytes(framerate=16000)))
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_invalid_wav_bytes_raises_speech_synthesis_error(self):
        synth = VoicevoxSynthesizer(opener=two_step_opener(wav_bytes=b"not a wav"))
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_env_vars_are_used_when_arguments_omitted(self):
        env_backup = {
            key: os.environ.get(key)
            for key in ("TACHIKOMA_VOICEVOX_BASE_URL", "TACHIKOMA_VOICEVOX_SPEAKER_ID")
        }
        try:
            os.environ["TACHIKOMA_VOICEVOX_BASE_URL"] = "http://localhost:50021"
            os.environ["TACHIKOMA_VOICEVOX_SPEAKER_ID"] = "1"
            calls = []
            synth = VoicevoxSynthesizer(opener=two_step_opener(calls=calls))
            synth.synthesize("こんにちは")
            self.assertIn("speaker=1", calls[0].full_url)
        finally:
            for key, value in env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
