"""Commands written down for a person to type must actually run.

Windows refuses to run a .ps1 file unless the execution policy allows it,
and on both of these machines the policy is undefined at every scope --
which means Restricted. So `powershell -File whatever.ps1`, typed into a
fresh terminal, dies before the script's first line:

    このシステムではスクリプトの実行が無効になっているため、
    ファイル ... を読み込むことができません

None of this was visible for weeks, because nothing that runs unattended
goes through that door: every scheduled task in this project already
passes -ExecutionPolicy Bypass, and an agent's shell inherits Bypass in
the process scope. The machine was fine. Only the instructions were
broken, and only for the one reader they were written for (2026-08-24).

The fix was to write the flag into all 49 places. This test is what stops
the 50th from being written without it. It does not touch the system
policy: changing that is a security setting on somebody's PC, and a
project should not quietly widen it to save eleven characters.

Run with the other suites:  powershell -ExecutionPolicy Bypass -File run_tests.ps1
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent

# Where a person could copy a command out of. Source that merely mentions
# PowerShell is not interesting; text meant to be typed is.
SUFFIXES = (".md", ".ps1", ".py")

SKIP_DIRECTORIES = {
    ".git", "node_modules", "build", "managed_components",
    "backups", "__pycache__", ".venv", "venv",
}

# powershell ... -File, with anything in between, as long as the run of
# switches between them does not already say Bypass.
LAUNCH = re.compile(r"powershell(?:\.exe)?((?:\s+-[A-Za-z]+(?:\s+\S+)?)*)\s+-File",
                    re.IGNORECASE)


SELF = Path(__file__).resolve()


def _files():
    for path in REPO.rglob("*"):
        if path.suffix.lower() not in SUFFIXES:
            continue
        if any(part in SKIP_DIRECTORIES for part in path.parts):
            continue
        # This file quotes the broken form on purpose, to say what it is.
        # A checker that trips over its own description of the fault is a
        # checker nobody keeps -- the same thing happened twice already
        # with the purity tests, which learned to read imports instead of
        # prose. Here the prose is the point, so skip only this one file.
        if path.resolve() == SELF:
            continue
        yield path


class LaunchCommandsRunTest(unittest.TestCase):
    def test_every_written_powershell_command_says_bypass(self):
        offenders = []
        for path in _files():
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                # 失敗台帳は「こう書くと落ちる」を見せる必要がある。その行
                # だけは見逃すが、見逃す条件は読み手にも見えていないと意味が
                # 無いので、目印は日本語の「禁止例」そのものにしてある。
                # 通りすがりに書ける言葉ではないので、黙って無効化される
                # 心配は無い。
                if "禁止例" in line:
                    continue
                for match in LAUNCH.finditer(line):
                    if "-ExecutionPolicy" in match.group(1):
                        continue
                    offenders.append(
                        f"{path.relative_to(REPO)}:{number}  {line.strip()}")

        self.assertEqual(
            offenders, [],
            "実行ポリシーで弾かれるコマンドが書かれている。"
            "-ExecutionPolicy Bypass を足すこと:\n  "
            + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
