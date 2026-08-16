import unittest

from orderagent import intent


class DetectTest(unittest.TestCase):
    def test_typical_order(self):
        result = intent.detect("マックでアイスコーヒーLをモバイルオーダーして")
        self.assertIsNotNone(result)
        self.assertEqual(result.chain, "mcd")
        self.assertIn("アイスコーヒー", result.item_text)
        self.assertEqual(result.quantity, 1)

    def test_chain_without_order_word_is_not_an_order(self):
        self.assertIsNone(intent.detect("マックってどこにある？"))

    def test_order_word_without_chain_is_not_an_order(self):
        self.assertIsNone(intent.detect("さっきの注文どうなった？"))

    def test_pickup_extraction(self):
        result = intent.detect("マクドでビッグマックを注文、ドライブスルーで")
        self.assertEqual(result.pickup, "drive_through")

    def test_quantity(self):
        result = intent.detect("マックでハンバーガー3個注文して")
        self.assertEqual(result.quantity, 3)

    def test_absurd_quantity_is_rejected_not_obeyed(self):
        result = intent.detect("マックでハンバーガー100個注文して")
        self.assertIn("quantity", result.missing)
        self.assertEqual(result.quantity, 1)

    def test_missing_item_is_flagged(self):
        result = intent.detect("マックでモバイルオーダーして")
        self.assertIn("item", result.missing)

    def test_unsupported_chain_still_detects(self):
        result = intent.detect("スタバでラテを注文して")
        self.assertEqual(result.chain, "starbucks")

    def test_no_chain_imperative_defaults_to_mcd(self):
        result = intent.detect("アイスコーヒーを注文して")
        self.assertIsNotNone(result)
        self.assertEqual(result.chain, "mcd")
        self.assertIn("アイスコーヒー", result.item_text)

    def test_no_chain_status_question_stays_chat(self):
        self.assertIsNone(intent.detect("さっきの注文どうなった？"))

    def test_no_chain_noun_only_stays_chat(self):
        self.assertIsNone(intent.detect("モバイルオーダーって何？"))


class MatchMenuTest(unittest.TestCase):
    MENU = [
        {"id": "1", "name": "アイスコーヒー(L)", "price": 220},
        {"id": "2", "name": "アイスコーヒー(M)", "price": 190},
        {"id": "3", "name": "アイスカフェラテ(M)", "price": 270},
        {"id": "4", "name": "ビッグマック®", "price": 600},
        {"id": "5", "name": "ビッグマック® セット", "price": 930},
    ]

    def test_exact_beats_substring(self):
        matches = intent.match_menu("アイスコーヒーL", self.MENU)
        self.assertEqual(matches[0]["id"], "1")

    def test_plain_item_beats_set_variant(self):
        matches = intent.match_menu("ビッグマック", self.MENU)
        self.assertEqual(matches[0]["id"], "4")

    def test_registered_trademark_sign_is_ignored(self):
        matches = intent.match_menu("ビッグマックセット", self.MENU)
        self.assertEqual(matches[0]["id"], "5")

    def test_no_match_returns_empty(self):
        self.assertEqual(intent.match_menu("寿司", self.MENU), [])

    def test_empty_query_matches_nothing(self):
        self.assertEqual(intent.match_menu("", self.MENU), [])


if __name__ == "__main__":
    unittest.main()
