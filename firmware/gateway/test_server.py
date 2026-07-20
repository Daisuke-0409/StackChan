import unittest

from .server import (
    MAX_INPUT_BYTES,
    MAX_SPEECH_AUDIO_BYTES,
    MAX_SAMPLE_RATE,
    MAX_TRANSCRIBE_AUDIO_BYTES,
    MIN_SAMPLE_RATE,
    _pcm_to_wav,
    dequeue_speech,
    enqueue_speech,
    process_chat,
    process_transcribe,
)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.env = {"AI_PROVIDER": "mock", "ALLOW_INSECURE_DEV": "1"}
        self.payload = {"device_id": "dev", "session_id": "s1", "request_id": "r1", "text": "こんにちは"}

    def test_mock_japanese_success(self):
        status, body = process_chat(self.payload, {}, self.env)
        self.assertEqual(status, 200)
        self.assertTrue(body["is_final"])
        self.assertIn("タチコマ", body["text"])

    def test_authentication_required(self):
        status, body = process_chat(self.payload, {}, {"AI_PROVIDER": "mock"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "authentication_failed")

    def test_empty_and_long_input(self):
        empty = dict(self.payload, text="")
        long_text = dict(self.payload, text="x" * (MAX_INPUT_BYTES + 1))
        self.assertEqual(process_chat(empty, {}, self.env)[0], 400)
        self.assertEqual(process_chat(long_text, {}, self.env)[0], 400)

    def test_required_ids(self):
        missing = dict(self.payload)
        del missing["request_id"]
        self.assertEqual(process_chat(missing, {}, self.env)[0], 400)


class SpeechQueueTests(unittest.TestCase):
    def setUp(self):
        # Each test uses its own device_id so tests can't interfere via the
        # shared module-level queue.
        self.device_id = f"dev-{id(self)}"

    def test_enqueue_then_dequeue_round_trips(self):
        audio = b"\x01\x00\x02\x00\x03\x00"
        status, body = enqueue_speech(self.device_id, audio)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(dequeue_speech(self.device_id), audio)

    def test_dequeue_is_one_shot(self):
        enqueue_speech(self.device_id, b"\x01\x00")
        self.assertEqual(dequeue_speech(self.device_id), b"\x01\x00")
        self.assertIsNone(dequeue_speech(self.device_id))

    def test_dequeue_unknown_device_returns_none(self):
        self.assertIsNone(dequeue_speech("no-such-device"))

    def test_second_enqueue_replaces_first_pending_one(self):
        enqueue_speech(self.device_id, b"\x01\x00")
        enqueue_speech(self.device_id, b"\x02\x00")
        self.assertEqual(dequeue_speech(self.device_id), b"\x02\x00")

    def test_rejects_empty_device_id(self):
        status, body = enqueue_speech("", b"\x01\x00")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_input")

    def test_rejects_empty_audio(self):
        status, _ = enqueue_speech(self.device_id, b"")
        self.assertEqual(status, 400)

    def test_rejects_odd_length_audio(self):
        status, _ = enqueue_speech(self.device_id, b"\x01\x00\x02")
        self.assertEqual(status, 400)

    def test_rejects_oversized_audio(self):
        status, _ = enqueue_speech(self.device_id, b"\x00" * (MAX_SPEECH_AUDIO_BYTES + 2))
        self.assertEqual(status, 413)

    def test_accepts_audio_at_exact_size_limit(self):
        status, _ = enqueue_speech(self.device_id, b"\x00" * MAX_SPEECH_AUDIO_BYTES)
        self.assertEqual(status, 200)


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        self.env = {"STT_PROVIDER": "mock", "ALLOW_INSECURE_DEV": "1"}
        self.audio = b"\x01\x00\x02\x00\x03\x00\x04\x00"

    def test_mock_success(self):
        status, body = process_transcribe(self.audio, {}, self.env, sample_rate=16000)
        self.assertEqual(status, 200)
        self.assertTrue(body["text"])

    def test_mock_returns_configured_text(self):
        env = dict(self.env, MOCK_TRANSCRIPTION="タチコマ、聞こえてます")
        status, body = process_transcribe(self.audio, {}, env, sample_rate=16000)
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "タチコマ、聞こえてます")

    def test_authentication_required(self):
        status, body = process_transcribe(self.audio, {}, {"STT_PROVIDER": "mock"}, sample_rate=16000)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "authentication_failed")

    def test_rejects_empty_audio(self):
        status, _ = process_transcribe(b"", {}, self.env, sample_rate=16000)
        self.assertEqual(status, 400)

    def test_rejects_odd_length_audio(self):
        status, _ = process_transcribe(b"\x01\x00\x02", {}, self.env, sample_rate=16000)
        self.assertEqual(status, 400)

    def test_rejects_oversized_audio(self):
        status, _ = process_transcribe(b"\x00" * (MAX_TRANSCRIBE_AUDIO_BYTES + 2), {}, self.env, sample_rate=16000)
        self.assertEqual(status, 413)

    def test_accepts_audio_at_exact_size_limit(self):
        status, _ = process_transcribe(b"\x00" * MAX_TRANSCRIBE_AUDIO_BYTES, {}, self.env, sample_rate=16000)
        self.assertEqual(status, 200)

    def test_rejects_sample_rate_below_minimum(self):
        status, _ = process_transcribe(self.audio, {}, self.env, sample_rate=MIN_SAMPLE_RATE - 1)
        self.assertEqual(status, 400)

    def test_rejects_sample_rate_above_maximum(self):
        status, _ = process_transcribe(self.audio, {}, self.env, sample_rate=MAX_SAMPLE_RATE + 1)
        self.assertEqual(status, 400)

    def test_accepts_sample_rate_at_bounds(self):
        self.assertEqual(process_transcribe(self.audio, {}, self.env, sample_rate=MIN_SAMPLE_RATE)[0], 200)
        self.assertEqual(process_transcribe(self.audio, {}, self.env, sample_rate=MAX_SAMPLE_RATE)[0], 200)

    def test_real_provider_without_credentials_is_server_error(self):
        status, body = process_transcribe(self.audio, {}, {"STT_PROVIDER": "openai", "ALLOW_INSECURE_DEV": "1"},
                                          sample_rate=16000)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "server_error")


class WavHeaderTests(unittest.TestCase):
    def test_wraps_pcm_with_valid_riff_wave_header(self):
        pcm = b"\x01\x00\x02\x00\x03\x00\x04\x00"
        wav = _pcm_to_wav(pcm, sample_rate=16000)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual(wav[8:12], b"WAVE")
        self.assertEqual(wav[36:40], b"data")
        self.assertEqual(wav[-len(pcm):], pcm)

    def test_declared_data_size_matches_pcm_length(self):
        pcm = b"\x00" * 100
        wav = _pcm_to_wav(pcm, sample_rate=16000)
        declared_size = int.from_bytes(wav[40:44], "little")
        self.assertEqual(declared_size, len(pcm))


if __name__ == "__main__":
    unittest.main()
