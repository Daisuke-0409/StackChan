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

### PC側の半分 — `notifier/` (2026-07-30 統合)

**もともと別リポジトリ (`Daisuke-0409/Tachikoma`、ブランチ mail) だったものを、
`git subtree` でこのリポジトリの `notifier/` に取り込んだ。34コミットの履歴ごと。**

分けていた理由が無くなったため統合した。承認リレーは「PC側で判断してロボットに
喋らせる」機能で2つのリポジトリにまたがっており、片方を直すたびに別々にコミット
する必要があった。加えて、リポジトリ名 `Tachikoma` が**ロボット本体に見える**ため
「古い方」と誤解されやすかった。

旧リポジトリはアーカイブ (読み取り専用) にして残す。削除はしない。

```
notifier/tachikoma_notifier/     PC側の通知・承認基盤
```

Claude Code など開発エージェントの Hook イベントを受け、状態を音声で知らせる。
出力先は3つ (ログのみ / Windows音声合成 / **StackChanのスピーカー**)。
両者は **`/v1/speak` だけ**で繋がっており、認証は共有ベアラートークン。

**2026-07-30 実測: テスト268件すべて通過。** 主なもの:

| モジュール | 状態 |
|---|---|
| `approval_store.py` / `approvals.py` / `permission_relay.py` | **実装済み・テストあり** |
| `voice_approval_gate.py` | 実装済み。低リスクな読み取り専用ツールに限定した厳格ゲート |
| `conversation_pipeline.py` / `gemini_responder.py` | Gemini応答→音声合成まで実動確認済み。**マイクから起動する入口が無い** |
| `stackchan_speech_sink.py` | StackChan の `/v1/speak` へ送る出力先 |
| `adapters/` (codex ほか) | 骨格のみ・実機未検証 |

**承認リレーは「未接続」であって「未実装」ではない。**
残っているのは Claude Code の実フローへ「はい/いいえ」を返す配線。

### 状態とリアクションは二層構造

```
TachikomaState    : Booting, Idle, Listening, Thinking, Speaking, Reacting, Error, Sleeping
TachikomaReaction : None, Happy, Confused
```

Happy/Confused は**状態ではなくリアクション**で、`Reacting` 状態の上に乗る。
感情を追加しても状態遷移を壊さずに済む。

---

## 3. 現在地 (2026-07-30 時点)

**Phase 番号は 2026-07-30 に廃止した。** 開発レポート(7/26)と本ファイルで
5.5 / 8 / 9 が別々の意味になっており、同じ番号が別物を指していた。
以降は**名前で呼び、順序だけを保つ**。

### 完了

| 名前 | 内容 |
|---|---|
| 機体と基盤 | 機体構築・アバター基盤・ステートマシン・Gateway基盤 |
| 会話ループ | 音声入出力・Gemini統合・記憶・Web検索 — **実用レベル** |
| 実運用ハードニング | 2026-07-22〜26 の実機バグ潰し。録音捕捉率 21% → ほぼ100% |
| 感情表現 | 判定・発火・モーション・**大型化まで全部完了** (2026-07-26 `c48cadb` で範囲3倍、`f728260` で整定時間を合わせて実際に届くように) |
| 権限システム | 話者識別・顔識別・記憶の可視性制御 — **ゲートウェイ側は完了** |
| 自前サーバ認証 | M5Stack の非公開認証を自前実装で置換 (2026-07-28) |
| 記憶の人格化 | 機体ごとから人格ごとへ (2026-07-28) |
| 別拠点の接続 | `forwarder.py` + Tailscale。実装・検証済み、会社への設置は未確認 |

会話ループで動いているもの:

- **音声対話** 発話終了から最初の音声まで約3.7秒
- **会話の記憶** 直近10往復 + 恒久的な事実(名前・居住地等)。
  事実抽出は返答送信後にバックグラウンドで行うため待ち時間に乗らない
