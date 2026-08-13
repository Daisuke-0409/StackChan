# タチコマ

M5Stack の StackChan (CoreS3 / ESP32-S3) をフォークし、頭脳を自前の AI ゲートウェイに
差し替えた音声対話ロボット。ダイスケ専用。

**この README だけで、環境構築から日々の運用まで足ります。**
設計の理由と現在地は [CLAUDE.md](CLAUDE.md) にあります。

**人格は1つ、入口は複数。** 家と会社に体が1台ずつあり、頭（記憶と判断）は家の PC に
1つだけあります。どちらの体で話しても同じ記憶になります。

このリポジトリは3つの部分でできています。

| 場所 | 中身 |
|---|---|
| `firmware/` | 実機のファーム（ESP32）と、**Tachikoma Gateway**（頭） |
| `notifier/` | **PC側の通知・承認基盤**。Claude Code などの状態を声で知らせる |
| `server/` | 自前サーバ（公式アプリの代替。ブラウザから映像・操作） |

---

## 1. 環境構築

まっさらな PC で動かすまで。**上から順に**。

### 1.1 用意するもの

| | |
|---|---|
| Python 3.11 | ゲートウェイと notifier 用 |
| Git | |
| 実機 | M5Stack CoreS3 + StackChan ボディ |
| Google AI Studio の API キー | Gemini の chat / TTS / STT で共用 |
| VOICEVOX ENGINE | 任意。入れると声が速くなる（無くても動く） |
| ESP-IDF v5.5.4 | **実機に焼くときだけ**必要 |

### 1.2 取ってくる

```powershell
git clone -b phase4-ai-gateway https://github.com/Daisuke-0409/StackChan.git
cd StackChan
```

### 1.3 git や pip が SSL で落ちるとき

会社のネットワークがプロキシで TLS を終端していると、`CERTIFICATE_VERIFY_FAILED` や
`unable to get local issuer certificate` で全滅します。家でも起きました。
**検証を切らずに**直します。

```powershell
$dir = "$env:USERPROFILE\.certs"; New-Item -ItemType Directory -Force $dir | Out-Null
$pem = "$dir\winroots.pem"; $out = New-Object System.Text.StringBuilder
foreach ($s in @("Cert:\LocalMachine\Root","Cert:\CurrentUser\Root","Cert:\LocalMachine\CA")) {
  Get-ChildItem $s -EA SilentlyContinue | ForEach-Object {
    $b = [Convert]::ToBase64String($_.RawData,'InsertLineBreaks')
    [void]$out.AppendLine("-----BEGIN CERTIFICATE-----"); [void]$out.AppendLine($b)
    [void]$out.AppendLine("-----END CERTIFICATE-----") } }
Set-Content $pem $out.ToString() -Encoding ascii
$env:PIP_CERT = $pem
git config --global http.sslCAInfo $pem
```

`PIP_CERT` は環境変数なので、pip が内部で起動する pip にも伝わります。
`--cert` だけでは伝わらず、ビルド依存の取得で落ちます。

### 1.4 PC 側の依存

```powershell
python -m pip install torch librosa opencv-python numpy scipy
```

声紋（話者識別）と顔認識に使います。ゲートウェイ本体は Python 標準ライブラリだけで
動きますが、これらが無いと話者識別が使えません。

### 1.5 ゲートウェイの設定を作る

```powershell
copy firmware\gateway\.env.example firmware\gateway\.env
```

できた `.env` を埋めます。**このファイルは git に入りません。**

```
DEVICE_TOKEN=<実機に焼くのと同じ任意の文字列>
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8080

AI_PROVIDER=gemini
AI_PROVIDER_API_KEY=<Google AI Studio のキー>
AI_PROVIDER_TIMEOUT_SECONDS=30

STT_PROVIDER=gemini
STT_VOCABULARY=大輔

TTS_PROVIDER=voicevox
VOICEVOX_URL=http://127.0.0.1:50021
VOICEVOX_SPEAKER=3

ENABLE_WEB_SEARCH=1
TACHIKOMA_DEBUG_LOGGING=0
ALLOW_INSECURE_DEV=0
```

