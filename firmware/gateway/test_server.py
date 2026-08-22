import tempfile
import datetime
import io
import json
import os
import unittest
import urllib.parse
from unittest import mock

from . import crm_bridge, server
from .server import (
    MAX_INPUT_BYTES,
    MAX_SPEECH_AUDIO_BYTES,
    MAX_SAMPLE_RATE,
    MAX_TRANSCRIBE_AUDIO_BYTES,
    MIN_SAMPLE_RATE,
    MIN_TRANSCRIBE_AUDIO_SECONDS,
    _extract_ready_sentences,
    _gemini_stream_chat_and_speak,
    _gemini_streaming_enabled,
    _gemini_tts_pcm,
    _pcm_to_wav,
    dequeue_speech,
    enqueue_speech,
    process_chat,
    process_transcribe,
)


_memory_tmp = None


def setUpModule():
    """Keep conversation memory out of the real store.

    process_chat() persists turns and profile facts per device_id, so without
    this a test run drops files like dev-mock-1877600144784.json into
    gateway/memory/ alongside real devices' memories -- which happened, and is
    exactly the directory that holds personal data.
    """
    global _memory_tmp
    _memory_tmp = tempfile.TemporaryDirectory()
    server.MEMORY_DIR = _memory_tmp.name


def tearDownModule():
    if _memory_tmp is not None:
        _memory_tmp.cleanup()


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

    def test_mock_provider_still_enqueues_the_confirmation_tone(self):
        # Regression check: process_chat()'s TTS branch must not change
        # behavior for non-gemini providers.
        device_id = f"dev-mock-{id(self)}"
        payload = dict(self.payload, device_id=device_id)
        status, _ = process_chat(payload, {}, self.env)
        self.assertEqual(status, 200)
        audio = dequeue_speech(device_id)
        self.assertIsNotNone(audio)
        self.assertGreater(len(audio), 0)


