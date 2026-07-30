import json
import os
import tempfile
import unittest
from urllib.error import URLError

from tachikoma_notifier.cloud_transcriber import (
    CloudTranscriber,
    DryRunTranscriptionLog,
    TranscriptionError,
    run_dry_run,
)


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def opener_returning(payload: dict):
    def opener(request, timeout_seconds):
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    return opener


def opener_raising(exc: Exception):
    def opener(request, timeout_seconds):
        raise exc

    return opener


class CloudTranscriberTests(unittest.TestCase):
    def test_requires_api_key(self):
        env_backup = os.environ.pop("TACHIKOMA_STT_API_KEY", None)
        try:
            with self.assertRaises(ValueError):
                CloudTranscriber()
        finally:
            if env_backup is not None:
                os.environ["TACHIKOMA_STT_API_KEY"] = env_backup

    def test_transcribe_returns_text_from_response(self):
        transcriber = CloudTranscriber(api_key="test-key", opener=opener_returning({"text": " approve read "}))
        result = transcriber.transcribe(b"fake-audio-bytes")
        self.assertEqual(result.text, "approve read")
        self.assertEqual(result.model, "whisper-1")

    def test_transcribe_rejects_empty_audio(self):
        transcriber = CloudTranscriber(api_key="test-key", opener=opener_returning({"text": "x"}))
        with self.assertRaises(ValueError):
            transcriber.transcribe(b"")

    def test_transcribe_rejects_oversized_audio(self):
        transcriber = CloudTranscriber(api_key="test-key", opener=opener_returning({"text": "x"}))
        with self.assertRaises(ValueError):
            transcriber.transcribe(b"0" * (25 * 1024 * 1024 + 1))

    def test_network_failure_raises_safe_transcription_error(self):
        transcriber = CloudTranscriber(api_key="test-key", opener=opener_raising(URLError("boom")))
        with self.assertRaises(TranscriptionError) as ctx:
            transcriber.transcribe(b"fake-audio-bytes")
        self.assertNotIn("test-key", str(ctx.exception))

    def test_missing_text_field_raises_transcription_error(self):
        transcriber = CloudTranscriber(api_key="test-key", opener=opener_returning({"unexpected": "shape"}))
        with self.assertRaises(TranscriptionError):
            transcriber.transcribe(b"fake-audio-bytes")

    def test_blank_text_field_raises_transcription_error(self):
        transcriber = CloudTranscriber(api_key="test-key", opener=opener_returning({"text": "   "}))
        with self.assertRaises(TranscriptionError):
            transcriber.transcribe(b"fake-audio-bytes")

    def test_invalid_json_raises_transcription_error(self):
        def opener(request, timeout_seconds):
            return FakeResponse(b"not json")

        transcriber = CloudTranscriber(api_key="test-key", opener=opener)
        with self.assertRaises(TranscriptionError):
            transcriber.transcribe(b"fake-audio-bytes")

    def test_api_key_never_appears_in_request_body(self):
        captured = {}

        def opener(request, timeout_seconds):
            captured["body"] = request.data
            captured["auth_header"] = request.get_header("Authorization")
            return FakeResponse(json.dumps({"text": "ok"}).encode("utf-8"))

        transcriber = CloudTranscriber(api_key="super-secret-key", opener=opener)
        transcriber.transcribe(b"fake-audio-bytes")
        self.assertNotIn(b"super-secret-key", captured["body"])
        self.assertEqual(captured["auth_header"], "Bearer super-secret-key")

    def test_dry_run_reads_file_transcribes_and_logs_only(self):
        logged = []
        log = DryRunTranscriptionLog(output_fn=logged.append)
        transcriber = CloudTranscriber(api_key="test-key", opener=opener_returning({"text": "承認します"}))
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            handle.write(b"fake-wav-bytes")
            path = handle.name
        try:
            result = run_dry_run(transcriber, path, log)
        finally:
            os.unlink(path)
        self.assertEqual(result.text, "承認します")
        self.assertEqual(len(logged), 1)
        self.assertIn("承認します", logged[0])


if __name__ == "__main__":
    unittest.main()
