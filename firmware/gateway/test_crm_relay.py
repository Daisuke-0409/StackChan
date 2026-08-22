"""Tests for the office-side relay, without an office.

The relay has been verified live because that is where the CRM is, but a
live check only holds while somebody is watching. These pin down the parts
that are ours rather than the CRM's: what crosses the boundary, what is
refused, and what never reaches a log.
"""
import unittest
from unittest import mock

from . import crm_relay


def _payload(count, rows):
    return 200, {"count": count, "results": list(rows)}


GRAVE = {"customer_name": "田中 太郎", "cemetery_name": "みたまA-12",
         "area": "郡司分", "zenrin_map_no": "12-I-4",
         "address": "宮崎市…", "deceased_name": "田中 花子"}

CUSTOMER = {"customer_id": 12, "customer_name": "田中 太郎",
            "phone": "0985-00-0000", "cemetery_name": "みたまA-12",
            "address": "宮崎市…", "order_date": "2026-07-01"}


class WhitelistTest(unittest.TestCase):
    """The boundary is the list, not the CRM's good manners."""

    def test_a_grave_row_loses_everything_unsayable(self):
        with mock.patch.object(crm_relay, "_get_json",
                               return_value=_payload(1, [GRAVE])):
            _, rows = crm_relay.lookup("田中", "ダイスケ")
        self.assertEqual(set(rows[0]), {"customer_name", "cemetery_name", "area"})

    def test_the_map_sheet_never_crosses(self):
        # A paper atlas index cannot be said aloud usefully, so it is not
        # carried at all.
        with mock.patch.object(crm_relay, "_get_json",
                               return_value=_payload(1, [GRAVE])):
            _, rows = crm_relay.lookup("田中", "ダイスケ")
        self.assertNotIn("zenrin_map_no", rows[0])

    def test_a_found_customer_keeps_only_four_fields(self):
        with mock.patch.object(crm_relay, "_get_json",
                               return_value=_payload(1, [CUSTOMER])):
            _, rows = crm_relay.find({"area": "加江田"}, "ダイスケ")
        self.assertEqual(set(rows[0]),
                         {"customer_id", "customer_name", "phone", "cemetery_name"})

    def test_a_column_the_crm_grows_later_does_not_start_crossing(self):
        grown = dict(GRAVE, mobile="090-…", note="要注意")
        with mock.patch.object(crm_relay, "_get_json",
                               return_value=_payload(1, [grown])):
            _, rows = crm_relay.lookup("田中", "ダイスケ")
        self.assertNotIn("mobile", rows[0])
        self.assertNotIn("note", rows[0])

    def test_the_two_lists_are_separate(self):
        # Widening the grave lookup must not widen the phone search.
        self.assertNotIn("phone", crm_relay.ALLOWED_FIELDS)
        self.assertIn("phone", crm_relay.ALLOWED_FIND_FIELDS)

    def test_show_carries_a_link_and_nothing_else(self):
        with mock.patch.object(crm_relay, "_get_json",
                               return_value=(200, {"url": "http://x/?customer_id=1",
                                                   "customer_name": "田中 太郎"})):
            result = crm_relay.show("1", "ダイスケ")
        self.assertEqual(result, {"url": "http://x/?customer_id=1"})
        self.assertNotIn("customer_name", result)

    def test_empty_values_are_dropped_rather_than_spoken_as_blanks(self):
        with mock.patch.object(crm_relay, "_get_json",
                               return_value=_payload(1, [dict(GRAVE, area="")])):
            _, rows = crm_relay.lookup("田中", "ダイスケ")
        self.assertNotIn("area", rows[0])


class CountTest(unittest.TestCase):
    def test_the_total_survives_the_cap(self):
        # "There are seven, narrow it down" needs the seven.
        rows = [dict(GRAVE, customer_name=f"田中 {i}") for i in range(3)]
        with mock.patch.object(crm_relay, "_get_json",
                               return_value=_payload(7, rows)):
            total, returned = crm_relay.lookup("田中", "ダイスケ")
        self.assertEqual(total, 7)
        self.assertEqual(len(returned), 3)

    def test_more_rows_than_the_cap_are_trimmed(self):
        rows = [dict(GRAVE, customer_name=f"田中 {i}") for i in range(9)]
        with mock.patch.object(crm_relay, "_get_json",
                               return_value=_payload(9, rows)):
            _, returned = crm_relay.lookup("田中", "ダイスケ")
        self.assertLessEqual(len(returned), crm_relay.MAX_RESULTS)


class FindRequestTest(unittest.TestCase):
    def test_blank_conditions_are_not_sent(self):
        seen = {}

        def capture(url, headers, timeout):
            seen["url"] = url
            return _payload(0, [])

        with mock.patch.object(crm_relay, "_get_json", side_effect=capture):
            crm_relay.find({"area": "加江田", "staff": "", "since": None},
                           "ダイスケ")
        self.assertIn("area=", seen["url"])
        self.assertNotIn("staff=", seen["url"])
        self.assertNotIn("since=", seen["url"])

    def test_show_can_ask_for_a_link_reachable_from_elsewhere(self):
        seen = {}

        def capture(url, headers, timeout):
            seen["headers"] = headers
            return 200, {"url": "http://office:8765/?customer_id=1"}

        with mock.patch.object(crm_relay, "_get_json", side_effect=capture):
            crm_relay.show("1", "ダイスケ", host="office:8765")
        self.assertEqual(seen["headers"].get("Host"), "office:8765")


