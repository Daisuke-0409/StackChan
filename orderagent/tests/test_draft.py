import unittest

from orderagent import draft


class NewDraftTest(unittest.TestCase):
    def test_starts_empty_and_unresolved(self):
        d = draft.new_draft("mcd")
        self.assertEqual(d.restaurant.chain, "mcd")
        self.assertIsNone(d.restaurant.store_id)
        self.assertIsNone(d.fulfillment)
        self.assertEqual(d.items, [])
        self.assertIsNone(d.active_item_id)
        self.assertFalse(d.is_resolved())

    def test_store_and_fulfillment_may_arrive_later(self):
        d = draft.new_draft("mcd")
        d.restaurant.store_id = "45520"
        d.fulfillment = "drive_thru"
        self.assertEqual(d.restaurant.store_id, "45520")
        self.assertEqual(d.fulfillment, "drive_thru")


class ItemIdTest(unittest.TestCase):
    def test_ids_are_sequential(self):
        d = draft.new_draft("mcd")
        self.assertEqual(d.next_item_id(), "item_1")
        self.assertEqual(d.next_item_id(), "item_2")

    def test_ids_are_not_reused_after_removal(self):
        d = draft.new_draft("mcd")
        first = d.next_item_id()
        d.items.append(draft.DraftItem(id=first, product="ナゲット"))
        d.items.clear()
        self.assertNotEqual(d.next_item_id(), first)


class QuantityTest(unittest.TestCase):
    def test_defaults_to_one(self):
        self.assertEqual(draft.DraftItem(id="item_1", product="ポテト").quantity, 1)

    def test_zero_is_refused(self):
        with self.assertRaises(draft.DraftError):
            draft.DraftItem(id="item_1", product="ポテト", quantity=0)

    def test_absurd_quantity_is_refused(self):
        with self.assertRaises(draft.DraftError):
            draft.DraftItem(id="item_1", product="ハンバーガー", quantity=100)

    def test_boolean_is_not_a_quantity(self):
        with self.assertRaises(draft.DraftError):
            draft.DraftItem(id="item_1", product="ポテト", quantity=True)

    def test_copy_item_validates_the_new_quantity(self):
        item = draft.DraftItem(id="item_1", product="ポテト")
        with self.assertRaises(draft.DraftError):
            draft.copy_item(item, quantity=0)


class CopyItemTest(unittest.TestCase):
    def test_leaves_the_original_alone(self):
        item = draft.DraftItem(id="item_1", product="ビッグマック")
        changed = draft.copy_item(item, variant="set")
        self.assertIsNone(item.variant)
        self.assertEqual(changed.variant, "set")
        self.assertEqual(changed.product, "ビッグマック")

    def test_variant_and_size_are_separate_axes(self):
        item = draft.DraftItem(id="item_1", product="ビッグマック")
        item = draft.copy_item(item, variant="set")
        item = draft.copy_item(item, size="L")
        self.assertEqual(item.variant, "set")
        self.assertEqual(item.size, "L")

    def test_options_are_copied_not_shared(self):
        item = draft.DraftItem(id="item_1", product="ビッグマックセット")
        changed = draft.copy_item(item, options={"drink": "コーラゼロ"})
        changed.options["drink"] = "オレンジ"
        self.assertEqual(item.options, {})


class ActiveItemTest(unittest.TestCase):
    def test_active_item_resolves_through_the_id(self):
        d = draft.new_draft("mcd")
        d.items.append(draft.DraftItem(id="item_1", product="ビッグマック"))
        d.items.append(draft.DraftItem(id="item_2", product="ナゲット"))
        d.active_item_id = "item_2"
        self.assertEqual(d.active_item.product, "ナゲット")

    def test_active_item_is_none_when_unset(self):
        self.assertIsNone(draft.new_draft("mcd").active_item)

    def test_require_names_the_missing_item(self):
        d = draft.new_draft("mcd")
        self.assertIsNone(d.get("item_9"))
        with self.assertRaises(draft.DraftError):
            d.require("item_9")


class ResolutionTest(unittest.TestCase):
    def test_empty_draft_is_not_resolved(self):
        self.assertFalse(draft.new_draft("mcd").is_resolved())

    def test_all_items_must_be_resolved(self):
        d = draft.new_draft("mcd")
        d.items.append(draft.DraftItem(id="item_1", product="ビッグマック",
                                       status=draft.STATUS_RESOLVED,
                                       product_id="100"))
        d.items.append(draft.DraftItem(id="item_2", product="チキンのやつ"))
        self.assertFalse(d.is_resolved())
        d.items[1].status = draft.STATUS_RESOLVED
        d.items[1].product_id = "200"
        self.assertTrue(d.is_resolved())

    def test_total_quantity_sums_lines(self):
        d = draft.new_draft("mcd")
        d.items.append(draft.DraftItem(id="item_1", product="ハンバーガー", quantity=2))
        d.items.append(draft.DraftItem(id="item_2", product="ポテト", quantity=3))
        self.assertEqual(d.total_quantity(), 5)


