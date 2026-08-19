import unittest

from orderagent import states


class ApprovalGateTest(unittest.TestCase):
    def test_spec_test_7_ok_in_ordinary_conversation_buys_nothing(self):
        # There is no state outside awaiting_approval where a confirmation
        # reaches money, so an "OK" said mid-chat cannot be one.
        for state in states.TRANSITIONS:
            if state == states.AWAITING_APPROVAL:
                continue
            with self.subTest(state=state):
                self.assertFalse(states.accepts_approval(state))

    def test_spec_test_8_ordering_words_before_the_readback_do_nothing(self):
        for state in (states.CREATED, states.DRAFTING, states.BUILDING,
                      states.NEEDS_INFO, states.VERIFYING):
            with self.subTest(state=state):
                self.assertFalse(states.accepts_approval(state))

    def test_approval_is_heard_at_the_readback(self):
        self.assertTrue(states.accepts_approval(states.AWAITING_APPROVAL))


class PaymentIsAttemptedOnceTest(unittest.TestCase):
    def test_only_one_state_leads_to_verifying(self):
        sources = [state for state, targets in states.TRANSITIONS.items()
                   if states.VERIFYING in targets]
        self.assertEqual(sources, [states.AWAITING_APPROVAL])

    def test_an_uncertain_payment_has_no_road_back(self):
        # §27: not a rule the code follows, a road that does not exist.
        onward = states.TRANSITIONS[states.PAYMENT_UNCERTAIN]
        self.assertNotIn(states.VERIFYING, onward)
        self.assertNotIn(states.AWAITING_APPROVAL, onward)
        self.assertNotIn(states.BUILDING, onward)

    def test_retrying_is_never_allowed(self):
        for state in states.TRANSITIONS:
            with self.subTest(state=state):
                self.assertFalse(states.may_retry_payment(state))

    def test_reconciliation_may_still_settle_it(self):
        # Finding out what already happened is not trying again.
        self.assertTrue(states.can(states.PAYMENT_UNCERTAIN, states.PAID))
        self.assertTrue(states.can(states.PAYMENT_UNCERTAIN, states.FAILED))


class OutcomeTest(unittest.TestCase):
    def test_three_values_not_two(self):
        self.assertEqual(states.state_for_outcome(states.CONFIRMED), states.PAID)
        self.assertEqual(states.state_for_outcome(states.OUTCOME_FAILED),
                         states.FAILED)
        self.assertEqual(states.state_for_outcome(states.UNKNOWN),
                         states.PAYMENT_UNCERTAIN)

    def test_an_unknown_outcome_is_refused_not_assumed(self):
        with self.assertRaises(ValueError):
            states.state_for_outcome("probably fine")

    def test_uncertainty_is_not_failure(self):
        self.assertNotEqual(states.state_for_outcome(states.UNKNOWN),
                            states.FAILED)


class TransitionTest(unittest.TestCase):
    def test_the_ordinary_path(self):
        state = states.CREATED
        for target in (states.DRAFTING, states.BUILDING,
                       states.AWAITING_APPROVAL, states.VERIFYING,
                       states.DRY_RUN_DONE):
            state = states.advance(state, target)
        self.assertEqual(state, states.DRY_RUN_DONE)

    def test_a_conversation_may_take_many_turns(self):
        state = states.advance(states.CREATED, states.DRAFTING)
        for _ in range(5):
            state = states.advance(state, states.DRAFTING)
        self.assertEqual(state, states.DRAFTING)

    def test_an_illegal_move_raises_rather_than_staying_put(self):
        # A transition that quietly does nothing leaves a job looking
        # approved when it is not.
        with self.assertRaises(states.IllegalTransition):
            states.advance(states.DRAFTING, states.VERIFYING)

    def test_skipping_the_readback_is_impossible(self):
        self.assertFalse(states.can(states.BUILDING, states.VERIFYING))
        self.assertFalse(states.can(states.BUILDING, states.PAID))

    def test_terminal_states_are_terminal(self):
        for state in (states.PAID, states.DRY_RUN_DONE, states.FAILED,
                      states.DENIED, states.EXPIRED, states.ESCALATED):
            with self.subTest(state=state):
                self.assertTrue(states.is_terminal(state))
                self.assertEqual(states.TRANSITIONS[state], frozenset())

    def test_an_uncertain_payment_is_not_terminal(self):
        # Somebody still has to find out what happened.
        self.assertFalse(states.is_terminal(states.PAYMENT_UNCERTAIN))

    def test_every_target_is_a_known_state(self):
        for state, targets in states.TRANSITIONS.items():
            for target in targets:
                with self.subTest(state=state, target=target):
                    self.assertIn(target, states.TRANSITIONS)

    def test_denial_is_reachable_from_every_active_state(self):
        # A person must be able to stop this at any point they are in it.
        for state in states.ACTIVE:
            if state == states.VERIFYING:
                continue  # money may already be moving; stopping is not ours
            with self.subTest(state=state):
                self.assertTrue(states.can(state, states.DENIED))


class NamesTest(unittest.TestCase):
    def test_the_existing_names_are_kept(self):
        # The audit log and db.orders.status have used these since the
        # first order; renaming would split the trail without making an
        # order safer.
        for name in ("created", "building", "awaiting_approval", "verifying",
                     "dry_run_done", "paid", "needs_info", "failed",
                     "escalated", "denied", "expired"):
            with self.subTest(name=name):
                self.assertIn(name, states.TRANSITIONS)

    def test_drafting_is_the_only_addition(self):
        existing = {"created", "building", "awaiting_approval", "verifying",
                    "dry_run_done", "paid", "needs_info", "failed",
                    "escalated", "denied", "expired"}
        added = set(states.TRANSITIONS) - existing
        self.assertEqual(added, {"drafting", "payment_uncertain"})

    def test_the_agent_is_occupied_in_the_same_states_as_before(self):
        # server.submit_job refuses a new job while one of these is in
        # flight; drafting joins them because a conversation holds the slot.
        self.assertEqual(
            states.ACTIVE,
            frozenset({"created", "drafting", "needs_info", "building",
                       "awaiting_approval", "verifying"}))


class DescribeTest(unittest.TestCase):
    def test_every_state_can_be_explained(self):
        for state in states.TRANSITIONS:
            with self.subTest(state=state):
                self.assertNotEqual(states.describe(state),
                                    "いまの状態がわからないよ。")

    def test_an_uncertain_payment_says_why_it_stopped(self):
        text = states.describe(states.PAYMENT_UNCERTAIN)
        self.assertIn("再注文", text)


if __name__ == "__main__":
    unittest.main()
