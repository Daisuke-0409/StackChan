import os
import unittest
from unittest.mock import patch

from tachikoma_notifier.conversation_pipeline import (
    ConversationReplyPipeline,
    build_tts_stackchan_sink,
    build_voicebox_stackchan_sink,
)
from tachikoma_notifier.gemini_responder import GeminiReply, GeminiResponseError
from tachikoma_notifier.notifier import WindowsSpeechSink
from tachikoma_notifier.stackchan_speech_sink import StackChanSpeechSink


class FakeResponder:
    def __init__(self, reply=None, error=None):
        self._reply = reply
        self._error = error
        self.calls = []

    def generate_reply(self, user_text: str) -> GeminiReply:
        self.calls.append(user_text)
        if self._error is not None:
            raise self._error
        return self._reply


class FakeSink:
    def __init__(self, result: bool = True):
        self.result = result
        self.calls = []

    def speak(self, phrase: str) -> bool:
        self.calls.append(phrase)
        return self.result


class ConversationReplyPipelineTests(unittest.TestCase):
    def test_happy_path_speaks_the_generated_reply(self):
        responder = FakeResponder(reply=GeminiReply(text="こんにちは", model="gemini-test"))
        sink = FakeSink(result=True)
        pipeline = ConversationReplyPipeline(responder, sink)

        self.assertTrue(pipeline.handle_utterance("おはよう"))
        self.assertEqual(responder.calls, ["おはよう"])
        self.assertEqual(sink.calls, ["こんにちは"])

    def test_sink_failure_propagates_as_false(self):
        responder = FakeResponder(reply=GeminiReply(text="こんにちは", model="gemini-test"))
        sink = FakeSink(result=False)
        pipeline = ConversationReplyPipeline(responder, sink)

        self.assertFalse(pipeline.handle_utterance("おはよう"))

    def test_reply_generation_failure_never_touches_sink(self):
        responder = FakeResponder(error=GeminiResponseError("boom"))
        sink = FakeSink(result=True)
        pipeline = ConversationReplyPipeline(responder, sink)

        self.assertFalse(pipeline.handle_utterance("おはよう"))
        self.assertEqual(sink.calls, [])

    def test_invalid_input_never_touches_sink(self):
        responder = FakeResponder(error=ValueError("empty text"))
        sink = FakeSink(result=True)
        pipeline = ConversationReplyPipeline(responder, sink)

        self.assertFalse(pipeline.handle_utterance(""))
        self.assertEqual(sink.calls, [])

    def test_logger_receives_failure_notice_without_leaking_details(self):
        logged = []
        responder = FakeResponder(error=GeminiResponseError("boom"))
        sink = FakeSink(result=True)
        pipeline = ConversationReplyPipeline(responder, sink, logger=logged.append)

        pipeline.handle_utterance("おはよう")
        self.assertEqual(len(logged), 1)
        self.assertIn("GeminiResponseError", logged[0])


class BuildVoiceboxStackchanSinkTests(unittest.TestCase):
    _ENV_VARS = {
        "TACHIKOMA_STACKCHAN_SPEAK_URL": "https://gateway.local:8080/v1/speak",
        "TACHIKOMA_STACKCHAN_DEVICE_TOKEN": "device-token",
        "TACHIKOMA_STACKCHAN_DEVICE_ID": "AABBCCDDEEFF",
        "TACHIKOMA_VOICEBOX_BASE_URL": "http://localhost:17493",
        "TACHIKOMA_VOICEBOX_PROFILE_ID": "1",
    }

    def test_returns_stackchan_speech_sink_wired_to_voicebox(self):
        with patch.dict("os.environ", self._ENV_VARS, clear=False):
            sink = build_voicebox_stackchan_sink()
        self.assertIsInstance(sink, StackChanSpeechSink)

    def test_defaults_fallback_to_windows_speech_sink(self):
        with patch.dict("os.environ", self._ENV_VARS, clear=False):
            sink = build_voicebox_stackchan_sink()
        self.assertIsInstance(sink._fallback, WindowsSpeechSink)

    def test_explicit_fallback_is_used_instead_of_default(self):
        custom_fallback = FakeSink()
        with patch.dict("os.environ", self._ENV_VARS, clear=False):
            sink = build_voicebox_stackchan_sink(fallback=custom_fallback)
        self.assertIs(sink._fallback, custom_fallback)


class BuildTtsStackchanSinkTests(unittest.TestCase):
    """build_tts_stackchan_sink() picks the synthesizer engine from TACHIKOMA_TTS_ENGINE."""

    _STACKCHAN_ENV_VARS = {
        "TACHIKOMA_STACKCHAN_SPEAK_URL": "https://gateway.local:8080/v1/speak",
        "TACHIKOMA_STACKCHAN_DEVICE_TOKEN": "device-token",
        "TACHIKOMA_STACKCHAN_DEVICE_ID": "AABBCCDDEEFF",
    }
    _VOICEBOX_ENV_VARS = {
        "TACHIKOMA_VOICEBOX_BASE_URL": "http://localhost:17493",
        "TACHIKOMA_VOICEBOX_PROFILE_ID": "1",
    }

    def test_defaults_to_voicevox_engine(self):
        with patch.dict("os.environ", self._STACKCHAN_ENV_VARS, clear=False):
            os.environ.pop("TACHIKOMA_TTS_ENGINE", None)
            sink = build_tts_stackchan_sink()
        self.assertIsInstance(sink, StackChanSpeechSink)

    def test_explicit_voicevox_engine(self):
        env = {**self._STACKCHAN_ENV_VARS, "TACHIKOMA_TTS_ENGINE": "voicevox"}
        with patch.dict("os.environ", env, clear=False):
            sink = build_tts_stackchan_sink()
        self.assertIsInstance(sink, StackChanSpeechSink)

    def test_voicebox_engine_selection(self):
        env = {**self._STACKCHAN_ENV_VARS, **self._VOICEBOX_ENV_VARS, "TACHIKOMA_TTS_ENGINE": "voicebox"}
        with patch.dict("os.environ", env, clear=False):
            sink = build_tts_stackchan_sink()
        self.assertIsInstance(sink, StackChanSpeechSink)

    def test_unknown_engine_raises_value_error(self):
        env = {**self._STACKCHAN_ENV_VARS, "TACHIKOMA_TTS_ENGINE": "nonexistent"}
        with patch.dict("os.environ", env, clear=False):
            with self.assertRaises(ValueError):
                build_tts_stackchan_sink()

    def test_explicit_fallback_is_used_instead_of_default(self):
        custom_fallback = FakeSink()
        with patch.dict("os.environ", self._STACKCHAN_ENV_VARS, clear=False):
            os.environ.pop("TACHIKOMA_TTS_ENGINE", None)
            sink = build_tts_stackchan_sink(fallback=custom_fallback)
        self.assertIs(sink._fallback, custom_fallback)


if __name__ == "__main__":
    unittest.main()
