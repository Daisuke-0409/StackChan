import unittest

from orderagent import adapter, draft, interpreter


MENU = [
    {"id": "100", "name": "ビッグマック", "price": 480},
    {"id": "101", "name": "ビッグマックセット", "price": 750},
    {"id": "200", "name": "チキンマックナゲット5ピース", "price": 260},
    {"id": "300", "name": "マックフライポテト", "price": 330},
]


class FakeAdapter:
    """A store that answers instantly and always the same way.

    The point of the seam: a lunch promotion that is or is not running, a
    branch with no drive-through, a menu that has changed under us -- all
    describable here, none of them requiring a real Tuesday.
    """

    chain = "fake"

    def __init__(self, menu=None, promotions=(), capabilities=None):
        self._menu = list(MENU if menu is None else menu)
        self._promotions = set(promotions)
        self._capabilities = capabilities or adapter.Capabilities(
            online_order=True, online_payment=True, pickup=True, promotions=True)
        self.cart_calls = []

    def find_stores(self, lat, lng, limit=3):
        return [{"id": "S1", "name": "テスト店", "distance_km": 0.4}]

    def capabilities(self, store_id):
        return self._capabilities

    def menu(self, store_id):
        return list(self._menu)

    def promotions(self, store_id):
        return set(self._promotions)

    def build_cart(self, store_id, items, job_id, log):
        self.cart_calls.append(items)
        total = sum(i["price"] * i["quantity"] for i in items)
        return {"cart_items": list(items), "cart_total_yen": total}


def _draft(*products, store_id="S1"):
    d = draft.new_draft("fake", store_id=store_id)
    for product in products:
        d.add(product)
    return d


def _match(text, menu_items):
    from orderagent import intent
    return intent.match_menu(text, menu_items)


class CapabilitiesTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(adapter.Capabilities(online_order=True, online_payment=True).level(),
                         adapter.FULL_AUTO)
        self.assertEqual(adapter.Capabilities(online_order=True).level(),
                         adapter.ORDER_ONLY)
        self.assertEqual(adapter.Capabilities().level(), adapter.MENU_ONLY)
        self.assertEqual(adapter.Capabilities(menu=False).level(), adapter.UNSUPPORTED)

    def test_a_store_that_cannot_order_is_still_useful(self):
        # "There is a KFC nine minutes away, but you order at the counter."
        caps = adapter.Capabilities(online_order=False)
        self.assertNotEqual(caps.level(), adapter.UNSUPPORTED)

    def test_unknown_fulfillment_is_not_promised(self):
        caps = adapter.Capabilities(pickup=True)
        self.assertTrue(caps.supports_fulfillment("takeout"))
        self.assertFalse(caps.supports_fulfillment("drive_thru"))
        self.assertFalse(caps.supports_fulfillment("teleport"))
        self.assertFalse(caps.supports_fulfillment(None))


class ResolveProductsTest(unittest.TestCase):
    def test_exact_names_resolve(self):
        d = _draft("ビッグマック", "マックフライポテト")
        result = adapter.resolve_products(d, FakeAdapter(), _match)
        self.assertTrue(result.ok())
        self.assertEqual([i.product_id for i in d.items], ["100", "300"])
        self.assertTrue(d.is_resolved())

    def test_an_unmatched_product_is_reported_not_guessed(self):
        d = _draft("存在しない商品")
        result = adapter.resolve_products(d, FakeAdapter(), _match)
        self.assertFalse(result.ok())
        self.assertEqual(result.unmatched, ["存在しない商品"])
        self.assertFalse(d.is_resolved())

    def test_the_guesser_is_only_asked_when_matching_fails(self):
        calls = []

        def guesser(text, menu_items):
            calls.append(text)
            return [{"id": "101", "name": "ビッグマックセット", "price": 750}]

        d = _draft("ビッグマック", "ダブチ")
        result = adapter.resolve_products(d, FakeAdapter(), _match, guesser)
        self.assertEqual(calls, ["ダブチ"])
        self.assertTrue(result.ok())

    def test_a_guess_is_recorded_so_it_can_be_read_aloud(self):
        def guesser(text, menu_items):
            return [{"id": "101", "name": "ビッグマックセット", "price": 750}]

        d = _draft("ダブチ")
        result = adapter.resolve_products(d, FakeAdapter(), _match, guesser)
        self.assertEqual(result.interpretations, [("ダブチ", "ビッグマックセット")])

    def test_the_spoken_name_is_kept(self):
        # The readback quotes what the user said; replacing it with the
        # catalogue name would hide the interpretation being made.
        def guesser(text, menu_items):
            return [{"id": "101", "name": "ビッグマックセット", "price": 750}]

        d = _draft("ダブチ")
        adapter.resolve_products(d, FakeAdapter(), _match, guesser)
        self.assertEqual(d.items[0].product, "ダブチ")

    def test_already_resolved_items_are_left_alone(self):
        d = _draft("ビッグマック")
        adapter.resolve_products(d, FakeAdapter(), _match)
        called = []
        adapter.resolve_products(d, FakeAdapter(), lambda t, m: called.append(t) or [])
        self.assertEqual(called, [])

    def test_a_draft_without_a_store_refuses(self):
        d = draft.new_draft("fake")
        d.add("ビッグマック")
        with self.assertRaises(draft.DraftError):
            adapter.resolve_products(d, FakeAdapter(), _match)


