"""Forwards one site's device traffic to a gateway running somewhere else.

The premise of the project is one persona reachable through several bodies,
and the memory store now matches that -- but only one gateway may own a
file-backed store, so the second body has to reach the first site's gateway
rather than run its own.

It cannot do that directly. Tailscale joins *computers*, and an ESP32 cannot
join it, so the robot has no route to a machine outside its own LAN. This
process is that route: it sits on a PC the robot can already reach, and
relays to the gateway over whatever link the PC has.

    office robot --(office LAN)--> this, on the office PC --(Tailscale)--> home gateway

The robot's provisioned URL does not change; it still points at a PC on its
own network. Nothing here inspects or decides anything -- the token, the
audio and the reply pass through untouched, so the gateway remains the only
thing that authenticates and the only thing that remembers.

    $env:FORWARDER_TARGET = "http://100.x.y.z:8080"    # the gateway's tailnet address
    python -u gateway/forwarder.py

Because the PC has to be running for the robot to speak, the robot is mute
whenever the PC is off. That is a property of the arrangement, not a fault:
outside office hours the office body logs failed polls and stays quiet, which
is exactly what a robot with nobody to talk to should do.
"""
from __future__ import annotations

import http.server
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

try:  # package import under the tests, plain when run as a script
    from . import logfile
except ImportError:  # pragma: no cover - depends on how it is started
    import logfile

TARGET = os.environ.get("FORWARDER_TARGET", "").rstrip("/")
HOST = os.environ.get("FORWARDER_HOST", "0.0.0.0")
PORT = int(os.environ.get("FORWARDER_PORT", "8080"))

# A reply is generated, spoken and returned within this window. Chat with web
# search grounding is the slow path; 30s recordings are the large one. Long
# enough not to cut those off, short enough that a hung upstream does not pin
# a worker thread all evening.
TIMEOUT_SECONDS = float(os.environ.get("FORWARDER_TIMEOUT_SECONDS", "60"))

# Bounded so a malformed or hostile request cannot be used to exhaust memory
# on the relaying PC. The largest legitimate body is a 30s 16kHz mono clip
# (~960KB); the gateway enforces its own limits behind this one.
MAX_BODY_BYTES = int(os.environ.get("FORWARDER_MAX_BODY_BYTES", str(8 * 1024 * 1024)))

# Hop-by-hop headers describe the single connection they arrived on and must
# not be copied onto the next one. Content-Length is recomputed from the body
# we actually forward.
_SKIP_REQUEST_HEADERS = {"host", "connection", "content-length", "keep-alive",
                         "proxy-connection", "transfer-encoding", "upgrade", "te"}
_SKIP_RESPONSE_HEADERS = {"connection", "content-length", "keep-alive",
                          "transfer-encoding", "upgrade", "server", "date"}

_stats_lock = threading.Lock()
_stats = {"polls": 0, "forwarded": 0, "failed": 0, "dropped": 0}
_last_summary = time.time()


def _log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def _is_speech_poll(path: str) -> bool:
    """The device polls this roughly every two seconds, forever.

    Logging each one would bury the entries that matter under thousands of
    lines saying nothing happened.
    """
    return path.startswith("/v1/speak_queue")


