import socket
import unittest

from orderagent import server


class SingleInstanceTest(unittest.TestCase):
    """Two order agents on one port is the shortest path to paying twice.

    A stale instance keeps its own job table, its own approval state and
    its own reading of ORDER_PAYMENT_ENABLED, so an approval spoken to one
    can arrive at another that never read the order aloud.
    """

    def test_the_second_bind_is_refused(self):
        first = server._SingleInstanceServer(("127.0.0.1", 0), server.Handler)
        try:
            port = first.server_address[1]
            with self.assertRaises(OSError):
                server._SingleInstanceServer(("127.0.0.1", port), server.Handler)
        finally:
            first.server_close()

    def test_reuse_is_off(self):
        # The default is on, and on Windows that permits the hijack.
        self.assertFalse(server._SingleInstanceServer.allow_reuse_address)

    def test_a_plain_server_would_have_allowed_it(self):
        # Documents why the subclass exists rather than asserting a
        # library's default stays put.
        from http.server import ThreadingHTTPServer
        self.assertTrue(ThreadingHTTPServer.allow_reuse_address)

    def test_the_port_is_free_again_afterwards(self):
        first = server._SingleInstanceServer(("127.0.0.1", 0), server.Handler)
        port = first.server_address[1]
        first.server_close()
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()


if __name__ == "__main__":
    unittest.main()