class SerializationTest(unittest.TestCase):
    def _sample(self):
        d = draft.new_draft("mcd", store_id="45520", fulfillment="drive_thru")
        d.items.append(draft.DraftItem(
            id=d.next_item_id(), product="ビッグマックセット", variant="set",
            size="L", quantity=1, options={"drink": "コーラゼロ", "side": "ポテトL"},
            status=draft.STATUS_RESOLVED, product_id="1234"))
        d.active_item_id = "item_1"
        return d

    def test_round_trip_preserves_everything(self):
        original = self._sample()
        restored = draft.OrderDraft.from_dict(original.to_dict())
        self.assertEqual(restored.to_dict(), original.to_dict())

    def test_shape_matches_the_spec(self):
        payload = self._sample().to_dict()
        self.assertEqual(set(payload), {"restaurant", "fulfillment", "items",
                                        "active_item_id", "next_id"})
        self.assertEqual(payload["restaurant"], {"chain": "mcd", "store_id": "45520"})
        self.assertEqual(set(payload["items"][0]),
                         {"id", "product", "variant", "size", "quantity",
                          "options", "status", "product_id"})

    def test_restored_draft_does_not_reissue_an_existing_id(self):
        # A draft whose counter was lost or hand-edited must still not hand
        # out item_1 twice.
        payload = self._sample().to_dict()
        payload["next_id"] = 1
        restored = draft.OrderDraft.from_dict(payload)
        self.assertEqual(restored.next_item_id(), "item_2")

    def test_options_survive_the_round_trip(self):
        restored = draft.OrderDraft.from_dict(self._sample().to_dict())
        self.assertEqual(restored.items[0].options,
                         {"drink": "コーラゼロ", "side": "ポテトL"})


class PurityTest(unittest.TestCase):
    def test_module_imports_nothing_that_talks_to_the_world(self):
        # STEP 2 is a data model. If this starts failing, something reached
        # for a menu, a browser or an LLM from inside the draft.
        import inspect
        source = inspect.getsource(draft)
        for forbidden in ("import requests", "urllib", "playwright",
                          "ai_match", "stores", "sqlite3", "socket"):
            self.assertNotIn(forbidden, source, f"draft.py must not use {forbidden}")



class AddTest(unittest.TestCase):
    def test_add_appends_and_becomes_active(self):
        d = draft.new_draft("mcd")
        first = d.add("ビッグマック")
        second = d.add("ナゲット")
        self.assertEqual([i.product for i in d.items], ["ビッグマック", "ナゲット"])
        self.assertEqual(d.active_item_id, second.id)
        self.assertNotEqual(first.id, second.id)

    def test_add_does_not_merge_identical_products(self):
        # Two lines that look alike may have been meant as two. Folding them
        # together would change a quantity nobody said out loud.
        d = draft.new_draft("mcd")
        d.add("ポテト")
        d.add("ポテト")
        self.assertEqual(len(d.items), 2)
        self.assertEqual(d.total_quantity(), 2)

    def test_add_validates_quantity(self):
        with self.assertRaises(draft.DraftError):
            draft.new_draft("mcd").add("ハンバーガー", quantity=99)


class ReplaceTest(unittest.TestCase):
    def test_replace_keeps_one_line(self):
        d = draft.new_draft("mcd")
        d.add("ビッグマック")
        d.replace("ダブルチーズバーガー")
        self.assertEqual([i.product for i in d.items], ["ダブルチーズバーガー"])

    def test_replace_keeps_the_id_and_the_quantity(self):
        d = draft.new_draft("mcd")
        original = d.add("ナゲット", quantity=2)
        after = d.replace("ポテト")
        self.assertEqual(after.id, original.id)
        self.assertEqual(after.quantity, 2)

    def test_replace_clears_product_specific_details(self):
        d = draft.new_draft("mcd")
        d.add("ビッグマックセット", variant="set", size="L",
              options={"drink": "コーラゼロ"})
        d.items[0].status = draft.STATUS_RESOLVED
        d.items[0].product_id = "1234"
        after = d.replace("ダブルチーズバーガー")
        self.assertIsNone(after.variant)
        self.assertIsNone(after.size)
        self.assertEqual(after.options, {})
        self.assertIsNone(after.product_id)
        self.assertEqual(after.status, draft.STATUS_DRAFT)

    def test_replace_accepts_details_heard_in_the_same_sentence(self):
        d = draft.new_draft("mcd")
        d.add("ビッグマック")
        after = d.replace("ダブルチーズバーガー", variant="set")
        self.assertEqual(after.variant, "set")

    def test_replace_without_a_target_or_active_item_refuses(self):
        with self.assertRaises(draft.DraftError):
            draft.new_draft("mcd").replace("ポテト")


