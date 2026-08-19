import unittest

from orderagent import reconcile
from orderagent.adapter import MENU_ONLY, RestaurantAdapter
from orderagent.mos import MosAdapter, NotYetWalked, _parse_distance_km


class ProtocolTest(unittest.TestCase):
    def test_satisfies_the_protocol(self):
        self.assertIsInstance(MosAdapter(), RestaurantAdapter)

    def test_chain_name(self):
        self.assertEqual(MosAdapter().chain, "mos")


class DistanceTest(unittest.TestCase):
    def test_kilometres(self):
        self.assertEqual(_parse_distance_km("22.8km"), 22.8)

    def test_metres(self):
        self.assertEqual(_parse_distance_km("700m"), 0.7)

    def test_nonsense_is_none(self):
        self.assertIsNone(_parse_distance_km("すぐ"))


class UnwalkedPartsTest(unittest.TestCase):
    """The parts scouted but not yet driven must refuse, not improvise."""

    def test_menu_refuses_rather_than_guessing(self):
        with self.assertRaises(NotYetWalked):
            MosAdapter().menu("宮崎大島バイパス店")

    def test_build_cart_refuses_rather_than_guessing(self):
        with self.assertRaises(NotYetWalked):
            MosAdapter().build_cart("宮崎大島バイパス店", [], "j", lambda e, d: None)

    def test_the_refusal_says_what_a_person_should_do(self):
        try:
            MosAdapter().menu("x")
        except NotYetWalked as exc:
            self.assertIn("実地確認", str(exc))


class SafeEmptinessTest(unittest.TestCase):
    def test_promotions_fall_to_the_false_branch(self):
        self.assertEqual(MosAdapter().promotions("x"), set())

    def test_recent_orders_report_unread_not_empty(self):
        evidence = MosAdapter().recent_orders("x")
        self.assertFalse(evidence.history_available)
        self.assertEqual(reconcile.reconcile(None, evidence, 0.0).outcome,
                         reconcile.UNRESOLVED)


if __name__ == "__main__":
    unittest.main()
