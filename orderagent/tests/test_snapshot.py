import unittest

from orderagent import quote, snapshot


def _cart(*rows, total=None):
    items = [{"name": n, "price": p, "quantity": q} for n, p, q in rows]
    if total is None:
        total = sum(p * q for _, p, q in rows)
    return {"cart_items": items, "cart_total_yen": total}


def _approved(*rows, fulfillment="takeout", store_id="S1", total=None, now=1000.0):
    order = quote.build_from_cart(store_id, "高鍋店", _cart(*rows, total=total),
                                  fulfillment)
    return snapshot.from_resolved_order(order, now=now)


BURGER_AND_FRIES = (("ビッグマックセット", 750, 1), ("マックフライポテト", 330, 1))


class FingerprintTest(unittest.TestCase):
    def test_the_same_order_fingerprints_the_same(self):
        a = _approved(*BURGER_AND_FRIES, now=1000.0)
        b = _approved(*BURGER_AND_FRIES, now=9999.0)
        self.assertEqual(a.fingerprint(), b.fingerprint())
        self.assertTrue(a.matches(b))

    def test_a_different_total_fingerprints_differently(self):
        a = _approved(*BURGER_AND_FRIES)
        b = _approved(*BURGER_AND_FRIES, total=1180)
        self.assertNotEqual(a.fingerprint(), b.fingerprint())

    def test_a_different_store_fingerprints_differently(self):
        self.assertNotEqual(_approved(*BURGER_AND_FRIES).fingerprint(),
                            _approved(*BURGER_AND_FRIES, store_id="S2").fingerprint())

    def test_a_different_collection_method_fingerprints_differently(self):
        self.assertNotEqual(
            _approved(*BURGER_AND_FRIES, fulfillment="takeout").fingerprint(),
            _approved(*BURGER_AND_FRIES, fulfillment="drive_thru").fingerprint())

    def test_a_different_quantity_fingerprints_differently(self):
        self.assertNotEqual(
            _approved(("ポテト", 330, 1)).fingerprint(),
            _approved(("ポテト", 330, 2), total=330).fingerprint())

    def test_the_display_name_is_cosmetic(self):
        a = snapshot.Snapshot(store_id="S1", store_name="高鍋店",
                              fulfillment="takeout", total_yen=100,
                              lines=[{"name": "x", "price": 100, "quantity": 1}])
        b = snapshot.Snapshot(store_id="S1", store_name="十号高鍋店",
                              fulfillment="takeout", total_yen=100,
                              lines=[{"name": "x", "price": 100, "quantity": 1}])
        self.assertTrue(a.matches(b))

    def test_it_survives_serialization(self):
        a = _approved(*BURGER_AND_FRIES)
        restored = snapshot.Snapshot.from_dict(a.to_dict())
        self.assertEqual(restored.fingerprint(), a.fingerprint())
        self.assertEqual(restored.created_at, a.created_at)


class FreshnessTest(unittest.TestCase):
    def test_a_new_approval_is_fresh(self):
        self.assertFalse(_approved(*BURGER_AND_FRIES, now=1000.0).is_expired(now=1100.0))

    def test_it_goes_stale(self):
        self.assertTrue(_approved(*BURGER_AND_FRIES, now=1000.0).is_expired(now=1400.0))

    def test_the_window_matches_the_existing_one(self):
        self.assertEqual(snapshot.DEFAULT_TTL_SECONDS, 300.0)


class ChangesTest(unittest.TestCase):
    def test_nothing_changed_is_silence(self):
        a = _approved(*BURGER_AND_FRIES)
        b = _approved(*BURGER_AND_FRIES)
        self.assertEqual(snapshot.changes(a, b), [])

    def test_the_total_is_reported_first(self):
        # The number a person remembers agreeing to.
        a = _approved(*BURGER_AND_FRIES)
        b = _approved(("ビッグマックセット", 800, 1), ("マックフライポテト", 330, 1))
        problems = snapshot.changes(a, b)
        self.assertIn("合計が1080円から1130円に変わったよ", problems[0])

    def test_the_spec_example(self):
        a = _approved(("セット", 850, 1))
        b = _approved(("セット", 900, 1))
        self.assertIn("850円から900円", snapshot.changes(a, b)[0])

    def test_a_changed_store_is_reported(self):
        a = _approved(*BURGER_AND_FRIES, store_id="S1")
        b = _approved(*BURGER_AND_FRIES, store_id="S2")
        self.assertTrue(any("店舗が" in p for p in snapshot.changes(a, b)))

    def test_a_changed_collection_method_is_reported(self):
        a = _approved(*BURGER_AND_FRIES, fulfillment="drive_thru")
        b = _approved(*BURGER_AND_FRIES, fulfillment="takeout")
        self.assertTrue(any("受け取り方法が" in p for p in snapshot.changes(a, b)))

    def test_a_vanished_line_is_reported_even_at_the_same_total(self):
        a = _approved(*BURGER_AND_FRIES)
        b = _approved(("ビッグマックセット", 750, 1), total=1080)
        problems = snapshot.changes(a, b)
        self.assertTrue(any("マックフライポテト" in p for p in problems))


