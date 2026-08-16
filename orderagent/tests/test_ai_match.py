import unittest

from orderagent import ai_match


def _gemini_body(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


class ParseResponseTest(unittest.TestCase):
    def test_valid_candidates(self):
        body = _gemini_body('{"candidates": ["ダブルチーズバーガー", "チーズバーガー"]}')
        self.assertEqual(ai_match._parse_response(body),
                         ["ダブルチーズバーガー", "チーズバーガー"])

    def test_empty_candidates(self):
        self.assertEqual(ai_match._parse_response(_gemini_body('{"candidates": []}')), [])

    def test_malformed_json_returns_empty(self):
        self.assertEqual(ai_match._parse_response(_gemini_body("これかな？")), [])

    def test_missing_structure_returns_empty(self):
        self.assertEqual(ai_match._parse_response({}), [])

    def test_non_string_entries_dropped(self):
        body = _gemini_body('{"candidates": ["A", 5, null]}')
        self.assertEqual(ai_match._parse_response(body), ["A"])


class SuggestGuardsTest(unittest.TestCase):
    def test_no_key_returns_empty(self):
        import os
        original = os.environ.pop("AI_PROVIDER_API_KEY", None)
        try:
            self.assertEqual(ai_match.suggest("ダブチ", [{"name": "x"}]), [])
        finally:
            if original is not None:
                os.environ["AI_PROVIDER_API_KEY"] = original

    def test_empty_menu_returns_empty(self):
        self.assertEqual(ai_match.suggest("ダブチ", []), [])


if __name__ == "__main__":
    unittest.main()
