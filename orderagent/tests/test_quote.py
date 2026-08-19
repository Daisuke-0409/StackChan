import unittest

from orderagent import quote


def _cart(*rows, total=None):
    items = [{"name": n, "price": p, "quantity": q} for n, p, q in rows]
    if total is None:
        total = sum(p * q for _, p, q in rows)
    return {"cart_items": items, "cart_total_yen": total}


REQUESTED = [
    {"id": "101", "name": "ビッグマックセット", "price": 750, "quantity": 1},
    {"id": "300", "name": "マックフライポテト", "price": 330, "quantity": 1},
]


class LinesFromCartTest(unittest.TestCase):
    def test_identical_rows_are_counted_together(self):
        cart = _cart(("ポテト", 330, 1), ("ポテト", 330, 1), ("バーガー", 480, 1))
        lines = quote.lines_from_cart(cart)
        self.assertEqual([(l.name, l.quantity) for l in lines],
                         [("ポテト", 2), ("バーガー", 1)])

    def test_same_name_at_a_different_price_stays_separate(self):
        cart = _cart(("ポテト", 330, 1), ("ポテト", 290, 1))
        self.assertEqual(len(quote.lines_from_cart(cart)), 2)

    def test_an_empty_cart_has_no_lines(self):
        self.assertEqual(quote.lines_from_cart({"cart_items": []}), [])


class ReconcileTest(unittest.TestCase):
    def test_agreement_is_silence(self):
        cart = _cart(("ビッグマックセット", 750, 1), ("マックフライポテト", 330, 1))
        self.assertEqual(quote.reconcile(REQUESTED, cart), [])

    def test_a_dropped_line_is_caught(self):
        # 2026-08-16: the scrape keyed on the 変更 button, and items that
        # cannot be customised have none, so the fries vanished quietly.
        cart = _cart(("ビッグマックセット", 750, 1), total=1080)
        problems = quote.reconcile(REQUESTED, cart)
        self.assertEqual(len(problems), 1)
        self.assertIn("マックフライポテト", problems[0])

    def test_a_wrong_quantity_is_caught(self):
        cart = _cart(("ビッグマックセット", 750, 2), ("マックフライポテト", 330, 1))
        problems = quote.reconcile(REQUESTED, cart)
        self.assertTrue(any("2点" in p for p in problems))

    def test_a_moved_price_is_caught(self):
        cart = _cart(("ビッグマックセット", 800, 1), ("マックフライポテト", 330, 1))
        problems = quote.reconcile(REQUESTED, cart)
        self.assertTrue(any("800" in p for p in problems))

    def test_an_uninvited_line_is_caught(self):
        cart = _cart(("ビッグマックセット", 750, 1), ("マックフライポテト", 330, 1),
                     ("アップルパイ", 150, 1))
        problems = quote.reconcile(REQUESTED, cart)
        self.assertTrue(any("アップルパイ" in p for p in problems))

    def test_decoration_does_not_count_as_a_difference(self):
        # The site writes ビッグマック® with spacing of its own.
        cart = _cart(("ビッグマック セット®", 750, 1), ("マックフライポテト", 330, 1))
        self.assertEqual(quote.reconcile(REQUESTED, cart), [])


class FulfillmentTest(unittest.TestCase):
    def test_what_was_asked_for_is_used_when_available(self):
        chosen, note = quote.choose_fulfillment("drive_thru",
                                                ["drive_thru", "takeout"])
        self.assertEqual(chosen, "drive_thru")
        self.assertIsNone(note)

    def test_a_substitution_is_never_silent(self):
        chosen, note = quote.choose_fulfillment("drive_thru", ["takeout", "eatin"])
        self.assertEqual(chosen, "takeout")
        self.assertIn("ドライブスルー", note)
        self.assertIn("お持ち帰り", note)

    def test_no_request_needs_no_apology(self):
        chosen, note = quote.choose_fulfillment(None, ["takeout"])
        self.assertEqual(chosen, "takeout")
        self.assertIsNone(note)

    def test_unknown_options_leave_the_request_alone(self):
        chosen, note = quote.choose_fulfillment("drive_thru", [])
        self.assertEqual(chosen, "drive_thru")
        self.assertIsNone(note)

    def test_an_unknown_method_is_named_as_unknown(self):
        self.assertEqual(quote.spoken_fulfillment("teleport"), "受け取り方法未指定")


class BuildTest(unittest.TestCase):
    def test_there_is_no_way_to_build_from_a_draft(self):
        # The structural half of the rule: the readback cannot be made from
        # what we asked for, only from what the store says it holds.
        self.assertFalse(hasattr(quote, "build_from_draft"))

    def test_totals_come_from_the_cart(self):
        cart = _cart(("ビッグマックセット", 750, 1), total=9999)
        order = quote.build_from_cart("S1", "テスト店", cart, "takeout")
        self.assertEqual(order.total_yen, 9999)

    def test_item_count(self):
        cart = _cart(("ポテト", 330, 2), ("バーガー", 480, 1))
        order = quote.build_from_cart("S1", "テスト店", cart)
        self.assertEqual(order.item_count(), 3)

    def test_round_trip(self):
        cart = _cart(("ポテト", 330, 1))
        order = quote.build_from_cart("S1", "テスト店", cart, "takeout",
                                      [("ダブチ", "ダブルチーズバーガー")],
                                      ["受け取り方法を変えたよ。"])
        payload = order.to_dict()
        self.assertEqual(payload["store_name"], "テスト店")
        self.assertEqual(payload["interpretations"], [["ダブチ", "ダブルチーズバーガー"]])


class SpokenTest(unittest.TestCase):
    def _order(self, **kwargs):
        cart = _cart(("ビッグマックセット", 750, 1), ("マックフライポテト", 330, 1))
        return quote.build_from_cart("S1", "高鍋店", cart, "takeout", **kwargs)

    def test_reads_the_cart_back(self):
        text = quote.spoken(self._order())
        self.assertIn("高鍋店", text)
        self.assertIn("ビッグマックセット 1点", text)
        self.assertIn("合計1080円", text)
        self.assertIn("お持ち帰り", text)

    def test_the_approval_wording_is_unchanged(self):
        # The gate accepts 注文 phrases only; the readback must ask for one.
        text = quote.spoken(self._order())
        self.assertIn("「注文して」で確定", text)
        self.assertIn("キャンセル", text)

    def test_an_interpretation_is_said_before_the_order_it_affected(self):
        text = quote.spoken(self._order(interpretations=[("ダブチ", "ダブルチーズバーガー")]))
        self.assertLess(text.index("解釈したよ"), text.index("高鍋店"))

    def test_a_substitution_note_is_spoken(self):
        text = quote.spoken(self._order(notes=["ドライブスルーが選べないよ。"]))
        self.assertIn("ドライブスルーが選べないよ", text)

    def test_an_unpriced_order_cannot_be_read_aloud(self):
        order = quote.build_from_cart("S1", "高鍋店", {"cart_items": [],
                                                     "cart_total_yen": None})
        with self.assertRaises(ValueError):
            quote.spoken(order)


if __name__ == "__main__":
    unittest.main()
