import io
import json
import os
import unittest
import wave
from urllib.error import URLError

from tachikoma_notifier.voicebox_synth import VoiceboxSynthesizer
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


def opener_returning(wav_bytes=None, *, calls=None):
    wav_bytes = wav_bytes if wav_bytes is not None else _make_wav_bytes()

    def opener(request, timeout_seconds):
        if calls is not None:
            calls.append(request)
        return FakeResponse(wav_bytes)

    return opener


def opener_raising(exc: Exception):
    def opener(request, timeout_seconds):
        raise exc

    return opener


class VoiceboxSynthesizerTests(unittest.TestCase):
    def test_requires_base_url_and_profile_id(self):
        with self.assertRaises(ValueError):
            VoiceboxSynthesizer(base_url="", profile_id="1")
        with self.assertRaises(ValueError):
            VoiceboxSynthesizer(base_url="http://localhost:17493", profile_id="")

    def test_synthesize_returns_pcm_from_wav_response(self):
        pcm = b"\x01\x00\x02\x00\x03\x00"
        synth = VoiceboxSynthesizer(
            base_url="http://localhost:17493",
            profile_id="1",
            opener=opener_returning(_make_wav_bytes(pcm=pcm)),
        )
        result = synth.synthesize("こんにちは")
        self.assertEqual(result, pcm)

    def test_synthesize_posts_to_generate_with_expected_body(self):
        calls = []
        synth = VoiceboxSynthesizer(
            base_url="http://localhost:17493", profile_id="42", opener=opener_returning(calls=calls)
        )
        synth.synthesize("こんにちは")
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(request.full_url, "http://localhost:17493/generate")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            body,
            {"text": "こんにちは", "profile_id": "42", "language": "ja", "engine": "qwen3-tts"},
        )

    def test_language_and_engine_are_configurable(self):
        calls = []
        synth = VoiceboxSynthesizer(
            base_url="http://localhost:17493",
            profile_id="1",
            language="en",
            engine="custom-engine",
            opener=opener_returning(calls=calls),
        )
        synth.synthesize("hello")
        body = json.loads(calls[0].data.decode("utf-8"))
        self.assertEqual(body["language"], "en")
        self.assertEqual(body["engine"], "custom-engine")

    def test_rejects_empty_text(self):
        synth = VoiceboxSynthesizer(base_url="http://localhost:17493", profile_id="1", opener=opener_returning())
        with self.assertRaises(ValueError):
            synth.synthesize("")

    def test_network_failure_raises_speech_synthesis_error(self):
        synth = VoiceboxSynthesizer(
            base_url="http://localhost:17493", profile_id="1", opener=opener_raising(URLError("boom"))
        )
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_non_mono_wav_raises_speech_synthesis_error(self):
        synth = VoiceboxSynthesizer(
            base_url="http://localhost:17493",
            profile_id="1",
            opener=opener_returning(_make_wav_bytes(channels=2)),
        )
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_wrong_sample_rate_raises_speech_synthesis_error(self):
        synth = VoiceboxSynthesizer(
            base_url="http://localhost:17493",
            profile_id="1",
            opener=opener_returning(_make_wav_bytes(framerate=16000)),
        )
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_invalid_wav_bytes_raises_speech_synthesis_error(self):
        synth = VoiceboxSynthesizer(
            base_url="http://localhost:17493", profile_id="1", opener=opener_returning(b"not a wav")
        )
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_api_key_sent_as_header_when_set(self):
        calls = []
        synth = VoiceboxSynthesizer(
            base_url="http://localhost:17493",
            profile_id="1",
            api_key="super-secret-voicebox-key",
            opener=opener_returning(calls=calls),
        )
        synth.synthesize("こんにちは")
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer super-secret-voicebox-key")
        self.assertNotIn(b"super-secret-voicebox-key", calls[0].data)

    def test_env_vars_are_used_when_arguments_omitted(self):
        env_backup = {
            key: os.environ.get(key)
            for key in ("TACHIKOMA_VOICEBOX_BASE_URL", "TACHIKOMA_VOICEBOX_PROFILE_ID")
        }
        try:
            os.environ["TACHIKOMA_VOICEBOX_BASE_URL"] = "http://localhost:17493"
            os.environ["TACHIKOMA_VOICEBOX_PROFILE_ID"] = "7"
            calls = []
            synth = VoiceboxSynthesizer(opener=opener_returning(calls=calls))
            synth.synthesize("こんにちは")
            body = json.loads(calls[0].data.decode("utf-8"))
            self.assertEqual(body["profile_id"], "7")
        finally:
            for key, value in env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
