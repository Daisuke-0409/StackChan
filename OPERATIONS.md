# OPERATIONS.md — タチコマ運用・作業ルール

タチコマを触るときの手順・守るべき規則・実際に踏んだ落とし穴・無言時の
復旧。CLAUDE.md から分離（Codex秘書の三層分離を輸入。人格=SOUL、
思想=PHILOSOPHY、ルール=ここ、現在地=CLAUDE.md）。

判断の"なぜ"は PHILOSOPHY.md。ここは"どうやるか"。

---

## 1. 作業ポリシー

### 確認のルール

- **1ステップにつき確認は最大1回、1フェーズにつき最大3回**。
  判断がつくものは自分で決めて進め、事後に報告する
- 以下は**必ず事前確認**する:
  - `idf.py flash` などの実機書き込み
  - `git push`
  - シークレット(APIキー・トークン)の扱い
  - 削除・上書きなど取り消せない操作

### 実機書き込みの手順

**フラッシュ前に必ず16MBフルバックアップを取る。**
`backups/` に `stackchan_backup_<日時>_<目的>.bin` として保存。
バックアップが失敗したらフラッシュしない。

```powershell
esptool.py -p COM3 -b 460800 read_flash 0 0x1000000 <backup>
```

921600 baud は `Serial data stream stopped` で失敗した実績があるため 460800 を使う。

### 家と会社

**どちらの実機で作業しているか毎回確認する。** 構成とIPアドレスが異なる。
デバイスに焼き込まれたゲートウェイURLは固定IPなので、PCのIPが変わると繋がらない
(`run_gateway.ps1` が起動時に現在のLAN IPを表示する)。

### 秘密情報

トークン・APIキー・Hook生JSON・会話内容を、ログ・音声・共通イベントに出さない。
以下は gitignore 済み:

- `firmware/gateway/.env` — APIキー、デバイストークン
- `firmware/gateway/memory/` — 実名・居住地などの個人データ
- `firmware/gateway/voicevox_dict.json` — 個人名の読み

---

## 2. 環境固有の落とし穴 (実際に踏んだもの・失敗台帳)

### 実機が無言になったら、まず**このPCのIPアドレス**を見る (2026-08-21)

機体は焼き込まれた `TACHIKOMA_GATEWAY_URL=http://192.168.2.120:8080/...` を
叩きに行く。**DHCP でこのPCのアドレスが変わると、機体は空き部屋をノックし
続ける。**この日は .120 → .103 に変わっていて、実機が丸一日無言だった。

- 症状: 実機が完全に沈黙。ゲートウェイのログに**その機体の行が1行も出ない**
  (会社の機体は Tailscale 経由なので平常どおり出る。これに騙されないこと)
- 確認: `Get-NetIPConfiguration`。ログの device_id を数えて、家の機体
  (`80456B4DE03C`) が居るかを見る
- 対処: **イーサネットを 192.168.2.120 に固定済み** (2026-08-21、要管理者権限)。
  再発しないはずだが、ルーターを替えたら真っ先にここを疑う

### 通信が長く切れた後、実機の音声再生が固まることがある (2026-08-21)

上記の復旧後、**音声データは機体まで届いているのに再生されない**状態が残った。
ログ上は `speak_queue ... 200` (機体が受け取った) が出るので、ゲートウェイ側は
正常に見える。顔も普通に動く。**電源を入れ直すと直る。**

- 切り分け: `/v1/announce` で喋らせて、ログが 200 になるか見る。
  200 なのに音が出ないなら機体側。204 のままならキューまで届いていない
- 過去のコーデッククラッシュ (`*(int*)0=0;`) とは別件。あれは録音側

### 自動起動タスクが勝手に無効になっていることがある (2026-08-21 に3回)

`Tachikoma Gateway` ほか全タスクが `Enabled=false` になっていた。**この状態で
PCを再起動すると何も起動しない。**`schtasks /change /enable` は成功と出ても
効かないことがあるので、`Enable-ScheduledTask` を使う。原因は未特定。

```powershell
foreach ($t in 'Tachikoma Gateway','VOICEVOX Engine (Tachikoma)',
               'Even Terminal (Tachikoma)','Even Terminal Codex (Tachikoma)',
               'Tachikoma Order Agent') { Enable-ScheduledTask -TaskName $t }
```

