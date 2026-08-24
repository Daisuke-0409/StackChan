# 迷ったらこれを実行する。いまどこまで進んでいて、次に何をすればいいかを出す。
#
#   powershell -ExecutionPolicy Bypass -File NEXT.ps1
#
# 手順書ではなく点検表。書いてある順に済ませていけば終わる。
# 済んだ項目は勝手に消えるので、何度実行してもいい。

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$gateway = Join-Path $root "firmware\gateway"
$envFile = Join-Path $gateway ".env"

function Read-Env([string]$path) {
    $map = @{}
    if (-not (Test-Path $path)) { return $map }
    foreach ($line in Get-Content $path) {
        $t = $line.Trim()
        if ($t -eq "" -or $t.StartsWith("#")) { continue }
        $i = $t.IndexOf("=")
        if ($i -lt 1) { continue }
        $map[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim().Trim('"')
    }
    return $map
}

# 家か会社かは、この機械の IP で分かる。手順が違うので先に判定する。
$addresses = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
              Select-Object -ExpandProperty IPAddress)
$isOffice = $addresses -contains "192.168.11.200"
$where = if ($isOffice) { "会社" } else { "家" }

$conf = Read-Env $envFile
$todo = New-Object System.Collections.ArrayList
$done = New-Object System.Collections.ArrayList

function Need([string]$title, [string]$how) {
    $null = $todo.Add([pscustomobject]@{ Title = $title; How = $how })
}
function Ok([string]$title) { $null = $done.Add($title) }

Write-Host ""
Write-Host "=== タチコマ / いまどこ ===" -ForegroundColor Cyan
Write-Host "  ここは $where のパソコンです"

# --- git ------------------------------------------------------------------
Push-Location $root
$branch = (git rev-parse --abbrev-ref HEAD 2>$null)
$behind = (git rev-list --count "HEAD..origin/$branch" 2>$null)
$ahead  = (git rev-list --count "origin/$branch..HEAD" 2>$null)
$dirty  = (git status --porcelain 2>$null)
Pop-Location

if ($behind -and [int]$behind -gt 0) {
    Need "最新のコードを取り込む ($behind コミット遅れ)" "git pull"
} else { Ok "コードは最新" }
if ($ahead -and [int]$ahead -gt 0) {
    Need "$ahead コミットが手元に残っている" "git push"
}
if ($dirty) { Need "保存していない変更がある" "git status" }

