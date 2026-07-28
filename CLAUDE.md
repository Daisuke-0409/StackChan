# タチコマ計画 — Claude Code 作業ガイド

M5Stack StackChan (CoreS3 / ESP32-S3) をフォークし、独自の音声対話ロボット
「タチコマ」に作り替えるプロジェクト。ダイスケ(大輔)専用。

このファイルは毎セッションの冒頭に読み込まれる。**構想は「タチコマ計画v3.0」、
実装の現在地はこのファイル**が正とする。両者に食い違いがあればコードを確認し、
このファイルを更新すること。

---

## 1. コンセプト

**「人格は1つ、入口は複数」** — タチコマという1つの人格に対し、StackChan 実機、
将来的には Even G2 や会社システムなど複数の入口からアクセスする。

---

## 2. アーキテクチャ

**デバイス側に AI は載っていない。** 返答はすべて PC 上の Tachikoma Gateway が生成する。

```
頭タッチ(押下→離す) → 録音 → /v1/transcribe (Gemini STT)
                              → /v1/chat (Gemini + Google検索 + 記憶)
                              → TTS (ローカル VOICEVOX)
                              → /v1/speak_queue にキュー
デバイスがポーリングして取得 → 再生
```

API キーはすべて PC 側に留まり、デバイスは一切触らない。
上流の xiaozhi クラウド AI は `TACHIKOMA_DISABLE_XIAOZHI_CLOUD` で完全に無効化済み
(工場出荷 NVS に実クレデンシャルが同梱されており、既定でウェイクワード音声を
外部送信するため)。

### 主要ファイル

| 役割 | パス |
|---|---|
| ゲートウェイ本体 | `firmware/gateway/server.py` |
| ゲートウェイ起動 | `firmware/gateway/run_gateway.ps1` |
| 別拠点の中継 | `firmware/gateway/forwarder.py` / `run_forwarder.ps1` |
| 記憶の統合 | `firmware/gateway/merge_memory.py` |
| 音声入力(録音・STT) | `firmware/main/stackchan/voice_input/voice_input_controller.cpp` |
| 音声出力(キュー取得・再生) | `firmware/main/ai_gateway/speech_announcer.cpp` |
| AI リクエスト | `firmware/main/ai_gateway/ai_gateway_client.cpp` |
| 状態機械 | `firmware/main/stackchan/state/` |
| モーション | `firmware/main/stackchan/motion/` |
| コーデック(I2S/I2C) | `firmware/main/hal/board/cores3_audio_codec.cc` |

### 状態とリアクションは二層構造

```
TachikomaState    : Booting, Idle, Listening, Thinking, Speaking, Reacting, Error, Sleeping
TachikomaReaction : None, Happy, Confused
```

Happy/Confused は**状態ではなくリアクション**で、`Reacting` 状態の上に乗る。
感情を追加しても状態遷移を壊さずに済む。

---

## 3. 現在地 (2026-07-28 時点)

### 完了

| Phase | 内容 |
|---|---|
| 0〜4 | 機体構築・アバター基盤・ステートマシン・Gateway基盤 |
| 5 | 会話ループ(音声入出力・Gemini統合・記憶・Web検索) — **実用レベル** |
| 5.5 | 感情表現(判定層＋発火経路＋モーション大型化) |
| 12(一部) | 話者識別・顔識別・記憶の可視性制御 — **ゲートウェイ側は完了** |

Phase 5 で動いているもの:

- **音声対話** 発話終了から最初の音声まで約3.7秒
- **会話の記憶** 直近10往復 + 恒久的な事実(名前・居住地等)。
  事実抽出は返答送信後にバックグラウンドで行うため待ち時間に乗らない
- **Web検索** Gemini の google_search グラウンディング。
  必要な質問だけ自動で検索し、通常会話の速度は落とさない
- **ローカルTTS** VOICEVOX (1文あたり約755ms、Gemini TTS の約3.5倍速)。
  エンジン未起動時は Gemini TTS に自動フォールバック
- **録音** 最大30秒

### 未着手・ブロック中

| Phase | 内容 | 状況 |
|---|---|---|
| 5.5 | 感情表現 | **次に着手**。詳細は下記 |
| 6 | 音声承認リレー | `ApprovalRequestStore` が**この環境に存在しない**。要新規実装 |
| 7 | 他コーディングエージェント対応 | 骨格のみ |
| 8 | Mac mini + NAS 移行 | 機材待ち |
| 9 | 会社システム連携 | 未着手 |
| 10 | 2台の記憶統合 | **方針変更で不要化**。記憶を人格キーに変更済み (下記) |
| 11 | Even G2 連携 | 機材待ち |
| 12 | 認証・社員個別認識 | 重要度高 |
| 13 | HTTPS化・デバイスペアリング | 本番前に必須 |
| 14 | 複数拠点対応(固定URL問題) | 家/会社でIPが異なる |
| 15 | 会社内リリース | 未着手 |

### 権限システムの現在地 (2026-07-26 夜)

**ゲートウェイ側は完成し、実機ログで検証済み。**

- 声紋で話者を識別し、記憶を相手ごとに出し分ける
- 「ここだけの話」等のキーワードで秘密指定（直前の会話にも遡って適用）
- 顔検出・顔認識・`/v1/vision` エンドポイントは実装済み

**設計の核心**: モデルに秘密を守らせるのではなく、**モデルに秘密を渡さない**。
聞いてはいけない事実はコンテキストに載せない。プロンプトで「言うな」と指示する
方式は、同情的な聞き方で引き出される余地が残る。

