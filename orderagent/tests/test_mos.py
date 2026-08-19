import unittest

from orderagent import reconcile
from orderagent.adapter import MENU_ONLY, RestaurantAdapter
from orderagent.mos import MosAdapter, _parse_distance_km


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


class CartScrapeTest(unittest.TestCase):
    """The cart page, as the real one was shaped on 2026-08-19."""

    ROWS = [{"name": "モスバーガー※", "price": "500", "quantity": "2"},
            {"name": "フレンチフライポテト", "price": "290", "quantity": "1"}]
    BODY = "カート内の商品\nモスバーガー※\n500\nお支払い金額：\n1,290円(税込)"

    class _FakePage:
        def __init__(self, rows, body):
            self._rows = rows
            self._body = body

        def evaluate(self, _js, *_args):
            return self._rows

        def inner_text(self, _selector):
            return self._body

    def _scrape(self):
        from orderagent.mos import _scrape_cart
        return _scrape_cart(self._FakePage(self.ROWS, self.BODY))

    def test_reads_names_quantities_and_prices(self):
        cart = self._scrape()
        self.assertEqual(cart["cart_items"], [
            {"name": "モスバーガー", "price": 500, "quantity": 2},
            {"name": "フレンチフライポテト", "price": 290, "quantity": 1},
        ])

    def test_strips_the_reduced_tax_marker_from_the_name(self):
        # 「モスバーガー※」 is the same product as 「モスバーガー」; the mark
        # means the reduced tax rate applies, and quoting it back sounds
        # like a different item.
        self.assertEqual(self._scrape()["cart_items"][0]["name"], "モスバーガー")

    def test_reads_the_amount_the_site_will_charge(self):
        self.assertEqual(self._scrape()["cart_total_yen"], 1290)


class ClosedStoreTest(unittest.TestCase):
    def test_ordering_from_a_closed_store_is_refused_by_name(self):
        # Live, at night: every branch is shut, and the refusal has to say
        # which shop it is talking about.
        with self.assertRaises(RuntimeError) as caught:
            MosAdapter().build_cart("宮崎大島バイパス店", [], "j", lambda e, d: None)
        self.assertIn("宮崎大島バイパス店", str(caught.exception))


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