def _summarise_if_due() -> None:
    global _last_summary
    with _stats_lock:
        if time.time() - _last_summary < 60:
            return
        _last_summary = time.time()
        polls, forwarded = _stats["polls"], _stats["forwarded"]
        failed, dropped = _stats["failed"], _stats["dropped"]
        _stats.update(polls=0, forwarded=0, failed=0, dropped=0)
    # dropped is counted rather than merely swallowed: a connection the
    # device closed early is routine, but a sudden pile of them is the
    # shape of a robot that has started giving up on every reply.
    _log(f"forwarder 60s summary: speech_polls={polls} other={forwarded} "
         f"failed={failed} dropped={dropped}")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TachikomaForwarder/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        """Silence the default per-request line; _relay does the logging.

        The default also prints the full request line, and the browser page
        passes its token as a query parameter -- that would write a
        credential into the log on every request.
        """

    def do_GET(self) -> None:
        self._relay("GET")

    def do_POST(self) -> None:
        self._relay("POST")

    def do_PUT(self) -> None:
        self._relay("PUT")

    def _read_body(self) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            return None
        return self.rfile.read(length) if length else b""

    def _relay(self, method: str) -> None:
        if self.path == "/healthz":
            # Answered locally on purpose: it says this relay is up, which is
            # the question being asked when the robot has gone quiet.
            self._respond(200, b'{"ok":true}', {"Content-Type": "application/json"})
            return

        if not TARGET:
            self._respond(500, b'{"error":"forwarder_not_configured"}',
                          {"Content-Type": "application/json"})
            _log("forwarder has no FORWARDER_TARGET set; refusing to guess")
            return

        body = self._read_body()
        if body is None:
            self._respond(413, b'{"error":"invalid_input"}', {"Content-Type": "application/json"})
            return

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in _SKIP_REQUEST_HEADERS}
        request = urllib.request.Request(f"{TARGET}{self.path}", data=body,
                                         headers=headers, method=method)

        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = response.read()
                status = response.status
                out = {k: v for k, v in response.headers.items()
                       if k.lower() not in _SKIP_RESPONSE_HEADERS}
        except urllib.error.HTTPError as exc:
            # An upstream 401 or 400 is an answer, not a failure of this hop:
            # pass it through so the device sees what the gateway decided.
            payload = exc.read() or b""
            status = exc.code
            out = {k: v for k, v in exc.headers.items()
                   if k.lower() not in _SKIP_RESPONSE_HEADERS}
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            with _stats_lock:
                _stats["failed"] += 1
            _log(f"forwarder {method} {self.path.split('?')[0]} -> upstream unreachable "
                 f"({type(exc).__name__}) after {int((time.time() - started) * 1000)}ms")
            self._respond(502, b'{"error":"upstream_unreachable"}',
                          {"Content-Type": "application/json"})
            return

        elapsed_ms = int((time.time() - started) * 1000)
        quiet = _is_speech_poll(self.path) and 200 <= status < 300
        with _stats_lock:
            if quiet:
                _stats["polls"] += 1
            else:
                _stats["forwarded"] += 1
        if not quiet:
            # Path without its query string: the browser page's token rides
            # in the query, and nothing is worth writing a credential to disk.
            _log(f"forwarder {method} {self.path.split('?')[0]} -> {status} "
                 f"{len(payload)}B {elapsed_ms}ms")
        _summarise_if_due()
        self._respond(status, payload, out)

    def _respond(self, status: int, payload: bytes, headers: dict[str, str]) -> None:
        try:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # The device gives up on slow replies and retries. Its half of the
            # conversation ending is routine and must not take the thread with it.
            pass


class _SingleInstanceServer(http.server.ThreadingHTTPServer):
    """Refuses to start when the port is already taken.

    Python turns SO_REUSEADDR on by default, and on Windows that does not
    mean what it means elsewhere: a second process can bind a port another
    one is already listening on, and requests are split between them at
    random. The visible symptom is that a change does not take effect --
    the old process is still answering half the time.

    The CRM hit this and fixed it on 2026-08-19; the relay hit it on
    2026-08-20, an hour after reading their note about it. Failing to start
    is the correct behaviour: a process that is already running does not
    need a second one, and a person who meant to restart it would rather
    be told.
    """

    allow_reuse_address = False

    def handle_error(self, request, client_address) -> None:
        """One counted line for a dropped connection, not ten of traceback.

        The device hangs up on a reply it has waited too long for and asks
        again; _respond already treats that as routine on the way out. On
        the way in it was not handled at all, so ThreadingHTTPServer printed
        its default traceback -- which came to 62% of a 73MB log, and buried
        the entries that say what actually happened.

        Only the three ways a peer can vanish are quietened. Everything else
        still prints in full: a log that hides real faults would be worse
        than a log nobody can read.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            with _stats_lock:
                _stats["dropped"] += 1
            return
        super().handle_error(request, client_address)


def main() -> int:
    logfile.install()   # before anything is said, so nothing is said elsewhere
    if not TARGET:
        print("FORWARDER_TARGET is not set, e.g. http://100.x.y.z:8080", file=sys.stderr)
        return 2
    server = _SingleInstanceServer((HOST, PORT), Handler)
    _log(f"Tachikoma forwarder listening on {HOST}:{PORT} -> {TARGET}")
    _log("point the device's provisioned gateway URL at this PC's LAN address")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("forwarder stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