class ResolveConditionsTest(unittest.TestCase):
    def _with_condition(self, promotions):
        d = _draft("ビッグマックセット")
        u = interpreter.classify("ランチならL、普通ならM", d)
        interpreter.apply(u, d)
        return d, FakeAdapter(promotions=promotions)

    def test_a_running_promotion_takes_the_true_branch(self):
        d, store = self._with_condition({"lunch"})
        outcomes = adapter.resolve_conditions(d, store)
        self.assertEqual(outcomes, [("cond_1", True)])
        self.assertEqual(d.items[0].size, "L")

    def test_a_promotion_that_is_not_running_takes_the_false_branch(self):
        d, store = self._with_condition(set())
        adapter.resolve_conditions(d, store)
        self.assertEqual(d.items[0].size, "M")

    def test_a_store_that_cannot_tell_charges_the_ordinary_price(self):
        # The safe direction: no discount promised that was not offered.
        d, store = self._with_condition(set())
        adapter.resolve_conditions(d, store)
        self.assertIs(d.conditions[0].outcome, False)

    def test_nothing_pending_is_not_an_error(self):
        d = _draft("ポテト")
        self.assertEqual(adapter.resolve_conditions(d, FakeAdapter()), [])

    def test_an_unknown_condition_kind_stays_pending(self):
        d = _draft("ポテト")
        d.add_condition(None, "weather_is_nice", "sunny", {"size": "L"}, {})
        adapter.resolve_conditions(d, FakeAdapter())
        self.assertEqual(len(d.pending_conditions()), 1)


class ToCartItemsTest(unittest.TestCase):
    def _settled(self):
        d = _draft("ビッグマック", "マックフライポテト")
        adapter.resolve_products(d, FakeAdapter(), _match)
        return d

    def test_produces_what_build_cart_takes(self):
        items = adapter.to_cart_items(self._settled(), FakeAdapter())
        self.assertEqual(items, [
            {"id": "100", "name": "ビッグマック", "price": 480, "quantity": 1},
            {"id": "300", "name": "マックフライポテト", "price": 330, "quantity": 1},
        ])

    def test_quantities_carry(self):
        d = _draft("ビッグマック")
        d.modify(d.items[0].id, quantity=3)
        adapter.resolve_products(d, FakeAdapter(), _match)
        items = adapter.to_cart_items(d, FakeAdapter())
        self.assertEqual(items[0]["quantity"], 3)

    def test_an_unresolved_product_refuses(self):
        d = _draft("ビッグマック")
        with self.assertRaises(draft.DraftError):
            adapter.to_cart_items(d, FakeAdapter())

    def test_a_pending_condition_refuses(self):
        d = self._settled()
        interpreter.apply(interpreter.classify("ランチならLにして", d), d)
        with self.assertRaises(draft.DraftError):
            adapter.to_cart_items(d, FakeAdapter())

    def test_an_empty_draft_refuses(self):
        with self.assertRaises(draft.DraftError):
            adapter.to_cart_items(draft.new_draft("fake", store_id="S1"), FakeAdapter())

    def test_a_product_that_left_the_menu_refuses(self):
        # Prices and availability move between the match and the cart.
        d = self._settled()
        shrunk = FakeAdapter(menu=[MENU[0]])
        with self.assertRaises(draft.DraftError):
            adapter.to_cart_items(d, shrunk)

    def test_the_fake_adapter_satisfies_the_protocol(self):
        self.assertIsInstance(FakeAdapter(), adapter.RestaurantAdapter)


class EndToEndTest(unittest.TestCase):
    def test_a_whole_conversation_reaches_a_cart(self):
        d = draft.new_draft("fake", store_id="S1")
        for text in ("ビッグマック",
                     "あ、やっぱビッグマックセット",
                     "ランチならL、普通ならM",
                     "あとマックフライポテト"):
            u = interpreter.classify(text, d)
            if u.is_actionable():
                interpreter.apply(u, d)

        store = FakeAdapter(promotions={"lunch"})
        # Conditions first: answering one changes the size, and the size is
        # part of which product this is.
        adapter.resolve_conditions(d, store)
        self.assertTrue(adapter.resolve_products(d, store, _match).ok())

        items = adapter.to_cart_items(d, store)
        self.assertEqual([(i["name"], i["quantity"]) for i in items],
                         [("ビッグマックセット", 1), ("マックフライポテト", 1)])
        # The correction did not leave a plain burger behind, and the
        # condition picked the branch the store's promotion selected.
        self.assertEqual(len(items), 2)
        self.assertEqual(d.items[0].size, "L")

        cart = store.build_cart("S1", items, "job1", lambda e, x: None)
        self.assertEqual(cart["cart_total_yen"], 750 + 330)


if __name__ == "__main__":
    unittest.main()