class LoggingTest(unittest.TestCase):
    def test_the_request_line_never_reaches_the_log(self):
        # It carries the name searched for -- a real customer -- into a
        # file on the office PC that nobody is accountable for. The CRM's
        # audit log is where that record belongs.
        said = []
        handler = crm_relay.Handler.__new__(crm_relay.Handler)
        handler.path = "/crm/lookup?name=%E7%94%B0%E4%B8%AD&asked_by=x"
        with mock.patch.object(crm_relay.Handler, "log_message",
                               lambda self, fmt, *args: said.append(fmt % args)):
            crm_relay.Handler.log_request(handler, 200)
        self.assertEqual(said, ["/crm/lookup -> 200"])
        self.assertNotIn("name=", said[0])


class OpenTest(unittest.TestCase):
    """The office monitor, driven by id and never by URL."""

    def test_it_opens_the_link_the_crm_gave(self):
        with mock.patch.object(crm_relay, "_get_json",
                               return_value=(200, {"url": crm_relay.CRM_BASE_URL + "/?customer_id=12"})),              mock.patch.object(crm_relay.webbrowser, "open") as opened:
            result = crm_relay.open_on_this_screen("12", "ダイスケ")
        opened.assert_called_once()
        self.assertTrue(result["opened"])

    def test_a_link_pointing_anywhere_else_is_refused(self):
        # The caller passes an id, never a URL -- but the CRM's reply is
        # still checked, because an endpoint that opens whatever it is
        # handed is a way to make the office PC visit anything.
        with mock.patch.object(crm_relay, "_get_json",
                               return_value=(200, {"url": "http://evil.example/"})),              mock.patch.object(crm_relay.webbrowser, "open") as opened:
            with self.assertRaises(ValueError):
                crm_relay.open_on_this_screen("12", "ダイスケ")
        opened.assert_not_called()

    def test_a_missing_link_opens_nothing(self):
        with mock.patch.object(crm_relay, "_get_json", return_value=(200, {})),              mock.patch.object(crm_relay.webbrowser, "open") as opened:
            with self.assertRaises(ValueError):
                crm_relay.open_on_this_screen("12", "ダイスケ")
        opened.assert_not_called()


class StartupTest(unittest.TestCase):
    def test_it_refuses_to_run_twice(self):
        first = crm_relay._SingleInstanceServer(("127.0.0.1", 0),
                                                crm_relay.Handler)
        try:
            with self.assertRaises(OSError):
                crm_relay._SingleInstanceServer(
                    ("127.0.0.1", first.server_address[1]), crm_relay.Handler)
        finally:
            first.server_close()

    def test_an_open_relay_is_not_a_thing_it_can_be(self):
        # It listens on the tailnet; starting without a token would put the
        # ledger one request from anything that can reach this PC.
        with mock.patch.object(crm_relay, "RELAY_TOKEN", ""):
            with self.assertRaises(SystemExit):
                crm_relay.main()


class TokenComparisonTest(unittest.TestCase):
    """The relay answers with the customer ledger, so the token is the
    whole boundary (audit B5). It used to be compared with !=, which
    returns on the first differing byte and leaks the prefix by timing."""

    def test_the_right_token_passes(self):
        with mock.patch.object(crm_relay, "RELAY_TOKEN", "s3cret-token-value"):
            self.assertTrue(crm_relay.token_ok("s3cret-token-value"))

    def test_a_wrong_token_fails(self):
        with mock.patch.object(crm_relay, "RELAY_TOKEN", "s3cret-token-value"):
            self.assertFalse(crm_relay.token_ok("s3cret-token-valuf"))
            self.assertFalse(crm_relay.token_ok("s3cret"))
            self.assertFalse(crm_relay.token_ok(""))

    def test_no_configured_token_admits_nobody(self):
        # Not even the empty string: an unconfigured relay must be shut,
        # not open to whoever sends no header at all.
        with mock.patch.object(crm_relay, "RELAY_TOKEN", ""):
            self.assertFalse(crm_relay.token_ok(""))


class RelayThrottleTest(unittest.TestCase):
    """Guessing costs time here too (audit B5)."""

    def setUp(self):
        crm_relay._failures.clear()
        self.addCleanup(crm_relay._failures.clear)

    def test_a_few_misses_cost_nothing(self):
        for _ in range(crm_relay._FAIL_LIMIT - 1):
            crm_relay.record_failure("100.1.1.1", now=500.0)
        self.assertEqual(crm_relay.block_remaining("100.1.1.1", now=500.0), 0.0)

    def test_enough_misses_buy_silence(self):
        for _ in range(crm_relay._FAIL_LIMIT):
            crm_relay.record_failure("100.1.1.1", now=500.0)
        self.assertGreater(crm_relay.block_remaining("100.1.1.1", now=500.0), 0)

    def test_the_gateway_is_not_locked_out_by_someone_else(self):
        for _ in range(crm_relay._FAIL_LIMIT):
            crm_relay.record_failure("100.1.1.1", now=500.0)
        self.assertEqual(crm_relay.block_remaining("100.76.60.88", now=500.0), 0.0)


if __name__ == "__main__":
    unittest.main()
