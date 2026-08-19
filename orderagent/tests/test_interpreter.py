import unittest

from orderagent import draft, interpreter


def _draft_with(*products):
    d = draft.new_draft("mcd")
    for product in products:
        d.add(product)
    return d


class AddTest(unittest.TestCase):
    def test_first_utterance_adds(self):
        u = interpreter.classify("ビッグマック", draft.new_draft("mcd"))
        self.assertEqual(u.action, interpreter.ADD)
        self.assertEqual(u.product, "ビッグマック")

    def test_ato_marks_an_addition(self):
        u = interpreter.classify("あとナゲット", _draft_with("ビッグマック"))
        self.assertEqual(u.action, interpreter.ADD)
        self.assertEqual(u.product, "ナゲット")

    def test_quantity_rides_along(self):
        u = interpreter.classify("ナゲット2個", draft.new_draft("mcd"))
        self.assertEqual(u.action, interpreter.ADD)
        self.assertEqual(u.changes.get("quantity"), 2)


class ReplaceTest(unittest.TestCase):
    def test_yappa_corrects_rather_than_adds(self):
        u = interpreter.classify("あ、やっぱビッグマックセット", _draft_with("ビッグマック"))
        self.assertEqual(u.action, interpreter.REPLACE)
        self.assertEqual(u.product, "ビッグマックセット")

    def test_janakute_corrects(self):
        u = interpreter.classify("てりやきじゃなくてダブルチーズバーガー",
                                 _draft_with("てりやき"))
        self.assertEqual(u.action, interpreter.REPLACE)

    def test_correction_with_nothing_to_correct_is_an_add(self):
        u = interpreter.classify("やっぱポテト", draft.new_draft("mcd"))
        self.assertEqual(u.action, interpreter.ADD)


class ModifyTest(unittest.TestCase):
    def test_size_alone_modifies_the_active_item(self):
        d = _draft_with("ビッグマックセット")
        u = interpreter.classify("Lにして", d)
        self.assertEqual(u.action, interpreter.MODIFY)
        self.assertEqual(u.changes, {"size": "L"})
        self.assertIsNone(u.target_item_id)

    def test_spoken_size_is_folded(self):
        u = interpreter.classify("エルにして", _draft_with("ポテト"))
        self.assertEqual(u.changes, {"size": "L"})

    def test_quantity_alone_modifies(self):
        u = interpreter.classify("3個にして", _draft_with("ナゲット"))
        self.assertEqual(u.action, interpreter.MODIFY)
        self.assertEqual(u.changes, {"quantity": 3})

    def test_named_item_targets_that_line(self):
        d = _draft_with("ビッグマックセット", "ナゲット")
        u = interpreter.classify("ビッグマックの方Lにして", d)
        self.assertEqual(u.action, interpreter.MODIFY)
        self.assertEqual(u.target_item_id, "item_1")

    def test_size_with_no_draft_asks(self):
        u = interpreter.classify("Lにして", draft.new_draft("mcd"))
        self.assertTrue(u.ambiguous)
        self.assertIsNotNone(u.question)


class RemoveTest(unittest.TestCase):
    def test_iranai_removes(self):
        u = interpreter.classify("やっぱナゲットいらない", _draft_with("ナゲット"))
        self.assertEqual(u.action, interpreter.REMOVE)

    def test_removal_beats_correction(self):
        # Both やっぱ and いらない are present; removal is what was stated.
        u = interpreter.classify("やっぱやめて", _draft_with("ポテト"))
        self.assertEqual(u.action, interpreter.REMOVE)

    def test_removal_targets_the_named_line(self):
        d = _draft_with("ビッグマック", "ナゲット")
        u = interpreter.classify("ナゲットいらない", d)
        self.assertEqual(u.target_item_id, "item_2")

    def test_removal_with_an_empty_draft_says_so(self):
        u = interpreter.classify("いらない", draft.new_draft("mcd"))
        self.assertTrue(u.ambiguous)
        self.assertIsNone(u.action)


class AmbiguityTest(unittest.TestCase):
    def test_overlapping_name_without_a_marker_asks(self):
        # "ビッグマックセット" after "ビッグマック" with no やっぱ and no あと
        # could be either reading, and they differ by one burger.
        u = interpreter.classify("ビッグマックセット", _draft_with("ビッグマック"))
        self.assertTrue(u.ambiguous)
        self.assertIsNone(u.action)
        self.assertIn("追加", u.question)

    def test_bare_word_during_a_set_waits_for_the_menu(self):
        d = draft.new_draft("mcd")
        d.add("ビッグマックセット", variant="set")
        u = interpreter.classify("コーラゼロ", d)
        self.assertTrue(u.needs_menu)
        self.assertIsNone(u.action)

    def test_unrelated_talk_is_not_an_order_edit(self):
        u = interpreter.classify("今日は暑いね", _draft_with("ポテト"))
        # It parses as an addition at worst; what matters is that a sentence
        # with no order words never removes or replaces anything.
        self.assertNotIn(u.action, (interpreter.REMOVE, interpreter.REPLACE))

    def test_ambiguous_utterances_are_not_actionable(self):
        u = interpreter.classify("ビッグマックセット", _draft_with("ビッグマック"))
        self.assertFalse(u.is_actionable())
        with self.assertRaises(draft.DraftError):
            interpreter.apply(u, _draft_with("ビッグマック"))


