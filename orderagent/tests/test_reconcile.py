import unittest

from orderagent import quote, reconcile, snapshot, states

ATTEMPTED_AT = 1000.0


def _approved(total=1080, store_id="S1"):
    cart = {"cart_items": [{"name": "セット", "price": total, "quantity": 1}],
            "cart_total_yen": total}
    order = quote.build_from_cart(store_id, "高鍋店", cart, "takeout")
    return snapshot.from_resolved_order(order, now=ATTEMPTED_AT - 30)


def _history(*orders, available=True, note=None):
    return reconcile.Evidence(history_available=available,
                              orders=list(orders), note=note)


def _entry(total=1080, store_id="S1", placed_at=ATTEMPTED_AT + 5,
           order_number="1234"):
    return {"store_id": store_id, "total_yen": total, "placed_at": placed_at,
            "order_number": order_number}


class ConfirmedTest(unittest.TestCase):
    def test_one_match_settles_it(self):
        verdict = reconcile.reconcile(_approved(), _history(_entry()), ATTEMPTED_AT)
        self.assertEqual(verdict.outcome, reconcile.CONFIRMED)
        self.assertEqual(verdict.order_number, "1234")
        self.assertTrue(verdict.is_confirmed())

    def test_the_order_number_is_read_out_twice(self):
        # It is the thing the person has to say at the counter.
        verdict = reconcile.reconcile(_approved(), _history(_entry()), ATTEMPTED_AT)
        self.assertEqual(reconcile.spoken(verdict).count("1234"), 2)

    def test_a_store_that_lags_is_still_matched(self):
        late = _entry(placed_at=ATTEMPTED_AT + 600)
        verdict = reconcile.reconcile(_approved(), _history(late), ATTEMPTED_AT)
        self.assertEqual(verdict.outcome, reconcile.CONFIRMED)

    def test_a_confirmation_without_a_number_still_confirms(self):
        entry = _entry(order_number=None)
        verdict = reconcile.reconcile(_approved(), _history(entry), ATTEMPTED_AT)
        self.assertEqual(verdict.outcome, reconcile.CONFIRMED)
        self.assertIn("分からなかった", reconcile.spoken(verdict))


class FailedTest(unittest.TestCase):
    def test_a_readable_history_with_nothing_in_it_means_it_did_not_happen(self):
        verdict = reconcile.reconcile(_approved(), _history(), ATTEMPTED_AT)
        self.assertEqual(verdict.outcome, reconcile.FAILED)

    def test_a_different_total_is_not_our_order(self):
        verdict = reconcile.reconcile(_approved(1080), _history(_entry(total=990)),
                                      ATTEMPTED_AT)
        self.assertEqual(verdict.outcome, reconcile.FAILED)

    def test_a_different_store_is_not_our_order(self):
        verdict = reconcile.reconcile(_approved(), _history(_entry(store_id="S9")),
                                      ATTEMPTED_AT)
        self.assertEqual(verdict.outcome, reconcile.FAILED)

    def test_an_order_from_before_the_attempt_is_not_ours(self):
        old = _entry(placed_at=ATTEMPTED_AT - 3600)
        verdict = reconcile.reconcile(_approved(), _history(old), ATTEMPTED_AT)
        self.assertEqual(verdict.outcome, reconcile.FAILED)

    def test_failure_offers_the_next_move(self):
        verdict = reconcile.reconcile(_approved(), _history(), ATTEMPTED_AT)
        self.assertIn("もう一度頼むなら", reconcile.spoken(verdict))


class UnresolvedTest(unittest.TestCase):
    def test_an_unreadable_history_is_not_a_failure(self):
        # Silence from a store is not proof that nothing was ordered.
        verdict = reconcile.reconcile(_approved(), _history(available=False),
                                      ATTEMPTED_AT)
        self.assertEqual(verdict.outcome, reconcile.UNRESOLVED)
        self.assertNotEqual(verdict.outcome, reconcile.FAILED)
        self.assertTrue(verdict.needs_a_person())

    def test_the_reason_survives_from_the_adapter(self):
        verdict = reconcile.reconcile(
            _approved(), _history(available=False, note="ログインを求められたよ。"),
            ATTEMPTED_AT)
        self.assertIn("ログイン", verdict.reason)

    def test_two_matches_is_a_possible_double_order(self):
        verdict = reconcile.reconcile(
            _approved(), _history(_entry(order_number="1"), _entry(order_number="2")),
            ATTEMPTED_AT)
        self.assertEqual(verdict.outcome, reconcile.UNRESOLVED)
        self.assertEqual(len(verdict.matches), 2)
        self.assertIn("二重注文", verdict.reason)

    def test_an_entry_without_a_time_stays_a_candidate(self):
        # A store that will not say when cannot rule itself out on time.
        verdict = reconcile.reconcile(_approved(), _history(_entry(placed_at=None)),
                                      ATTEMPTED_AT)
        self.assertEqual(verdict.outcome, reconcile.CONFIRMED)

    def test_uncertainty_always_says_no_reorder_was_attempted(self):
        # The reassurance is the point of telling them at all.
        for evidence in (_history(available=False),
                         _history(_entry(order_number="1"), _entry(order_number="2"))):
            with self.subTest():
                verdict = reconcile.reconcile(_approved(), evidence, ATTEMPTED_AT)
                text = reconcile.spoken(verdict)
                self.assertIn("再注文は止めている", text)


class SafetyTest(unittest.TestCase):
    def test_nothing_here_can_reorder(self):
        # Checked against the imports rather than the prose: the module's
        # docstring explains that it does not retry, and a test that read
        # the text would fail on the word.
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(reconcile))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & {"urllib", "playwright", "requests",
                                     "http", "socket"}, set())

        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        self.assertEqual(called & {"build_cart", "submit_order", "execute"}, set())

    def test_the_verdicts_line_up_with_the_state_machine(self):
        # CONFIRMED/FAILED settle an uncertain payment; UNRESOLVED leaves it
        # where it was, which the machine allows because it is not terminal.
        self.assertTrue(states.can(states.PAYMENT_UNCERTAIN, states.PAID))
        self.assertTrue(states.can(states.PAYMENT_UNCERTAIN, states.FAILED))
        self.assertFalse(states.is_terminal(states.PAYMENT_UNCERTAIN))

    def test_evidence_round_trips(self):
        evidence = _history(_entry(), note="ok")
        self.assertEqual(evidence.to_dict()["orders"][0]["order_number"], "1234")

    def test_a_verdict_round_trips(self):
        verdict = reconcile.reconcile(_approved(), _history(_entry()), ATTEMPTED_AT)
        self.assertEqual(verdict.to_dict()["outcome"], reconcile.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
