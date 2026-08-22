# run_tests.ps1 -- 全テストを1コマンドで。
#
#   powershell -File run_tests.ps1              # 全部
#   powershell -File run_tests.ps1 notifier     # 1つだけ
#
# 中身は tools/run_suites.py。スイートの定義をそこに1つだけ置いて、CI も
# 同じものを呼ぶ (.github/workflows/tests.yml)。ここに一覧を書き写すと
# CI とずれるし、ずれたことに誰も気づかない。
#
# 「Ran 0 tests ... OK」は成功ではない、という判定もあちら側にある:
# notifier の313件は tests/__init__.py が無いせいで discover から見えず、
# 0件成功を成功と読み違えたまま長く放置されていた (2026-08-22 に発見)。

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONIOENCODING = "utf-8"

$python = $env:TACHIKOMA_PYTHON
if (-not $python) {
    $python = (Get-Command python.exe -All -ErrorAction SilentlyContinue |
               Select-Object -First 1 -ExpandProperty Source)
}
if (-not $python) { Write-Error "python.exe が見つからない" }

& $python (Join-Path $repo "tools/run_suites.py") @args
exit $LASTEXITCODE
