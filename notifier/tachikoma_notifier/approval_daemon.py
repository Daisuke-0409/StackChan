"""R4: the approval relay's PC-side daemon -- Claude Code asks, Tachikoma
speaks, Daisuke answers, the decision travels back.

One HTTP endpoint on localhost (POST /approval, port 8378) receives the
PreToolUse hook JSON that claude_hook_approval.py forwards. For each request
the daemon walks the already-tested approval machinery end to end:

    hook JSON -> ApprovalRequest in ApprovalRequestStore
             -> announced through the existing SpeechSink path
                (the robot's speaker when TACHIKOMA_STACKCHAN_* is set)
             -> the desk microphone listens for one short answer
                (pc_ear's segmenter + calibration + /v1/transcribe)
             -> a clear no rejects; a clear yes goes through
                VoiceApprovalGate, which is the only road to "allow"
             -> {"decision": "allow" | "deny" | "ask", "reason": ...}

The safety invariant, stated once and enforced everywhere: anything that is
not a clear spoken yes on a low-risk read-only tool comes out "ask". A dead
gateway, a missing microphone, an expired window, an unparseable utterance,
a crashed handler -- all "ask", which the hook client turns into silence so
Claude Code falls through to its normal permission prompt. "deny" needs a
clear no. "allow" additionally needs every condition of the existing
VoiceApprovalGate (single pending request, risk_level=low, tool on
SAFE_VOICE_TOOL_NAMES, exact phrase match), so a risky tool is announced but
can never be voice-approved no matter what is heard.

The hook's tool_input never leaves the payload: not spoken, not logged, not
stored (safe_summary is built from tool_name alone, and the approval model's
metadata sanitizer would strip it anyway).

Run via run_approval_daemon.ps1, which loads gateway/.env for DEVICE_TOKEN.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Optional

from tachikoma_notifier.approval_store import ApprovalRequestStore, ApprovalStoreError
from tachikoma_notifier.approvals import (
    ApprovalActor,
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalRequest,
    ApprovalRiskLevel,
    ApprovalStatus,
    ConfirmationMethod,
    ReplayError,
)
from tachikoma_notifier.pc_ear import (
    FRAME_MS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    CALIBRATION_SECONDS,
    SILENCE_OVER_AMBIENT,
    SPEECH_OVER_AMBIENT,
    SPEECH_RMS_FLOOR,
    GatewayClient,
    UtteranceSegmenter,
    _rms,
    reply_mute_seconds,
)
from tachikoma_notifier.voice_approval_gate import (
    SAFE_VOICE_TOOL_NAMES,
    VoiceApprovalDenied,
    VoiceApprovalGate,
    normalize_utterance,
)

DAEMON_HOST = "127.0.0.1"  # localhost only, by design -- never configurable
DEFAULT_PORT = 8378
MAX_BODY_BYTES = 64 * 1024

# The whole answer budget, mute included. The hook client gives up at 30s,
# so announcement + mute + listening + transcription must fit under that.
ANSWER_WINDOW_SECONDS = 20.0
MIN_LISTEN_SECONDS = 8.0
APPROVAL_TTL_SECONDS = 120.0

# ---------------------------------------------------------------------------
# Every Japanese phrase the daemon speaks or listens for lives in this block.
#
# The announcement names what the tool does, not what it's called -- "Grep"
# read aloud by VOICEVOX means nothing; "ファイルの中身の検索" does. Unknown
# tools fall back to their raw name, truncated so the sentence stays short
# enough to sit through before the answer window opens.
_TOOL_LABELS = {
    "Read": "ファイルの読み取り",
    "Glob": "ファイル名の検索",
    "Grep": "ファイルの中身の検索",
    "Edit": "ファイルの編集",
    "Write": "ファイルの書き込み",
    "Bash": "コマンドの実行",
    "WebFetch": "ウェブページの取得",
    "WebSearch": "ウェブ検索",
}
_MAX_LABEL_CHARS = 12

# Only a voice-approvable tool gets asked "いいですか？". Inviting a yes that
# the gate would then refuse teaches the user that yes sometimes silently
# does nothing; a risky tool instead says where the real decision happens.
_ANNOUNCE_ASKABLE = "クロードコードが{label}をしたいそうです。いいですか？"
_ANNOUNCE_RISKY = "クロードコードが{label}をしたいそうです。パソコンで確認してね。"

# The only utterances that mean no. Exact match after the same normalization
# the approval gate uses -- a substring match would let "だめじゃないよ"
# deny. The yes side deliberately does NOT live here: yes phrases belong to
# voice_approval_gate.EXPLICIT_APPROVAL_PHRASES, the single place that can
# turn speech into an approval.
_NO_PHRASES = frozenset({"いいえ", "だめ", "ダメ", "駄目", "やめて"})
# ---------------------------------------------------------------------------

Listener = Callable[[str], Optional[str]]
Speaker = Callable[[str], bool]
Logger = Callable[[str], None]


def tool_risk_level(tool_name: str) -> ApprovalRiskLevel:
    """Low only for the gate's read-only allowlist; everything else is high.

    Misclassifying high would still be caught by the gate's own tool-name
    check, so this is belt on top of braces, not the braces.
    """
    if tool_name in SAFE_VOICE_TOOL_NAMES:
        return ApprovalRiskLevel.LOW
    return ApprovalRiskLevel.HIGH


def format_announcement(tool_name: str) -> str:
    label = _TOOL_LABELS.get(tool_name, tool_name)[:_MAX_LABEL_CHARS]
    template = (
        _ANNOUNCE_ASKABLE if tool_name in SAFE_VOICE_TOOL_NAMES else _ANNOUNCE_RISKY
    )
    return template.format(label=label)


def _result(decision: str, reason: str) -> dict[str, str]:
    return {"decision": decision, "reason": reason}


def _ask(reason: str) -> dict[str, str]:
    return _result("ask", reason)


class ApprovalDaemon:
    """One approval at a time: announce, listen, decide, answer.

    The speaker and listener are injected callables so the whole decision
    tree is testable without audio or network; production wiring happens in
    main(). handle_approval() never raises -- an exception anywhere inside
    is just another spelling of "ask".
    """

    def __init__(
        self,
        *,
        speak: Speaker,
        listen: Listener,
        store: Optional[ApprovalRequestStore] = None,
        log: Logger = print,
    ) -> None:
        # terminal_retention_seconds=0: the store's content-dedupe would
        # otherwise hand back a finished request when the same tool asks
        # twice within the retention window -- but for this daemon every
        # hook call is a genuinely new question.
        self._store = store or ApprovalRequestStore(terminal_retention_seconds=0.0)
        self._gate = VoiceApprovalGate(self._store)
        self._speak = speak
        self._listen = listen
        self._log = log
        self._busy = threading.Lock()

    def handle_approval(self, payload: Any) -> dict[str, str]:
        if not isinstance(payload, Mapping):
            return _ask("payload is not a JSON object")
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            return _ask("payload has no tool_name")
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            session_id = None
        # One microphone, one voice, one question at a time. A second hook
        # arriving mid-conversation cannot wait 20+ seconds inside its own
        # 30-second budget, so it gets its answer from the PC as usual.
        if not self._busy.acquire(blocking=False):
            return _ask("another approval is already in progress")
        try:
            return self._relay(tool_name.strip(), session_id)
        except Exception:
            return _ask("internal error")
        finally:
            self._busy.release()

    def _relay(self, tool_name: str, session_id: Optional[str]) -> dict[str, str]:
        request = self._register(tool_name, session_id)
        if request is None:
            return _ask("approval request could not be registered")

        announcement = format_announcement(tool_name)
        try:
            announced = bool(self._speak(announcement))
        except Exception:
            announced = False
        if not announced:
            # relay_failed is terminal and audited; the user heard nothing,
            # so nothing spoken afterwards could be an answer to this.
            self._advance(request, ApprovalStatus.RELAY_FAILED)
            return _ask("announcement failed")
        if not (
            self._advance(request, ApprovalStatus.ANNOUNCED)
            and self._advance(request, ApprovalStatus.AWAITING_CONFIRMATION)
        ):
            self._cancel(request)
            return _ask("approval request left the expected lifecycle")
        self._log(
            f"[approval] announced tool={tool_name} risk={request.risk_level.value}"
        )

        try:
            utterance = self._listen(announcement)
        except Exception:
            utterance = None
        if not utterance:
            self._cancel(request)
            return _ask("no clear answer within the window")

        if normalize_utterance(utterance) in _NO_PHRASES:
            # A clear no denies any tool, risky ones included -- refusing is
            # always safe, so it needs none of the approval gate's conditions.
            if self._reject(request):
                self._log(f"[approval] denied by voice tool={tool_name}")
                return _result("deny", "clear no by voice")
            self._cancel(request)
            return _ask("rejection could not be recorded")

        # Everything else -- yes phrases, mishearings, unrelated speech --
        # goes to the gate, the single authority on turning speech into an
        # approval. It re-checks risk, tool allowlist, single-pending, and
        # exact phrase match; any refusal is "ask", never "deny".
        try:
            self._gate.try_approve(request.approval_id, utterance)
        except VoiceApprovalDenied as exc:
            self._cancel(request)
            return _ask(str(exc))
        except (ApprovalStoreError, ReplayError):
            self._cancel(request)
            return _ask("approval could not be applied")
        self._log(f"[approval] approved by voice tool={tool_name}")
        return _result("allow", "clear yes on a low-risk read-only tool")

    def _register(self, tool_name: str, session_id: Optional[str]) -> Optional[ApprovalRequest]:
        # Two attempts: if a stale twin (same tool, same session, still
        # active from an earlier "ask" whose cleanup failed) occupies the
        # dedupe slot, close it and take its place. More than one leftover
        # for the same key cannot exist, so two attempts is exact, not lazy.
        for _ in range(2):
            try:
                request = ApprovalRequest.create(
                    source="claude_code",
                    session_id=session_id,
                    tool_name=tool_name,
                    safe_summary=f"Claude Code tool permission: {tool_name}",
                    risk_level=tool_risk_level(tool_name),
                    ttl_seconds=APPROVAL_TTL_SECONDS,
                )
                registered = self._store.register(request)
            except (ApprovalStoreError, ValueError):
                return None
            if registered.approval_id == request.approval_id:
                return registered
            self._cancel(registered)
        return None

    def _advance(self, request: ApprovalRequest, status: ApprovalStatus) -> bool:
        try:
            self._store.advance_status(request.approval_id, status)
            return True
        except (ApprovalStoreError, ValueError):
            return False

    def _reject(self, request: ApprovalRequest) -> bool:
        try:
            self._store.apply_decision(
                ApprovalDecision.create(
                    request.approval_id,
                    ApprovalDecisionType.REJECT,
                    actor=ApprovalActor.USER,
                    confirmation_method=ConfirmationMethod.VOICE,
                )
            )
            return True
        except (ApprovalStoreError, ReplayError, ValueError):
            return False

    def _cancel(self, request: ApprovalRequest) -> None:
        """Best-effort close, so a leftover can never block the next request.

        The voice gate requires exactly one pending request; a request
        abandoned as "ask" would otherwise sit pending until expiry and turn
        every approval in the next two minutes into "ask" as well.
        """
        try:
            self._store.apply_decision(
                ApprovalDecision.create(
                    request.approval_id,
                    ApprovalDecisionType.CANCEL,
                    actor=ApprovalActor.SYSTEM,
                    confirmation_method=ConfirmationMethod.SYSTEM,
                )
            )
        except (ApprovalStoreError, ReplayError, ValueError):
            pass


class MicAnswerListener:
    """Opens the desk microphone for one short spoken answer.

    Reuses pc_ear's pieces unchanged: room-tone calibration (hard-coded
    thresholds under the room tone are how the robot once recorded 30
    seconds per utterance), UtteranceSegmenter for the cut, and
    GatewayClient./v1/transcribe for the words. Before any of that it goes
    deaf for the announcement's estimated duration -- the question comes out
    of the robot's speaker, and the desk mic must not transcribe the robot
    asking it. The mute spends part of the fixed answer window rather than
    extending it, because the hook client's 30-second budget is the hard
    ceiling; MIN_LISTEN_SECONDS guarantees the human still gets a real turn.
    """

    def __init__(
        self,
        client: GatewayClient,
        *,
        input_device_substring: str = "",
        window_seconds: float = ANSWER_WINDOW_SECONDS,
        audio: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        log: Logger = print,
    ) -> None:
        self._client = client
        self._wanted = input_device_substring
        self._window_seconds = window_seconds
        self._audio = audio
        self._sleep = sleep
        self._clock = clock
        self._log = log

    def listen(self, announcement: str) -> Optional[str]:
        audio = self._audio_module()
        mute = reply_mute_seconds(announcement)
        self._log(f"[approval] muted {mute:.1f}s while the robot asks")
        self._sleep(mute)
        budget = max(MIN_LISTEN_SECONDS, self._window_seconds - mute)
        device = self._find_device(audio)
        with audio.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            device=device,
        ) as stream:
            segmenter = self._calibrated_segmenter(stream)
            deadline = self._clock() + budget
            while self._clock() < deadline:
                frame, _overflowed = stream.read(FRAME_SAMPLES)
                pcm = segmenter.feed(bytes(frame), int(self._clock() * 1000))
                if pcm is not None:
                    # transcribe() returns None on any gateway failure; the
                    # daemon reads that as no answer, which is "ask".
                    return self._client.transcribe(pcm)
        self._log("[approval] no utterance in the answer window")
        return None

    def _audio_module(self) -> Any:
        if self._audio is not None:
            return self._audio
        import sounddevice  # deferred: the daemon module must import without it

        return sounddevice

    def _find_device(self, audio: Any) -> Optional[int]:
        if not self._wanted:
            return None
        for index, info in enumerate(audio.query_devices()):
            if info["max_input_channels"] > 0 and self._wanted.lower() in info["name"].lower():
                return index
        self._log(f"[approval] no input device matching '{self._wanted}'; using default")
        return None

    def _calibrated_segmenter(self, stream: Any) -> UtteranceSegmenter:
        levels: list[int] = []
        for _ in range(int(CALIBRATION_SECONDS * 1000 / FRAME_MS)):
            frame, _overflowed = stream.read(FRAME_SAMPLES)
            levels.append(_rms(bytes(frame)))
        ambient = sorted(levels)[len(levels) // 2]
        speech_rms = int(os.environ.get("TACHIKOMA_EAR_SPEECH_RMS", "0")) or max(
            int(ambient * SPEECH_OVER_AMBIENT), SPEECH_RMS_FLOOR
        )
        silence_rms = int(os.environ.get("TACHIKOMA_EAR_SILENCE_RMS", "0")) or max(
            int(ambient * SILENCE_OVER_AMBIENT), int(SPEECH_RMS_FLOOR * 0.6)
        )
        self._log(
            f"[approval] ambient rms={ambient} -> speech>={speech_rms} silence>={silence_rms}"
        )
        return UtteranceSegmenter(
            speech_rms=speech_rms, silence_rms=silence_rms, log=self._log
        )


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: Mapping[str, Any]) -> None:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class ApprovalDaemonHandler(BaseHTTPRequestHandler):
    daemon: ApprovalDaemon

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            _json_response(self, 200, {"ok": True, "service": "tachikoma-approval-daemon"})
            return
        _json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/approval":
            _json_response(self, 404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid body length")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            # Even the error shape carries a decision, so the hook client
            # can treat every readable response uniformly.
            _json_response(self, 400, _ask("invalid request body"))
            return
        _json_response(self, 200, self.daemon.handle_approval(payload))

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never the payload: tool_input may hold commands and file contents.
        print(f"[approval] {self.command} {self.path} {args[1] if len(args) > 1 else ''}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Tachikoma voice approval daemon (R4)")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("TACHIKOMA_APPROVAL_PORT", str(DEFAULT_PORT)))
    )
    parser.add_argument(
        "--log-only", action="store_true", help="print announcements instead of speaking"
    )
    args = parser.parse_args()

    token = os.environ.get("DEVICE_TOKEN", "")
    if not token:
        # Announcements still work without a token; listening cannot, so
        # every answer will be "ask". Say so once instead of failing later.
        print(
            "DEVICE_TOKEN is empty; transcription will fail and every decision "
            "will be 'ask'. Run via run_approval_daemon.ps1 so gateway/.env is loaded.",
            file=sys.stderr,
        )
    base_url = os.environ.get("TACHIKOMA_EAR_GATEWAY", "http://127.0.0.1:8080")
    device_id = os.environ.get("TACHIKOMA_EAR_DEVICE_ID", "80456B4DE03C")
    wanted = os.environ.get("TACHIKOMA_EAR_INPUT_DEVICE", "UGREEN")

    # Imported here, not at module top: notifier.py pulls in the adapter
    # stack this module otherwise never needs.
    from tachikoma_notifier.notifier import _build_sink

    sink = _build_sink(args.log_only)
    client = GatewayClient(base_url, token, device_id)
    listener = MicAnswerListener(client, input_device_substring=wanted)
    daemon = ApprovalDaemon(speak=sink.speak, listen=listener.listen)

    handler = type("BoundApprovalDaemonHandler", (ApprovalDaemonHandler,), {"daemon": daemon})
    server = ThreadingHTTPServer((DAEMON_HOST, args.port), handler)
    print(f"[approval] gateway={base_url} device_id={device_id} mic~'{wanted}'")
    print(f"Tachikoma approval daemon listening on http://{DAEMON_HOST}:{args.port}/approval")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[approval] stopped")
        raise SystemExit(0)


__all__ = [
    "ANSWER_WINDOW_SECONDS",
    "ApprovalDaemon",
    "ApprovalDaemonHandler",
    "DAEMON_HOST",
    "DEFAULT_PORT",
    "MicAnswerListener",
    "format_announcement",
    "tool_risk_level",
]
