# タチコマ運用マニュアル

**ダイスケがやること**だけを書いたもの。仕組みの説明は `CLAUDE.md` と
`gateway/README.md` にある。ここは手順書。

---

## 0. 全体の形

```
        家                                    会社
  ┌──────────────┐                    ┌──────────────┐
  │ 家のPC        │                    │ 会社のPC      │
  │  ゲートウェイ  │ ←── Tailscale ──── │  転送役       │
  │  = 頭 (1つ)   │                    │  (中継のみ)   │
  │  記憶もここ    │                    └──────┬───────┘
  └──────┬───────┘                           │ 会社LAN
         │ 家LAN                              │
    家の体 (80:45:6B:4D:E0:3C)          会社の体 (80:45:6B:4D:E7:AC)
```

**頭は家に1つだけ。** 会社の体は会社PCを経由して家の頭に話しかける。
記憶は家のPCに1ファイルだけあり、どちらの体で話しても同じ記憶になる。

**会社PCが起動していない間、会社の体は喋らない。** これは仕様。営業時間外は
接続失敗をログに出して黙るだけで、壊れてはいない。

---

## 1. 家で使う（毎回）

### 起動

```powershell
powershell -File C:\Users\mylit\StackChanDev\StackChan\firmware\gateway\run_gateway.ps1
```

起動時に `LAN addresses: ...` が出る。**この一覧に、実機に焼いてあるアドレスが
含まれていること**を確認する。含まれていなければ実機は繋がらない（PCのIPが
変わったということ。5章の手順で焼き直すか、ルータでIPを固定する）。

VOICEVOX を先に起動しておくと声がずんだもんになり、応答も速い（1文あたり約755ms）。
起動していなければ自動的に Gemini TTS に切り替わる。止まりはしない。

### 話しかける

頭を**押して、離す**。押している間が録音（最大30秒）。離すと返事が返る。
発話終了から最初の音声まで約3.7秒。

会話は30秒間続く（`kFollowUpWindowMs`）。その間は頭を触らずに続けて話せる。

### 秘密にしたいこと

「ここだけの話」と言ってから話す。**直前の会話にも遡って**秘密指定される。
秘密にした内容は、権限の低い相手が聞いているときはモデルに渡されない。
「言うな」と指示しているのではなく、そもそも渡していない。

### 終了

ゲートウェイのウィンドウで `Ctrl+C`。実機は放っておいてよい。

---

## 2. 会社で使う（毎回）

会社PCで**転送役だけ**を起動する。会社PCにゲートウェイは要らない（起動しては
いけない。3章参照）。

```powershell
powershell -File C:\Users\user\...\firmware\gateway\run_forwarder.ps1
```

起動時に Tailscale の状態が出る。`not logged in` と出たら:

```powershell
& "C:\Program Files\Tailscale\tailscale.exe" up
```

表示されたURLをブラウザで開いてログインする。

### 転送役が生きているかの確認

会社PCのブラウザで `http://localhost:8080/healthz` を開く。`{"ok":true}` が
出れば転送役は動いている。出るのに体が喋らないなら、原因は家側（家のPCか
ゲートウェイが落ちている）。

---

## 3. 記憶

### どこにあるか

```
firmware/gateway/memory/tachikoma.json   会話20往復 + 恒久的な事実
firmware/gateway/memory/people.json      登録済みの人（声紋・顔・役割）
firmware/gateway/memory/settings.json    設定
```

**家のPCにしかない。** gitには入らない。実名や居住地が入っているので、
コピーを置く場所には気をつける。

### 忘れさせる

`tachikoma.json` を削除する。次の起動で空から始まる。
特定の人の登録を消すには Web UI から削除する（声紋と顔ごと消える）。

### 会社PCでゲートウェイを起動してはいけない

起動すると会社PCが**独自の記憶を書き始め**、家の記憶と枝分かれする。
一度分かれた2つの会話履歴は、時刻が記録されていないため機械的には統合できない。
会社PCのタスクスケジューラにある `Tachikoma Gateway` は**無効にしておくこと**。

### それでも分かれてしまったら

```powershell
# 家の記憶に、会社から持ち帰った記憶を統合する（まず確認だけ）
Set-Location C:\Users\mylit\StackChanDev\StackChan\firmware
python -m gateway.merge_memory memory gateway\memory\tachikoma.json <持ち帰ったファイル>

# 表示を読んで納得したら、順序を指定して書き込む
python -m gateway.merge_memory memory gateway\memory\tachikoma.json <持ち帰ったファイル> --turns home-first --write
```

`--write` を付けない限り何も書き換わらない。書き込む前に元ファイルの控えを
`.before-merge` として残す。

登録済みの人も同じように統合する:

```powershell
python -m gateway.merge_memory people gateway\memory\people.json <持ち帰ったpeople.json>
```

