import base64
import json
import os
import unittest
from urllib.error import URLError

from tachikoma_notifier.gemini_tts_synth import DEFAULT_MODEL, DEFAULT_VOICE_NAME, GeminiTtsSynthesizer
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


def _audio_payload(pcm: bytes = b"\x01\x00\x02\x00", *, sample_rate: int = DEFAULT_SAMPLE_RATE) -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": f"audio/L16;codec=pcm;rate={sample_rate}",
                                "data": base64.b64encode(pcm).decode("ascii"),
                            }
                        }
                    ]
                }
            }
        ]
    }


def opener_returning(payload: dict):
    def opener(request, timeout_seconds):
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    return opener


def opener_raising(exc: Exception):
    def opener(request, timeout_seconds):
        raise exc

    return opener


class GeminiTtsSynthesizerTests(unittest.TestCase):
    def test_requires_api_key(self):
        env_backup = os.environ.pop("TACHIKOMA_GEMINI_API_KEY", None)
        try:
            with self.assertRaises(ValueError):
                GeminiTtsSynthesizer()
        finally:
            if env_backup is not None:
                os.environ["TACHIKOMA_GEMINI_API_KEY"] = env_backup

    def test_synthesize_returns_pcm_decoded_from_base64(self):
        pcm = b"\x01\x00\x02\x00\x03\x00"
        synth = GeminiTtsSynthesizer(api_key="test-key", opener=opener_returning(_audio_payload(pcm)))
        result = synth.synthesize("こんにちは")
        self.assertEqual(result, pcm)

    def test_rejects_empty_text(self):
        synth = GeminiTtsSynthesizer(api_key="test-key", opener=opener_returning(_audio_payload()))
        with self.assertRaises(ValueError):
            synth.synthesize("")
        with self.assertRaises(ValueError):
            synth.synthesize("   ")

    def test_wraps_text_in_read_aloud_instruction(self):
        captured = {}

        def opener(request, timeout_seconds):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(json.dumps(_audio_payload()).encode("utf-8"))

        synth = GeminiTtsSynthesizer(api_key="test-key", opener=opener)
        synth.synthesize("こんにちは")
        sent_text = captured["body"]["contents"][0]["parts"][0]["text"]
        self.assertIn("こんにちは", sent_text)
        self.assertNotEqual(sent_text, "こんにちは")

    def test_sends_voice_name_and_audio_modality(self):
        captured = {}

        def opener(request, timeout_seconds):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(json.dumps(_audio_payload()).encode("utf-8"))

        synth = GeminiTtsSynthesizer(api_key="test-key", voice_name="Puck", opener=opener)
        synth.synthesize("こんにちは")
        config = captured["body"]["generationConfig"]
        self.assertEqual(config["responseModalities"], ["AUDIO"])
        self.assertEqual(
            config["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Puck"
        )

    def test_network_failure_raises_speech_synthesis_error(self):
        synth = GeminiTtsSynthesizer(api_key="test-key", opener=opener_raising(URLError("boom")))
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_missing_audio_data_raises_speech_synthesis_error(self):
        synth = GeminiTtsSynthesizer(api_key="test-key", opener=opener_returning({"candidates": [{}]}))
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_wrong_sample_rate_raises_speech_synthesis_error(self):
        synth = GeminiTtsSynthesizer(
            api_key="test-key", opener=opener_returning(_audio_payload(sample_rate=16000))
        )
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_invalid_base64_raises_speech_synthesis_error(self):
        payload = _audio_payload()
        payload["candidates"][0]["content"]["parts"][0]["inlineData"]["data"] = "not-valid-base64!!"
        synth = GeminiTtsSynthesizer(api_key="test-key", opener=opener_returning(payload))
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_api_key_sent_as_header_never_in_url_or_body(self):
        captured = {}

        def opener(request, timeout_seconds):
            captured["url"] = request.full_url
            captured["body"] = request.data
            captured["api_key_header"] = request.get_header("X-goog-api-key")
            return FakeResponse(json.dumps(_audio_payload()).encode("utf-8"))

        synth = GeminiTtsSynthesizer(api_key="super-secret-key", opener=opener)
        synth.synthesize("こんにちは")
        self.assertNotIn("super-secret-key", captured["url"])
        self.assertNotIn(b"super-secret-key", captured["body"])
        self.assertEqual(captured["api_key_header"], "super-secret-key")

    def test_model_and_voice_env_vars_override_defaults(self):
        env_backup = {
            key: os.environ.get(key) for key in ("TACHIKOMA_GEMINI_TTS_MODEL", "TACHIKOMA_GEMINI_TTS_VOICE")
        }
        try:
            os.environ["TACHIKOMA_GEMINI_TTS_MODEL"] = "gemini-custom-tts"
            os.environ["TACHIKOMA_GEMINI_TTS_VOICE"] = "Puck"
            captured = {}

            def opener(request, timeout_seconds):
                captured["url"] = request.full_url
                captured["body"] = json.loads(request.data.decode("utf-8"))
                return FakeResponse(json.dumps(_audio_payload()).encode("utf-8"))

            synth = GeminiTtsSynthesizer(api_key="test-key", opener=opener)
            synth.synthesize("こんにちは")
            self.assertIn("gemini-custom-tts", captured["url"])
            self.assertEqual(
                captured["body"]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"][
                    "voiceName"
                ],
                "Puck",
            )
        finally:
            for key, value in env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_defaults(self):
        self.assertEqual(DEFAULT_MODEL, "gemini-2.5-flash-preview-tts")
        self.assertEqual(DEFAULT_VOICE_NAME, "Kore")


if __name__ == "__main__":
    unittest.main()