- `GATEWAY_HOST` は **`0.0.0.0`**。既定の `127.0.0.1` のままだと実機から繋がりません
- `DEVICE_TOKEN` は実機に焼く値と一致していれば何でもよい。
  **家と会社で分けたほうがよい**（片方が漏れても、もう片方に波及しない）
- `STT_VOCABULARY` は音から推測できない固有名詞。無いと「ダイスケ」が「大助」になります

### 1.6 モデルを取ってくる（54MB）

```powershell
python firmware\docs\fetch_models.py
```

`firmware/gateway/models/` に入ります。git には乗っていません。

### 1.7 VOICEVOX（任意）

入れて起動しておくと、1文あたり約 755ms で喋ります（Gemini TTS は約 2667ms）。
**起動していなければ自動で Gemini TTS に切り替わる**ので、無くても止まりません。
既定はポート 50021。

### 1.8 ESP-IDF（実機に焼くときだけ）

v5.5.4 が必要です。このPCは VS Code 拡張のレイアウトで入っているため、
素の `export.ps1` は失敗します。**この4行**で通ります。

```powershell
$env:IDF_TOOLS_PATH='C:\Espressif'
$env:IDF_PYTHON_ENV_PATH='C:\Espressif\tools\python\v5.5.4\venv'
$env:IDF_PYTHON_CHECK_CONSTRAINTS='no'
. C:\esp\v5.5.4\esp-idf\export.ps1
```

インストールし直す必要はありません。壊れているのではなく、置き場所が違うだけです。

### 1.9 コーデックのクラッシュ修正を当てる（実機を焼くなら必須）

`managed_components/` は ESP-IDF のコンポーネントマネージャが管理していて
git には入りません。そこに**「エラーが起きたら意図的にクラッシュする」1行**があり、
当たると**会話の直後に実機が再起動します**（1回目の会話は成功し、2回目以降が
全部おかしくなる、という形で出ます）。

ビルドの前に一度実行してください。**すでに当たっていれば何もしません。**

```powershell
python firmware\patches\apply_codec_dev_fix.py
```

コンポーネントを取り直した後（`dependencies.lock` が変わった、
`managed_components/` を消した等）は、**もう一度実行してから焼くこと。**

### 1.10 PC側の notifier（使うなら）

追加の依存はありません。テストで動作確認できます。

```powershell
$env:PYTHONPATH="$PWD\notifier"
python -m unittest discover -s notifier\tachikoma_notifier\tests -t notifier\tachikoma_notifier\tests -p "test_*.py"
```

268 件通れば OK です。

### 1.11 別拠点の転送役（会社の PC だけ）

会社の体を家の頭に繋ぐ中継です。**API キーもトークンも要りません**（運ぶだけで
中を見ないため）。

```powershell
copy firmware\gateway\.env.forwarder.example firmware\gateway\.env.forwarder
```

`.env.forwarder` の `FORWARDER_TARGET` に、**家の PC の Tailscale アドレス**を書きます。
家の PC で `tailscale ip -4` を実行すると分かります。

---

## 2. 動かす

### 起動

```powershell
powershell -File firmware\gateway\run_gateway.ps1
```

起動時に出るものを2つ確認してください。

- **`LAN addresses: ...`** — この一覧に、実機に焼いてあるアドレスが含まれていること。
  含まれていなければ実機は繋がりません（PC の IP が変わったということ）
- **`gateway memory store: tachikoma.json`** — どの記憶を使っているか。
  `(new, nothing remembered yet)` と付いていたら空から始まっています

前の記憶を引き継いだときは、その直前に一度だけこう出ます。

```
gateway memory: adopted 80456B4DE03C.json as the shared store
```

### 話しかける

頭を**押して、離す**。押している間が録音（最大30秒）。離すと返事が返ります。
発話終了から最初の音声まで約 3.7 秒。会話は 30 秒間続くので、その間は頭に触らず
続けて話せます。

### 秘密にしたいこと

「ここだけの話」と言ってから話してください。**直前の会話にも遡って**秘密指定されます。
秘密は、権限の低い相手が聞いているときは**そもそもモデルに渡りません**。

