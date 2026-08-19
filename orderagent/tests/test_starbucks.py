import unittest

from orderagent import reconcile
from orderagent.adapter import FULL_AUTO, MENU_ONLY, RestaurantAdapter
from orderagent.starbucks import (LeftoverCart, SessionExpired, StarbucksAdapter,
                                  _parse_distance_km)


class ProtocolTest(unittest.TestCase):
    def test_satisfies_the_protocol(self):
        self.assertIsInstance(StarbucksAdapter(), RestaurantAdapter)

    def test_chain_name(self):
        self.assertEqual(StarbucksAdapter().chain, "starbucks")


class DistanceTest(unittest.TestCase):
    def test_kilometres(self):
        self.assertEqual(_parse_distance_km("3.0km"), 3.0)

    def test_metres_become_kilometres(self):
        self.assertEqual(_parse_distance_km("400m"), 0.4)

    def test_unparseable_is_none(self):
        self.assertIsNone(_parse_distance_km("すぐそこ"))
        self.assertIsNone(_parse_distance_km(""))


class ConfirmationScrapeTest(unittest.TestCase):
    """The confirmation screen, as captured from the real site 2026-08-19."""

    SCREEN = """注文内容を確認する
宮崎神宮東店
変更
ドライブスルー
変更
ニックネーム：Dai
Tall 芳醇 巨峰 フローズン ティー 1点
¥619
キャラメルワッフル 2点
¥400
商品を追加する
総合計：¥1,019
本体合計(3点)¥944
お支払い方法：
スターバックスカード(メインカード)
残高：¥279
残高が不足しています
利用規約に同意の上、決済する"""

    class _FakePage:
        def __init__(self, text):
            self._text = text

        def inner_text(self, _selector):
            return self._text

    def _scrape(self, text=None):
        page = self._FakePage(text if text is not None else self.SCREEN)
        return StarbucksAdapter()._scrape_confirmation(page)

    def test_reads_every_line_with_quantities(self):
        cart = self._scrape()
        self.assertEqual(cart["cart_items"], [
            {"name": "Tall 芳醇 巨峰 フローズン ティー", "price": 619, "quantity": 1},
            {"name": "キャラメルワッフル", "price": 400, "quantity": 2},
        ])

    def test_reads_the_total(self):
        self.assertEqual(self._scrape()["cart_total_yen"], 1019)

    def test_reads_the_card_balance(self):
        self.assertEqual(self._scrape()["balance_yen"], 279)

    def test_reports_insufficient_balance(self):
        self.assertFalse(self._scrape()["sufficient_balance"])

    def test_sufficient_balance_when_the_site_is_silent(self):
        enough = self.SCREEN.replace("残高が不足しています\n", "")
        self.assertTrue(self._scrape(enough)["sufficient_balance"])


class SafeEmptinessTest(unittest.TestCase):
    def test_promotions_fall_to_the_false_branch(self):
        self.assertEqual(StarbucksAdapter().promotions("宮崎神宮東店"), set())

    def test_recent_orders_report_unread_not_empty(self):
        evidence = StarbucksAdapter().recent_orders("宮崎神宮東店")
        self.assertFalse(evidence.history_available)
        self.assertEqual(reconcile.reconcile(None, evidence, 0.0).outcome,
                         reconcile.UNRESOLVED)


class FailureTypesTest(unittest.TestCase):
    def test_expired_session_is_its_own_signal(self):
        self.assertTrue(issubclass(SessionExpired, RuntimeError))

    def test_leftover_cart_is_its_own_signal(self):
        # Distinct types because they need different sentences from the
        # robot: one asks for a login, the other for an empty basket.
        self.assertTrue(issubclass(LeftoverCart, RuntimeError))
        self.assertIsNot(LeftoverCart, SessionExpired)


if __name__ == "__main__":
    unittest.main()