| 罠 | 内容 |
|---|---|
| `pdMS_TO_TICKS(N)` | `CONFIG_FREERTOS_HZ=100` (10ms tick) では `pdMS_TO_TICKS(5)` が整数除算で **0** になり、`vTaskDelay(0)` = yield のみになる。優先度8・core0固定でidleタスクを枯渇させ、10秒でウォッチドッグを踏んだ |
| PowerShell の `&&` | **Windows PowerShell 5.1 には `&&` が無い**。`cd X && cmd` はパーサーエラーになる。ユーザーに渡すコマンドは1行ずつ分けるか `;` を使う。(これを承知していながら実際に渡してしまい、push が2回失敗した) |
| PowerShell の .ps1 | BOM無しUTF-8で書いた .ps1 は PowerShell 5.1 が **Shift-JIS として読む**。日本語リテラルが化ける。テストスクリプトはASCIIで書くか、BOM付きで保存する |
| git の HTTPS | pip と同じく VPN のルート証明書が無く `SSL certificate ... unable to get local issuer certificate` で落ちる。**検証を無効化しない**こと。`C:\Users\mylit\.certs\winroots.pem` に Windows のルートストアを書き出し、`git config --global http.sslCAInfo` で参照させて解決済み |
| PowerShell の Set-Content | `-Encoding utf8` は **BOM付き**で書く。Python の `open(..., encoding="utf-8")` が1文字目で `JSONDecodeError` になる。設定ファイルは `utf-8-sig` で読むこと |
| pyserial で COM3 を開く | DTR/RTS がアサートされ **ESP32 が再起動する**。`serial.Serial()` を未オープンで作り `dtr=False`/`rts=False` にしてから `.open()` する |
| `GATEWAY_HOST` の既定値 | `server.py` の既定は `127.0.0.1` で、そのまま起動すると**デバイスから繋がらない**。`run_gateway.ps1` が `0.0.0.0` を強制する |
| `ESP_ERROR_CHECK` | コーデックのI2C失敗で即 `abort()` → 再起動していた。一時的な失敗は致命的扱いしない |

---

### コードを入れ替えたら、プロセスを名指しで殺してから上げ直す (2026-08-22)

`self_check.ps1 -Repair` は**死んでいるサービスしか触らない**。健康に動いている
古いコードのプロセスは、監視から見れば正常なので入れ替わらない。
`Stop-ScheduledTask` もラッパーしか殺さないので、python は生き残る。

    $p = (Get-NetTCPConnection -LocalPort 8080 -State Listen).OwningProcess | Select -First 1
    Stop-Process -Id $p -Force
    Start-ScheduledTask -TaskName "Tachikoma Gateway"

入れ替わったかは版で確かめる（推測しない）:

    curl http://127.0.0.1:8080/health    # {"ok":true,"version":"0.9.0",...}
    curl http://127.0.0.1:8766/health

### ゲートウェイが「起動しているのに何も答えない」ときは、二重起動を疑う (2026-08-22)

症状: `/health` すら応答なし（HTTPエラーではなく**接続が即切れる**）。
ログには "listening on 0.0.0.0:8080" と出ていて、一見正常に起動している。
実機は無言、self_check も [DEAD] を出す。

原因: `Stop-ScheduledTask` は**PowerShellのラッパーしか殺さず、その下の
python は生き残る**。次に起動したプロセスは Windows の SO_REUSEADDR で
同じポートに bind できてしまい、OSが接続を古い方に配る。古い方は親シェルが
死んで標準出力が壊れているので、リクエストを受けるたびログ出力で例外→接続切断。

    Get-NetTCPConnection -LocalPort 8080 -State Listen   # 掴んでいるPIDを見る
    Get-Process -Id <PID>                                # StartTime で新旧を判定
    Stop-Process -Id <古いPID> -Force

対策済み: ゲートウェイに二重起動ガードを入れた（2つ目は起動を拒否して理由を
言う）。self_check.ps1 -Repair も、再起動前にポートを掴んだプロセスを
強制終了するようにした。**タスクを止めただけで安心しないこと。**

## 3. デバイスが無言のとき

**第一手は健康診断**。どのサービスが死んでいるか、原因込みで1画面に出る:

```powershell
powershell -File self_check.ps1          # 診断
powershell -File self_check.ps1 -Repair  # 死んでいたら起こす
```

普段は scheduled task「Tachikoma Health Check」が5分毎にこれを -Repair 付きで
回している（ログ: StackChanDev\logs\health.log）。つまり無言が5分以上続く時点で
自動復旧も失敗している = 下の手動調査へ。

次に**ゲートウェイが起動しているか**を疑う。デバイス側は正常でも、返答生成側が
落ちていれば何も喋らない。デバイスログの症状:

```
esp-tls: delayed connect error: Connection reset by peer
HTTP_CLIENT: Connection failed, sock < 0
[SpeechAnnouncer] speech queue poll failed
TachikomaState: Thinking -> Error by AiRequestFailed
```

起動コマンド:

```powershell
powershell -File firmware\gateway\run_gateway.ps1
```
