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


GENERATION_ID = "79ad2015-4d14-4d38-ab60-0e2134133009"


def make_opener(*, statuses=("completed",), wav_bytes=None, calls=None):
    """Simulates: POST /generate (status[0]), then GET /history/{id} once per remaining status, then GET /audio/{id}."""
    wav_bytes = wav_bytes if wav_bytes is not None else _make_wav_bytes()
    history_statuses = list(statuses[1:])

    def opener(request, timeout_seconds):
        if calls is not None:
            calls.append(request)
        if request.full_url.endswith("/generate"):
            return FakeResponse(json.dumps({"id": GENERATION_ID, "status": statuses[0]}).encode("utf-8"))
        if "/history/" in request.full_url:
            status = history_statuses.pop(0) if history_statuses else "completed"
            return FakeResponse(json.dumps({"id": GENERATION_ID, "status": status}).encode("utf-8"))
        if "/audio/" in request.full_url:
            return FakeResponse(wav_bytes)
        raise AssertionError(f"unexpected URL: {request.full_url}")

    return opener


class VoiceboxSynthesizerTests(unittest.TestCase):
    def make_synth(self, **overrides):
        defaults = dict(
            base_url="http://localhost:17493",
            profile_id="a0715b38-0a0c-487a-917f-255139f1ea1e",
            opener=make_opener(),
            sleep_fn=lambda seconds: None,
        )
        defaults.update(overrides)
        return VoiceboxSynthesizer(**defaults)

    def test_requires_base_url_and_profile_id(self):
        with self.assertRaises(ValueError):
            VoiceboxSynthesizer(base_url="", profile_id="1")
        with self.assertRaises(ValueError):
            VoiceboxSynthesizer(base_url="http://localhost:17493", profile_id="")

    def test_synthesize_returns_pcm_when_already_completed(self):
        pcm = b"\x01\x00\x02\x00\x03\x00"
        synth = self.make_synth(opener=make_opener(statuses=("completed",), wav_bytes=_make_wav_bytes(pcm=pcm)))
        result = synth.synthesize("こんにちは")
        self.assertEqual(result, pcm)

    def test_synthesize_polls_history_while_generating(self):
        calls = []
        synth = self.make_synth(
            opener=make_opener(statuses=("loading_model", "generating", "generating", "completed"), calls=calls)
        )
        synth.synthesize("こんにちは")
        generate_calls = [c for c in calls if c.full_url.endswith("/generate")]
        history_calls = [c for c in calls if "/history/" in c.full_url]
        audio_calls = [c for c in calls if "/audio/" in c.full_url]
        self.assertEqual(len(generate_calls), 1)
        self.assertEqual(len(history_calls), 3)
        self.assertEqual(len(audio_calls), 1)

    def test_synthesize_posts_to_generate_with_expected_body(self):
        calls = []
        synth = self.make_synth(profile_id="42", opener=make_opener(calls=calls))
        synth.synthesize("こんにちは")
        request = calls[0]
        self.assertEqual(request.full_url, "http://localhost:17493/generate")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            body,
            {"text": "こんにちは", "profile_id": "42", "language": "ja", "engine": "qwen"},
        )

    def test_language_and_engine_are_configurable(self):
        calls = []
        synth = self.make_synth(language="en", engine="chatterbox", opener=make_opener(calls=calls))
        synth.synthesize("hello")
        body = json.loads(calls[0].data.decode("utf-8"))
        self.assertEqual(body["language"], "en")
        self.assertEqual(body["engine"], "chatterbox")

    def test_rejects_empty_text(self):
        synth = self.make_synth()
        with self.assertRaises(ValueError):
            synth.synthesize("")

    def test_failed_generation_raises_speech_synthesis_error(self):
        synth = self.make_synth(opener=make_opener(statuses=("generating", "failed")))
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_poll_timeout_raises_speech_synthesis_error(self):
        synth = self.make_synth(
            opener=make_opener(statuses=("generating",) * 100),
            poll_interval_seconds=1.0,
            poll_timeout_seconds=2.0,
        )
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_generate_network_failure_raises_speech_synthesis_error(self):
        def opener(request, timeout_seconds):
            raise URLError("boom")

        synth = self.make_synth(opener=opener)
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_missing_generation_id_raises_speech_synthesis_error(self):
        def opener(request, timeout_seconds):
            return FakeResponse(json.dumps({"status": "completed"}).encode("utf-8"))

        synth = self.make_synth(opener=opener)
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_non_mono_wav_raises_speech_synthesis_error(self):
        synth = self.make_synth(opener=make_opener(wav_bytes=_make_wav_bytes(channels=2)))
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_wrong_sample_rate_raises_speech_synthesis_error(self):
        synth = self.make_synth(opener=make_opener(wav_bytes=_make_wav_bytes(framerate=16000)))
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_invalid_wav_bytes_raises_speech_synthesis_error(self):
        synth = self.make_synth(opener=make_opener(wav_bytes=b"not a wav"))
        with self.assertRaises(SpeechSynthesisError):
            synth.synthesize("こんにちは")

    def test_api_key_sent_as_header_when_set(self):
        calls = []
        synth = self.make_synth(api_key="super-secret-voicebox-key", opener=make_opener(calls=calls))
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
            synth = VoiceboxSynthesizer(opener=make_opener(calls=calls), sleep_fn=lambda seconds: None)
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