if ($isOffice) {
    # --- 会社でやること ---------------------------------------------------
    $cache = Join-Path $root "firmware\build\CMakeCache.txt"
    if (Test-Path $cache) { Ok "会社の機体のトークンはこの機械にある" }

    # 「トークンを家に送ったか」は、送った側では分からない。分かるのは結果の
    # ほうで、/v1/chat と /v1/transcribe は認証が要るから、そこに 200 が出て
    # いれば家の頭がこの機体を受け入れている。転送役のログがその証拠になる。
    $fwdLog = (Read-Env (Join-Path $gateway ".env.forwarder"))["TACHIKOMA_LOG_FILE"]
    $talked = $false
    if ($fwdLog -and (Test-Path $fwdLog)) {
        $talked = $null -ne (Get-Content $fwdLog -Encoding UTF8 -Tail 3000 -ErrorAction SilentlyContinue |
                             Select-String -Pattern '(/v1/chat|/v1/transcribe) -> 200' |
                             Select-Object -First 1)
    }
    if ($talked) {
        Ok "会社の体は家の頭と会話できている (トークンは届いている)"
    } else {
        Need "会社の機体のトークンを家に送る" @"
powershell -ExecutionPolicy Bypass -File firmware\gateway\copy_device_token.ps1 -SendTo oo
  家で受け取る: tailscale file get .
  (会話の記録がログに無いだけかもしれない。一度話しかけてから見直すこと)
"@
    }
    foreach ($t in @("Tachikoma Forwarder", "Tachikoma CRM Relay")) {
        $state = (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue).State
        if ($state -eq "Running") { Ok "$t 稼働中" }
        elseif ($state) { Need "$t が止まっている" "Start-ScheduledTask -TaskName '$t'" }
    }
    $gw = (Get-ScheduledTask -TaskName "Tachikoma Gateway" -ErrorAction SilentlyContinue)
    if ($gw -and $gw.State -ne "Disabled") {
        Need "会社でゲートウェイが有効になっている (記憶が枝分かれする)" `
             "Disable-ScheduledTask -TaskName 'Tachikoma Gateway'"
    } else { Ok "会社のゲートウェイは無効 (正しい)" }
} else {
    # --- 家でやること -----------------------------------------------------

    # 1. 会社の機体のトークン
    $tokens = @()
    if ($conf.ContainsKey("DEVICE_TOKEN")) {
        $tokens = @($conf["DEVICE_TOKEN"].Split(",") | Where-Object { $_.Trim() })
    }
    if ($tokens.Count -ge 2) {
        Ok "DEVICE_TOKEN に $($tokens.Count) 台ぶん入っている (会社の体も喋れる)"
    } else {
        Need "会社の機体のトークンを受け取って足す → 会社の体が喋るようになる" @"
tailscale file get .
  受け取ったファイルの中身を firmware\gateway\.env の
  DEVICE_TOKEN= の末尾にカンマで足す (指紋 dde5d869 / 48文字)
  足したらファイルを消す
"@
    }

    # 2. ローカル音声認識器
    python -c "import faster_whisper" 2>$null
    $hasWhisper = ($LASTEXITCODE -eq 0)
    if ($hasWhisper) { Ok "faster-whisper は入っている" }
    else { Need "音声認識器を入れる → 聞き取りが良くなる" "pip install faster-whisper" }

    $sttEnv = Join-Path $gateway ".env.local_stt"
    if (Test-Path $sttEnv) { Ok "認識器の設定ファイルがある" }
    elseif ($hasWhisper) {
        Need "認識器の設定を作る" "copy firmware\gateway\.env.local_stt.example firmware\gateway\.env.local_stt"
    }

    $listening = Get-NetTCPConnection -State Listen -LocalPort 9000 -ErrorAction SilentlyContinue
    if ($listening) { Ok "認識器が動いている (9000番)" }
    elseif ($hasWhisper -and (Test-Path $sttEnv)) {
        Need "認識器を起動する (初回はモデルのダウンロードあり)" "powershell -ExecutionPolicy Bypass -File firmware\gateway\run_local_stt.ps1"
    }

    if ($conf["STT_PROVIDER"] -eq "local") { Ok "ゲートウェイは認識器を見ている" }
    elseif ($listening) {
        Need "ゲートウェイを認識器に繋ぎ替える" @"
firmware\gateway\.env に3行:
  STT_PROVIDER=local
  STT_PROVIDER_URL=http://127.0.0.1:9000/v1/audio/transcriptions
  STT_PROVIDER_API_KEY=local
そのあとゲートウェイを再起動
"@
    } else {
        # 切り替え前に、いまの状態の感触を控えておく価値がある
        if ($conf["STT_VOCABULARY"] -and $conf["STT_VOCABULARY"].Split(",").Count -ge 5) {
            Ok "STT_VOCABULARY は複数語 (名前の混入対策が効いている)"
        } elseif ($conf.ContainsKey("STT_VOCABULARY")) {
            Need "STT_VOCABULARY が少なすぎる (1語だと言っていない名前が混ざる)" @"
firmware\gateway\.env の STT_VOCABULARY を15語くらいに:
  タチコマ,大輔,ダイボ石材,加江田,佐土原,田野,綾,篠崎,染川,霊標,管理表,墓所,区画,宮崎,霊園
"@
        }
    }

    # 3. CRM
    if ($conf["CRM_RELAY_TOKEN"]) { Ok "CRM の合言葉は入っている" }
    else { Need "CRM を引けるようにする" "powershell -ExecutionPolicy Bypass -File firmware\gateway\setup_crm_relay_token.ps1" }

    # 「モニターに出して」は、どの体に話しかけたかで画面を選ぶ。会社の機体の
    # 番号を教えていないと、会社で話しかけたのに家の画面に出る = 誰も見ていない
    # 画面に顧客カルテを開く、という一番まずい形になる。
    if ($conf["CRM_OFFICE_DEVICE_IDS"]) {
        Ok "会社の体に話しかけたら会社の画面に出る設定がある"
    } elseif ($conf["CRM_RELAY_TOKEN"]) {
        Need "「モニターに出して」の行き先を教える" @"
firmware\gateway\.env に1行:
  CRM_OFFICE_DEVICE_IDS=80456B4DE7AC
これが無いと、会社で話しかけても家の画面にカルテが開く
(誰も見ていない画面に顧客情報を出すことになる)
"@
    }

    if ($conf["CRM_RELAY_URL"] -and $conf["CRM_RELAY_URL"] -notmatch "127\.0\.0\.1|localhost") {
        Ok "CRM の宛先は tailnet を向いている"
    } elseif ($conf["CRM_RELAY_TOKEN"]) {
        Need "CRM の宛先を会社の tailnet アドレスにする" @"
firmware\gateway\.env:
  CRM_RELAY_URL=http://100.76.60.88:8767
127.0.0.1 のままだと、家の画面に出すリンクが家からは開けない形になる
"@
    }

    $gw = (Get-ScheduledTask -TaskName "Tachikoma Gateway" -ErrorAction SilentlyContinue)
    if ($gw -and $gw.State -eq "Disabled") {
        Need "家のゲートウェイが無効になっている" "Enable-ScheduledTask -TaskName 'Tachikoma Gateway'"
    }
}

# --- 出力 -----------------------------------------------------------------
Write-Host ""
if ($done.Count -gt 0) {
    Write-Host "済んでいること:" -ForegroundColor DarkGray
    foreach ($d in $done) { Write-Host "  o $d" -ForegroundColor DarkGray }
}

Write-Host ""
if ($todo.Count -eq 0) {
    Write-Host "次にやることはありません。" -ForegroundColor Green
    Write-Host "続きの候補は CLAUDE.md の「次セッションの開始点」に書いてあります。"
} else {
    Write-Host "次にやること:" -ForegroundColor Yellow
    $n = 1
    foreach ($t in $todo) {
        Write-Host ""
        Write-Host "  [$n] $($t.Title)" -ForegroundColor Yellow
        foreach ($line in $t.How -split "`n") {
            if ($line.Trim()) { Write-Host "      $($line.TrimEnd())" }
        }
        $n++
    }
}
Write-Host ""
Write-Host "詳しい事情は CLAUDE.md の「次セッションの開始点」に書いてあります。"
Write-Host ""
