"""The live payment path -- tested without a browser, and without money.

confirm_payment is the one function that can spend, so what matters is
provable at the boundary: which guards run before the click, that the
click happens exactly once, and that an unreadable outcome is reported as
unknown rather than assumed to be success.
"""
import unittest
from unittest import mock

from orderagent import config, payment
from orderagent import starbucks as sbx


CONFIRM_SCREEN = """注文内容を確認する
宮崎神宮東店
TO GO (お持ち帰り)
キャラメルワッフル 1点
¥216
総合計：¥216
残高：¥1,000
利用規約に同意の上、決済する"""

PAID_SCREEN = """ご注文ありがとうございます
注文番号 A123
宮崎神宮東店"""


class _FakeLocator:
    def __init__(self, page):
        self._page = page

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self._page.clicks += 1
        self._page.text = self._page.after_click


class _FakePage:
    def __init__(self, after_click, url="https://webapp.starbucks.co.jp/order"):
        self.url = url
        self.text = CONFIRM_SCREEN
        self.after_click = after_click
        self.clicks = 0

    def inner_text(self, _selector):
        return self.text

    def get_by_role(self, _role, name=None):
        return _FakeLocator(self)

    def screenshot(self, **_kwargs):
        raise RuntimeError("no screenshots in tests")


def _run(page):
    job = {"job_id": "t1", "chain": "starbucks", "store_name": "宮崎神宮東店",
           "approved": {"expected_total_yen": 216, "expected_item_count": 1}}
    cart = {"cart_total_yen": 216, "cart_items": [{"name": "キャラメルワッフル",
                                                   "price": 216, "quantity": 1}],
            "balance_yen": 1000, "sufficient_balance": True}
    import contextlib

    @contextlib.contextmanager
    def fake_attached(*_a, **_kw):
        yield page

    with mock.patch.object(sbx.browser_mod, "attached_page", fake_attached), \
         mock.patch("time.sleep"):
        return sbx.confirm_payment(job, cart), page


class ConfirmPaymentTest(unittest.TestCase):
    def test_success_reports_the_order_number(self):
        result, page = _run(_FakePage(PAID_SCREEN))
        self.assertTrue(result["payment_executed"])
        self.assertEqual(result["outcome"], "PAID")
        self.assertEqual(result["order_number"], "A123")
        self.assertEqual(page.clicks, 1)

    def test_button_still_there_means_nothing_was_placed(self):
        result, page = _run(_FakePage(CONFIRM_SCREEN))
        self.assertFalse(result["payment_executed"])
        self.assertEqual(result["outcome"], "NOT_PLACED")
        self.assertEqual(page.clicks, 1)      # and never a second

    def test_unreadable_outcome_is_unknown_not_success(self):
        result, page = _run(_FakePage("ただいま処理中です"))
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(page.clicks, 1)
        self.assertIn("履歴", result["note"])

    def test_refuses_when_the_page_moved_away(self):
        page = _FakePage(PAID_SCREEN, url="https://webapp.starbucks.co.jp/order/choose-products")
        with self.assertRaises(RuntimeError):
            _run(page)
        self.assertEqual(page.clicks, 0)

    def test_refuses_when_the_total_changed_under_us(self):
        page = _FakePage(PAID_SCREEN)
        page.text = CONFIRM_SCREEN.replace("¥216\n総合計：¥216", "¥300\n総合計：¥300")
        with self.assertRaises(RuntimeError):
            _run(page)
        self.assertEqual(page.clicks, 0)


class ExecuteGuardTest(unittest.TestCase):
    """payment.execute stays the gate; confirm_payment is only its hand."""

    JOB = {"chain": "starbucks",
           "approved": {"expected_total_yen": 216, "expected_item_count": 1}}
    CART = {"cart_total_yen": 216,
            "cart_items": [{"name": "x", "price": 216, "quantity": 1}],
            "sufficient_balance": True}

    def test_insufficient_balance_refuses_before_any_click(self):
        cart = dict(self.CART, sufficient_balance=False, balance_yen=100)
        with self.assertRaises(payment.PaymentRefused):
            payment.execute(self.JOB, cart)

    def test_unwalked_chain_refuses_even_with_the_flag_on(self):
        original = config.PAYMENT_ENABLED
        config.PAYMENT_ENABLED = True
        try:
            with self.assertRaises(payment.PaymentRefused):
                payment.execute(dict(self.JOB, chain="mcd"), self.CART)
        finally:
            config.PAYMENT_ENABLED = original

    def test_dry_run_still_runs_every_check(self):
        # A mismatched cart must fail in dry run too, or the rehearsal
        # proves nothing about the performance.
        bad = dict(self.CART, cart_total_yen=999)
        with self.assertRaises(payment.PaymentRefused):
            payment.execute(self.JOB, bad)


if __name__ == "__main__":
    unittest.main()