- **Web検索** Gemini の google_search グラウンディング。
  必要な質問だけ自動で検索し、通常会話の速度は落とさない
- **ローカルTTS** VOICEVOX (1文あたり約755ms、Gemini TTS の約3.5倍速)。
  エンジン未起動時は Gemini TTS に自動フォールバック
- **録音** 最大30秒

### これからやること (上から順に効く)

| 名前 | 状況 |
|---|---|
| **HTTPS化(人が使う面)** | **すぐできる。**Tailscale の証明書は既に利用可能。コマンド1つ。これ1つで Even G2・CRM・スマホ操作の前提が同時に揃う。下記 |
| **Even G2 連携** | **機材到着済み・着手可能。**上記が前提。下記「Even G2 の前提」 |
| **承認リレーの繋ぎ込み** | **新規実装ではない。**PC側に土台は実装済み(下記「もう1つのリポジトリ」)。残るは Claude Code の実フローへの配線。**Even G2 と組むと視界に出してリングで承認できる** |
| **会社の体を繋ぐ** | 会社PCに転送役を置く(10分)。営業時間内に |
| CRM連携 | **調査済み・実装未着手**。方式は下記「CRM連携の方針」 |
| Mac mini + NAS 移行 | 機材待ち。**構成は決定**: Mac mini M4 Pro 32GB を常時起動の頭、NAS 8TB を記憶の保管庫(DB)。**NASに声紋・顔認識をやらせないこと** (CPUが足りない)。記憶のDB化はここ |
| 他コーディングエージェント対応 | Codex / Gemini CLI / GitHub Actions のアダプタは骨格のみ・実機未検証 (PC側) |
| 実機のHTTPSとペアリング | 機体ごとの鍵。会社に機体を置きっぱなしにする直前に |
| 社内リリース | 上記が揃ってから |

**取り下げ**: 「2台の記憶統合」は不要化した。統合すべき2つを最初から作らない。

### セッション開始時の約束 (ダイスケの指定・恒久)

**毎セッション、冒頭で必ずこの順で説明してから作業に入る:**
「前回はここまで終わった」→「全体の中で現在ここ」→「今回はここから始める予定」。

### 次セッションの開始点 (2026-08-15 深夜に記録)

**前回終わったこと**: R1ハンズフリー・R2 PCの耳を実機確認でクローズ /
R4承認リレーのコード完成＋フック登録済み (実地テスト未) /
R5: /talk 稼働・Even Hub登録・ターミナルモードON・
**Even Terminal で Claude Code がメガネに映り会話成立を確認済み** /
実機修正2回焼き込み / 声紋の自動学習を導入。

**今回やること (この順)**:
1. **R5本体**: `/plugin marketplace add even-realities/everything-evenhub` →
   `/plugin install everything-evenhub` → アプリ足場生成 → /talk の中身を移植 →
   シミュレータ確認 → QRをダイスケへ → メガネでタチコマと会話
2. **Codex**: ダイスケは **ChatGPT Plus 契約あり**。`npm i -g @openai/codex` →
   ログイン → Evenアプリにホスト追加 → メガネ3択完成
3. R3 (ルーターが手元に戻っていれば): 3点確認 → 設定
4. R4実地テスト: `notifier\run_approval_daemon.ps1` 起動 → 新セッションで Read →
   タチコマが読み上げ → 「はい」
5. **未pushが4件たまっている** — 冒頭で確認して上げる

**朝いち調査 (2026-08-16 深夜に発生)**:
1. **ゲートウェイと Even Terminal のタスクが 02:00 頃に揃って死んだ**。
   LastTaskResult=0xC000013A (STATUS_CONTROL_C_EXIT = コンソールへの Ctrl+C/閉じる)。
   Python 側のエラー記録なし。疑い: デスクトップに残っていた PowerShell 窓
   (QR表示・旧dev server) を閉じた際に conhost が巻き添えにした?
   **対策候補**: タスクを powershell ラッパーではなく python 直接起動にする /
   ログ追記は python 側で行う。再発したらこれを最優先で