class QueryTest(unittest.TestCase):
    def test_spec_test_5_price_question_changes_nothing(self):
        d = _draft_with("ビッグマックセット")
        before = d.to_dict()
        u = interpreter.classify("ランチ安い？", d)
        self.assertEqual(u.action, interpreter.QUERY)
        self.assertEqual(u.query_topic, "promotion")
        self.assertEqual(d.to_dict(), before)

    def test_query_is_not_actionable(self):
        u = interpreter.classify("いくら？", _draft_with("ポテト"))
        self.assertTrue(u.is_query())
        self.assertFalse(u.is_actionable())

    def test_apply_refuses_a_query(self):
        d = _draft_with("ポテト")
        u = interpreter.classify("いくら？", d)
        with self.assertRaises(draft.DraftError):
            interpreter.apply(u, d)
        self.assertEqual(len(d.items), 1)

    def test_question_without_a_mark_is_still_a_question(self):
        # Speech-to-text drops ？ more often than people think.
        u = interpreter.classify("今ランチやってる", _draft_with("ポテト"))
        self.assertEqual(u.action, interpreter.QUERY)

    def test_price_topic(self):
        u = interpreter.classify("それいくら？", _draft_with("ナゲット"))
        self.assertEqual(u.query_topic, "price")

    def test_contents_topic(self):
        u = interpreter.classify("今何が入ってる？", _draft_with("ナゲット"))
        self.assertEqual(u.query_topic, "contents")

    def test_query_can_name_the_line_it_asks_about(self):
        d = _draft_with("ビッグマックセット", "ナゲット")
        u = interpreter.classify("ビッグマックの方いくら？", d)
        self.assertEqual(u.action, interpreter.QUERY)
        self.assertEqual(u.target_item_id, "item_1")

    def test_a_question_never_removes(self):
        # The precedence that matters: asking about something must not
        # delete it, even when the sentence contains a removal word.
        d = _draft_with("ナゲット")
        u = interpreter.classify("ナゲットいらないかな？", d)
        self.assertNotEqual(u.action, interpreter.REMOVE)
        self.assertEqual(len(d.items), 1)

    def test_an_order_is_still_an_order(self):
        # The safe precedence must not swallow ordinary ordering.
        for text in ("ビッグマックセット", "あとナゲット", "Lにして"):
            with self.subTest(text=text):
                u = interpreter.classify(text, _draft_with("ポテト"))
                self.assertNotEqual(u.action, interpreter.QUERY, text)


class ApplyTest(unittest.TestCase):
    def _run(self, utterances):
        d = draft.new_draft("mcd")
        for text in utterances:
            u = interpreter.classify(text, d)
            if u.is_actionable():
                interpreter.apply(u, d)
        return d

    def test_spec_test_1_correction_does_not_duplicate(self):
        d = self._run(["ビッグマック", "あ、やっぱビッグマックセット"])
        self.assertEqual([(i.product, i.quantity) for i in d.items],
                         [("ビッグマックセット", 1)])

    def test_spec_test_2_addition_keeps_both(self):
        d = self._run(["ビッグマック", "あとナゲット"])
        self.assertEqual([i.product for i in d.items], ["ビッグマック", "ナゲット"])

    def test_spec_test_3_size_accumulates_on_one_line(self):
        d = self._run(["ビッグマックセット", "Lにして"])
        self.assertEqual(len(d.items), 1)
        self.assertEqual(d.items[0].size, "L")

    def test_spec_test_4_removal_after_addition(self):
        d = self._run(["ビッグマック", "あとナゲット", "やっぱナゲットいらない"])
        self.assertEqual([i.product for i in d.items], ["ビッグマック"])

    def test_a_longer_conversation_converges_to_one_line(self):
        d = self._run(["ビッグマック", "あ、やっぱビッグマックセット", "Lにして"])
        self.assertEqual(len(d.items), 1)
        self.assertEqual(d.items[0].product, "ビッグマックセット")
        self.assertEqual(d.items[0].size, "L")


class PurityTest(unittest.TestCase):
    def test_interpreter_talks_to_nothing(self):
        import inspect
        import re
        imports = re.findall(r"^\s*(?:from|import)\s+(\S+)",
                             inspect.getsource(interpreter), re.M)
        for name in imports:
            self.assertNotIn(name.split(".")[0],
                             {"urllib", "playwright", "requests", "sqlite3",
                              "socket", "http", "ai_match", "stores"},
                             f"interpreter.py must not import {name}")


if __name__ == "__main__":
    unittest.main()
