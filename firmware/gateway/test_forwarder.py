"""The forwarder's quiet handling of a device that hangs up.

The robot abandons a reply it has waited too long for and asks again.
That is normal, and _respond has always treated it as normal on the way
out. On the way in nothing handled it, so the server printed its default
traceback -- ten lines per occurrence, 62% of a 73MB log by 2026-08-24,
and the reason the entries that mattered could not be found.
"""
import unittest
from unittest import mock

from . import forwarder


class _FakeServer(forwarder._SingleInstanceServer):
    """The class under test without binding a port."""

    def __init__(self):  # noqa: D107 -- deliberately skips the socket setup
        pass


class HandleErrorTest(unittest.TestCase):
    def setUp(self):
        with forwarder._stats_lock:
            forwarder._stats.update(polls=0, forwarded=0, failed=0, dropped=0)

    def _raise_into_handle_error(self, exc):
        server = _FakeServer()
        with mock.patch.object(forwarder.http.server.ThreadingHTTPServer,
                               "handle_error") as fallback:
            try:
                raise exc
            except type(exc):
                server.handle_error(object(), ("127.0.0.1", 1234))
        return fallback

    def test_a_reset_connection_is_counted_not_printed(self):
        fallback = self._raise_into_handle_error(ConnectionResetError(10054, "reset"))
        fallback.assert_not_called()
        self.assertEqual(forwarder._stats["dropped"], 1)

    def test_an_aborted_connection_is_counted_not_printed(self):
        fallback = self._raise_into_handle_error(ConnectionAbortedError(10053, "abort"))
        fallback.assert_not_called()
        self.assertEqual(forwarder._stats["dropped"], 1)

    def test_a_broken_pipe_is_counted_not_printed(self):
        fallback = self._raise_into_handle_error(BrokenPipeError(32, "pipe"))
        fallback.assert_not_called()
        self.assertEqual(forwarder._stats["dropped"], 1)

    def test_any_other_fault_still_prints_in_full(self):
        # The point of quietening three exceptions is that everything else
        # stays loud. A log that hides real faults is worse than a noisy one.
        fallback = self._raise_into_handle_error(ValueError("something real"))
        fallback.assert_called_once()
        self.assertEqual(forwarder._stats["dropped"], 0)

    def test_a_timeout_is_not_treated_as_a_dropped_connection(self):
        # TimeoutError is an OSError like the others and is not one of them:
        # a peer that stopped answering is worth seeing.
        fallback = self._raise_into_handle_error(TimeoutError("timed out"))
        fallback.assert_called_once()
        self.assertEqual(forwarder._stats["dropped"], 0)


class SummaryTest(unittest.TestCase):
    def test_dropped_connections_appear_in_the_summary(self):
        with forwarder._stats_lock:
            forwarder._stats.update(polls=30, forwarded=2, failed=0, dropped=7)
        forwarder._last_summary = 0.0
        with mock.patch.object(forwarder, "_log") as logged:
            forwarder._summarise_if_due()
        line = logged.call_args[0][0]
        self.assertIn("dropped=7", line)
        self.assertIn("speech_polls=30", line)

    def test_the_counters_reset_after_a_summary(self):
        with forwarder._stats_lock:
            forwarder._stats.update(polls=1, forwarded=1, failed=1, dropped=1)
        forwarder._last_summary = 0.0
        with mock.patch.object(forwarder, "_log"):
            forwarder._summarise_if_due()
        self.assertEqual(forwarder._stats["dropped"], 0)


if __name__ == "__main__":
    unittest.main()