2. **メガネからの発話が speaker not identified (best=0.54)** → least privilege →
   master の事実3件が伏せられた。メガネのマイク特性が実機と違い、声紋が届かない。
   選択肢: (a) メガネ音声からもサンプル学習が効くまで会話を重ねる
   (b) メガネという入口自体を「ダイスケの所持品=本人」として信頼する設計
   (R7 のペアリング設計と一体で決める)。**タチコマがメガネで名前を答えられない
   場合はこれが原因** — 故障ではない

**運用メモ**: Even Terminal は**ログオン時タスク化済み** (2026-08-16)。
グローバルインストール、ポート3456、ログは `logs\even-terminal.log`。
設定とトークンは `~\.even-terminal` に永続化されるため、再起動しても
iPhone のペアリングは保持される。これで**PCを立ち上げれば
ゲートウェイ・VOICEVOX・Even Terminal の3つが自動で揃う**。

### いま現物がどうなっているか (2026-08-15 更新)

**次のセッションはここから始める。**

- **会話は成立している。** 頭タッチ・ハンズフリーとも実機ログで一往復の成立を確認
  (2026-08-14 21:55: transcribe → speaker identified master → chat 200 → 再生)
- **記憶の人格化は実地稼働中。** `tachikoma.json` に恒久的な事実5件
  (自分の名前タチコマ / 大輔の名前・住所・勤務先・誕生日、後4者は master 限定)
- **ゲートウェイと VOICEVOX はタスクスケジューラ起動**
  (`Tachikoma Gateway` / `VOICEVOX Engine (Tachikoma)`、ログオン時)。
  ログは `StackChanDev\logs\gateway.log` に常時追記。
  起動確認は `python:` 行 (3.11 であること) と `biometrics warmed voice=True`
- **話者識別はサンプル自動追加あり** (score>=0.75 の一致で声紋サンプルを追加、
  最新8件保持)。それまでサンプル1件で 0.53〜0.82 と不安定だった
- ~~未解明~~ → **原因特定・修正済み** (2026-08-15): Thinking 中の物音が並行
  アップロードを起こしていた。ハンズフリー検出を Idle 限定に修正 (`c7b3cd9`)
- **レビューで見つかった先送り3件** (エージェント検証 2026-08-15、対処は次回):
  (1) 文間ギャップが 1500ms 超のとき自声ガードに隙間 (再生開始と状態遷移のラグ。
      次の一手: SpeechAnnouncer の再生中フラグを直接見る)
  (2) 窓の期限切れと発話開始が同時のとき録音が30秒上限まで孤立する競合 (稀)
  (3) 極端に短い返事では Speaking エッジが観測されず会話ウィンドウが開かない
- **vendored コードの地雷 (未対処・記録のみ)**: uart-eth-modem コンストラクタの
  abort / UVC 系の未知イベント abort。通常運用では踏まない
- 会社の記憶は未回収 / 会社PCの転送役は未確認 (変化なし)
- Tailscale: 家PCはHTTPS証明書利用可能・serve 稼働。iPhone 在籍

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

**「2台の記憶統合」は、統合すべき2つを作らないことで回避する方針に変更。**
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

### HTTPS化は2つに分かれる (2026-07-30 決定)

一塊に見えて、性質も難易度も違う2つが混ざっている。

**人が使う面 (小さい・実装ではなく設定)**
ブラウザ操作ページ、CRM、**Even G2 の Web アプリ**。Tailscale の HTTPS で足りる。
インターネットには公開されない。家のPCは自分の tailnet 名 (`tailscale status --json` の Self.DNSName) で
**証明書が既に利用可能**なので、次の1コマンドで済む:

```powershell
& "C:\Program Files\Tailscale\tailscale.exe" serve --bg --https=443 http://127.0.0.1:8080   # https://<tailnet名>/ で公開される
```

