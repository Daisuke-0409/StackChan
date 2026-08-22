"""The redesign engine, wired into the real server (audit B1-B3).

These tests are about the three ways an order agent loses money or trust:
a slow payment misread as "nothing happened" (B2), a state change the
machine should not have (B1), and a crash that takes the evidence with it
(B3). Each was possible before this wiring; each is now a test.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orderagent import config, db, reconcile, server, states
from orderagent.starbucks import _settle_after_click, _settlement_verdict


SCREEN = "カート 総合計 590円 利用規約に同意の上、決済する"


class SettlementVerdictTest(unittest.TestCase):
    """B2: what the deadline may and may not claim."""

    def test_a_page_that_never_changed_is_not_placed(self):
        verdict = _settlement_verdict(SCREEN, SCREEN)
        self.assertEqual(verdict["outcome"], "NOT_PLACED")
        self.assertFalse(verdict["payment_executed"])

    def test_a_page_that_changed_without_a_number_is_unknown(self):
        # An error banner, a spinner, half a navigation -- all of these
        # "changed". None of them prove nothing was charged, so none of
        # them may invite a reorder.
        verdict = _settlement_verdict(SCREEN, SCREEN + " エラーが発生しました")
        self.assertEqual(verdict["outcome"], "UNKNOWN")
        self.assertTrue(verdict["payment_executed"])


class _SlowPage:
    """A page whose order number arrives on the third read."""

    def __init__(self, bodies):
        self._bodies = list(bodies)

    def inner_text(self, _selector):
        return self._bodies.pop(0) if len(self._bodies) > 1 else self._bodies[0]


class SettlePollTest(unittest.TestCase):
    """B2: the poll outlives a slow store where sleep(8) did not."""

    @mock.patch("orderagent.starbucks.SETTLE_POLL_SECONDS", 0.01)
    @mock.patch("orderagent.starbucks.SETTLE_DEADLINE_SECONDS", 1.0)
    def test_a_late_order_number_is_still_paid(self):
        page = _SlowPage([SCREEN, "処理中...", "注文番号 12345 ありがとうございました"])
        outcome = _settle_after_click(page, SCREEN)
        self.assertEqual(outcome["outcome"], "PAID")
        self.assertEqual(outcome["order_number"], "12345")

    @mock.patch("orderagent.starbucks.SETTLE_POLL_SECONDS", 0.01)
    @mock.patch("orderagent.starbucks.SETTLE_DEADLINE_SECONDS", 0.05)
    def test_reads_may_fail_without_raising(self):
        class _DeadPage:
            def inner_text(self, _selector):
                raise RuntimeError("navigation destroyed the context")
        outcome = _settle_after_click(_DeadPage(), SCREEN)
        # Nothing was ever readable after the click: the page we last saw
        # is the screen as it was before, and that alone cannot prove the
        # click was ignored -- but it is all the evidence there is.
        self.assertIn(outcome["outcome"], ("NOT_PLACED", "UNKNOWN"))


class SetStatusTest(unittest.TestCase):
    """B1: every status change goes through the machine."""

    def test_a_legal_move_moves(self):
        job = {"job_id": "j1", "status": states.CREATED}
        server._set_status(job, states.BUILDING)
        self.assertEqual(job["status"], states.BUILDING)

    def test_an_illegal_move_refuses_and_changes_nothing(self):
        job = {"job_id": "j1", "status": states.PAID}
        with self.assertRaises(states.IllegalTransition):
            server._set_status(job, states.VERIFYING)
        self.assertEqual(job["status"], states.PAID)

    def test_force_is_loud_but_does_not_die(self):
        job = {"job_id": "j1", "status": states.PAID}
        with mock.patch.object(server.db, "record"):
            server._set_status(job, states.FAILED, force=True)
        self.assertEqual(job["status"], states.FAILED)


def _snapshot_dict(total=590):
    return {"store_id": "s1", "store_name": "テスト店", "fulfillment": "takeout",
            "lines": [{"name": "ラテ", "price": total, "quantity": 1}],
            "total_yen": total, "payment_method_ref": None,
            "created_at": time.time()}


class _Adapter:
    def __init__(self, evidence):
        self._evidence = evidence

    def recent_orders(self, store_id):
        return self._evidence


class ReconcileWiringTest(unittest.TestCase):
    """B1: payment_uncertain now looks at the store instead of shrugging."""

    def _job(self):
        return {"job_id": "j1", "status": states.PAYMENT_UNCERTAIN,
                "chain": "starbucks", "store_key": "s1",
                "snapshot": _snapshot_dict(),
                "payment_attempted_at": time.time()}

    def _run(self, evidence):
        job = self._job()
        with mock.patch.object(server, "_adapter_for",
                               return_value=_Adapter(evidence)), \
             mock.patch.object(server.db, "record"):
            spoken = server._reconcile_uncertain(job)
        return job, spoken

    def test_a_matching_history_entry_settles_to_paid(self):
        job, spoken = self._run(reconcile.Evidence(
            history_available=True,
            orders=[{"store_id": "s1", "total_yen": 590,
                     "placed_at": time.time(), "order_number": "A77"}]))
        self.assertEqual(job["status"], states.PAID)
        self.assertEqual(job["order_number"], "A77")
        self.assertIn("A77", spoken)

    def test_a_readable_empty_history_settles_to_failed(self):
        job, spoken = self._run(reconcile.Evidence(history_available=True, orders=[]))
        self.assertEqual(job["status"], states.FAILED)
        self.assertIn("成立していない", spoken)

    def test_an_unreadable_history_stays_uncertain_and_says_so(self):
        job, spoken = self._run(reconcile.Evidence(history_available=False))
        self.assertEqual(job["status"], states.PAYMENT_UNCERTAIN)
        self.assertIn("再注文は止めている", spoken)

    def test_an_adapter_that_crashes_reads_as_unresolved(self):
        class _Broken:
            def recent_orders(self, store_id):
                raise RuntimeError("browser is gone")
        job = self._job()
        with mock.patch.object(server, "_adapter_for", return_value=_Broken()), \
             mock.patch.object(server.db, "record"):
            spoken = server._reconcile_uncertain(job)
        self.assertEqual(job["status"], states.PAYMENT_UNCERTAIN)
        self.assertIsNotNone(spoken)


class SweepTest(unittest.TestCase):
    """B3: what a crash leaves behind is found, settled and audible."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db_patch = mock.patch.object(
            config, "DB_PATH", Path(self._tmp.name) / "orders.db")
        self._db_patch.start()
        self.addCleanup(self._db_patch.stop)
        self.addCleanup(self._tmp.cleanup)
        server._jobs.clear()
        self.addCleanup(server._jobs.clear)
        self.announced = []
        announce = mock.patch.object(
            server, "_announce_async",
            side_effect=lambda device, text: self.announced.append(text))
        announce.start()
        self.addCleanup(announce.stop)

    def _seed(self, job_id, status, device_id="dev1"):
        db.upsert_order({"job_id": job_id, "created_at": time.time(),
                         "chain": "starbucks", "store_key": "s1",
                         "store_name": "テスト店",
                         "items": [{"name": "ラテ", "price": 590, "quantity": 1}],
                         "pickup": "takeout", "total_yen": 590,
                         "status": status, "device_id": device_id,
                         "snapshot": _snapshot_dict()})

    def test_a_job_that_died_verifying_becomes_uncertain_and_is_announced(self):
        self._seed("dead1", states.VERIFYING)
        with mock.patch.object(server, "_adapter_for",
                               return_value=_Adapter(
                                   reconcile.Evidence(history_available=False))):
            server._sweep_interrupted()
        job = server._jobs["dead1"]
        self.assertEqual(job["status"], states.PAYMENT_UNCERTAIN)
        self.assertTrue(any("再起動する前に決済していた" in t for t in self.announced))
        # And the database agrees, so a second crash changes nothing.
        self.assertEqual(db.load_unfinished([states.PAYMENT_UNCERTAIN])[0]["job_id"],
                         "dead1")

    def test_a_job_awaiting_approval_is_failed_and_announced(self):
        self._seed("dead2", states.AWAITING_APPROVAL)
        server._sweep_interrupted()
        self.assertEqual(server._jobs["dead2"]["status"], states.FAILED)
        self.assertTrue(any("取り消しちゃった" in t for t in self.announced))

    def test_a_mid_build_job_is_failed_quietly(self):
        self._seed("dead3", states.BUILDING)
        server._sweep_interrupted()
        self.assertEqual(server._jobs["dead3"]["status"], states.FAILED)
        self.assertEqual(self.announced, [])

    def test_an_uncertain_job_survives_the_sweep_unchanged(self):
        self._seed("dead4", states.PAYMENT_UNCERTAIN)
        server._sweep_interrupted()
        self.assertEqual(server._jobs["dead4"]["status"], states.PAYMENT_UNCERTAIN)

    def test_a_terminal_job_is_left_alone(self):
        self._seed("done1", states.PAID)
        server._sweep_interrupted()
        self.assertNotIn("done1", server._jobs)


if __name__ == "__main__":
    unittest.main()
