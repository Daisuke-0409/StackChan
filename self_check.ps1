# self_check.ps1 -- タチコマの健康診断。全サービスの生死を1画面で。
#
#   powershell -ExecutionPolicy Bypass -File self_check.ps1            # 診断だけ
#   powershell -ExecutionPolicy Bypass -File self_check.ps1 -Repair    # 死んでいたら起こす
#
# 全障害が「ロボットが無言」でしか発見されなかった(監査C2)への答え。
# 5分毎の scheduled task がこれを -Repair 付きで回す(監査C1の再起動
# ポリシー)。タスクが勝手に無効になる問題(C5)も、ここで検知して直す。
#
# 家と会社では台帳が違う。同じ機械ではないので、同じ検査表を当てると
# 嘘をつく。会社で「Gateway OK」と出ていたのは 8080 を握っていたのが
# 転送役だったからで、健康診断としては誤診だった(2026-08-24 に発見)。
# それ以上に危ないのは -Repair のほうで、会社の『Tachikoma Gateway』が
# Disabled なのは故障ではなく意図(頭は家に1つ)なのに、修理側は
# 「無効化されたタスクは C5 の再発」とみなして有効化しようとする。
# 会社でそれが走ると頭が2つになり、記憶が黙って枝分かれする。
#
# 出力は1サービス1行。ログに落として後から読めるよう、日時を頭に付ける。
# 終了コード: 必須サービスが全部生きていれば 0、欠けていれば 1。

param([switch]$Repair)

$ErrorActionPreference = "Continue"

# 家か会社かは、この機械の IP で分かる (NEXT.ps1 と同じ判定)。
# 会社の IP があるときだけ会社、それ以外は家。家の固定 IP のほうで
# 判定しないのは、それが外れているとき(2026-08-21 に実際に起きた)に
# 家を会社と誤認して、家のゲートウェイを検査表から落としてしまうため。
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
               Select-Object -ExpandProperty IPAddress)
$isOffice = $addresses -contains "192.168.11.200"
$site = if ($isOffice) { "会社" } else { "家" }

# サービス台帳。
#   Task     = scheduled task 名 (無ければ $null: 手動起動のもの)
#   Port     = listen していれば生きている印
#   Health   = 200 が返れば中身も生きている印 ($null なら port 確認のみ)
#   Required = 落ちていたら終了コード1にするか
if ($isOffice) {
    # 会社にあるのは体と、体を家の頭に繋ぐ配線だけ。頭はここでは動かさない。
    $services = @(
        # /healthz は転送役が自分で答える = この配線自体が生きているか。
        @{ Name = "Forwarder";  Task = "Tachikoma Forwarder";      Port = 8080
           Health = "http://127.0.0.1:8080/healthz";               Required = $true },
        # /health は家まで中継される = 会社の体が頭に届いているか。
        # ここが落ちていると会社の体は無言になるが、直す場所は家にある。
        @{ Name = "Head";       Task = $null;                      Port = 8080
           Health = "http://127.0.0.1:8080/health";                Required = $true },
        @{ Name = "CRMRelay";   Task = "Tachikoma CRM Relay";      Port = 8767
           Health = "http://127.0.0.1:8767/healthz";               Required = $true },
        # 会社の VOICEVOX は誰も使っていない (音声合成は家の頭がやり、
        # 音は再生待ちの列に乗って降りてくる)。落ちていても実害は無い。
        @{ Name = "VOICEVOX";   Task = "VOICEVOX Engine (Tachikoma)"; Port = 50021
           Health = "http://127.0.0.1:50021/version";              Required = $false }
    )
} else {
    $services = @(
        @{ Name = "VOICEVOX";    Task = "VOICEVOX Engine (Tachikoma)"; Port = 50021
           Health = "http://127.0.0.1:50021/version";                  Required = $true },
        @{ Name = "LocalSTT";    Task = "Tachikoma Local STT";         Port = 9000
           Health = "http://127.0.0.1:9000/healthz";                   Required = $true },
        @{ Name = "Gateway";     Task = "Tachikoma Gateway";           Port = 8080
           Health = "http://127.0.0.1:8080/health";                    Required = $true },
        @{ Name = "OrderAgent";  Task = "Tachikoma Order Agent";       Port = 8766
           Health = "http://127.0.0.1:8766/health";                    Required = $true },
        @{ Name = "EvenTerm";    Task = "Even Terminal (Tachikoma)";   Port = $null
           Health = $null;                                             Required = $false },
        @{ Name = "EvenCodex";   Task = "Even Terminal Codex (Tachikoma)"; Port = $null
           Health = $null;                                             Required = $false },
        @{ Name = "Approval";    Task = $null;                         Port = 8378
           Health = "http://127.0.0.1:8378/health";                    Required = $false }
    )
}