これで同時に解けるもの: スマホからブラウザ操作ページが開ける
(いまは secure context でないため `crypto.subtle` が使えず動かない)、
CRM を外から見る前提、CRM の指紋認証(WebAuthn)、**G2 の前提**。

**実機のHTTPS + ペアリング (大きい・ファーム側の実装)**
ESP32 からゲートウェイへの通信の暗号化と、機体ごとの鍵。
会社に機体を置きっぱなしにする直前でよい。

### CRM連携の方針 (2026-07-30 調査)

対象は `ダイボ石材_CRMプロトタイプ` (Python 単一ファイル + SQLite、ポート 8765)。
顧客 2,116 / 案件 2,664 / 墓所 2,068 の**実データ**。

- `/api/field/search?name=&address=` が施主名・住所・霊園・区画・**GPS座標**を返す。
  音声で引くのに最適
- **`is_local()` が 127.0.0.1 からのアクセスを合言葉なしで通す。**
  同じPCの中から叩く限り認証の作り込みは不要
- CRM 自身のドキュメントが「現状のまま外部公開してはいけない」と明記
  (暗号化なし・個人アカウントなし・監査ログなし)

**採用する方式**: 会社PC側でCRMを引き、**答えの文だけ**を返す。
顧客データは会社の中で完結し、**Gemini にも渡らない**。
これは権限システムの原則(モデルに秘密を渡さない)と同じ考え方。
ゲートウェイ側には「これはCRMへの質問だ」と判断する層と、
**その質問と答えを記憶に残さない**扱いが要る。

ダイスケは顧客情報の持ち帰りと、家から会社ネットワークへのアクセス権限を
持っている。ただし**それは Google に送る許可ではない**ので、方式Aを崩さないこと。

### Even G2 の前提 (2026-07-30 調査)

- 開発は公式の **Even Hub**。仕組みは
  **「コードは自分のサーバで動き、iPhone の Even App が WebView で読み込み、
  表示と入力が BLE でメガネへ中継される」**
  → **G2 のアプリ = このゲートウェイがホストする Web ページ**。新しい頭は要らない
- **G2 にスピーカーは無い。カメラも無い。マイクはある。**
  よって G2 でのタチコマは**「声で話しかけて、文字で返る」**。
  声で返すなら iPhone 側で鳴らす
- カメラが無いので **G2 では顔認識による話者識別は使えない** (声紋は使える)
- 画面 640×350 のマイクロLED、1,200nit、他人からは見えない。電池約2日。
  入力は R1 リング(回す・タップ)
- iPhone は tailnet に在籍済み。**HTTPS化(人が使う面)が前提**(WebアプリはHTTPS配信が要る)
- 逆解析による直接BLE制御も存在するが未完成。**公式SDKを使うこと**

### Even G2 の追加調査 (2026-08-15 夜、登録完了後)

**準備は全部済んだ**: Even Hub アカウント作成・Console 到達 /
iPhone アプリの「ターミナルモード」ON (=Developer Mode の日本語名) /
音声言語=日本語 / PC に Node v24 あり。

**G2 関連は実は2系統ある。混同しないこと:**

1. **Even Hub アプリ (R5 の本体)** — 自作アプリを QR サイドロードで
   メガネに読ませる。`npm create vite` + `@evenrealities/even_hub_sdk` +
   `evenhub` CLI。**シミュレータあり** (`evenhub-simulator http://localhost:5173`、
   576×288 — 実機なしで開発可能)。タチコマとの会話はこちらで作る。
   /talk ページ (実装済み) の中身を SDK の殻に移植する。
2. **Even Terminal (公式機能・作らなくていい)** — Claude Code / Codex の
   セッションをメガネに流す公式機能。PC で `@evenrealities/even-terminal`
   (npm、既定ポート3456) を起動すると LAN URL とトークンが表示され、
   iPhone アプリの「ホスト設定」に入れる (QRも可)。
   **R4 と相性が良い**: メガネで Claude Code の様子を見ながら、
   承認はタチコマに「はい」で返せる。

