"""ApprovalDaemon: the R4 safety invariants, as tests.

The one rule that matters: anything that is not a clear spoken yes on a
low-risk read-only tool must come out "ask" -- never "allow". Each test here
breaks the happy path at a different joint (announcer, microphone, gateway,
phrasing, tool risk) and checks the daemon lands on "ask" every time.
"""
import json
import struct
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tachikoma_notifier.approval_daemon import (
    ApprovalDaemon,
    ApprovalDaemonHandler,
    MicAnswerListener,
    format_announcement,
    tool_risk_level,
)
from tachikoma_notifier.approvals import ApprovalRiskLevel
from tachikoma_notifier.pc_ear import CALIBRATION_SECONDS, FRAME_MS, FRAME_SAMPLES

QUIET_FRAME = struct.pack(f"<{FRAME_SAMPLES}h", *([100] * FRAME_SAMPLES))
LOUD_FRAME = struct.pack(f"<{FRAME_SAMPLES}h", *([8000] * FRAME_SAMPLES))


def hook_payload(tool="Read", session="s1"):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": tool,
        "tool_input": {"file_path": "config.json"},
    }


class RecordingSpeaker:
    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.phrases = []

    def __call__(self, phrase):
        self.phrases.append(phrase)
        if self.error is not None:
            raise self.error
        return self.result


class RecordingListener:
    """Hands out scripted answers; None means the window closed empty."""

    def __init__(self, *answers, error=None):
        self.answers = list(answers)
        self.error = error
        self.calls = 0

    def __call__(self, announcement):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.answers.pop(0) if self.answers else None


def make_daemon(listener, speaker=None):
    speaker = speaker if speaker is not None else RecordingSpeaker()
    return ApprovalDaemon(speak=speaker, listen=listener, log=lambda _line: None)


