# self_check.ps1 -- タチコマの健康診断。全サービスの生死を1画面で。
#
#   powershell -File self_check.ps1            # 診断だけ
#   powershell -File self_check.ps1 -Repair    # 死んでいたら起こす
#
# 全障害が「ロボットが無言」でしか発見されなかった(監査C2)への答え。
# 5分毎の scheduled task がこれを -Repair 付きで回す(監査C1の再起動
# ポリシー)。タスクが勝手に無効になる問題(C5)も、ここで検知して直す。
#
# 出力は1サービス1行。ログに落として後から読めるよう、日時を頭に付ける。
# 終了コード: 必須サービスが全部生きていれば 0、欠けていれば 1。

param([switch]$Repair)

$ErrorActionPreference = "Continue"

# このPC(家・脳側)のサービス台帳。
#   Task     = scheduled task 名 (無ければ $null: 手動起動のもの)
#   Port     = listen していれば生きている印
#   Health   = 200 が返れば中身も生きている印 ($null なら port 確認のみ)
#   Required = 落ちていたら終了コード1にするか
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

$now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$allRequiredUp = $true

# --- 静的IP: 実機は 192.168.2.120 を向いて焼かれている。ここが変わると
# --- 全機体が無言になる(2026-08-21に実際に起きた)。最初に確認する。
$expectedIp = "192.168.2.120"
$hasIp = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -eq $expectedIp }).Count -gt 0
if ($hasIp) {
    Write-Output "$now [OK]   StaticIP   $expectedIp"
} else {
    Write-Output "$now [DEAD] StaticIP   $expectedIp がこのPCに無い。実機は全部無言になる。OPERATIONS.md の 2 を見て固定し直すこと"
    $allRequiredUp = $false
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
    if ($svc.Task -and $null -eq $task) { $why = "タスク『$($svc.Task)』が存在しない" }
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
