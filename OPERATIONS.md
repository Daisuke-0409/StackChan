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

### 手で打つコマンドには `-ExecutionPolicy Bypass` を必ず付ける (2026-08-24)

Windows の実行ポリシーは**どのスコープでも未定義 = Restricted**。
**禁止例** `powershell -File whatever.ps1` — 素の端末に打つと1行目に届く前に死ぬ:

```
このシステムではスクリプトの実行が無効になっているため、
ファイル ... を読み込むことができません
```

**機械は壊れていない。壊れていたのは手順書のほうだけ**で、しかも
**唯一の読み手 (大輔) にしか起きない**。自動で走るものはこの扉を通らない —
自動起動タスクは元から全部 `-ExecutionPolicy Bypass` を渡しているし、
エージェントのシェルはプロセススコープで Bypass を継いでいる。
だから数週間、誰も気づけなかった。

書いてある49箇所は直した。**50箇所目を防ぐのは `tests_commands.py`** で、
Bypass の無い `powershell ... -File` が文書に現れたら repo スイートが落ちる。

**ポリシー自体は変えない。**それは人のPCのセキュリティ設定で、
11文字を惜しんでプロジェクトが黙って緩めていいものではない。

### 会社と家に同じ検査表を当てない (2026-08-24)

`self_check.ps1` は家の台帳しか持っておらず、会社で走らせると嘘をついた。
8080 を握っているのが**転送役**なのに「Gateway OK」と読み、家の固定IP
`192.168.2.120` が無いことを [DEAD] と言った。

診断が誤るだけなら実害は小さい。**危ないのは `-Repair` のほう**で、
修理側は「Disabled なタスクは監査C5の再発」とみなして有効化する。
会社の `Tachikoma Gateway` が Disabled なのは**故障ではなく意図**
(頭は家に1つ)。会社でこれが走ると**頭が2つになり、記憶が黙って枝分かれする**。

事故になっていなかったのは、転送役がたまたま 8080 を握っていて
「Gateway OK」と誤診されていたからで、**転送役が落ちた瞬間に発動する**
状態だった。IP で拠点を見て台帳を切り替え、会社では
`Tachikoma Gateway` を**起こさず、有効なら無効に戻す**ようにした。

### サービスのログは PowerShell の `*>>` に書かせない (2026-08-24)

`forwarder.log` が **69MB** まで育ち、しかも **grep も tail も効かない**状態だった。
2つの原因が重なっていた。

**1つ目: 例外のスタックトレース。**機体は返事が遅いと接続を切って掛け直す。
これは正常な動作で、`_respond` は最初から「機体が諦めるのは日常」として扱って
いた。ところが**扱っていたのは応答を書く側だけ**で、受け取る側で切られた分は
Python の HTTP サーバが既定のトレースを吐いていた。1回につき10行、**ログの
62%**。読みたい行がその下に埋まる。

**2つ目: エンコードの混在。**PowerShell 5.1 の `*>>` は **UTF-16 で書く**。
先頭は別のものが UTF-8 で書いていたので、1つのファイルに2つのエンコードが入り、
どちらの読み手も通しで読めなくなっていた。

```
 C o n n e c t i o n R e s e t E r r o r :   [ W i n E r r o r   1 0 0 5 4 ]
```

**直し方**: `gateway/logfile.py` がサービス自身にログを書かせる
(`TACHIKOMA_LOG_FILE` を `.env.forwarder` / `.env.crm_relay` に置く)。
BOM付き UTF-8・サイズで切り替え。起動スクリプトの銘板も同じファイルへ入れる
ので、**「体が黙った」は1つのファイルを grep すれば足りる**。
接続切断は1行に数えて `dropped=N` として要約に出す — 握り潰すのではなく数える。
急に増えたら、それは機体が毎回諦めている形。

### ウイルス対策に引っかかる書き方をしない (2026-08-24)

上の作業中、Avast が `run_crm_relay.ps1` を **IDP.Generic** として**遮断した**
(挙動監視シールド)。誤検知だが、引っかかったのはこちらの書き方のほう。

| やった書き方 | なぜ怪しく見えるか |
|---|---|
| `[System.IO.File]::AppendAllText(...)` | スクリプトが .NET を直に呼んでファイルを書く = ドロッパーの定型 |
| **禁止例** タスクで `$env:X='...'; & powershell -File ...` | PowerShell が環境変数を仕込んで PowerShell を入れ子で起動する = 定番の起動手口 |

**除外設定を足して済ませないこと。**ウイルス対策を黙らせる方向に倒すと、次に
本物が来たときに気づけない。**書き方のほうを変える**: ファイル追記は
`Add-Content -Encoding UTF8`、環境変数は `.env.*` に置いてタスクの起動行は
元の形のまま保つ。

**教訓**: 常駐サービスの起動スクリプトは、何週間も見逃されてきた形から
**不用意に離れない**。変えるなら、変えた直後に警告が出ないことを確かめる。