class GeminiProviderTests(unittest.TestCase):
    """AI_PROVIDER=gemini: real Gemini chat + Gemini TTS, without breaking mock/openai-compatible."""

    def setUp(self):
        # A real (non-mock) provider always requires a configured
        # DEVICE_TOKEN -- ALLOW_INSECURE_DEV only permits an insecure
        # AI_PROVIDER_URL, it does not bypass device authentication.
        self.env = {"AI_PROVIDER": "gemini", "ALLOW_INSECURE_DEV": "1", "DEVICE_TOKEN": "test-device-token"}
        self.headers = {"Authorization": "Bearer test-device-token"}
        self.payload = {"device_id": "dev-gemini", "session_id": "s1", "request_id": "r1", "text": "こんにちは"}

    def test_without_api_key_is_server_error(self):
        status, body = process_chat(self.payload, self.headers, self.env)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "server_error")

    def test_without_api_key_does_not_enqueue_anything(self):
        device_id = f"dev-gemini-noauth-{id(self)}"
        payload = dict(self.payload, device_id=device_id)
        process_chat(payload, self.headers, self.env)
        self.assertIsNone(dequeue_speech(device_id))

    def test_gemini_tts_pcm_without_api_key_returns_none(self):
        self.assertIsNone(_gemini_tts_pcm("こんにちは", {}))


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

    def test_append_true_queues_in_order_instead_of_replacing(self):
        enqueue_speech(self.device_id, b"\x01\x00", append=True)
        enqueue_speech(self.device_id, b"\x02\x00", append=True)
        enqueue_speech(self.device_id, b"\x03\x00", append=True)
        self.assertEqual(dequeue_speech(self.device_id), b"\x01\x00")
        self.assertEqual(dequeue_speech(self.device_id), b"\x02\x00")
        self.assertEqual(dequeue_speech(self.device_id), b"\x03\x00")
        self.assertIsNone(dequeue_speech(self.device_id))

    def test_append_true_on_empty_queue_behaves_like_first_item(self):
        # append=True with nothing queued yet still has to seed the list.
        status, _ = enqueue_speech(self.device_id, b"\x01\x00", append=True)
        self.assertEqual(status, 200)
        self.assertEqual(dequeue_speech(self.device_id), b"\x01\x00")

    def test_append_false_after_append_true_still_replaces(self):
        enqueue_speech(self.device_id, b"\x01\x00", append=True)
        enqueue_speech(self.device_id, b"\x02\x00", append=True)
        enqueue_speech(self.device_id, b"\x03\x00")  # default append=False
        self.assertEqual(dequeue_speech(self.device_id), b"\x03\x00")
        self.assertIsNone(dequeue_speech(self.device_id))


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        self.env = {"STT_PROVIDER": "mock", "ALLOW_INSECURE_DEV": "1"}
        # 1 second of dummy audio at 16000Hz (16-bit mono), well above
        # MIN_TRANSCRIBE_AUDIO_SECONDS so these tests exercise the mock/provider
        # path rather than the too-short-audio short-circuit.
        self.audio = b"\x01\x00\x02\x00\x03\x00\x04\x00" * 4000

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

    def test_short_audio_skips_stt_and_returns_empty_text(self):
        # Below MIN_TRANSCRIBE_AUDIO_SECONDS: too little audio to plausibly
        # contain speech, so STT must not be called (Gemini hallucinates a
        # fabricated sentence rather than admitting it heard nothing).
        short_audio = b"\x01\x00\x02\x00\x03\x00\x04\x00"  # 4 samples @16kHz = 0.00025s
        env = dict(self.env, MOCK_TRANSCRIPTION="タチコマ、聞こえてます")
        status, body = process_transcribe(short_audio, {}, env, sample_rate=16000)
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "")

    def test_audio_at_exact_minimum_duration_is_transcribed(self):
        min_bytes = int(MIN_TRANSCRIBE_AUDIO_SECONDS * 16000) * 2
        audio = b"\x00\x00" * (min_bytes // 2)
        status, body = process_transcribe(audio, {}, self.env, sample_rate=16000)
        self.assertEqual(status, 200)
        self.assertTrue(body["text"])

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


class SentenceSplittingTests(unittest.TestCase):
    """_extract_ready_sentences is pure/network-free, so exercise it directly."""

    def test_splits_on_each_delimiter(self):
        sentences, remainder = _extract_ready_sentences("こんにちは、元気ですか？今日は晴れです。")
        self.assertEqual(sentences, ["こんにちは、元気ですか？", "今日は晴れです。"])
        self.assertEqual(remainder, "")

    def test_no_delimiter_yields_only_remainder(self):
        sentences, remainder = _extract_ready_sentences("まだ話している途中です")
        self.assertEqual(sentences, [])
        self.assertEqual(remainder, "まだ話している途中です")

    def test_short_fragment_merges_into_next_sentence(self):
        # "はい。" alone is 3 characters (at the GEMINI_MIN_SENTENCE_CHARS
        # boundary) and must not become its own sentence.
        sentences, remainder = _extract_ready_sentences("はい。それでは始めましょう。")
        self.assertEqual(sentences, ["はい。それでは始めましょう。"])
        self.assertEqual(remainder, "")

    def test_short_fragments_merge_one_step_at_a_time(self):
        # "え。" (2 chars) is too short alone, so it merges with the next
        # delimiter-terminated span ("え。あ。", 4 chars) and flushes there
        # -- merging is step-by-step against the growing candidate, not an
        # unbounded chain across every subsequent short fragment.
        sentences, remainder = _extract_ready_sentences("え。あ。うーん、そうですね。")
        self.assertEqual(sentences, ["え。あ。", "うーん、そうですね。"])
        self.assertEqual(remainder, "")

    def test_incremental_calls_accumulate_correctly(self):
        # Mirrors how the streaming loop actually calls this: once per
        # arriving delta, carrying the remainder forward each time.
        pending = ""
        all_sentences = []
        for delta in ["こんにち", "は。元気で", "すか？", "はい、", "元気です。"]:
            pending += delta
            ready, pending = _extract_ready_sentences(pending)
            all_sentences.extend(ready)
        if pending.strip():
            all_sentences.append(pending)
        self.assertEqual(all_sentences, ["こんにちは。", "元気ですか？", "はい、元気です。"])


class _FakeSseResponse:
    """Minimal stand-in for the context-manager/iterable urllib returns,
    carrying pre-built SSE `data: {...}` lines as bytes."""

    def __init__(self, events: list[dict]):
        self._lines = []
        for event in events:
            self._lines.append(("data: " + __import__("json").dumps(event)).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def __iter__(self):
        return iter(self._lines)


def _sse_text_event(delta: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": delta}]}}]}


class GeminiStreamingTests(unittest.TestCase):
    def setUp(self):
        self.env = {"AI_PROVIDER": "gemini", "AI_PROVIDER_API_KEY": "fake-key",
                    "ALLOW_INSECURE_DEV": "1", "DEVICE_TOKEN": "test-device-token"}
        self.headers = {"Authorization": "Bearer test-device-token"}
        self.device_id = f"dev-stream-{id(self)}"
        self.payload = {"device_id": self.device_id, "session_id": "s1", "request_id": "r1",
                        "text": "こんにちは"}

    def test_streaming_enabled_by_default(self):
        self.assertTrue(_gemini_streaming_enabled({}))
        self.assertTrue(_gemini_streaming_enabled({"GEMINI_STREAMING": "1"}))

    def test_streaming_disabled_via_env(self):
        self.assertFalse(_gemini_streaming_enabled({"GEMINI_STREAMING": "0"}))

    def test_process_chat_routes_to_streaming_by_default(self):
        with mock.patch.object(server, "_gemini_stream_chat_and_speak",
                               return_value=(200, {"text": "ok", "request_id": "r1",
                                                   "session_id": "s1", "is_final": True})) as streamed:
            process_chat(self.payload, self.headers, self.env)
        streamed.assert_called_once()

    def test_process_chat_skips_streaming_when_disabled(self):
        env = dict(self.env, GEMINI_STREAMING="0")
        with mock.patch.object(server, "_gemini_stream_chat_and_speak") as streamed:
            with mock.patch.object(server, "_gemini_chat_response",
                                   return_value=(200, {"text": "ok", "request_id": "r1",
                                                       "session_id": "s1", "is_final": True})) as non_streamed:
                process_chat(self.payload, self.headers, env)
        streamed.assert_not_called()
        non_streamed.assert_called_once()

    def test_without_api_key_is_server_error_and_enqueues_nothing(self):
        env = dict(self.env)
        del env["AI_PROVIDER_API_KEY"]
        status, body = _gemini_stream_chat_and_speak("こんにちは", self.payload, env)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "server_error")
        self.assertIsNone(dequeue_speech(self.device_id))

    def test_multiple_sentences_enqueue_in_order(self):
        events = [_sse_text_event(d) for d in ["こんにちは。", "元気です", "か？"]]
        fake_pcm_calls = []

        def fake_tts(text, env):
            fake_pcm_calls.append(text)
            return (b"\x01\x00" * 10) if text == "こんにちは。" else (b"\x02\x00" * 10)

        with mock.patch.object(server.urllib.request, "urlopen", return_value=_FakeSseResponse(events)):
            with mock.patch.object(server, "_gemini_tts_pcm", side_effect=fake_tts):
                status, body = _gemini_stream_chat_and_speak("こんにちは", self.payload, self.env)

        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "こんにちは。元気ですか？")
        self.assertEqual(fake_pcm_calls, ["こんにちは。", "元気ですか？"])
        # FIFO order: first sentence's audio must come out of the queue first.
        first = dequeue_speech(self.device_id)
        second = dequeue_speech(self.device_id)
        self.assertEqual(first, b"\x01\x00" * 10)
        self.assertEqual(second, b"\x02\x00" * 10)
        self.assertIsNone(dequeue_speech(self.device_id))

    def test_short_fragments_reduce_tts_call_count(self):
        events = [_sse_text_event(d) for d in ["え。", "あ。", "うーん、そうですね。"]]
        with mock.patch.object(server.urllib.request, "urlopen", return_value=_FakeSseResponse(events)):
            with mock.patch.object(server, "_gemini_tts_pcm", return_value=b"\x01\x00" * 10) as fake_tts:
                status, _ = _gemini_stream_chat_and_speak("何か言って", self.payload, self.env)
        self.assertEqual(status, 200)
        # "え。" alone (2 chars) merges forward into "え。あ。" (4 chars,
        # over threshold) instead of getting its own TTS call -- 2 calls
        # total, not 3 (one per raw delimiter).
        self.assertEqual(fake_tts.call_args_list,
                         [mock.call("え。あ。", self.env), mock.call("うーん、そうですね。", self.env)])

    def test_trailing_fragment_without_delimiter_is_flushed_at_stream_end(self):
        events = [_sse_text_event(d) for d in ["最後の一言です"]]  # no trailing delimiter
        with mock.patch.object(server.urllib.request, "urlopen", return_value=_FakeSseResponse(events)):
            with mock.patch.object(server, "_gemini_tts_pcm", return_value=b"\x01\x00" * 10) as fake_tts:
                status, body = _gemini_stream_chat_and_speak("何か言って", self.payload, self.env)
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "最後の一言です")
        fake_tts.assert_called_once_with("最後の一言です", self.env)

    def test_sentence_tts_failure_is_skipped_not_fatal(self):
        events = [_sse_text_event(d) for d in ["だめな文です。", "これは話せます。"]]

        def fake_tts(text, env):
            return None if text == "だめな文です。" else b"\x02\x00" * 10

        with mock.patch.object(server.urllib.request, "urlopen", return_value=_FakeSseResponse(events)):
            with mock.patch.object(server, "_gemini_tts_pcm", side_effect=fake_tts):
                status, body = _gemini_stream_chat_and_speak("何か言って", self.payload, self.env)
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "だめな文です。これは話せます。")
        # Only the successful sentence's audio made it to the queue.
        self.assertEqual(dequeue_speech(self.device_id), b"\x02\x00" * 10)
        self.assertIsNone(dequeue_speech(self.device_id))

    def test_all_sentences_failing_falls_back_to_confirmation_tone(self):
        events = [_sse_text_event(d) for d in ["だめな文です。"]]
        with mock.patch.object(server.urllib.request, "urlopen", return_value=_FakeSseResponse(events)):
            with mock.patch.object(server, "_gemini_tts_pcm", return_value=None):
                status, _ = _gemini_stream_chat_and_speak("何か言って", self.payload, self.env)
        self.assertEqual(status, 200)
        audio = dequeue_speech(self.device_id)
        self.assertIsNotNone(audio)
        self.assertGreater(len(audio), 0)

    def test_http_error_401_maps_to_authentication_failed(self):
        import urllib.error
        with mock.patch.object(server.urllib.request, "urlopen",
                               side_effect=urllib.error.HTTPError("url", 401, "unauthorized", {}, None)):
            status, body = _gemini_stream_chat_and_speak("何か言って", self.payload, self.env)
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "authentication_failed")


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


