import json
import os
import unittest
from urllib.error import URLError

from tachikoma_notifier.gemini_responder import (
    DEFAULT_MODEL,
    GeminiResponder,
    GeminiResponseError,
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


def _candidate_payload(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def opener_returning(payload: dict):
    def opener(request, timeout_seconds):
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    return opener


def opener_raising(exc: Exception):
    def opener(request, timeout_seconds):
        raise exc

    return opener


class GeminiResponderTests(unittest.TestCase):
    def test_requires_api_key(self):
        env_backup = os.environ.pop("TACHIKOMA_GEMINI_API_KEY", None)
        try:
            with self.assertRaises(ValueError):
                GeminiResponder()
        finally:
            if env_backup is not None:
                os.environ["TACHIKOMA_GEMINI_API_KEY"] = env_backup

    def test_generate_reply_returns_text_from_response(self):
        responder = GeminiResponder(api_key="test-key", opener=opener_returning(_candidate_payload(" こんにちは ")))
        reply = responder.generate_reply("hi")
        self.assertEqual(reply.text, "こんにちは")
        self.assertEqual(reply.model, DEFAULT_MODEL)

    def test_generate_reply_rejects_empty_input(self):
        responder = GeminiResponder(api_key="test-key", opener=opener_returning(_candidate_payload("x")))
        with self.assertRaises(ValueError):
            responder.generate_reply("")
        with self.assertRaises(ValueError):
            responder.generate_reply("   ")

    def test_generate_reply_rejects_oversized_input(self):
        responder = GeminiResponder(api_key="test-key", opener=opener_returning(_candidate_payload("x")))
        with self.assertRaises(ValueError):
            responder.generate_reply("0" * 4001)

    def test_network_failure_raises_safe_error(self):
        responder = GeminiResponder(api_key="test-key", opener=opener_raising(URLError("boom")))
        with self.assertRaises(GeminiResponseError) as ctx:
            responder.generate_reply("hi")
        self.assertNotIn("test-key", str(ctx.exception))

    def test_missing_candidates_raises_response_error(self):
        responder = GeminiResponder(api_key="test-key", opener=opener_returning({"unexpected": "shape"}))
        with self.assertRaises(GeminiResponseError):
            responder.generate_reply("hi")

    def test_blank_text_raises_response_error(self):
        responder = GeminiResponder(api_key="test-key", opener=opener_returning(_candidate_payload("   ")))
        with self.assertRaises(GeminiResponseError):
            responder.generate_reply("hi")

    def test_invalid_json_raises_response_error(self):
        def opener(request, timeout_seconds):
            return FakeResponse(b"not json")

        responder = GeminiResponder(api_key="test-key", opener=opener)
        with self.assertRaises(GeminiResponseError):
            responder.generate_reply("hi")

    def test_api_key_sent_as_header_never_in_url_or_body(self):
        captured = {}

        def opener(request, timeout_seconds):
            captured["url"] = request.full_url
            captured["body"] = request.data
            captured["api_key_header"] = request.get_header("X-goog-api-key")
            return FakeResponse(json.dumps(_candidate_payload("ok")).encode("utf-8"))

        responder = GeminiResponder(api_key="super-secret-key", opener=opener)
        responder.generate_reply("hi")
        self.assertNotIn("super-secret-key", captured["url"])
        self.assertNotIn(b"super-secret-key", captured["body"])
        self.assertEqual(captured["api_key_header"], "super-secret-key")

    def test_model_env_var_overrides_default(self):
        env_backup = os.environ.get("TACHIKOMA_GEMINI_MODEL")
        try:
            os.environ["TACHIKOMA_GEMINI_MODEL"] = "gemini-custom-model"
            responder = GeminiResponder(api_key="test-key", opener=opener_returning(_candidate_payload("ok")))
            reply = responder.generate_reply("hi")
            self.assertEqual(reply.model, "gemini-custom-model")
        finally:
            if env_backup is None:
                os.environ.pop("TACHIKOMA_GEMINI_MODEL", None)
            else:
                os.environ["TACHIKOMA_GEMINI_MODEL"] = env_backup

    def test_explicit_model_argument_overrides_env(self):
        responder = GeminiResponder(
            api_key="test-key", model="gemini-explicit", opener=opener_returning(_candidate_payload("ok"))
        )
        reply = responder.generate_reply("hi")
        self.assertEqual(reply.model, "gemini-explicit")


if __name__ == "__main__":
    unittest.main()