**接続済み (2026-07-28 更新)**: デバイスからカメラ画像を `/v1/vision` に送る経路は
`face_tracker.cpp` で実装され、実機で動作を確認した。会話中 (Listening/Thinking/
Speaking/Reacting) だけ約700ms間隔でフレームを送る。

ただし**首を動かす部分は止めてある** (`kFaceTrackingMovesHead = false`)。
タッチセンサが可動部に付いているため首振りが誤検知を生み、それが会話を開始し、
会話中だから顔追従が首を振る、という自己増殖ループになって、
**電話中に勝手に話しかけてくる**実害が出た。押下と振動を区別できるようになるまで戻さない。
経緯は `firmware/docs/2026-07-28-office-session.md` の5章。

**注意**: 誰も登録されていない間は全員 master 扱い。これは意図的で、
そうしないと本人が自分の記憶を見られなくなる。誰か登録された時点で制御が始まる。

### 記憶は機体ではなく人格に紐づく (2026-07-28 変更)

記憶の保存先は `gateway/memory/<brain_id>.json` (既定 `tachikoma.json`)。
以前は機体のMAC名 (`80456B4DE03C.json`) で、これは**構想と逆**だった。
`people.json` は元から機体に紐づいていないため、2台目は同じ人物を認識できるのに
その人と話した内容は何も知らない、という状態になる。

機体は**応答先の単位ではあるが、記憶の単位ではない**。話速キュー・保留コマンド・
保留感情・現在の話者は今も `device_id` キーのまま (共通にすると2台が同時に喋る)。

| 変数 | 既定 | 意味 |
|---|---|---|
| `TACHIKOMA_MEMORY_SCOPE` | `shared` | `device` で旧来の機体別に戻る |
| `TACHIKOMA_BRAIN_ID` | `tachikoma` | 記憶ファイル名 = どの人格か |
| `TACHIKOMA_MEMORY_DIR` | `gateway/memory` | 保存先。NASのパスも可 |

共有前の機体別ファイルは初回に自動で引き継ぐ (コピー。原本は残す)。
2つ以上ある場合は**何も引き継がない** — 順序を機械が推測すると、唯一信頼される
場所に嘘の過去を書くことになる。手で統合すること。

**Phase 10「2台の記憶統合」は、統合すべき2つを作らないことで回避する方針に変更。**
ゲートウェイは家の1台に集約する。

### 会社の機体をどう繋ぐか (2026-07-28 決定)

**ESP32 は Tailscale に入れない。** Tailscale は PC に入れるもので、実機は参加できない。
よって経路は:

```
会社の実機 → (会社LAN) → 会社PC の転送役 → (Tailscale) → 家のゲートウェイ
```

実機の向き先は会社PCのLAN IPのままでよい。**会社PCが起動している間だけ会社の体が
喋る**。ダイスケが会社にログインできるのは営業時間のみなので、これは制約ではなく
前提と一致している。営業時間外の会社の体は接続失敗をログに出して黙るだけで、害はない。

**決定: 会社での会話とその記憶は家のPCに保存される。** ダイスケ承知の上。
CRMの顧客データを扱う段階で再検討すること。

**デバイストークンは当面1つ**。2台が同じ合言葉を使う。片方だけ無効化できないという
一点だけが弱いので、**会社に機体を置きっぱなしにする時点で機体ごとに分ける**。
現状は `_authorized()` が単一文字列比較 (`server.py`)。
`TACHIKOMA_MEMORY_DIR` をNASに向けることは可能だが、**ファイル共有に対して
ゲートウェイを2台走らせてはいけない** (`os.replace` の原子性がSMBでは保証されず、
互いの記憶を黙って消す)。複数ゲートウェイが要るならDB化が前提 = Phase 8。

### Phase 5.5 の実態 (重要)

計画書は「しぐさの大型化」と書いているが、コードを確認した結果**そうではない**:

- Happy/Confused のモーションデータ (`kHappySteps`, `kConfusedSteps`) → **実装済み**
- 状態機械のリアクション処理 (`StartReaction`) → **実装済み**
- 会話内容から感情を**判定する層** → **存在しない**
- `HappyRequested`/`ConfusedRequested` を**発火する呼び出し** → **存在しない**
  (自己テストを除き0件)

つまり**表現する体はあるが、感情を決める頭が無い**。既存モーションは一度も
再生されていない。まず判定層と発火経路を作り、その後で動きを派手にする。

---

## 4. 作業ポリシー

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

## 5. 環境固有の落とし穴 (実際に踏んだもの)

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

## 6. デバイスが無言のとき

まず**ゲートウェイが起動しているか**を疑う。デバイス側は正常でも、返答生成側が
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

---

## 7. 未解決の問題

- **2回連続録音の2回目でアップロードが失敗する**ことがある(タイムアウト寄り、
  再現性未確認)。承認フロー(Phase 6)は短い発話が連続するため、着手前に原因特定を
  優先する
- **`ApprovalRequestStore` が存在しない**。計画書v3.0は実装済みと記載しているが、
  `StackChanDev` 配下に1件もヒットしない。Phase 6 は新規実装から始まる

---

## 8. 参照

- **運用手順(ダイスケがやること): `firmware/docs/operations.md`**
- **会社セッションの記録: `firmware/docs/2026-07-28-office-session.md`**
  (自前サーバ構築・認証突破・上流コードの落とし穴・積み残し)
- 直近の詳細な作業記録: `firmware/docs/2026-07-26-session-summary.md`
- ゲートウェイの機能別ドキュメント: `firmware/gateway/README.md`
- ファーム側の運用・既知バグ: `firmware/README.md`