### 終了

ゲートウェイのウィンドウで `Ctrl+C`。実機は放っておいて構いません。

---

## 3. 実機に書き込む

**バックアップを取らずに焼かないこと。** 順番は必ずこれ。

### 3.1 バックアップ（約7分）

```powershell
$stamp = Get-Date -Format 'yyyyMMdd_HHmm'
& 'C:\Espressif\tools\python\v5.5.4\venv\Scripts\esptool.exe' -p COM3 -b 460800 read_flash 0 0x1000000 "..\backups\stackchan_backup_${stamp}_目的.bin"
```

できたファイルが **16,777,216 バイト**ちょうどであることを確認してください。
違ったら焼きません。921600 baud は失敗した実績があるので 460800 を使います。

### 3.2 ビルドと書き込み

1.8 の4行で ESP-IDF を通してから:

```powershell
idf.py -C firmware build
idf.py -C firmware -p COM3 -b 460800 flash
```

**`Hash of data verified.`** が出れば成功です。

### 3.3 ログを見る

**COM3 を普通に開くと ESP32 が再起動します**（DTR/RTS がリセット線に繋がっている）。
測っている対象を再起動させないよう、`dtr=False` / `rts=False` を設定してから開きます。

---

## 4. 会社の体を繋ぐ

会社の PC では**転送役だけ**を起動します。**会社でゲートウェイを起動してはいけません**
（記憶が枝分かれします）。

```powershell
powershell -File firmware\gateway\run_forwarder.ps1
```

ブラウザで `http://localhost:8080/healthz` を開き、`{"ok":true}` が出れば動いています。

実機の向き先（焼いてある URL）は**変えなくて構いません**。今までどおり会社 PC を
指していれば、転送役がその先を引き受けます。

**会社の PC が起動している間だけ、会社の体が喋ります。** 営業時間外は接続失敗を
ログに出して黙るだけで、壊れてはいません。

---

## 5. 記憶

### どこにあるか

```
firmware/gateway/memory/tachikoma.json   会話20往復 + 恒久的な事実
firmware/gateway/memory/people.json      登録済みの人（声紋・顔・役割）
firmware/gateway/memory/settings.json    設定
```

**家の PC にしかありません。** git には入りません。実名や居住地が入っているので、
コピーを置く場所には気をつけてください。

### 忘れさせる

`tachikoma.json` を削除します。次の起動で空から始まります。
特定の人の登録を消すには Web UI から削除します（声紋と顔ごと消えます）。

### 分かれてしまった記憶を統合する

**持ち帰ったファイルを `memory/` の中に入れないでください。** 記憶ファイルが2つある
状態になると、引き継ぎが自動で止まります（どちらが古いか機械には分からないため、
わざとそう作ってあります）。デスクトップなど、外に置いてから実行します。

```powershell
cd firmware
$office = "C:\Users\mylit\OneDrive\Desktop\会社の記憶"
python -m gateway.merge_memory memory gateway\memory\tachikoma.json "$office\80456B4DE7AC.json"
```

まず確認だけで、何も書き換わりません。「両方に会話がある。順番を選べ」と言われるので、
納得したら末尾に `--turns home-first --write` を足して再実行します。

人の登録も同じように統合します。

```powershell
python -m gateway.merge_memory people gateway\memory\people.json "$office\people.json"
```

**同じ名前が2回登録されている**と警告が出たら正常です。登録 ID はランダムなので、
同じ人でも家と会社では別人として登録されています。`--fold-same-name --write` で
1人にまとめてください。まとめないと声紋が2つに分かれて認識率が落ちます。

---

## 6. うまくいかないとき