class ModifyTest(unittest.TestCase):
    def test_modify_changes_details_not_the_product(self):
        d = draft.new_draft("mcd")
        d.add("ビッグマックセット")
        after = d.modify(size="L", options={"drink": "コーラゼロ"})
        self.assertEqual(after.product, "ビッグマックセット")
        self.assertEqual(after.size, "L")
        self.assertEqual(after.options, {"drink": "コーラゼロ"})

    def test_modify_refuses_to_change_the_product(self):
        d = draft.new_draft("mcd")
        d.add("ポテト")
        with self.assertRaises(draft.DraftError):
            d.modify(product="ナゲット")

    def test_modify_refuses_an_empty_change(self):
        d = draft.new_draft("mcd")
        d.add("ポテト")
        with self.assertRaises(draft.DraftError):
            d.modify()

    def test_changing_the_shape_un_resolves_the_line(self):
        d = draft.new_draft("mcd")
        item = d.add("ポテト")
        d.modify(item.id, size="M")
        d.items[0].status = draft.STATUS_RESOLVED
        d.items[0].product_id = "999"
        after = d.modify(item.id, size="L")
        self.assertEqual(after.status, draft.STATUS_DRAFT)
        self.assertIsNone(after.product_id)

    def test_changing_only_the_quantity_keeps_the_resolution(self):
        # How many of a product does not change which product it is.
        d = draft.new_draft("mcd")
        item = d.add("ポテト")
        d.items[0].status = draft.STATUS_RESOLVED
        d.items[0].product_id = "999"
        after = d.modify(item.id, quantity=3)
        self.assertEqual(after.status, draft.STATUS_RESOLVED)
        self.assertEqual(after.product_id, "999")

    def test_modify_targets_a_named_item_and_moves_the_focus(self):
        d = draft.new_draft("mcd")
        burger = d.add("ビッグマックセット")
        d.add("ナゲット")
        self.assertEqual(d.active_item_id, "item_2")
        d.modify(burger.id, size="L")
        self.assertEqual(d.active_item_id, burger.id)


class RemoveTest(unittest.TestCase):
    def test_remove_takes_the_line_out(self):
        d = draft.new_draft("mcd")
        d.add("ビッグマック")
        nuggets = d.add("ナゲット")
        d.remove(nuggets.id)
        self.assertEqual([i.product for i in d.items], ["ビッグマック"])

    def test_focus_falls_back_to_what_is_left(self):
        d = draft.new_draft("mcd")
        d.add("ビッグマック")
        d.add("ナゲット")
        d.remove()
        self.assertEqual(d.active_item_id, "item_1")

    def test_focus_clears_when_nothing_is_left(self):
        d = draft.new_draft("mcd")
        d.add("ナゲット")
        d.remove()
        self.assertIsNone(d.active_item_id)
        self.assertEqual(d.items, [])

    def test_remove_names_an_unknown_item(self):
        d = draft.new_draft("mcd")
        d.add("ポテト")
        with self.assertRaises(draft.DraftError):
            d.remove("item_9")


class SpecScenarioTest(unittest.TestCase):
    """再設計 §35 TEST 1-4, at the draft level.

    These exercise the operations, not the Japanese. Deciding that
    「あ、やっぱ…」 means REPLACE rather than ADD is STEP 4's job; these
    pin down that the operation, once chosen, produces the right order.
    """

    def test_1_correction_does_not_duplicate(self):
        d = draft.new_draft("mcd")
        d.add("ビッグマック")          # 「ビッグマック」
        d.replace("ビッグマックセット")  # 「あ、やっぱビッグマックセット」
        self.assertEqual([(i.product, i.quantity) for i in d.items],
                         [("ビッグマックセット", 1)])

    def test_2_addition_keeps_both(self):
        d = draft.new_draft("mcd")
        d.add("ビッグマック")   # 「ビッグマック」
        d.add("ナゲット")       # 「あとナゲット」
        self.assertEqual([(i.product, i.quantity) for i in d.items],
                         [("ビッグマック", 1), ("ナゲット", 1)])

    def test_3_successive_modifications_accumulate(self):
        d = draft.new_draft("mcd")
        d.add("ビッグマックセット")                    # 「ビッグマックセット」
        d.modify(size="L")                            # 「Lにして」
        d.modify(options={"drink": "コーラゼロ"})       # 「コーラゼロ」
        item = d.items[0]
        self.assertEqual(len(d.items), 1)
        self.assertEqual(item.size, "L")
        self.assertEqual(item.options, {"drink": "コーラゼロ"})

    def test_4_removal_after_addition(self):
        d = draft.new_draft("mcd")
        d.add("ビッグマック")
        d.add("ナゲット")        # 「ナゲットも」
        d.remove()               # 「やっぱナゲットいらない」
        self.assertEqual([i.product for i in d.items], ["ビッグマック"])

    def test_operations_survive_serialization(self):
        d = draft.new_draft("mcd", store_id="45520")
        d.add("ビッグマック")
        d.replace("ビッグマックセット")
        d.modify(size="L")
        d.add("ナゲット")
        restored = draft.OrderDraft.from_dict(d.to_dict())
        self.assertEqual(restored.to_dict(), d.to_dict())
        self.assertEqual(restored.active_item.product, "ナゲット")


if __name__ == "__main__":
    unittest.main()
