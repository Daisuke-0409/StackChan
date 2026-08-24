"""What the log file must do, so it stays readable and stays bounded.

The two faults being defended against both happened to forwarder.log:
it grew to 73MB with no ceiling, and it ended up holding two encodings
because PowerShell wrote part of it and something else wrote the rest.
"""
import io
import os
import tempfile
import unittest

from . import logfile


class RotatingLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tachikoma_log_")
        self.path = os.path.join(self.dir, "svc.log")

    def test_it_writes_utf8_that_a_plain_reader_can_read(self):
        log = logfile.RotatingLog(self.path, 1_000_000, 3)
        log.write("会社の体が黙った\n")
        log.close()

        # utf-8-sig, the way every reader in this repo opens a log.
        with io.open(self.path, encoding="utf-8-sig") as handle:
            self.assertEqual(handle.read(), "会社の体が黙った\n")

    def test_the_head_of_the_file_carries_a_bom(self):
        # Without it PowerShell's Get-Content reads the Japanese as ANSI.
        log = logfile.RotatingLog(self.path, 1_000_000, 3)
        log.write("x\n")
        log.close()
        with io.open(self.path, "rb") as handle:
            self.assertTrue(handle.read().startswith(b"\xef\xbb\xbf"))

    def test_it_appends_to_an_existing_file_without_a_second_bom(self):
        first = logfile.RotatingLog(self.path, 1_000_000, 3)
        first.write("one\n")
        first.close()
        second = logfile.RotatingLog(self.path, 1_000_000, 3)
        second.write("two\n")
        second.close()

        with io.open(self.path, "rb") as handle:
            self.assertEqual(handle.read().count(b"\xef\xbb\xbf"), 1)
        with io.open(self.path, encoding="utf-8-sig") as handle:
            self.assertEqual(handle.read(), "one\ntwo\n")

    def test_it_rotates_instead_of_growing_without_end(self):
        log = logfile.RotatingLog(self.path, 200, 2)
        for i in range(200):
            log.write(f"line {i} padded out to make this worth rotating\n")
        log.close()

        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(os.path.exists(self.path + ".1"))
        # keep=2 means .1 and .2 and no further.
        self.assertFalse(os.path.exists(self.path + ".3"))
        for name in (self.path, self.path + ".1", self.path + ".2"):
            if os.path.exists(name):
                self.assertLess(os.path.getsize(name), 200 * 4)

    def test_rotation_keeps_the_newest_lines_in_the_live_file(self):
        log = logfile.RotatingLog(self.path, 200, 2)
        for i in range(100):
            log.write(f"line {i} padded out to make this worth rotating\n")
        log.write("THE LAST THING SAID\n")
        log.close()
        with io.open(self.path, encoding="utf-8-sig") as handle:
            self.assertIn("THE LAST THING SAID", handle.read())


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tachikoma_log_")

    def test_no_setting_means_no_redirection(self):
        self.assertIsNone(logfile.install({}))

    def test_an_unopenable_path_does_not_stop_the_service(self):
        # A directory where the file should be: opening it must fail.
        blocked = os.path.join(self.dir, "in-the-way")
        os.makedirs(blocked)
        self.assertIsNone(logfile.install({"TACHIKOMA_LOG_FILE": blocked}))

    def test_a_bad_size_falls_back_instead_of_raising(self):
        path = os.path.join(self.dir, "svc.log")
        log = logfile.install({"TACHIKOMA_LOG_FILE": path,
                               "TACHIKOMA_LOG_MAX_BYTES": "not a number"})
        try:
            self.assertIsNotNone(log)
            self.assertEqual(log._max_bytes, logfile.DEFAULT_MAX_BYTES)
        finally:
            import sys
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__
            if log:
                log.close()


if __name__ == "__main__":
    unittest.main()