$now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$allRequiredUp = $true

Write-Output "$now [....] ここは $site のパソコン"

if ($isOffice) {
    # --- 会社で頭が動いていないことの確認。無効なのが正常。
    # --- 他のサービスと逆で、生きていたらそれが異常。
    $gw = Get-ScheduledTask -TaskName "Tachikoma Gateway" -ErrorAction SilentlyContinue
    if ($null -eq $gw -or $gw.State -eq "Disabled") {
        Write-Output "$now [OK]   Gateway    会社では無効。正しい (頭は家に1つ)"
    } else {
        Write-Output "$now [DEAD] Gateway    会社で有効になっている。記憶が枝分かれする"
        $allRequiredUp = $false
        if ($Repair) {
            if ($gw.State -eq "Running") {
                Stop-ScheduledTask -TaskName "Tachikoma Gateway" -ErrorAction SilentlyContinue
            }
            Disable-ScheduledTask -TaskName "Tachikoma Gateway" | Out-Null
            Write-Output "$now [REP.] Gateway    無効に戻した"
        }
    }
} else {
    # --- 静的IP: 実機は 192.168.2.120 を向いて焼かれている。ここが変わると
    # --- 全機体が無言になる(2026-08-21に実際に起きた)。最初に確認する。
    $expectedIp = "192.168.2.120"
    if ($addresses -contains $expectedIp) {
        Write-Output "$now [OK]   StaticIP   $expectedIp"
    } else {
        Write-Output "$now [DEAD] StaticIP   $expectedIp がこのPCに無い。実機は全部無言になる。OPERATIONS.md の 2 を見て固定し直すこと"
        $allRequiredUp = $false
    }
}

# --- 機体からこのPCへ届くか -------------------------------------------------
# ここまでの検査は全部「このPCから見て」で、それだけでは足りない。2026-08-24 に
# 全部緑のまま機体が3時間45分無言だった。原因はネットワークの再分類で、Windows が
# 会社の Wi-Fi を Public に付け替え、8080 を通す規則が Private 限定だったため、
# 機体からの接続だけが静かに落とされていた。PC は健康で、扉が閉まっていた。
$lanIp = if ($isOffice) { "192.168.11.200" } else { "192.168.2.120" }
$lanAlias = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
             Where-Object { $_.IPAddress -eq $lanIp } |
             Select-Object -First 1 -ExpandProperty InterfaceAlias)
