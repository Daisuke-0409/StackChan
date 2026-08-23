"""The Gemini model names may not drift apart (audit D4).

Four files name a Gemini model, in three processes that deploy separately
and cannot import each other: the gateway is stdlib-only and moves to a Mac
mini one day, the notifier runs beside Claude Code, the order agent owns the
browser. Merging the constants would couple three things that are deliberately
independent.

So the copies stay and this makes them unable to drift in silence. The risk
being defended against is real and has happened: gemini-2.5-flash went
"no longer available to new users" and 404'd in production, and finding
every place it was written took longer than fixing it.

Run with the other suites:  powershell -ExecutionPolicy Bypass -File run_tests.ps1
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent


def _literal(relative: str, name: str) -> str:
    """The string a module assigns to `name`, read as text.

    Read rather than imported: importing three packages from three
    top-level directories into one process is exactly the coupling this
    test exists to avoid needing.
    """
    text = (REPO / relative).read_text(encoding="utf-8")
    match = re.search(rf'^{re.escape(name)} = "([^"]+)"', text, re.MULTILINE)
    assert match, f"{relative} に {name} の代入が見つからない"
    return match.group(1)


CHAT_SITES = [
    ("firmware/gateway/server.py", "GEMINI_DEFAULT_CHAT_MODEL"),
    ("firmware/gateway/server.py", "GEMINI_DEFAULT_STT_MODEL"),
    ("notifier/tachikoma_notifier/gemini_responder.py", "DEFAULT_MODEL"),
    ("orderagent/ai_match.py", "_MODEL"),
]

TTS_SITES = [
    ("firmware/gateway/server.py", "GEMINI_DEFAULT_TTS_MODEL"),
    ("notifier/tachikoma_notifier/gemini_tts_synth.py", "DEFAULT_MODEL"),
]


class ModelNamesAgreeTest(unittest.TestCase):
    def test_every_text_model_is_the_same_name(self):
        names = {site: _literal(*site) for site in CHAT_SITES}
        self.assertEqual(
            len(set(names.values())), 1,
            "テキスト生成のモデル名がファイル間でずれている。"
            "1つだけ直すと、直っていない方が本番で404する:\n"
            + "\n".join(f"  {path}:{const} = {value}"
                        for (path, const), value in names.items()))

    def test_every_speech_model_is_the_same_name(self):
        names = {site: _literal(*site) for site in TTS_SITES}
        self.assertEqual(
            len(set(names.values())), 1,
            "音声合成のモデル名がファイル間でずれている:\n"
            + "\n".join(f"  {path}:{const} = {value}"
                        for (path, const), value in names.items()))

    def test_no_fifth_place_has_appeared(self):
        """A new hardcoded model name means this list is out of date.

        Comments are allowed to mention old names -- they are the record of
        what died and why. Only assignments count.
        """
        found = set()
        for path in REPO.rglob("*.py"):
            parts = set(path.parts)
            if parts & {"node_modules", "__pycache__", ".git", "dist"}:
                continue
            if path.name.startswith("test_") or path.name == "tests_models.py":
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if re.match(r'^\s*[A-Za-z_]+ = "gemini-[^"]+"', line):
                    found.add((str(path.relative_to(REPO)).replace("\\", "/"),
                               line.split("=")[0].strip()))
        known = set(CHAT_SITES) | set(TTS_SITES)
        self.assertEqual(
            found - known, set(),
            "モデル名を直書きした場所が増えている。"
            "上の CHAT_SITES / TTS_SITES に足すこと")


if __name__ == "__main__":
    unittest.main()
