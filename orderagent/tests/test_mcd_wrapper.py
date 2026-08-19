import unittest
from unittest import mock

from orderagent import draft, reconcile
from orderagent.adapter import (FULL_AUTO, MENU_ONLY, RestaurantAdapter,
                                resolve_products, to_cart_items)
from orderagent.mcd import McdAdapter
from orderagent import intent


class ProtocolConformanceTest(unittest.TestCase):
    def test_satisfies_the_protocol(self):
        self.assertIsInstance(McdAdapter(), RestaurantAdapter)

    def test_chain_name(self):
        self.assertEqual(McdAdapter().chain, "mcd")


class FindStoresTest(unittest.TestCase):
    def test_maps_key_to_id_and_keeps_original_fields(self):
        rows = [{"key": "45520", "name": "１０号高鍋店", "distance_km": 0.53}]
        with mock.patch.object(McdAdapter.find_stores.__globals__["stores"],
                               "nearest", return_value=rows):
            found = McdAdapter().find_stores(32.1, 131.5)
        self.assertEqual(found[0]["id"], "45520")
        self.assertEqual(found[0]["key"], "45520")
        self.assertEqual(found[0]["distance_km"], 0.53)


class CapabilitiesTest(unittest.TestCase):
    def _capabilities(self, detail):
        with mock.patch.object(McdAdapter.capabilities.__globals__["stores"],
                               "store_detail", return_value=detail):
            return McdAdapter().capabilities("45520")

    def test_mop_store_is_full_auto(self):
        caps = self._capabilities({"mopEnabled": True, "mcDeliveryEnabled": False})
        self.assertEqual(caps.level(), FULL_AUTO)
        self.assertTrue(caps.supports_fulfillment("takeout"))

    def test_non_mop_store_still_offers_its_menu(self):
        caps = self._capabilities({"mopEnabled": False})
        self.assertEqual(caps.level(), MENU_ONLY)

    def test_unreachable_store_data_degrades_to_menu_only(self):
        caps = self._capabilities(None)
        self.assertEqual(caps.level(), MENU_ONLY)

    def test_drive_thru_is_never_promised(self):
        # 高鍋 has a physical drive-through and no web drive-through pickup;
        # the store data cannot tell them apart, so the adapter must not.
        caps = self._capabilities({"mopEnabled": True})
        self.assertFalse(caps.supports_fulfillment("drive_through"))


class SafeEmptinessTest(unittest.TestCase):
    def test_promotions_fall_to_the_false_branch(self):
        self.assertEqual(McdAdapter().promotions("45520"), set())

    def test_recent_orders_report_unread_not_empty(self):
        evidence = McdAdapter().recent_orders("45520")
        self.assertFalse(evidence.history_available)
        # With no readable history the snapshot is never consulted, and the
        # verdict must be UNRESOLVED -- silence is not proof of no order.
        verdict = reconcile.reconcile(None, evidence, attempted_at=0.0)
        self.assertEqual(verdict.outcome, reconcile.UNRESOLVED)


class EngineIntegrationTest(unittest.TestCase):
    """The wrapper under the real engine functions, network mocked out."""

    MENU = [{"id": "1010", "name": "ハンバーガー", "price": 190},
            {"id": "1360", "name": "ダブルチーズバーガー", "price": 490}]

    def _drafted(self):
        order = draft.OrderDraft(restaurant=draft.Restaurant(chain="mcd",
                                                             store_id="45520"))
        order.add("ダブチ", quantity=1)
        return order

    def test_resolve_and_hand_off_through_the_wrapper(self):
        adapter = McdAdapter()
        with mock.patch.object(McdAdapter.menu.__globals__["stores"], "menu",
                               return_value=self.MENU):
            order = self._drafted()
            resolution = resolve_products(
                order, adapter, intent.match_menu,
                guesser=lambda spoken, menu: [self.MENU[1]])
            self.assertTrue(resolution.ok())
            self.assertEqual(resolution.interpretations, [("ダブチ", "ダブルチーズバーガー")])
            items = to_cart_items(order, adapter)
        self.assertEqual(items, [{"id": "1360", "name": "ダブルチーズバーガー",
                                  "price": 490, "quantity": 1}])


if __name__ == "__main__":
    unittest.main()
