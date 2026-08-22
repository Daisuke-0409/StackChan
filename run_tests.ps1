# run_tests.ps1 -- 全テストを1コマンドで。
#
#   powershell -File run_tests.ps1
#
# 3つのスイートは置き場所も起動の仕方も違い、それぞれ別の呼び方が要る。
# 覚えていられないものは実行されなくなる: notifier の313件は tests/ に
# __init__.py が無いせいで discover から見えず、「Ran 0 tests」を成功と
# 見間違えたまま長いこと誰も走らせていなかった (2026-08-22に発見)。
# 数を文書に書き写すのもやめる。数えるのはこのスクリプトの仕事。
#
# 終了コード: 全部通れば 0、1つでも落ちれば 1。

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONIOENCODING = "utf-8"

# 実測済み: 3スイートとも標準ライブラリだけで通る (重い依存は全部関数内
# import)。なので CI でも動く。ここで python を選ぶのは launcher と同じ理由。
$python = $env:TACHIKOMA_PYTHON
if (-not $python) {
    $python = (Get-Command python.exe -All -ErrorAction SilentlyContinue |
               Select-Object -First 1 -ExpandProperty Source)
}
if (-not $python) { Write-Error "python.exe が見つからない" }

$suites = @(
    @{ Name = "gateway";    Start = "firmware/gateway"; Top = "."         },
    @{ Name = "orderagent"; Start = "orderagent/tests"; Top = "."         },
    @{ Name = "notifier";   Start = "notifier";         Top = "notifier"  },
    # リポジトリ全体にかかる規律のテスト (モデル名のドリフト等)。
    # discover ではなく名指し: ルートから discover すると orderagent を
    # もう一度拾って二重に数える。
    @{ Name = "repo";       Module = "tests_models"                       }
)

$total = 0
$failed = @()
foreach ($suite in $suites) {
    Write-Host "--- $($suite.Name) ---"
    if ($suite.Module) {
        $output = & $python -m unittest $suite.Module 2>&1 | Out-String
    } else {
        $output = & $python -m unittest discover -s $suite.Start -t $suite.Top 2>&1 | Out-String
    }
    $ran = 0
    if ($output -match "Ran (\d+) test") { $ran = [int]$Matches[1] }
    $total += $ran
    if ($output -match "(?m)^OK") {
        $skipped = ""
        if ($output -match "OK \(skipped=(\d+)\)") { $skipped = " (skip $($Matches[1]))" }
        Write-Host "  OK  $ran 件$skipped"
    } else {
        Write-Host "  NG  $ran 件 -- 下に詳細"
        Write-Host $output
        $failed += $suite.Name
    }
    # 0件は成功ではない。discover が何も見つけていないだけの可能性がある。
    if ($ran -eq 0) {
        Write-Host "  !!  1件も実行されていない。tests/ に __init__.py があるか確認"
        $failed += $suite.Name
    }
}

Write-Host ""
if ($failed.Count -eq 0) {
    Write-Host "全部通った: $total 件"
    exit 0
}
Write-Host "失敗したスイート: $($failed -join ', ')  (実行 $total 件)"
exit 1