class ApprovalDaemonDecisionTests(unittest.TestCase):
    def test_clear_yes_on_low_risk_tool_allows(self):
        speaker = RecordingSpeaker()
        daemon = make_daemon(RecordingListener("はい"), speaker)
        result = daemon.handle_approval(hook_payload("Read"))
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(len(speaker.phrases), 1)
        self.assertIn("クロードコード", speaker.phrases[0])

    def test_all_yes_phrases_allow(self):
        for phrase in ("いいよ", "オッケー", "はい。"):
            with self.subTest(phrase=phrase):
                result = make_daemon(RecordingListener(phrase)).handle_approval(
                    hook_payload("Grep")
                )
                self.assertEqual(result["decision"], "allow")

    def test_clear_no_denies(self):
        for phrase in ("いいえ", "だめ", "やめて"):
            with self.subTest(phrase=phrase):
                result = make_daemon(RecordingListener(phrase)).handle_approval(
                    hook_payload("Read")
                )
                self.assertEqual(result["decision"], "deny")

    def test_clear_no_denies_even_a_risky_tool(self):
        # Refusing is always safe, so "no" needs none of the gate's checks.
        result = make_daemon(RecordingListener("いいえ")).handle_approval(
            hook_payload("Bash")
        )
        self.assertEqual(result["decision"], "deny")

    def test_risky_tool_asks_despite_clear_yes(self):
        # THE invariant: a yes cannot approve what the gate does not allow.
        speaker = RecordingSpeaker()
        daemon = make_daemon(RecordingListener("はい"), speaker)
        result = daemon.handle_approval(hook_payload("Bash"))
        self.assertEqual(result["decision"], "ask")
        # ...but the tool was still announced aloud.
        self.assertEqual(len(speaker.phrases), 1)

    def test_unrecognized_speech_asks(self):
        result = make_daemon(RecordingListener("たぶん大丈夫")).handle_approval(
            hook_payload("Read")
        )
        self.assertEqual(result["decision"], "ask")

    def test_negation_containing_yes_substring_asks(self):
        result = make_daemon(RecordingListener("はいじゃないです")).handle_approval(
            hook_payload("Read")
        )
        self.assertEqual(result["decision"], "ask")

    def test_empty_answer_window_asks(self):
        result = make_daemon(RecordingListener(None)).handle_approval(
            hook_payload("Read")
        )
        self.assertEqual(result["decision"], "ask")

    def test_listener_crash_asks(self):
        # Gateway down, mic unplugged, sounddevice missing -- all of them
        # surface as an exception from the listener.
        result = make_daemon(
            RecordingListener(error=RuntimeError("mic exploded"))
        ).handle_approval(hook_payload("Read"))
        self.assertEqual(result["decision"], "ask")

    def test_announcer_failure_asks_and_never_listens(self):
        listener = RecordingListener("はい")
        daemon = make_daemon(listener, RecordingSpeaker(result=False))
        result = daemon.handle_approval(hook_payload("Read"))
        self.assertEqual(result["decision"], "ask")
        # If Daisuke heard no question, nothing he says is an answer to it.
        self.assertEqual(listener.calls, 0)

    def test_announcer_crash_asks(self):
        daemon = make_daemon(
            RecordingListener("はい"), RecordingSpeaker(error=OSError("no robot"))
        )
        self.assertEqual(daemon.handle_approval(hook_payload("Read"))["decision"], "ask")

    def test_missing_tool_name_asks_without_announcing(self):
        speaker = RecordingSpeaker()
        daemon = make_daemon(RecordingListener("はい"), speaker)
        payload = hook_payload("Read")
        del payload["tool_name"]
        self.assertEqual(daemon.handle_approval(payload)["decision"], "ask")
        self.assertEqual(speaker.phrases, [])

    def test_non_object_payload_asks(self):
        daemon = make_daemon(RecordingListener("はい"))
        self.assertEqual(daemon.handle_approval(["not", "an", "object"])["decision"], "ask")

    def test_busy_daemon_asks_instead_of_queueing(self):
        # One microphone, one conversation: a hook arriving mid-flow cannot
        # wait 20s inside its own 30s budget.
        daemon = make_daemon(RecordingListener("はい"))
        with daemon._busy:
            result = daemon.handle_approval(hook_payload("Read"))
        self.assertEqual(result["decision"], "ask")

    def test_request_after_an_ask_is_not_blocked_by_the_leftover(self):
        # An abandoned request must not clog the gate's single-pending rule
        # for the next two minutes.
        daemon = make_daemon(RecordingListener(None, "はい"))
        self.assertEqual(daemon.handle_approval(hook_payload("Read"))["decision"], "ask")
        self.assertEqual(daemon.handle_approval(hook_payload("Read"))["decision"], "allow")

    def test_repeated_approvals_of_the_same_tool_both_allow(self):
        # The store's content-dedupe must not hand the second hook call the
        # first one's finished request.
        daemon = make_daemon(RecordingListener("はい", "はい"))
        self.assertEqual(daemon.handle_approval(hook_payload("Read"))["decision"], "allow")
        self.assertEqual(daemon.handle_approval(hook_payload("Read"))["decision"], "allow")


class AnnouncementTests(unittest.TestCase):
    def test_low_risk_announcement_asks_a_question(self):
        text = format_announcement("Read")
        self.assertIn("ファイルの読み取り", text)
        self.assertIn("いいですか", text)
        self.assertLessEqual(len(text), 40)

    def test_risky_announcement_does_not_invite_a_yes(self):
        text = format_announcement("Bash")
        self.assertIn("コマンドの実行", text)
        self.assertNotIn("いいですか", text)

    def test_unknown_tool_falls_back_to_its_name_truncated(self):
        text = format_announcement("mcp__something_very_long_tool_name")
        self.assertIn("mcp__somethi", text)
        self.assertLessEqual(len(text), 45)

    def test_risk_level_tracks_the_gate_allowlist(self):
        self.assertIs(tool_risk_level("Read"), ApprovalRiskLevel.LOW)
        self.assertIs(tool_risk_level("Glob"), ApprovalRiskLevel.LOW)
        self.assertIs(tool_risk_level("Grep"), ApprovalRiskLevel.LOW)
        self.assertIs(tool_risk_level("Bash"), ApprovalRiskLevel.HIGH)
        self.assertIs(tool_risk_level("Write"), ApprovalRiskLevel.HIGH)