### CRM を引く検証は、監査ログに残ることを承知してやる (2026-08-24)

CRM は照会1回につき audit_log に1行残す (追記のみ・削除する API は無い)。
これは正しい設計で、**点検のつもりの照会も等しく残る**。

だから `check_crm_live.py` は `asked_by` に人の名前ではなく
**`acceptance-check`** を渡す。契約上、登録済み担当者名に一致しない値は
拒否されずに通る。あとで台帳を見た人が「これは人の照会ではなく点検だ」と
分かるようにするため。**担当者の名前を騙って点検しないこと。**

同じ理由で、この検証は**値を1つも表示しない**。件数と項目名と、
組み立てた文が満たすべき性質だけを見る。R6 が問うているのは「何が返るか」
であって「誰が返るか」ではないので、それで足りる。

### 機体ごとに、焼かれているファームの日付が違う (2026-08-24)

会社の体で連続会話ができなかった。**家では 2026-08-15 に直してクローズした症状**
がそのまま残っていた。原因は再発ではなく、**焼き直していなかったこと**。

```
会社の機体を焼いたビルド : firmware/build/stack-chan.bin  7/28 13:54

入っていなかった修正:
  abd92fc  7/30  ハンズフリー時に状態機械へ発話開始を伝える  ← これが症状の本体
  7a2b9a0  7/30  自分の声を拾わない
  9393838  8/13  発話の終わりを検出できるようにし、閾値を部屋より上に
  c7b3cd9  8/15  会話の開始を Idle 限定に
  (コーデックのクラッシュ修正も未適用だった)
```

**症状からファームの古さを見分けられる**: ハンズフリーの発話が
`transcribe -> 200` まで行くのに `chat` が続かず、その前後に `vision` が無い。
vision は会話中しか送られないので、**状態機械が会話に入っていない**証拠になる。
機体は録音も書き起こしも成功させたうえで、返ってきた文を自分で捨てている。

**教訓**: 「直した」はゲートウェイなら全機体に一度に効くが、**ファームは効かない**。
実機側の修正を入れたら、**どの機体がどの版か**を意識すること。片方だけ直っている
状態は、同じ症状を2度調べさせる。

**焼き直しで消えないもの**: NVS (0x9000) は `idf.py flash` の対象外なので
WiFi 設定や機体の設定は残る。パーティション表が変わっていないことは
焼く前に確認すること (変わっていると NVS の位置がずれる)。

### 実機のログは `capture_serial.py` で読む (2026-08-24)

4つの文書が `capture_boot.py` を参照していたが、**そのファイルは存在しなかった**
(前のセッションの使い捨てで、コミットされていなかった)。実機側の不具合を追う
唯一の手段がこれなので、`firmware/tools/capture_serial.py` として作り直した。

```powershell
C:\Users\user\.espressif\python_env\idf5.5_py3.11_env\Scripts\python.exe `
  firmware/tools/capture_serial.py --seconds 600 --out logs/probe.log
```

**COM を普通に開くと実機が再起動する** (DTR/RTS がリセット線に繋がっている)。
未オープンで作って `dtr`/`rts` を False にしてから開くこと。これがこの道具の
存在理由で、それ以外は薄い。書きながら流すので、以前の版に必要だった
「実行中は0バイトに見えるのが正常」という注意書きは要らなくなった。

pyserial は ESP-IDF の venv に esptool の依存として入っている。**何も入れなくていい。**

**読み方**: `TachikomaState:` の行が状態機械の遷移。会話が成立しない理由は
たいていここに書いてある。ゲートウェイ側のログと合わせると、どちら側で
落ちたかが1分で分かる。

### 「短すぎる録音」は失敗ではない (2026-08-24)

`StopRecordingAndUpload` は録音が `kMinRecordingMs` (300ms) に満たないとき
`AiRequestFailed` を撃っていた。**要求は1つも出していないのに**、状態機械は
Error に落ち、`error_ms` = 4000 のあいだ**何も聞けなくなる**。

物音や話し始めの一瞬で空振りの録音が走ると、その直後に人が話した言葉は
**まるごとその4秒に入って消える**。

同じファイルの `FollowUpTick` は同じ場面で `SpeechFinished` を撃っていて、
理由もこう書いてあった — *「AiRequestFailed でも動くが Error を経由する。
咳ひとつには大げさすぎる」*。**片方にだけ、その判断が適用されていなかった。**

直し方は順番の入れ替え: 送るものが無いときは `UserSpeechEnded` を撃たない
(それは `Thinking` へ行く事象で、**`Thinking` からは返事かタイムアウトでしか
出られない**)。代わりに `SpeechFinished` で `Listening` から `Idle` へ戻る。

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
powershell -ExecutionPolicy Bypass -File self_check.ps1          # 診断
powershell -ExecutionPolicy Bypass -File self_check.ps1 -Repair  # 死んでいたら起こす
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
powershell -ExecutionPolicy Bypass -File firmware\gateway\run_gateway.ps1
```