class VerifyTest(unittest.TestCase):
    def test_an_unchanged_cart_passes(self):
        approved = _approved(*BURGER_AND_FRIES, now=1000.0)
        problems = snapshot.verify(approved, _cart(*BURGER_AND_FRIES),
                                   "S1", "takeout", now=1010.0)
        self.assertEqual(problems, [])

    def test_a_price_rise_stops_the_payment(self):
        approved = _approved(("セット", 850, 1), now=1000.0)
        problems = snapshot.verify(approved, _cart(("セット", 900, 1)),
                                   "S1", "takeout", now=1010.0)
        self.assertTrue(problems)
        self.assertIn("850円から900円", problems[0])

    def test_staleness_alone_stops_the_payment(self):
        # Nothing changed; the approval is simply old.
        approved = _approved(*BURGER_AND_FRIES, now=1000.0)
        problems = snapshot.verify(approved, _cart(*BURGER_AND_FRIES),
                                   "S1", "takeout", now=2000.0)
        self.assertEqual(len(problems), 1)
        self.assertIn("時間が経ちすぎ", problems[0])

    def test_a_store_swapped_under_us_is_caught(self):
        approved = _approved(*BURGER_AND_FRIES, now=1000.0)
        problems = snapshot.verify(approved, _cart(*BURGER_AND_FRIES),
                                   "S2", "takeout", now=1010.0)
        self.assertTrue(any("店舗が" in p for p in problems))


class PaymentBridgeTest(unittest.TestCase):
    def test_it_produces_what_the_payment_gate_takes(self):
        expectation = _approved(*BURGER_AND_FRIES).to_payment_expectation()
        self.assertEqual(set(expectation),
                         {"expected_total_yen", "expected_item_count",
                          "expected_items"})
        self.assertEqual(expectation["expected_total_yen"], 1080)
        self.assertEqual(expectation["expected_item_count"], 2)

    def test_the_existing_gate_accepts_it_unchanged(self):
        from orderagent import payment
        approved = _approved(*BURGER_AND_FRIES)
        payment.verify_cart(approved.to_payment_expectation(),
                            _cart(*BURGER_AND_FRIES))

    def test_the_existing_gate_still_refuses_a_mismatch(self):
        from orderagent import payment
        approved = _approved(*BURGER_AND_FRIES)
        with self.assertRaises(payment.PaymentRefused):
            payment.verify_cart(approved.to_payment_expectation(),
                                _cart(("ビッグマックセット", 750, 1), total=1080))

    def test_counts_add_up_across_quantities(self):
        expectation = _approved(("ポテト", 330, 3)).to_payment_expectation()
        self.assertEqual(expectation["expected_item_count"], 3)


class ConsentTest(unittest.TestCase):
    def test_an_unpriced_order_cannot_be_approved(self):
        order = quote.build_from_cart("S1", "高鍋店",
                                      {"cart_items": [], "cart_total_yen": None})
        with self.assertRaises(ValueError):
            snapshot.from_resolved_order(order)

    def test_payment_method_is_a_reference_not_a_number(self):
        # The agent never handles payment details; this records which one
        # was meant, nothing more.
        snap = _approved(*BURGER_AND_FRIES)
        self.assertIsNone(snap.payment_method_ref)
        order = quote.build_from_cart("S1", "高鍋店", _cart(*BURGER_AND_FRIES),
                                      "takeout")
        snap = snapshot.from_resolved_order(order, payment_method_ref="profile-default")
        self.assertEqual(snap.payment_method_ref, "profile-default")
        self.assertNotIn("payment_method_ref", snap.content())


if __name__ == "__main__":
    unittest.main()
