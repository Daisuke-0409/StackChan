import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