**開発加速**: Even 公式の Claude Code プラグインがある。次セッションで
`/plugin marketplace add even-realities/everything-evenhub` →
`/plugin install everything-evenhub` (足場生成・シミュレータ・パッケージングの
スキル12種)。R5 の見積は 1〜2 晩に短縮済み。
`TACHIKOMA_MEMORY_DIR` をNASに向けることは可能だが、**ファイル共有に対して
ゲートウェイを2台走らせてはいけない** (`os.replace` の原子性がSMBでは保証されず、
互いの記憶を黙って消す)。複数ゲートウェイが要るならDB化が前提 =「Mac mini + NAS 移行」。

### 感情表現の実態 (2026-07-30 再確認)

**この節は以前「感情を決める頭が無い」と書いていたが、それは古い。**
実際にはゲートウェイから実機まで繋がっている。経路を追った結果:

```
モデルに回答の先頭で [happy]/[sad]/[neutral] を必ず付けさせる
  → _split_emotion_tag() がタグを剥がす (_EMOTION_TAG_RE)
  → _EMOTION_TO_REACTION で happy→happy, sad→confused (neutral は意図的に無反応)
  → set_pending_emotion() が機体ごとに保留
  → /v1/speak_queue の応答に X-Tachikoma-Emotion ヘッダとして乗る
  → speech_announcer.cpp がヘッダを読み、モーションを直接再生する
```

**タグは音声より先に決まる。** ストリーミングでは先頭チャンクで解決するので、
文が1つも合成される前に動きを始められる。`[hap` のような途中までのタグを
「タグ無し」と誤判定しない作りになっている (`_EMOTION_TAG_MAYBE_RE`)。

**モーションは状態機械を経由しない** (`speech_announcer.cpp` の注記)。
`HappyRequested`/`ConfusedRequested` は `Reacting` 状態へ遷移させるが、
`Reacting` + `SpeechFinished` の遷移規則が無いため、発話中に使うと
`Speaking` から抜けられずタイムアウトで `Error` に落ちる。
喋りながらの反応は、モーション管理を直接叩くのが正しい。

**「動きを派手にする」も終わっている** (2026-07-30 再確認)。
`c48cadb` で範囲を3倍にし、`f728260` で「指示した角度に実際に届く」ようにしてある
(サーボの整定より短い間隔で次の指令を出すと、頭が目標に着く前に上書きされる。
`kServoSpeedExpressive` では約0.3秒で整定するので、ステップを350ms以上にした)。

つまり感情表現に残作業は無い。**次にやるべきは、実機で実際に動くのを目で見て
確かめること**であって、コードを足すことではない。

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
  再現性未確認)。承認フローは短い発話が連続するため、着手前に原因特定を
  優先する
- **2回目の発話が通らない。2026-07-30 夜、未解決のまま。** 詳細は下記
- **会社PCに転送役が設置済みかどうか未確認。**「済んでいない」ではなく「不明」

### 2回目の発話が通らない (2026-07-30 発生 → 2026-08-13/14 解決)

**解決済み。原因は3層+2層の計5つが積み重なっていた。** 下の記録は経緯として残す。
最終的な原因一覧 (すべて修正・実機検証済み):

1. **vendored ライブラリの意図的クラッシュ** `*(int*)0=0;` — 返事直後のマイク
   再オープンで I2S 再設定が失敗すると panic。「1回目だけ動く」の正体。
   `patches/apply_codec_dev_fix.py` で再適用可能 (managed_components は git 外)
2. **無音判定の時計停止** — FollowUpTick が recording_ で早期 return し、
   発話長が常に 0ms → 全部 "too short" で破棄
