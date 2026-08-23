# 会社の環境で今日の成果を動かす

対象: 会社の PC (VS Code + Claude Code) と、会社のタチコマ実機

家の環境とは **PC も実機もネットワークも別物**なので、git clone だけでは
動かない。以下は必要な作業の全部。

---

## 0. いちばん重要な注意

**会社の実機は古いファームの可能性が高い。**`firmware/README.md` の
「Before flashing any other device running this firmware」に、2026-07-22 に
見つかった4件の不具合が挙げてある。とくに `483a44c` は、工場出荷 NVS に
xiaozhi クラウドの実クレデンシャルが入っていて、既定でウェイクワード音声を
`api.tenclass.net` / `mqtt.xiaozhi.me` へ送る、というもの。

会社のネットワークに繋ぐ前に、今日のファームを焼くこと。

---

## 1. リポジトリ

```powershell
git clone -b phase4-ai-gateway https://github.com/Daisuke-0409/StackChan.git
cd StackChan
```

Claude Code には最初にこう伝える:

```
CLAUDE.md と firmware/docs/office-setup.md を読んで。
ここは会社の環境。家とは別のPC・別の実機・別のネットワーク。
```

---

## 2. git に乗っていないもの (4つ)

`.gitignore` で除外してあるので、clone しても入らない。

| 対象 | どうするか |
|---|---|
| `firmware/gateway/.env` | **新規に作る**(下記) |
| `firmware/gateway/models/` (54MB) | **再取得する**(下記) |
| `firmware/gateway/memory/` | **コピーしない**。会社は会社で覚えさせる |
| `firmware/gateway/voicevox_dict.json` | 必要なら作る(名前の読み) |

`memory/` を持ち込まない理由は2つ。家の会話の記憶と会社の記憶が混ざるのは
まず望ましくないし、`people.json` には声紋という生体データが入っている。
2台の記憶統合は Phase 10 の話で、そのときに設計する。

---

## 3. `.env` を作る

`gateway/.env.example` をコピーして埋める。

```
DEVICE_TOKEN=<会社の実機に焼く任意の文字列。家のものとは別にしてよい>
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

`DEVICE_TOKEN` は実機に焼く値と一致していればよい。家と同じにする必要はなく、
**分けたほうがよい**(片方が漏れてももう片方に波及しない)。

---

## 4. PC 側の依存

```powershell
python -m pip install torch librosa opencv-python numpy scipy
```

会社のネットワークがプロキシで TLS を終端していると、pip が
`CERTIFICATE_VERIFY_FAILED` で全滅する。家でも起きた。**検証を切らずに**直す:

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

`PIP_CERT` は環境変数なので、pip が内部で起動する pip にも伝わる。
`--cert` だけでは伝わらずビルド依存の取得で落ちる。

### モデルの取得 (54MB)

```powershell
python firmware\docs\fetch_models.py
```

無ければ `firmware/gateway/models/` に3つ置く:

| ファイル | 用途 | 取得元 |
|---|---|---|
| `voice_encoder.pt` | 声紋 (17MB) | resemble-ai/Resemblyzer の `pretrained.pt` |
| `face_detection_yunet.onnx` | 顔検出 (0.2MB) | opencv/opencv_zoo |
| `face_recognition_sface.onnx` | 顔認識 (39MB) | opencv/opencv_zoo |

### VOICEVOX

無くても動く(Gemini TTS に自動フォールバック)が、1文あたり 755ms 対 2667ms
なので体感が全く違う。入れるなら、エンジンだけをログオン時に起動する
タスクを登録しておくと楽 (家では `VOICEVOX Engine (Tachikoma)` として登録)。

---

## 5. ESP-IDF (ファームを焼くなら)

家は v5.5.4。`idf.py --version` で確認。

**焼かずに済ませる選択肢もある。**会社の実機を家と同じ状態にしたいだけなら
焼く必要があるが、ゲートウェイだけ動かして家の実機を持ち込む、という手も
ある。会社で実機開発をするなら ESP-IDF を入れる。

---

## 6. 実機に会社の PC の住所を焼く (ここを忘れると動かない)

タチコマは**接続先の IP を NVS に焼いて持っている**。家では
`192.168.2.120` を向いている。会社の PC の IP を焼き直さないと、会社では
何も返ってこない。

```powershell
ipconfig    # 会社PCの IPv4 を確認
```

```powershell
idf.py -D DEVELOPMENT_BUILD=ON `
  -D "TACHIKOMA_GATEWAY_URL=http://<会社PCのIP>:8080/v1/chat" `
  -D "TACHIKOMA_TRANSCRIBE_QUEUE_URL=http://<会社PCのIP>:8080/v1/transcribe" `
  -D "TACHIKOMA_SPEAK_QUEUE_URL=http://<会社PCのIP>:8080/v1/speak_queue" `
  -D "TACHIKOMA_VISION_URL=http://<会社PCのIP>:8080/v1/vision" `
  -D "TACHIKOMA_DEVICE_TOKEN=<.env の DEVICE_TOKEN と同じ値>" reconfigure
idf.py build
```

**焼く前に必ずフルバックアップを取る。**

```powershell
esptool.py -p COM3 -b 460800 read_flash 0 0x1000000 backups\office_before_first_flash.bin
idf.py -p COM3 flash
```

461800 なのは、921600 で `Serial data stream stopped` に当たったから。

会社 PC の IP が DHCP で変わると、また焼き直しになる。**固定 IP か DHCP 予約**
にしておくと後が楽。

---

## 7. 起動と確認

```powershell
powershell -ExecutionPolicy Bypass -File firmware\gateway\run_gateway.ps1
```

起動時に現在の LAN IP を表示する。**実機に焼いた IP と一致しているか**を
ここで確認する。

```powershell
curl.exe http://127.0.0.1:8080/health
```

設定アプリはブラウザから `http://<会社PCのIP>:8080/`。トークンは `.env` の
`DEVICE_TOKEN`。

---

## 8. 会社の実機に覚えさせる

声紋は環境ごとに登録する。会社の実機に向かって:

```
「自己紹介するね。俺の名前は大輔です。よろしく」
```

5秒程度、はっきりと。1.2秒未満だと声紋が作れない。

**同僚の登録は同意を取ってから。**顔と声は個人情報保護法の個人識別符号に
あたる。会社で運用するなら、同意と削除手段(設定アプリの「覚えている人」から
削除できる)を先に用意しておく。

---

## 9. 会社ネットワークで詰まりそうなところ

| 症状 | 見るところ |
|---|---|
| Gemini に繋がらない | プロキシ。`generativelanguage.googleapis.com` への到達性 |
| pip が全滅 | 上記の `PIP_CERT` |
| 実機から繋がらない | Windows Defender ファイアウォールの 8080 受信許可 |
| 実機が無言 | まず `run_gateway.ps1` が動いているか。次に焼いた IP |

デバイスが無言のときの切り分けは `firmware/README.md` の
「The device is silent」節にまとめてある。

---

## 10. 今日わかっている未解決

- **ハンズフリー会話が未動作。**窓は開くが音声を検出しない。しきい値
  (`kFollowUpSpeechRms = 1400`) が実際の声の大きさと合っていない可能性。
  ログの `follow-up speech detected (rms=...)` が出れば成功、出なければ
  しきい値が高い。実測値を見てから決める
- 2回連続録音の2回目が稀に失敗する (診断ログは入れた、再現待ち)
- Phase 6 承認基盤は未実装 (計画書は実装済みとしているが、存在しない)