class CrmBridgeTests(unittest.TestCase):
    """R6: the conversation half of the grave lookup.

    The network is faked through the injectable fetch; what is under test is
    detection (a customer-data interceptor must not fire on small talk), the
    contract's spoken forms, and the rule that a recognized question is
    answered by the bridge in every case including failures.
    """

    ENV = {"CRM_RELAY_URL": "http://relay.test:8767", "CRM_RELAY_TOKEN": "t"}

    def _fetch(self, count, results):
        def fetch(url, token):
            self.fetched_url = url
            return 200, {"count": count, "results": results}
        return fetch

    # --- detection ------------------------------------------------------

    def test_grave_question_with_name_detects(self):
        self.assertEqual(crm_bridge.detect("田中さんの墓所どこ？"), "田中")

    def test_full_name_and_polite_form_detects(self):
        self.assertEqual(crm_bridge.detect("佐藤一郎様のお墓の場所を教えて"), "佐藤一郎")

    def test_grave_smalltalk_without_name_stays_chat(self):
        self.assertIsNone(crm_bridge.detect("お墓参りどこ行く？"))

    def test_named_but_not_a_question_stays_chat(self):
        self.assertIsNone(crm_bridge.detect("田中さんの墓所の掃除をした"))

    def test_self_reference_stays_chat(self):
        self.assertIsNone(crm_bridge.detect("俺の墓はどこになるんだろうね"))

    # --- narrowing over two turns ---------------------------------------

    def _many_then_one(self):
        """A fetch that answers 6 for a surname and 1 once given a full name."""
        calls = []

        def fetch(url, token):
            calls.append(url)
            if "%20" in url or "+" in url.split("name=")[1].split("&")[0]:
                return 200, {"count": 1, "results": [
                    {"customer_name": "山田 太郎", "cemetery_name": "みたまA-12",
                     "area": ""}]}
            return 200, {"count": 6, "results": [
                {"customer_name": "山田 一郎", "cemetery_name": "みたまB-1", "area": ""},
                {"customer_name": "山田 二郎", "cemetery_name": "みたまB-2", "area": ""},
                {"customer_name": "山田 三郎", "cemetery_name": "みたまB-3", "area": ""}]}
        return fetch, calls

    def setUp(self):
        crm_bridge._pending.clear()

    def test_a_crowded_surname_asks_for_the_given_name(self):
        fetch, _ = self._many_then_one()
        reply = crm_bridge.intercept("山田さんの墓所どこ？", "ダイスケ", env=self.ENV,
                                     fetch=fetch, device_id="D1", now=100.0)
        self.assertIn("6件", reply)
        self.assertIn("下の名前", reply)

    def test_the_next_short_utterance_is_read_as_the_answer(self):
        fetch, calls = self._many_then_one()
        crm_bridge.intercept("山田さんの墓所どこ？", "ダイスケ", env=self.ENV,
                             fetch=fetch, device_id="D1", now=100.0)
        reply = crm_bridge.intercept("太郎", "ダイスケ", env=self.ENV, fetch=fetch,
                                     device_id="D1", now=110.0)
        self.assertIn("みたまA-12", reply)
        self.assertIn("%E5%B1%B1%E7%94%B0+%E5%A4%AA%E9%83%8E", calls[-1])

    def test_the_wrappers_people_put_around_an_answer_come_off(self):
        for said in ("下の名前は太郎", "太郎です", "太郎の方", "太郎さん"):
            with self.subTest(said=said):
                crm_bridge._pending.clear()
                fetch, calls = self._many_then_one()
                crm_bridge.intercept("山田さんの墓所どこ？", "ダイスケ", env=self.ENV,
                                     fetch=fetch, device_id="D1", now=100.0)
                reply = crm_bridge.intercept(said, "ダイスケ", env=self.ENV,
                                             fetch=fetch, device_id="D1", now=110.0)
                self.assertIn("みたまA-12", reply, said)

    def test_a_single_result_never_starts_a_wait(self):
        reply = crm_bridge.intercept(
            "田中さんの墓所どこ？", "ダイスケ", env=self.ENV,
            fetch=self._fetch(1, [{"customer_name": "田中", "cemetery_name": "専唱寺",
                                   "area": "郡司分"}]),
            device_id="D1", now=100.0)
        self.assertIn("専唱寺", reply)
        self.assertIsNone(crm_bridge.intercept("太郎", "ダイスケ", env=self.ENV,
                                               fetch=self._fetch(0, []),
                                               device_id="D1", now=110.0))

    def test_the_wait_expires(self):
        fetch, _ = self._many_then_one()
        crm_bridge.intercept("山田さんの墓所どこ？", "ダイスケ", env=self.ENV,
                             fetch=fetch, device_id="D1", now=100.0)
        self.assertIsNone(crm_bridge.intercept("太郎", "ダイスケ", env=self.ENV,
                                               fetch=fetch, device_id="D1",
                                               now=100.0 + 121.0))

    def test_another_body_is_not_answering_this_question(self):
        fetch, _ = self._many_then_one()
        crm_bridge.intercept("山田さんの墓所どこ？", "ダイスケ", env=self.ENV,
                             fetch=fetch, device_id="D1", now=100.0)
        self.assertIsNone(crm_bridge.intercept("太郎", "ダイスケ", env=self.ENV,
                                               fetch=fetch, device_id="D2", now=110.0))

    def test_another_person_is_not_answering_this_question(self):
        fetch, _ = self._many_then_one()
        crm_bridge.intercept("山田さんの墓所どこ？", "ダイスケ", env=self.ENV,
                             fetch=fetch, device_id="D1", now=100.0)
        self.assertIsNone(crm_bridge.intercept("太郎", "篠崎", env=self.ENV,
                                               fetch=fetch, device_id="D1", now=110.0))

    def test_a_long_sentence_is_a_person_moving_on(self):
        fetch, _ = self._many_then_one()
        crm_bridge.intercept("山田さんの墓所どこ？", "ダイスケ", env=self.ENV,
                             fetch=fetch, device_id="D1", now=100.0)
        self.assertIsNone(crm_bridge.intercept(
            "そういえば今日の天気ってどうだったっけ", "ダイスケ", env=self.ENV,
            fetch=fetch, device_id="D1", now=110.0))

    def test_giving_up_is_answered_not_looked_up(self):
        fetch, calls = self._many_then_one()
        crm_bridge.intercept("山田さんの墓所どこ？", "ダイスケ", env=self.ENV,
                             fetch=fetch, device_id="D1", now=100.0)
        before = len(calls)
        reply = crm_bridge.intercept("もういいや", "ダイスケ", env=self.ENV,
                                     fetch=fetch, device_id="D1", now=110.0)
        self.assertIn("やめておく", reply)
        self.assertEqual(len(calls), before)

    def test_a_wrong_given_name_costs_one_turn_not_the_lookup(self):
        fetch, _ = self._many_then_one()
        crm_bridge.intercept("山田さんの墓所どこ？", "ダイスケ", env=self.ENV,
                             fetch=fetch, device_id="D1", now=100.0)
        reply = crm_bridge.intercept("四郎", "ダイスケ", env=self.ENV,
                                     fetch=self._fetch(0, []), device_id="D1",
                                     now=110.0)
        self.assertIn("見つからなかった", reply)
        # Still waiting: the next attempt is still read as an answer.
        second = crm_bridge.intercept("太郎", "ダイスケ", env=self.ENV, fetch=fetch,
                                      device_id="D1", now=120.0)
        self.assertIn("みたまA-12", second)

    def test_a_fresh_question_supersedes_the_wait(self):
        fetch, _ = self._many_then_one()
        crm_bridge.intercept("山田さんの墓所どこ？", "ダイスケ", env=self.ENV,
                             fetch=fetch, device_id="D1", now=100.0)
        reply = crm_bridge.intercept(
            "田中さんの墓所どこ？", "ダイスケ", env=self.ENV,
            fetch=self._fetch(1, [{"customer_name": "田中", "cemetery_name": "専唱寺",
                                   "area": "郡司分"}]),
            device_id="D1", now=110.0)
        self.assertIn("専唱寺", reply)
        self.assertIsNone(crm_bridge.intercept("太郎", "ダイスケ", env=self.ENV,
                                               fetch=fetch, device_id="D1", now=120.0))

    def test_the_wait_holds_no_customer_records(self):
        # Only a surname and a deadline. Rows are re-fetched rather than
        # kept warm in case they are wanted again.
        fetch, _ = self._many_then_one()
        crm_bridge.intercept("山田さんの墓所どこ？", "ダイスケ", env=self.ENV,
                             fetch=fetch, device_id="D1", now=100.0)
        held = list(crm_bridge._pending.values())[0]
        self.assertEqual(set(held), {"surname", "expires_at"})

    # --- spoken forms (contract: CRM_LOOKUP_API_CONTRACT.md) ------------

    def test_single_result_with_area_speaks_district(self):
        reply = crm_bridge.intercept(
            "田中さんの墓所どこ？", "ダイスケ", env=self.ENV,
            fetch=self._fetch(1, [{"customer_name": "田中", "cemetery_name": "専唱寺",
                                   "area": "郡司分"}]))
        self.assertIn("郡司分地区の専唱寺", reply)

    def test_single_result_without_area_never_says_missing(self):
        reply = crm_bridge.intercept(
            "田中さんの墓所どこ？", "ダイスケ", env=self.ENV,
            fetch=self._fetch(1, [{"customer_name": "田中", "cemetery_name": "みたまA-12",
                                   "area": ""}]))
        self.assertIn("みたまA-12", reply)
        self.assertNotIn("分かりません", reply)
        self.assertNotIn("わからない", reply)

    def test_zero_results_is_an_answer_not_an_error(self):
        reply = crm_bridge.intercept("田中さんの墓所どこ？", "ダイスケ", env=self.ENV,
                                     fetch=self._fetch(0, []))
        self.assertIn("見つからなかった", reply)

    def test_many_results_reports_true_total_and_asks_to_narrow(self):
        rows = [{"customer_name": f"田中{i}", "cemetery_name": f"みたまB-{i}", "area": ""}
                for i in range(3)]
        reply = crm_bridge.intercept("田中さんの墓所どこ？", "ダイスケ", env=self.ENV,
                                     fetch=self._fetch(6, rows))
        self.assertIn("6件", reply)
        self.assertIn("下の名前", reply)

    def test_asked_by_is_passed_through(self):
        crm_bridge.intercept("田中さんの墓所どこ？", "ダイスケ", env=self.ENV,
                             fetch=self._fetch(0, []))
        self.assertIn("asked_by=%E3%83%80%E3%82%A4%E3%82%B9%E3%82%B1", self.fetched_url)

    # --- failure still answers here, never via the LLM ------------------

    def test_unreachable_relay_answers_spoken_failure(self):
        def fetch(url, token):
            raise OSError("no route")
        reply = crm_bridge.intercept("田中さんの墓所どこ？", "ダイスケ", env=self.ENV,
                                     fetch=fetch)
        self.assertIsNotNone(reply)
        self.assertIn("繋がらな", reply)

    def test_unconfigured_bridge_is_invisible(self):
        self.assertIsNone(crm_bridge.intercept("田中さんの墓所どこ？", "ダイスケ",
                                               env={}))


