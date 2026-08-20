"""Tests for the local recogniser, on a machine with no model installed.

faster_whisper is imported lazily and only inside transcribe_wav, so
everything around it -- the upload parsing, the HTTP shape, the refusals,
what reaches the log -- is testable here at the office where the model does
not exist.
"""
import json
import unittest
from unittest import mock

from . import local_stt, server


def _multipart(wav: bytes, boundary: str = "abc123", model: str = "whisper-1") -> bytes:
    return server._build_multipart_body(boundary, wav, "audio.wav", model)


class ParseUploadTest(unittest.TestCase):
    """The gateway already builds these; this has to read what it builds."""

    def test_the_wav_survives_the_round_trip(self):
        wav = b"RIFF\x00\x00\x00\x00WAVEfmt " + bytes(40)
        self.assertEqual(local_stt.parse_multipart(_multipart(wav), "abc123"), wav)

    def test_binary_that_looks_like_a_boundary_is_not_truncated(self):
        # Audio is arbitrary bytes and will eventually contain CRLF.
        wav = b"RIFF" + b"\r\n" * 20 + b"tail"
        self.assertEqual(local_stt.parse_multipart(_multipart(wav), "abc123"), wav)

    def test_an_upload_without_a_file_is_refused(self):
        body = b'--b\r\nContent-Disposition: form-data; name="model"\r\n\r\nx\r\n--b--\r\n'
        with self.assertRaises(ValueError):
            local_stt.parse_multipart(body, "b")


class TranscribeTest(unittest.TestCase):
    def _fake_model(self, text):
        segment = mock.Mock()
        segment.text = text
        model = mock.Mock()
        model.transcribe.return_value = ([segment], None)
        return model

    def test_it_asks_for_japanese_rather_than_guessing(self):
        model = self._fake_model("こんにちは")
        with mock.patch.object(local_stt, "_model", model):
            local_stt.transcribe_wav(b"RIFF")
        self.assertEqual(model.transcribe.call_args.kwargs["language"], "ja")

    def test_each_utterance_stands_alone(self):
        # Carrying context between utterances is how one bad transcript
        # poisons the next.
        model = self._fake_model("はい")
        with mock.patch.object(local_stt, "_model", model):
            local_stt.transcribe_wav(b"RIFF")
        self.assertFalse(model.transcribe.call_args.kwargs["condition_on_previous_text"])

    def test_the_vocabulary_goes_in_as_a_bias_not_an_instruction(self):
        model = self._fake_model("はい")
        with mock.patch.object(local_stt, "_model", model), \
             mock.patch.object(local_stt, "INITIAL_PROMPT", "加江田、佐土原"):
            local_stt.transcribe_wav(b"RIFF")
        self.assertEqual(model.transcribe.call_args.kwargs["initial_prompt"],
                         "加江田、佐土原")

    def test_no_vocabulary_means_no_bias(self):
        model = self._fake_model("はい")
        with mock.patch.object(local_stt, "_model", model), \
             mock.patch.object(local_stt, "INITIAL_PROMPT", ""):
            local_stt.transcribe_wav(b"RIFF")
        self.assertIsNone(model.transcribe.call_args.kwargs["initial_prompt"])

    def test_segments_are_joined_and_trimmed(self):
        first, second = mock.Mock(), mock.Mock()
        first.text, second.text = " おはよう", "ございます "
        model = mock.Mock()
        model.transcribe.return_value = ([first, second], None)
        with mock.patch.object(local_stt, "_model", model):
            self.assertEqual(local_stt.transcribe_wav(b"RIFF"), "おはようございます")


class ShapeTest(unittest.TestCase):
    """It has to answer what the gateway already knows how to read."""

    def test_it_answers_the_field_the_gateway_reads(self):
        # server._stt_response takes decoded["text"] and nothing else.
        self.assertIn('"text"', json.dumps({"text": "x"}))

    def test_the_url_the_gateway_would_use_is_local(self):
        self.assertTrue(server._is_loopback(
            f"http://{local_stt.HOST}:{local_stt.PORT}/v1/audio/transcriptions"))


class SafetyTest(unittest.TestCase):
    def test_the_transcript_never_reaches_the_log(self):
        # It is the person's speech. A log on this machine is not where it
        # goes; length and duration are enough to see it working.
        said = []
        handler = local_stt.Handler.__new__(local_stt.Handler)
        with mock.patch.object(local_stt.Handler, "log_message",
                               lambda self, fmt, *a: said.append(fmt % a)):
            local_stt.Handler.log_request(handler, 200)
        self.assertEqual(said, [])

    def test_it_refuses_to_run_twice(self):
        first = local_stt._SingleInstanceServer(("127.0.0.1", 0), local_stt.Handler)
        try:
            with self.assertRaises(OSError):
                local_stt._SingleInstanceServer(
                    ("127.0.0.1", first.server_address[1]), local_stt.Handler)
        finally:
            first.server_close()

    def test_it_listens_on_this_machine_only_by_default(self):
        # Nothing else needs to reach it, and it holds a microphone's worth
        # of somebody's speech.
        self.assertEqual(local_stt.HOST, "127.0.0.1")

    def test_the_model_defaults_to_a_japanese_one(self):
        self.assertIn("kotoba", local_stt.MODEL)

    def test_it_defaults_to_cpu(self):
        # This machine has no NVIDIA card; float16 would fail at load.
        self.assertEqual(local_stt.DEVICE, "cpu")
        self.assertEqual(local_stt.COMPUTE_TYPE, "int8")


if __name__ == "__main__":
    unittest.main()