| 症状 | まず疑うこと |
|---|---|
| **1回目は話せるが2回目以降が無反応・異様に遅い** | **実機がクラッシュして再起動している。** シリアルログに `Guru Meditation Error` と `rst:0xc` が出ていないか見る。1.9 の修正が当たっているか確認する。**ログを絞り込むとき `boot:` 行を捨てないこと** — 2週間これで見落とした |
| 体が何も喋らない | **ゲートウェイが起動しているか。** デバイスログに `Connection reset by peer` / `speech queue poll failed` が出ていればこれ |
| 401 が返る | `.env` の `DEVICE_TOKEN` と、実機に焼いた `TACHIKOMA_DEVICE_TOKEN` の不一致 |
| 会社の体だけ喋らない | 会社 PC の転送役（`/healthz`）→ 家の PC → Tailscale のログイン、の順に確認 |
| 触っていないのに喋り出す | 首を振っていないか。振っているなら `kFaceTrackingMovesHead` が `true` に戻っている |
| `adopted` のログが出ない | すでに `tachikoma.json` がある（2回目以降は出ない）か、`memory/` に記憶ファイルが2つある |
| 統合が「何もしない」と言う | 記憶ファイルが2つ以上あるとき、わざと何もしません。片方を外に出して再実行 |
| 2回続けて録音すると2回目が失敗 | **既知の未解決問題。** 再現性未確認 |
| USB が落ちる | 充電専用ケーブルを使っていないか |
| `python` の依存が無いと言われる | ESP-IDF 同梱の Python に解決されている。venv を PATH の先頭に置く |

---

## 7. 環境固有の落とし穴（実際に踏んだもの）

| 罠 | 内容 |
|---|---|
| `pdMS_TO_TICKS(N)` | tick 10ms の環境では `pdMS_TO_TICKS(5)` が整数除算で **0** になり、ビジーループ化してウォッチドッグを踏む |
| PowerShell の `&&` | Windows PowerShell 5.1 には**存在しない**。パーサーエラーになる |
| BOM 無し UTF-8 の `.ps1` | PowerShell 5.1 が Shift-JIS として読み、日本語が化ける |
| COM3 を開く | DTR/RTS がアサートされ **ESP32 が再起動する** |
| `Set-Content -Encoding utf8` | BOM 付きで書かれ、Python が `JSONDecodeError` になる。`utf-8-sig` で読むこと |
| `GATEWAY_HOST` の既定値 | `127.0.0.1` のままだとデバイスから繋がらない |
| `ESP_ERROR_CHECK` | コーデックの I2C 失敗で即 `abort()` → 再起動していた。一時的な失敗は致命的扱いしない |

---

## 8. やってはいけないこと

- **バックアップを取らずに実機を焼く。** 16MB ちょうどを確認してから
- **会社の PC でゲートウェイを起動する。** 記憶が枝分かれする
- **顔追従の首振りを戻す。** タッチセンサで「押された」と「揺れた」を区別できる
  ようになるまで。人が触っていないのに喋ってはいけない
- **NAS の共有フォルダに対してゲートウェイを2台走らせる。** ファイル置き換えの
  原子性が SMB 越しでは保証されず、片方の記憶が黙って消える。複数必要なら DB 化が先
- **git の SSL 検証を無効化する。** 1.3 の方法で直すこと
- **API キー・トークン・会話内容をログやコミットに出す**

---

## 9. もっと詳しく

| 見るもの | どこ |
|---|---|
| 設計の理由・現在地・決めごと | [CLAUDE.md](CLAUDE.md) |
| ゲートウェイの機能別の説明 | [firmware/gateway/README.md](firmware/gateway/README.md) |
| ファーム側の運用・既知バグ | [firmware/README.md](firmware/README.md) |
| 会社セッションの記録 | [firmware/docs/2026-07-28-office-session.md](firmware/docs/2026-07-28-office-session.md) |
| 会社環境の構築手順 | [firmware/docs/office-setup.md](firmware/docs/office-setup.md) |
| PC側 notifier | [notifier/tachikoma_notifier/README.md](notifier/tachikoma_notifier/README.md) |
| 自前サーバ | [server/README.MD](server/README.MD) |

---

このリポジトリは [m5stack/StackChan](https://github.com/m5stack/StackChan) のフォークです。
`firmware/xiaozhi-esp32`、`app/`、`remote/`、`server/` の大部分は上流のものです。
上流のカタログ的な README は git 履歴に残っています（`git show 6002683:README.md`）。