class CrmChatIntegrationTests(unittest.TestCase):
    """The chat path must not remember or forward a recognized CRM question."""

    def test_crm_reply_short_circuits_chat_without_memory(self):
        payload = {"request_id": "r", "session_id": "s", "device_id": "d",
                   "text": "田中さんの墓所どこ？"}
        with mock.patch.object(server.crm_bridge, "intercept", return_value="答え") as icpt, \
             mock.patch.object(server, "_remember_exchange") as remember, \
             mock.patch.object(server, "_provider_response") as provider, \
             mock.patch.object(server, "_tts_pcm", return_value=None), \
             mock.patch.object(server, "_generate_beep_pcm", return_value=b"\x00\x00"):
            status, body = server.process_chat(
                payload, {"Authorization": "Bearer t"}, {"DEVICE_TOKEN": "t"})
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "答え")
        icpt.assert_called_once()
        remember.assert_not_called()   # the exchange is never memorized
        provider.assert_not_called()   # and never reaches the LLM



class SttRequestTests(unittest.TestCase):
    """Transcription is the one task here with a right answer."""

    def _sent_body(self, env):
        captured = {}

        class _Response:
            status = 200

            def read(self, *a):
                return json.dumps({"candidates": [{"content": {"parts": [
                    {"text": "こんにちは"}]}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, *a, **kw):
            captured["body"] = json.loads(request.data.decode())
            return _Response()

        with mock.patch.object(server.urllib.request, "urlopen", fake_urlopen):
            server._gemini_stt_text(bytes(200), 16000, env)
        return captured["body"]

    ENV = {"AI_PROVIDER_API_KEY": "k"}

    def test_it_is_not_sampled(self):
        # The default temperature exists so a model can pick a different
        # word for variety, which is the opposite of a verbatim transcript.
        body = self._sent_body(self.ENV)
        self.assertEqual(body["generationConfig"]["temperature"], 0)

    def test_it_can_still_be_overridden_deliberately(self):
        body = self._sent_body(dict(self.ENV, GEMINI_STT_TEMPERATURE="0.4"))
        self.assertAlmostEqual(body["generationConfig"]["temperature"], 0.4)

    def test_a_busy_model_is_asked_once_more(self):
        # A dropped 503 costs the whole utterance, and the person repeats
        # themselves for a reason that had nothing to do with them.
        calls = []

        class _Response:
            status = 200

            def read(self, *a):
                return json.dumps({"candidates": [{"content": {"parts": [
                    {"text": "こんにちは"}]}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def flaky(request, *a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise server.urllib.error.HTTPError(
                    "u", 503, "busy", {}, io.BytesIO(b"{}"))
            return _Response()

        with mock.patch.object(server.urllib.request, "urlopen", flaky),              mock.patch.object(server.time, "sleep"):
            text = server._gemini_stt_text(bytes(200), 16000,
                                           dict(self.ENV, GEMINI_STT_RETRY_DELAY_SECONDS="0"))
        self.assertEqual(text, "こんにちは")
        self.assertEqual(len(calls), 2)

    def test_it_gives_up_after_one_retry(self):
        calls = []

        def always_busy(request, *a, **kw):
            calls.append(1)
            raise server.urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b"{}"))

        with mock.patch.object(server.urllib.request, "urlopen", always_busy),              mock.patch.object(server.time, "sleep"):
            self.assertIsNone(server._gemini_stt_text(bytes(200), 16000, self.ENV))
        self.assertEqual(len(calls), 2)

    def test_a_real_answer_is_not_retried(self):
        # 400 means the request was wrong; asking again wastes the wait.
        calls = []

        def refused(request, *a, **kw):
            calls.append(1)
            raise server.urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"{}"))

        with mock.patch.object(server.urllib.request, "urlopen", refused):
            self.assertIsNone(server._gemini_stt_text(bytes(200), 16000, self.ENV))
        self.assertEqual(len(calls), 1)

    def test_a_local_recogniser_may_be_plain_http(self):
        # The thing most worth pointing STT at is a speech recogniser
        # running on this machine, which is http://127.0.0.1 and always
        # will be. A loopback address never leaves the box.
        for url in ("http://127.0.0.1:9000/v1/audio/transcriptions",
                    "http://localhost:9000/v1/audio/transcriptions"):
            with self.subTest(url=url):
                self.assertTrue(server._is_loopback(url))

    def test_anything_off_the_box_still_needs_https(self):
        for url in ("http://192.168.11.5:9000/", "http://example.com/",
                    "http://127.0.0.1.evil.example/"):
            with self.subTest(url=url):
                self.assertFalse(server._is_loopback(url))

    def test_a_plain_http_recogniser_elsewhere_is_refused(self):
        status, _ = server._stt_response(
            b"", 16000,
            {"STT_PROVIDER": "openai", "STT_PROVIDER_URL": "http://192.168.11.5/x",
             "STT_PROVIDER_API_KEY": "k"})
        self.assertEqual(status, 503)

    def test_the_vocabulary_reaches_the_prompt(self):
        prompt = server._stt_prompt({"STT_VOCABULARY": "加江田, 佐土原 ,篠崎"})
        for word in ("加江田", "佐土原", "篠崎"):
            self.assertIn(word, prompt)

    def test_the_prompt_forbids_inventing_words(self):
        # Measured 2026-08-20: with a one-word vocabulary the model put
        # 大輔 on the end of a sentence nobody said it in, when the audio
        # was hard. A transcription prompt has to say this.
        self.assertIn("音声に含まれていない語を絶対に追加しないでください",
                      server._GEMINI_STT_PROMPT)

    def test_spelling_guidance_is_conditional_on_hearing_the_word(self):
        # "必ずこの表記を使ってください" reads as an instruction to produce
        # the word rather than to spell it a certain way.
        prompt = server._stt_prompt({"STT_VOCABULARY": "加江田,篠崎"})
        self.assertIn("実際に聞こえたときだけ", prompt)
        self.assertIn("聞こえなければ使わないでください", prompt)
        self.assertNotIn("必ずこの表記を使ってください", prompt)

    def test_an_empty_vocabulary_leaves_the_prompt_alone(self):
        self.assertEqual(server._stt_prompt({"STT_VOCABULARY": " , "}),
                         server._GEMINI_STT_PROMPT)


class DeviceTokenTests(unittest.TestCase):
    """More than one body, and only the bodies we gave a token to."""

    def _auth(self, supplied, configured):
        return server._authorized({"Authorization": f"Bearer {supplied}"},
                                  {"DEVICE_TOKEN": configured})

    def test_a_single_token_still_works(self):
        self.assertTrue(self._auth("abc", "abc"))
        self.assertFalse(self._auth("abc", "xyz"))

    def test_either_body_is_admitted(self):
        # The office robot is flashed with its own token; the home gateway
        # answered 401 to every poll it made until this existed.
        both = "home-token,office-token"
        self.assertTrue(self._auth("home-token", both))
        self.assertTrue(self._auth("office-token", both))

    def test_a_stranger_is_not(self):
        self.assertFalse(self._auth("guessed", "home-token,office-token"))

    def test_removing_an_entry_revokes_one_body(self):
        # What a single shared string could never do.
        self.assertFalse(self._auth("office-token", "home-token"))

    def test_spacing_in_the_list_is_forgiven(self):
        self.assertTrue(self._auth("office-token", " home-token , office-token "))

    def test_an_empty_entry_admits_nobody(self):
        self.assertFalse(self._auth("", "home-token,,"))
        self.assertFalse(self._auth("Bearer ", "home-token,,"))

    def test_no_token_configured_still_needs_the_dev_flag(self):
        self.assertFalse(server._authorized({}, {"DEVICE_TOKEN": ""}))
        self.assertTrue(server._authorized(
            {}, {"DEVICE_TOKEN": "", "AI_PROVIDER": "mock",
                 "ALLOW_INSECURE_DEV": "1"}))

    def test_the_header_name_is_case_insensitive(self):
        self.assertTrue(server._authorized({"authorization": "Bearer t"},
                                           {"DEVICE_TOKEN": "t"}))

class CrmFindTests(unittest.TestCase):
    """Searching by circumstance, and who may hear a phone number."""

    ENV = {"CRM_RELAY_URL": "http://relay.test", "CRM_RELAY_TOKEN": "t"}
    TODAY = datetime.date(2026, 8, 20)

    def setUp(self):
        crm_bridge._pending.clear()

    def _fetch(self, count, results=()):
        captured = {}

        def fetch(url, token):
            captured["url"] = url
            return 200, {"count": count, "results": list(results)}
        fetch.captured = captured
        return fetch

    # --- recognising the question --------------------------------------

    def test_the_whole_sentence_becomes_three_conditions(self):
        conditions = crm_bridge.detect_find(
            "最近受けた案件で加江田のお客さんで俺の担当のお客さんいなかったっけ？電話番号教えて",
            "ダイスケ", today=self.TODAY)
        self.assertEqual(conditions["area"], "加江田")
        self.assertEqual(conditions["staff"], "ダイスケ")
        self.assertEqual(conditions["since"], "2026-05-22")

    def test_mine_resolves_to_the_speaker(self):
        self.assertEqual(
            crm_bridge.detect_find("俺の担当のお客さん誰かいる？", "ダイスケ",
                                   today=self.TODAY)["staff"], "ダイスケ")

    def test_a_named_colleague_is_used_as_given(self):
        self.assertEqual(
            crm_bridge.detect_find("篠崎さんの担当のお客さん教えて", "ダイスケ",
                                   today=self.TODAY)["staff"], "篠崎")

    def test_periods_become_dates(self):
        for said, expected in (("今月", "2026-08-01"), ("先月", "2026-07-01"),
                               ("今年", "2026-01-01")):
            with self.subTest(said=said):
                conditions = crm_bridge.detect_find(
                    f"{said}のお客さん誰かいた？", "ダイスケ", today=self.TODAY)
                self.assertEqual(conditions["since"], expected)

    def test_a_phone_question_about_a_name_is_a_condition(self):
        # Otherwise it has no district, staff or period, falls through to
        # ordinary chat, and takes the customer's name to Gemini with it.
        conditions = crm_bridge.detect_find("田中さんの電話番号教えて", "ダイスケ",
                                            today=self.TODAY)
        self.assertEqual(conditions, {"name": "田中"})

    def test_a_role_word_is_not_taken_for_a_name(self):
        # "お客さんの電話番号" is not a request about somebody called お客.
        # Found by asking the live CRM and watching it search for a
        # customer named 客.
        conditions = crm_bridge.detect_find("佐土原のお客さんの電話番号教えて",
                                            "ダイスケ", today=self.TODAY)
        self.assertEqual(conditions, {"area": "佐土原"})
        self.assertNotIn("name", conditions)

    def test_a_role_word_is_not_taken_for_a_staff_member(self):
        self.assertIsNone(crm_bridge.detect_find("お客さんの担当誰？", "ダイスケ",
                                                 today=self.TODAY))

    def test_small_talk_is_left_alone(self):
        for said in ("今日は暑いね", "お客さん来たよ", "電話が鳴ってる"):
            with self.subTest(said=said):
                self.assertIsNone(crm_bridge.detect_find(said, "ダイスケ",
                                                         today=self.TODAY))

    def test_a_grave_question_is_not_a_find(self):
        self.assertIsNone(crm_bridge.detect_find("田中さんの墓所どこ？", "ダイスケ",
                                                 today=self.TODAY))

    # --- who may hear a number -----------------------------------------

    ONE = [{"customer_id": 12, "customer_name": "田中 太郎",
            "phone": "0985-00-0000", "cemetery_name": "みたまA-1"}]

    def test_master_hears_the_number(self):
        reply = crm_bridge.intercept("加江田のお客さんの電話番号教えて", "ダイスケ",
                                     env=self.ENV, fetch=self._fetch(1, self.ONE),
                                     device_id="D1", now=100.0, role="master")
        self.assertIn("0985-00-0000", reply)

    def test_a_colleague_is_pointed_at_the_screen_instead(self):
        for role in ("colleague", "household", "guest", "unknown"):
            with self.subTest(role=role):
                reply = crm_bridge.intercept(
                    "加江田のお客さんの電話番号教えて", "篠崎", env=self.ENV,
                    fetch=self._fetch(1, self.ONE), device_id="D1", now=100.0,
                    role=role)
                self.assertNotIn("0985-00-0000", reply)
                self.assertIn("CRMの画面", reply)

    def test_the_name_is_still_answered_to_everyone(self):
        # Where a grave is, and who the customer is, are not secrets from a
        # colleague standing at the office robot. The number is.
        reply = crm_bridge.intercept("加江田のお客さんの電話番号教えて", "篠崎",
                                     env=self.ENV, fetch=self._fetch(1, self.ONE),
                                     device_id="D1", now=100.0, role="colleague")
        self.assertIn("田中 太郎", reply)

    def test_a_missing_number_is_said_plainly(self):
        rows = [dict(self.ONE[0], phone="")]
        reply = crm_bridge.intercept("加江田のお客さんの電話番号教えて", "ダイスケ",
                                     env=self.ENV, fetch=self._fetch(1, rows),
                                     device_id="D1", now=100.0, role="master")
        self.assertIn("入っていなかった", reply)

    # --- the answer and the follow-up ----------------------------------

    def test_nobody_matching_is_said_with_the_conditions(self):
        reply = crm_bridge.intercept("加江田の俺の担当のお客さんいる？", "ダイスケ",
                                     env=self.ENV, fetch=self._fetch(0), device_id="D1",
                                     now=100.0, role="master")
        self.assertIn("加江田", reply)
        self.assertIn("見つからなかった", reply)

    def test_several_are_listed_and_a_name_is_asked_for(self):
        rows = [{"customer_id": 1, "customer_name": "田中", "phone": "1"},
                {"customer_id": 2, "customer_name": "佐藤", "phone": "2"}]
        reply = crm_bridge.intercept("加江田のお客さん誰かいる？", "ダイスケ",
                                     env=self.ENV, fetch=self._fetch(2, rows),
                                     device_id="D1", now=100.0, role="master")
        self.assertIn("2人", reply)
        self.assertIn("誰の電話番号", reply)
        # No phone read out while it is still ambiguous who is meant.
        self.assertNotIn("電話番号は1", reply)

    def test_the_follow_up_name_narrows_the_same_conditions(self):
        rows = [{"customer_id": 1, "customer_name": "田中", "phone": "1"},
                {"customer_id": 2, "customer_name": "佐藤", "phone": "2"}]
        crm_bridge.intercept("加江田のお客さん誰かいる？", "ダイスケ", env=self.ENV,
                             fetch=self._fetch(2, rows), device_id="D1", now=100.0,
                             role="master")
        narrowed = self._fetch(1, self.ONE)
        reply = crm_bridge.intercept("田中", "ダイスケ", env=self.ENV, fetch=narrowed,
                                     device_id="D1", now=110.0, role="master")
        self.assertIn("0985-00-0000", reply)
        self.assertIn("area=", narrowed.captured["url"])
        self.assertIn("name=", narrowed.captured["url"])

    def test_the_wait_holds_conditions_and_not_customers(self):
        rows = [{"customer_id": 1, "customer_name": "田中", "phone": "1"},
                {"customer_id": 2, "customer_name": "佐藤", "phone": "2"}]
        crm_bridge.intercept("加江田のお客さん誰かいる？", "ダイスケ", env=self.ENV,
                             fetch=self._fetch(2, rows), device_id="D1", now=100.0,
                             role="master")
        held = list(crm_bridge._pending.values())[0]
        self.assertEqual(set(held), {"conditions", "expires_at", "ids"})
        # ids are integers; the records they name stay in the CRM. That is
        # what makes "それモニターに出して" possible without keeping a
        # customer here between turns.
        self.assertTrue(all(isinstance(i, int) for i in held["ids"]))
        self.assertNotIn("田中", str(held))
        self.assertNotIn("phone", str(held))

    # --- putting a record on a screen ----------------------------------

    ONE_WITH_ID = [{"customer_id": 12, "customer_name": "田中 太郎",
                    "phone": "0985-00-0000", "cemetery_name": "みたまA-1"}]

    def _found_one(self, device_id="D1", env=None):
        crm_bridge.intercept("加江田のお客さん誰かいる？", "ダイスケ",
                             env=env or self.ENV, fetch=self._fetch(1, self.ONE_WITH_ID),
                             device_id=device_id, now=100.0, role="master")

    def test_the_phrases_people_use(self):
        for said in ("それモニターに出して", "画面に出して", "表示して",
                     "そっちに映して"):
            with self.subTest(said=said):
                self.assertTrue(crm_bridge.detect_show(said), said)

    def test_ordinary_talk_is_not_a_display_request(self):
        for said in ("お茶出して", "今日は暑いね", "加江田のお客さん誰かいる？"):
            with self.subTest(said=said):
                self.assertFalse(crm_bridge.detect_show(said), said)

    def test_showing_before_searching_asks_who(self):
        reply = crm_bridge.intercept("モニターに出して", "ダイスケ", env=self.ENV,
                                     fetch=self._fetch(0), device_id="D1",
                                     now=100.0, role="master")
        self.assertIn("先に誰のことか", reply)

    def test_the_office_body_drives_the_office_monitor(self):
        env = dict(self.ENV, CRM_OFFICE_DEVICE_IDS="80456B4DE7AC")
        self._found_one(device_id="80456B4DE7AC", env=env)
        seen = {}

        def fetch(url, token):
            seen["url"] = url
            return 200, {"opened": True, "url": "http://crm/?customer_id=12"}

        reply = crm_bridge.intercept("それモニターに出して", "ダイスケ", env=env,
                                     fetch=fetch, device_id="80456B4DE7AC",
                                     now=110.0, role="master")
        self.assertIn("/crm/open", seen["url"])
        self.assertIn("customer_id=12", seen["url"])
        self.assertIn("会社のモニター", reply)

    def test_any_other_body_opens_where_the_gateway_is(self):
        seen = {}

        def fetch(url, token):
            seen["url"] = url
            # The relay builds the record URL on the host the bridge named
            # (the `host=` it just passed); the mock must mirror that, or it
            # would return a link the real relay never would.
            host = urllib.parse.parse_qs(
                urllib.parse.urlsplit(url).query).get("host", ["relay.test:8765"])[0]
            return 200, {"url": f"http://{host}/?customer_id=12"}

        self._found_one(device_id="HOME")
        with mock.patch.object(crm_bridge.webbrowser, "open") as opened:
            reply = crm_bridge.intercept("それモニターに出して", "ダイスケ",
                                         env=self.ENV, fetch=fetch,
                                         device_id="HOME", now=110.0, role="master")
        self.assertIn("/crm/show", seen["url"])
        self.assertIn("host=", seen["url"])
        opened.assert_called_once_with("http://relay.test:8765/?customer_id=12")
        self.assertIn("ログイン", reply)

    def test_a_link_off_the_expected_host_is_refused(self):
        # A relay handing back a link to somewhere other than the host the
        # bridge named is a compromised or misconfigured relay; the home
        # side must refuse to open it (the office side already does).
        def fetch(url, token):
            return 200, {"url": "http://evil.example/?customer_id=12"}

        self._found_one(device_id="HOME")
        with mock.patch.object(crm_bridge.webbrowser, "open") as opened:
            reply = crm_bridge.intercept("それモニターに出して", "ダイスケ",
                                         env=self.ENV, fetch=fetch,
                                         device_id="HOME", now=110.0, role="master")
        opened.assert_not_called()
        self.assertIn("想定と違った", reply)

    def test_showing_is_ambiguous_while_several_matched(self):
        rows = [{"customer_id": 1, "customer_name": "田中"},
                {"customer_id": 2, "customer_name": "佐藤"}]
        crm_bridge.intercept("加江田のお客さん誰かいる？", "ダイスケ", env=self.ENV,
                             fetch=self._fetch(2, rows), device_id="D1",
                             now=100.0, role="master")
        reply = crm_bridge.intercept("モニターに出して", "ダイスケ", env=self.ENV,
                                     fetch=self._fetch(0), device_id="D1",
                                     now=110.0, role="master")
        self.assertIn("誰を出す", reply)

    def test_an_unreachable_office_says_so_rather_than_pretending(self):
        env = dict(self.ENV, CRM_OFFICE_DEVICE_IDS="80456B4DE7AC")
        self._found_one(device_id="80456B4DE7AC", env=env)

        def boom(url, token):
            raise OSError("office PC is off")

        reply = crm_bridge.intercept("それモニターに出して", "ダイスケ", env=env,
                                     fetch=boom, device_id="80456B4DE7AC",
                                     now=110.0, role="master")
        self.assertIn("出せなかった", reply)

    # --- failures still answer -----------------------------------------

    def test_an_unreachable_office_is_answered_not_passed_on(self):
        def boom(url, token):
            raise OSError("office PC is off")
        reply = crm_bridge.intercept("加江田のお客さんの電話番号教えて", "ダイスケ",
                                     env=self.ENV, fetch=boom, device_id="D1",
                                     now=100.0, role="master")
        self.assertIsNotNone(reply)
        self.assertIn("会社のパソコン", reply)

    def test_no_relay_configured_stays_out_of_the_way(self):
        self.assertIsNone(crm_bridge.intercept(
            "加江田のお客さんの電話番号教えて", "ダイスケ", env={}, device_id="D1",
            now=100.0, role="master"))


class GlassesTokenTest(unittest.TestCase):
    """The G2 entrance holds its own, weaker key (audit B4).

    /g2/config used to answer with DEVICE_TOKEN -- the token that opens
    /v1/speak, settings writes and people writes -- to anything that could
    reach the port.
    """

    MASTER = "master-token-aaaaaaaaaaaa"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = {"DEVICE_TOKEN": self.MASTER, "AI_PROVIDER": "mock",
                    "TACHIKOMA_MEMORY_DIR": self._tmp.name}

    def _headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_the_glasses_token_is_not_the_master_token(self):
        self.assertNotEqual(server.g2_token(self.env), self.MASTER)
        self.assertTrue(server.g2_token(self.env))

    def test_the_same_token_comes_back_next_launch(self):
        # The app fetches at every launch; a token that changed each time
        # would authenticate once and then read as a bug.
        self.assertEqual(server.g2_token(self.env), server.g2_token(self.env))

    def test_it_opens_chat_and_transcribe(self):
        headers = self._headers(server.g2_token(self.env))
        self.assertTrue(server._authorized(headers, self.env, allow_g2=True))

    def test_it_opens_nothing_else(self):
        headers = self._headers(server.g2_token(self.env))
        # settings, people, speak -- every route that does not pass
        # allow_g2 sees this token as no credential at all.
        self.assertFalse(server._authorized(headers, self.env))
        status, _ = server.process_settings_put({"first_person": "私"},
                                                headers, self.env)
        self.assertEqual(status, 401)

    def test_the_master_token_still_opens_everything(self):
        headers = self._headers(self.MASTER)
        self.assertTrue(server._authorized(headers, self.env))
        self.assertTrue(server._authorized(headers, self.env, allow_g2=True))

    def test_a_wrong_token_opens_nothing(self):
        headers = self._headers("not-the-token")
        self.assertFalse(server._authorized(headers, self.env, allow_g2=True))

    def test_an_unwritable_memory_dir_yields_no_token_rather_than_a_new_one(self):
        env = dict(self.env, TACHIKOMA_MEMORY_DIR=os.path.join(
            self._tmp.name, "nope.txt", "deeper"))
        with open(os.path.join(self._tmp.name, "nope.txt"), "w") as handle:
            handle.write("a file, not a directory")
        self.assertEqual(server.g2_token(env), "")
        # And an empty token must never authenticate an empty header.
        self.assertFalse(server._authorized({"Authorization": "Bearer "},
                                            env, allow_g2=True))


class LoopbackTest(unittest.TestCase):
    """Which addresses count as "this machine" for /g2/config (B4)."""

    def test_loopback_addresses(self):
        for address in ("127.0.0.1", "127.0.0.5", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(server._client_is_local(address), address)

    def test_the_house_lan_is_not_loopback(self):
        for address in ("192.168.2.50", "192.168.2.120", "100.76.60.88", ""):
            self.assertFalse(server._client_is_local(address), address)


class AuthThrottleTest(unittest.TestCase):
    """Guessing the token costs time (audit B5)."""

    def setUp(self):
        server._auth_failures.clear()
        self.addCleanup(server._auth_failures.clear)

    def test_a_few_misses_cost_nothing(self):
        for _ in range(server._AUTH_FAIL_LIMIT - 1):
            server.auth_record_failure("192.168.2.9", now=1000.0)
        self.assertEqual(server.auth_block_remaining("192.168.2.9", now=1000.0), 0.0)

    def test_enough_misses_buy_silence(self):
        for _ in range(server._AUTH_FAIL_LIMIT):
            server.auth_record_failure("192.168.2.9", now=1000.0)
        self.assertGreater(server.auth_block_remaining("192.168.2.9", now=1000.0), 0)

    def test_the_block_expires(self):
        for _ in range(server._AUTH_FAIL_LIMIT):
            server.auth_record_failure("192.168.2.9", now=1000.0)
        later = 1000.0 + server._AUTH_BLOCK_SECONDS + 1
        self.assertEqual(server.auth_block_remaining("192.168.2.9", now=later), 0.0)

    def test_a_persistent_guesser_waits_longer_each_time(self):
        first = self._block_after_a_round(at=1000.0)
        second = self._block_after_a_round(at=1000.0 + server._AUTH_BLOCK_SECONDS + 1)
        self.assertGreater(second, first)

    def _block_after_a_round(self, at):
        for _ in range(server._AUTH_FAIL_LIMIT):
            server.auth_record_failure("192.168.2.9", now=at)
        return server.auth_block_remaining("192.168.2.9", now=at)

    def test_slow_misses_never_add_up(self):
        # A body with a stale token retries occasionally for hours. That is
        # a misconfiguration to fix, not an attack to block.
        for i in range(server._AUTH_FAIL_LIMIT * 3):
            server.auth_record_failure("192.168.2.9",
                                       now=1000.0 + i * (server._AUTH_FAIL_WINDOW_SECONDS + 1))
        self.assertEqual(server.auth_block_remaining("192.168.2.9", now=99999.0), 0.0)

    def test_one_guesser_does_not_lock_out_the_house(self):
        for _ in range(server._AUTH_FAIL_LIMIT):
            server.auth_record_failure("192.168.2.9", now=1000.0)
        self.assertEqual(server.auth_block_remaining("192.168.2.120", now=1000.0), 0.0)

    def test_the_table_does_not_grow_without_limit(self):
        for i in range(1100):
            server.auth_record_failure(f"10.0.{i // 256}.{i % 256}", now=1000.0)
        server.auth_record_failure("10.9.9.9", now=1000.0 + 10_000)
        self.assertLessEqual(len(server._auth_failures), 1024)


if __name__ == "__main__":
    unittest.main()