**同じ人が両方で登録されていると警告が出る。** 登録IDはランダムなので、同じ人でも
別人として登録される。そのままだと声紋が2つに分かれて認識率が落ちる。
`--fold-same-name` を付けると1人にまとめる。

---

## 4. 実機に書き込む

**バックアップを取らずに焼かない。** 順番は必ずこれ。

### 4.1 バックアップ（約7分）

```powershell
$stamp = Get-Date -Format 'yyyyMMdd_HHmm'
& 'C:\Espressif\tools\python\v5.5.4\venv\Scripts\esptool.exe' -p COM3 -b 460800 read_flash 0 0x1000000 "C:\Users\mylit\StackChanDev\backups\stackchan_backup_${stamp}_<目的>.bin"
```

できたファイルが **16,777,216 バイト** ちょうどであることを確認する。
違ったら焼かない。921600 baud は失敗した実績があるので 460800 を使う。

### 4.2 ビルド環境の準備

このPCは ESP-IDF が VS Code 拡張のレイアウトで入っているため、素の
`export.ps1` は失敗する。この3行を先に実行する:

```powershell
$env:IDF_TOOLS_PATH='C:\Espressif'
$env:IDF_PYTHON_ENV_PATH='C:\Espressif\tools\python\v5.5.4\venv'
$env:IDF_PYTHON_CHECK_CONSTRAINTS='no'
. C:\esp\v5.5.4\esp-idf\export.ps1
```

### 4.3 ビルドと書き込み

```powershell
idf.py -C C:\Users\mylit\StackChanDev\StackChan\firmware build
idf.py -C C:\Users\mylit\StackChanDev\StackChan\firmware -p COM3 -b 460800 flash
```

`Hash of data verified.` が出れば成功。

### 4.4 起動確認

`idf.py monitor` は使わなくてよい。ログだけ見たいときは、**COM3 を普通に開くと
ESP32 が再起動する**（DTR/RTS がリセット線に繋がっている）ので、
`dtr=False` / `rts=False` を設定してから開くこと。

---

## 5. 症状別の対処

| 症状 | まず疑うこと |
|---|---|
| 体が何も喋らない | **ゲートウェイが起動しているか。** デバイスログに `Connection reset by peer` / `speech queue poll failed` が出ていればこれ |
| 401 が返る | `.env` の `DEVICE_TOKEN` と、実機に焼いた `TACHIKOMA_DEVICE_TOKEN` が一致していない |
| 会社の体だけ喋らない | 会社PCの転送役が動いているか（`/healthz`）→ 家のPCが起動しているか → Tailscale がログイン済みか |
| 触っていないのに喋り出す | 首を振っていないか確認。振っているなら `kFaceTrackingMovesHead` が `true` に戻っている |
| 2回続けて録音すると2回目が失敗 | 既知の未解決問題。再現性未確認 |
| USB が落ちる | 充電専用ケーブルを使っていないか。データ通信対応のものに替える |
| ゲートウェイが `python` を見つけられない | ESP-IDF 同梱の Python に解決されている。torch/librosa/opencv が入っていない環境なので、venv を PATH の先頭に置く |

---

## 6. やってはいけないこと

- **バックアップ無しで焼く。**
- **NASの共有フォルダに記憶を置いて、ゲートウェイを2台走らせる。**
  ファイルの置き換えの原子性がSMB越しでは保証されず、片方の記憶が黙って消える。
  複数のゲートウェイが必要になったらDB化が先（Phase 8）。
- **会社PCでゲートウェイを起動する。** 記憶が枝分かれする。
- **`kFaceTrackingMovesHead` を `true` に戻す。** タッチセンサで押下と振動を
  区別できるようになるまでは戻さない。電話中に勝手に話しかけてくる実害が出た。
- **APIキーやトークンをログ・音声・コミットに出す。**
- **git の SSL 検証を無効化する。** 証明書は `C:\Users\mylit\.certs\winroots.pem`
  を参照させて解決済み。

---

## 7. 次に会社へ行ったときにやること（今回限り）

営業時間しか入れないので、この順で。所要15分程度。

1. **タスクスケジューラの `Tachikoma Gateway` を無効化し、実行中なら止める**
2. **記憶を持ち帰る** — `firmware/gateway/memory/` から2ファイル:
   - `80456B4DE7AC.json`（会社の体の記憶）
   - `people.json`（会社で登録した人）
3. **転送役を置く** — `gateway/.env.forwarder.example` を `.env.forwarder` に
   コピーし、`FORWARDER_TARGET` に家のTailscaleアドレスを書く。
   `run_forwarder.ps1` を起動して `http://localhost:8080/healthz` を確認
4. **Tailscale にログイン**（未ログインなら）
5. 家に戻ったら3章の手順で記憶を統合する

会社の体の向き先（実機に焼いてあるゲートウェイURL）は**変えなくてよい**。
今までどおり会社PCを指していれば、転送役がその先を引き受ける。
