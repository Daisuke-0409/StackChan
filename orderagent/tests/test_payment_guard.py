import unittest

from orderagent import config, payment


def _job(total=700, count=1):
    return {"approved": {"expected_total_yen": total, "expected_item_count": count}}


class VerifyCartTest(unittest.TestCase):
    def test_matching_cart_passes(self):
        payment.verify_cart(_job()["approved"],
                            {"cart_total_yen": 700, "cart_items": [{"name": "x", "price": 700}]})

    def test_total_mismatch_refuses(self):
        with self.assertRaises(payment.PaymentRefused):
            payment.verify_cart(_job(700)["approved"],
                                {"cart_total_yen": 800, "cart_items": [{}]})

    def test_unreadable_total_refuses(self):
        with self.assertRaises(payment.PaymentRefused):
            payment.verify_cart(_job()["approved"], {"cart_total_yen": None, "cart_items": []})

    def test_over_limit_refuses_even_when_approved(self):
        over = config.MAX_ORDER_YEN + 1
        with self.assertRaises(payment.PaymentRefused):
            payment.verify_cart(_job(over)["approved"],
                                {"cart_total_yen": over, "cart_items": [{}]})

    def test_missing_items_refuse(self):
        with self.assertRaises(payment.PaymentRefused):
            payment.verify_cart(_job(700, count=2)["approved"],
                                {"cart_total_yen": 700, "cart_items": [{"name": "x"}]})


class ExecuteTest(unittest.TestCase):
    def test_dry_run_never_reports_payment(self):
        # config.PAYMENT_ENABLED is False in tests (env var unset).
        self.assertFalse(config.PAYMENT_ENABLED)
        result = payment.execute(_job(), {"cart_total_yen": 700, "cart_items": [{"name": "x"}]})
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["payment_executed"])

    def test_live_path_refuses_because_unimplemented(self):
        # Even with the flag forced on, the unwalked checkout must refuse.
        original = config.PAYMENT_ENABLED
        config.PAYMENT_ENABLED = True
        try:
            with self.assertRaises(payment.PaymentRefused):
                payment.execute(_job(), {"cart_total_yen": 700, "cart_items": [{"name": "x"}]})
        finally:
            config.PAYMENT_ENABLED = original


if __name__ == "__main__":
    unittest.main()