3. **閾値が部屋の暗騒音より下** (1400/900 vs 暗騒音 1457-2257) → 発話が
   終わらず 30 秒上限まで録音。現在 4000/2500 (実測に基づく)
4. **PATH の python 入れ替わり** (3.9 が先に来て torch 無し) → 声紋が無言で
   OFF。run_gateway.ps1 が依存を import できる python を自分で選ぶように
5. **温めがモデルだけで経路が冷えたまま** (librosa→numba の初回コンパイル
   14.9s) → 最初の話者が request 内で 67 秒払っていた。起動時に実クリップを
   通して温める

**教訓 (高くついた順)**: ログを絞るとき `boot:` を捨てない (再起動の証拠を
2週間捨て続けた) / 「たまたま動いた」検証はプロセスの寿命を確認する /
閾値は必ず実測してから置く。

---

以下は当時の調査記録 (履歴):

**症状**: 再起動直後の1回目は成功する。2回目以降が通らない。
1回目は頭タッチ、2回目はハンズフリー継続の経路 — **別のコードを通っている**。

**実機ログで確認した事実** (2026-07-30 22:0x):

```
follow-up speech detected (rms=7061 / 17059 / 20288 / 23432)
W TachikomaState: Rejected SpeechFinished in Idle
W TachikomaState: Rejected UserSpeechEnded in Idle
[VoiceInput] transcribed text len=73        ← 認識は成功している
[AiGateway] request failed id= error=internal
[VoiceInput] AiGatewayClient rejected transcribed text
TachikomaState: Idle -> Error by AiRequestFailed
```

**マイクも声量も問題ない。73文字認識できている。送る直前にデバイスが捨てている。**

**打った手 (`7a2b9a0`)**: 自分の声を拾わないようにした
(`Speaking` 中と終了後1500msはハンズフリー検出を止める)。
RMS 7061〜23432 は再生中の実測で、しきい値 1400 の5〜16倍だった。
**焼いて検証済み (Hash verified) だが、症状は解消しなかった。**
この修正自体は実測に基づく本物のバグ潰しなので戻す必要はない。

**原因は特定済み (2026-07-30 23:00、コードで確認)**:

| イベント | どこで発火するか |
|---|---|
| `UserSpeechStarted` | `voice_input_controller.cpp:340` — **頭タッチ経路だけ** |
| `UserSpeechEnded` | 同 `:744` `StopRecordingAndUpload()` — **両方の経路が通る** |

ハンズフリー経路は `FollowUpTick()` の中で `recording_ = true` を直接立てるだけで、
**`UserSpeechStarted` を撃たない**。state machine は `Idle` のまま留まり、
そこへ `UserSpeechEnded` が届いて拒否される。以降 `AiResponseReady` も
`Idle`/`Speaking` で拒否され、会話全体が崩れる。**自分の声とは無関係に、
すべてのハンズフリー発話がこの経路で失敗する。** 実機ログの
`Rejected UserSpeechEnded in Idle` / `Rejected AiResponseReady in Idle` がこれ。

**直し方**: follow-up が録音を開始する時点で、頭タッチ経路と同じく
`UserSpeechStarted` を通知する。ただし `FollowUpTick()` は `mutex_` を保持したまま
走るので、**state manager の Notify をロック内から呼ばないこと**
(state manager は自前のロックを持つ。順序を保証していない2つのロックを
重ねると deadlock になる)。ロックの外で撃つか、フラグを立てて呼び出し側で撃つ。

### 2026-07-30 深夜の到達点 — まだ直っていない

打った手は4つ。**すべて焼いて検証済み (Hash verified)。それでもハンズフリーは動かない。**

| # | 修正 | コミット |
|---|---|---|
| 1 | 声紋モデルを起動時に読み込む (初回13秒の固まりを解消) | `9c4172d` |
| 2 | 自分の声を拾わない | `7a2b9a0` |
| 3 | ハンズフリーで `UserSpeechStarted` を撃つ | `abd92fc` |
| 4 | 誤検知で `Listening` に居座るのを解消 | `abd92fc` |