class FakeStream:
    """Feeds scripted frames, then silence forever."""

    def __init__(self, frames):
        self._frames = list(frames)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, _count):
        frame = self._frames.pop(0) if self._frames else QUIET_FRAME
        return frame, False


class FakeAudio:
    def __init__(self, frames):
        self._frames = frames

    def query_devices(self):
        return [{"name": "UGREEN desk mic", "max_input_channels": 1}]

    def RawInputStream(self, **_kwargs):  # noqa: N802 - mirrors sounddevice
        return FakeStream(self._frames)


class FakeClock:
    """Advances one frame's worth of time per reading, deterministically."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += FRAME_MS / 1000.0
        return self.now


class FakeGatewayClient:
    def __init__(self, text):
        self.text = text
        self.pcm = None

    def transcribe(self, pcm):
        self.pcm = pcm
        return self.text


def make_listener(client, frames):
    return MicAnswerListener(
        client,
        input_device_substring="UGREEN",
        audio=FakeAudio(frames),
        sleep=lambda _seconds: None,
        clock=FakeClock(),
        log=lambda _line: None,
    )


def answer_frames():
    calibration = [QUIET_FRAME] * int(CALIBRATION_SECONDS * 1000 / FRAME_MS)
    speech = [LOUD_FRAME] * 30          # 600ms of voice
    tail = [QUIET_FRAME] * 50           # 1000ms silence > the 800ms cut
    return calibration + speech + tail


class MicAnswerListenerTests(unittest.TestCase):
    def test_utterance_is_segmented_and_transcribed(self):
        client = FakeGatewayClient("はい")
        listener = make_listener(client, answer_frames())
        self.assertEqual(listener.listen(format_announcement("Read")), "はい")
        # The recording that went to the gateway holds the loud frames.
        self.assertGreaterEqual(len(client.pcm), 30 * len(LOUD_FRAME))

    def test_gateway_failure_becomes_no_answer(self):
        # GatewayClient.transcribe returns None on any HTTP failure; the
        # daemon reads None as "ask".
        client = FakeGatewayClient(None)
        listener = make_listener(client, answer_frames())
        self.assertIsNone(listener.listen(format_announcement("Read")))

    def test_silence_runs_out_the_window(self):
        client = FakeGatewayClient("はい")
        listener = make_listener(client, [])   # nothing but room tone
        self.assertIsNone(listener.listen(format_announcement("Read")))
        self.assertIsNone(client.pcm)

    def test_mutes_for_the_announcement_before_listening(self):
        # The question comes out of the robot's speaker; the desk mic must
        # not transcribe the robot asking it.
        slept = []
        listener = MicAnswerListener(
            FakeGatewayClient("はい"),
            audio=FakeAudio(answer_frames()),
            sleep=slept.append,
            clock=FakeClock(),
            log=lambda _line: None,
        )
        listener.listen(format_announcement("Read"))
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0.0)


class ApprovalDaemonHttpTests(unittest.TestCase):
    def _server(self, daemon):
        handler = type("BoundApprovalDaemonHandler", (ApprovalDaemonHandler,), {"daemon": daemon})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def _post(self, server, data):
        request = Request(
            f"http://127.0.0.1:{server.server_port}/approval",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_allow_round_trip(self):
        daemon = make_daemon(RecordingListener("はい"))
        server = self._server(daemon)
        body = self._post(server, json.dumps(hook_payload("Read")).encode("utf-8"))
        self.assertEqual(body["decision"], "allow")

    def test_invalid_json_is_a_400_that_still_says_ask(self):
        daemon = make_daemon(RecordingListener("はい"))
        server = self._server(daemon)
        request = Request(
            f"http://127.0.0.1:{server.server_port}/approval",
            data=b"{not-json",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(
            json.loads(raised.exception.read().decode("utf-8"))["decision"], "ask"
        )

    def test_unknown_path_is_404(self):
        daemon = make_daemon(RecordingListener("はい"))
        server = self._server(daemon)
        request = Request(
            f"http://127.0.0.1:{server.server_port}/elsewhere",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