if ($lanAlias) {
    $category = (Get-NetConnectionProfile -InterfaceAlias $lanAlias `
                 -ErrorAction SilentlyContinue).NetworkCategory
    if ($category -eq "Public") {
        Write-Output "$now [DEAD] Network    $lanAlias が Public に分類されている。機体からの接続は落とされる"
        Write-Output "$now [....] Network    管理者の PowerShell で戻す: Set-NetConnectionProfile -InterfaceAlias '$lanAlias' -NetworkCategory Private"
        $allRequiredUp = $false
        # -Repair でも直さない。ネットワークの分類とファイアウォールは
        # このPCのセキュリティ設定で、点検表が黙って広げていいものではない。
    } elseif ($category) {
        Write-Output "$now [OK]   Network    $lanAlias は $category"
    }
}

# 機体が実際にポーリングしているか。転送役は要求が来たときだけ書くので、
# **ログが止まっていること自体が信号**になる (毎分1行の要約が出るはずなので)。
if ($isOffice) {
    $fwdLog = ""
    $envFile = Join-Path $PSScriptRoot "firmware\gateway\.env.forwarder"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile -Encoding UTF8) {
            if ($line.Trim().StartsWith("TACHIKOMA_LOG_FILE=")) {
                $fwdLog = $line.Trim().Substring(19).Trim().Trim('"')
            }
        }
    }
    if ($fwdLog -and (Test-Path $fwdLog)) {
        $age = ((Get-Date) - (Get-Item $fwdLog).LastWriteTime).TotalMinutes
        $lastSummary = Get-Content $fwdLog -Encoding UTF8 -Tail 40 -ErrorAction SilentlyContinue |
                       Select-String -Pattern 'speech_polls=(\d+)' | Select-Object -Last 1
        $polls = if ($lastSummary) { [int]$lastSummary.Matches[0].Groups[1].Value } else { -1 }
        if ($age -gt 3) {
            Write-Output ("$now [DEAD] Robot      転送役のログが {0:N0} 分止まっている。機体が届いていない" -f $age)
            $allRequiredUp = $false
        } elseif ($polls -eq 0) {
            Write-Output "$now [DEAD] Robot      直近1分の speech_polls=0。機体が届いていない"
            $allRequiredUp = $false
        } elseif ($polls -gt 0) {
            Write-Output "$now [OK]   Robot      機体は届いている (直近1分で $polls 回)"
        }
    }
}

foreach ($svc in $services) {
    $portUp = $false
    if ($null -ne $svc.Port) {
        $portUp = @(Get-NetTCPConnection -LocalPort $svc.Port -State Listen `
                    -ErrorAction SilentlyContinue).Count -gt 0
    }

    $healthUp = $false
    if ($portUp -and $svc.Health) {
        try {
            $resp = Invoke-WebRequest $svc.Health -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
            $healthUp = ($resp.StatusCode -eq 200)
        } catch { $healthUp = $false }
    }

    $task = $null
    $taskRunning = $false
    if ($svc.Task) {
        $task = Get-ScheduledTask -TaskName $svc.Task -ErrorAction SilentlyContinue
        if ($null -ne $task) { $taskRunning = ($task.State -eq "Running") }
    }

    # 生きている、の定義: health があるなら health、port だけなら port、
    # どちらも無い(EvenTerm系)ならタスクの Running。
    if ($svc.Health)          { $alive = $healthUp }
    elseif ($null -ne $svc.Port) { $alive = $portUp }
    else                      { $alive = $taskRunning }

    if ($alive) {
        Write-Output "$now [OK]   $($svc.Name)"
        continue
    }

    # ここから死亡診断。原因を1行で言い切る。
    $why = ""
    if ($svc.Name -eq "Head") { $why = "会社の体が家の頭に届いていない。直す場所は家 (家PCとTailscaleを見る)" }
    elseif ($svc.Task -and $null -eq $task) { $why = "タスク『$($svc.Task)』が存在しない" }
    elseif ($task -and $task.State -eq "Disabled") { $why = "タスクが無効化されている(C5の再発)" }
    elseif ($portUp -and -not $healthUp) { $why = "port は開いているが health が返らない(固まっている)" }
    elseif ($taskRunning) { $why = "タスクは Running だが port $($svc.Port) を聞いていない(起動途中か、死にかけ)" }
    else { $why = "動いていない" }

    $mark = "[DEAD]"
    if (-not $svc.Required) { $mark = "[off ]" }
    Write-Output "$now $mark $($svc.Name)  $why"
    if ($svc.Required) { $allRequiredUp = $false }

    # --- 修理。タスクがあるものだけ。起動直後(3分以内)は起動中とみなして
    # --- 手を出さない: STTのモデル読みは分単位で、急かすと永久再起動になる。
    if (-not $Repair -or -not $svc.Task -or $null -eq $task) { continue }
    $info = Get-ScheduledTaskInfo -TaskName $svc.Task -ErrorAction SilentlyContinue
    if ($taskRunning -and $info -and $info.LastRunTime -gt (Get-Date).AddMinutes(-3)) {
        Write-Output "$now [....] $($svc.Name)  起動から3分経っていないので待つ"
        continue
    }
    if ($task.State -eq "Disabled") { Enable-ScheduledTask -TaskName $svc.Task | Out-Null }
    if ($taskRunning) { Stop-ScheduledTask -TaskName $svc.Task -ErrorAction SilentlyContinue }

    # Stop-ScheduledTask kills the PowerShell wrapper, NOT the python it
    # started. A half-dead server left holding the port is worse than a
    # dead one: the next start binds the same port anyway (Windows lets it),
    # the OS splits connections between the two, and the old process --
    # whose stdout pipe died with its wrapper -- drops every request it
    # gets. That is a service which is listening and answers nothing, and
    # it is what this script itself caused on 2026-08-22. So take the port
    # back by force before starting anything.
    if ($null -ne $svc.Port) {
        $holders = @(Get-NetTCPConnection -LocalPort $svc.Port -State Listen `
                     -ErrorAction SilentlyContinue |
                     Select-Object -ExpandProperty OwningProcess -Unique)
        foreach ($holder in $holders) {
            Write-Output "$now [KILL] $($svc.Name)  ポートを掴んだままの PID $holder を止める"
            Stop-Process -Id $holder -Force -ErrorAction SilentlyContinue
        }
        if ($holders.Count -gt 0) { Start-Sleep -Seconds 2 }
    }

    Start-ScheduledTask -TaskName $svc.Task
    Write-Output "$now [REP.] $($svc.Name)  タスクを起動し直した"
}

if ($allRequiredUp) { exit 0 } else { exit 1 }