**次に見るべき最重要の手がかり**: 最後のキャプチャに
**`follow-up speech detected` が1件も出ていない**。
修正前のログは誤検知で埋まっていたので、これは #2 が効きすぎている可能性を示す。

**有力な仮説**: 状態機械が `Idle` に戻り切っていない。
#2 のガードは `speaking` なら無条件に return するので、
機械が `Speaking` に留まると**ハンズフリーが恒久的に無効化される**。
修正前のログには `Rejected AiResponseReady in Speaking` /
`Rejected SpeechStarted in Listening` が出ており、状態が壊れる経路が実在する。

**明日の最初の一手**: 会話を1往復したあと、
**状態機械がどの状態で止まっているかをログで確認する**。
`follow-up speech detected` が出ないのか、出ても弾かれるのかで切り分ける。
`GetCurrentState()` を定期的にログに出す一時的な計装を入れるのが早い。

**調査環境について**: 今夜の最大の障害は「ゲートウェイのログが読めない」ことだった。
自動起動タスク `Tachikoma Gateway` を登録済みで、**次のログオンから
`StackChanDev\logs\gateway.log` に追記される**。まずそれを使うこと。
シリアルは `capture_boot.py` (dtr/rts を False にしてから開くので実機を再起動しない)。
**このスクリプトは終了時にまとめて書き出す**ので、実行中に0バイトに見えても正常。

**閾値には触っていない**。実測値 7061〜23432 はすべて**タチコマ自身の声**で、
人の声の実測値はまだ1つも取れていない。#2 が効いた状態で人の声のRMSを測ってから
`kFollowUpSpeechRms` / `kPostSpeechCooldownMs` を調整すること。順序を逆にしないこと。

**調査の障害**: 書き込み後、シリアルキャプチャが0バイトで取れていない
(USB-Serial/JTAG がリセットで再列挙されるため)。**先にゲートウェイのログを
ファイルに出す** (自動起動タスク `Tachikoma Gateway` が
`StackChanDev\logs\gateway.log` に追記する) ようにしてから調査を再開すること。
「2回目のリクエストがゲートウェイに届いているか」が分かれ道:
届いていれば PC 側、届いていなければ実機側。
- **ブランチ名「mail」の意図が不明** (PC側リポジトリ)。コード・コミット・README の
  いずれにもメール関連の実装が無い。ダイスケに心当たりがあれば

### 取り下げた記述 (2026-07-30)

**「`ApprovalRequestStore` が存在しない。承認リレーは新規実装から」は誤りだった。**
`StackChanDev` 配下だけを探して「無い」と結論していたが、実体は**別リポジトリ**に
あり、テストも通っている:

```
C:\Users\mylit\エージェントAIプロトタイプver0.1\tachikoma_notifier\
  approval_store.py / approvals.py / permission_relay.py
  voice_approval_gate.py / manual_approval_cli.py
```

計画書v3.0の「実装済み」が正しく、CLAUDE.md の訂正のほうが間違っていた。
**承認リレーはゼロからの実装ではなく、繋ぎ込み。**
探し物が見つからないときは、まず**探した範囲**を疑うこと。

---

## 8. 参照

- **環境構築と運用手順のすべて: `README.md`** (これ1本で足りる)
- **これから作るものの要件と受け入れ条件: `REQUIREMENTS.md`** (2026-08-13 作成。R1〜R8)
- **会社セッションの記録: `firmware/docs/2026-07-28-office-session.md`**
  (自前サーバ構築・認証突破・上流コードの落とし穴・積み残し)
- 直近の詳細な作業記録: `firmware/docs/2026-07-26-session-summary.md`
- ゲートウェイの機能別ドキュメント: `firmware/gateway/README.md`
- ファーム側の運用・既知バグ: `firmware/README.md`
