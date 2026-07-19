import unittest
from urllib.error import URLError

from tachikoma_notifier.stackchan_speech_sink import StackChanSpeechSink
from tachikoma_notifier.windows_wave_synth import SpeechSynthesisError


class FakeResponse:
    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def opener_returning(status: int = 200):
    def opener(request, timeout_seconds):
        return FakeResponse(status)

    return opener


def opener_raising(exc: Exception):
    def opener(request, timeout_seconds):
        raise exc

    return opener


class FakeFallbackSink:
    def __init__(self, result: bool = True):
        self.result = result
        self.calls = []

    def speak(self, phrase: str) -> bool:
        self.calls.append(phrase)
        return self.result


class StackChanSpeechSinkTests(unittest.TestCase):
    def make_sink(self, **overrides):
        defaults = dict(
            endpoint="https://gateway.local:8080/v1/speak",
            device_token="super-secret-token",
            device_id="AABBCCDDEEFF",
            synthesizer=lambda text: b"\x01\x00\x02\x00",
            opener=opener_returning(200),
        )
        defaults.update(overrides)
        return StackChanSpeechSink(**defaults)

    def test_requires_endpoint_token_and_device_id(self):
        with self.assertRaises(ValueError):
            StackChanSpeechSink(endpoint="", device_token="t", device_id="d")
        with self.assertRaises(ValueError):
            StackChanSpeechSink(endpoint="https://x", device_token="", device_id="d")
        with self.assertRaises(ValueError):
            StackChanSpeechSink(endpoint="https://x", device_token="t", device_id="")

    def test_speak_happy_path_returns_true(self):
        sink = self.make_sink()
        self.assertTrue(sink.speak("承認待ちだよ"))

    def test_speak_sends_pcm_bytes_and_auth_header(self):
        captured = {}

        def opener(request, timeout_seconds):
            captured["body"] = request.data
            captured["auth"] = request.get_header("Authorization")
            captured["device_id"] = request.get_header("X-device-id")
            return FakeResponse(200)

        sink = self.make_sink(opener=opener)
        sink.speak("承認待ちだよ")
        self.assertEqual(captured["body"], b"\x01\x00\x02\x00")
        self.assertEqual(captured["auth"], "Bearer super-secret-token")
        self.assertEqual(captured["device_id"], "AABBCCDDEEFF")

    def test_device_token_never_appears_in_request_body(self):
        captured = {}

        def opener(request, timeout_seconds):
            captured["body"] = request.data
            return FakeResponse(200)

        sink = self.make_sink(opener=opener)
        sink.speak("承認待ちだよ")
        self.assertNotIn(b"super-secret-token", captured["body"])

    def test_synthesis_failure_returns_false_without_fallback(self):
        def failing_synth(text):
            raise SpeechSynthesisError("boom")

        sink = self.make_sink(synthesizer=failing_synth)
        self.assertFalse(sink.speak("承認待ちだよ"))

    def test_network_failure_returns_false_without_fallback(self):
        sink = self.make_sink(opener=opener_raising(URLError("boom")))
        self.assertFalse(sink.speak("承認待ちだよ"))

    def test_server_error_status_returns_false(self):
        sink = self.make_sink(opener=opener_returning(500))
        self.assertFalse(sink.speak("承認待ちだよ"))

    def test_synthesis_failure_falls_back_when_fallback_configured(self):
        fallback = FakeFallbackSink(result=True)

        def failing_synth(text):
            raise SpeechSynthesisError("boom")

        sink = self.make_sink(synthesizer=failing_synth, fallback=fallback)
        self.assertTrue(sink.speak("承認待ちだよ"))
        self.assertEqual(fallback.calls, ["承認待ちだよ"])

    def test_network_failure_falls_back_when_fallback_configured(self):
        fallback = FakeFallbackSink(result=False)
        sink = self.make_sink(opener=opener_raising(URLError("boom")), fallback=fallback)
        self.assertFalse(sink.speak("承認待ちだよ"))
        self.assertEqual(fallback.calls, ["承認待ちだよ"])

    def test_env_vars_are_used_when_arguments_omitted(self):
        import os

        env_backup = {
            key: os.environ.get(key)
            for key in ("TACHIKOMA_STACKCHAN_SPEAK_URL", "TACHIKOMA_STACKCHAN_DEVICE_TOKEN", "TACHIKOMA_STACKCHAN_DEVICE_ID")
        }
        try:
            os.environ["TACHIKOMA_STACKCHAN_SPEAK_URL"] = "https://gateway.local:8080/v1/speak"
            os.environ["TACHIKOMA_STACKCHAN_DEVICE_TOKEN"] = "env-token"
            os.environ["TACHIKOMA_STACKCHAN_DEVICE_ID"] = "env-device"
            sink = StackChanSpeechSink(synthesizer=lambda text: b"\x01\x00", opener=opener_returning(200))
            self.assertTrue(sink.speak("hi"))
        finally:
            for key, value in env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
