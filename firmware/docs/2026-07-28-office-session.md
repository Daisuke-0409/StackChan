# 2026-07-28 会社セッション記録

会社の PC (`192.168.11.200`) と会社の実機 (MAC `80:45:6B:4D:E7:AC`) での作業。
家で続きをやるための引き継ぎメモ。

---

## 1. いちばん大きな成果

**M5Stack アプリ連携の認証を、自前実装で置き換えて突破した。**

`hal/utils/secret_logic/secret_logic.cpp` の `generate_auth_token()` は、上流では
`__attribute__((weak))` のスタブで文字列 `"hi-stack-chan"` を返すだけだった。本物は
非公開バイナリ側にあり、このリポジトリには入っていない。だからソースからビルドした
ファームは M5Stack のリレー (`47.113.125.164:12800`) に弾かれ、Avatar アプリは
`Connecting to server...` から進まなかった。**設定ミスでも、タチコマ改造の副作用でもない。**

隠されていたのは方式ではなく実装だった。`server/README.MD` 8.1 に手順が書かれ、
`server/utility/rsa.go` に検証側がある:

```
plain  = "<MAC>|<nonce>|<unix seconds>"     3要素必須
cipher = RSA-OAEP(SHA-256 digest, SHA-256 MGF1, label なし, サーバ公開鍵)
header = Base64(cipher)                     2048bit なら 344 文字
```

これを mbedtls (`MBEDTLS_RSA_PKCS_V21` + `MBEDTLS_MD_SHA256`) で実装し、自前の鍵と
自前サーバに対して成立させた。実機ログ:

```
[secret_logic] auth token built for 80:45:6B:4D:E7:AC (344 chars)
I (10848) WS-Avatar: Connected to server!
```

344 文字は `openssl` で作った参照トークンと一致。**カメラ映像も中国のサーバを経由せず
ブラウザまで届いた** (67 フレーム / 365 KB を確認)。

### 時刻の要件

`internal/web_socket/web_socket.go` はタイムスタンプがサーバ時刻と `±10 秒` を超えると
拒否する。デバイス時計が SNTP で同期していないと、実装が正しくても永久に 401 になる。
`generate_auth_token()` は `ts < 1700000000` を検出したらトークンを作らずログに出す。

---

## 2. 会社側に構築したもの (リポジトリ外)

家では作り直しになる。**秘密鍵と資格情報はリポジトリに入れていない。**

| 何 | どこ |
|---|---|
| Go 1.26.3 | `C:\Users\user\tachikoma\go` (ZIP 展開、管理者権限不要) |
| MariaDB 11.4.4 | `C:\Users\user\tachikoma\mariadb-11.4.4-winx64` + `mariadb-data` |
| サーバ実行ファイル | `C:\Users\user\tachikoma\stackchan-server.exe` |
| **サーバ設定 (秘密鍵入り)** | `C:\Users\user\tachikoma\server-config\config.yaml` |
| RSA 鍵 4 本 | `C:\Users\user\tachikoma\keys\` |
| 実行ディレクトリ | `C:\Users\user\tachikoma\server-run\` |
| ログ | `C:\Users\user\tachikoma\logs\` |

`config.yaml` をリポジトリ外に置いたのは、`server/manifest/config/config.yaml` が
**追跡ファイル**で、そこに秘密鍵を書くと誤コミットが常に起こりうるため。GoFrame には
`GF_GCFG_PATH` で参照させている。

### MySQL ではなく MariaDB

Oracle のダウンロードが 403 で自動取得できなかった。`check_list/create_mysql_database.sql`
は 8 テーブル 5.3KB で `utf8mb4_0900_ai_ci` も `JSON_TABLE` も使っておらず、`ENGINE=` 指定
すら無いため MariaDB で問題なく通る。接続文字列の collation は `utf8mb4_general_ci`。

### 自動起動 (タスクスケジューラ、ログオン時)

```
MariaDB (Tachikoma)             ポート 3306 を見て二重起動を防ぐ
StackChan Server (Tachikoma)    MariaDB を最大 40 秒待つ。GF_GCFG_PATH が必要
VOICEVOX Engine (Tachikoma)     ポート 50021
Tachikoma Gateway               VOICEVOX を最大 30 秒待つ
```

ゲートウェイ側は venv を PATH 先頭に置く必要がある。`run_gateway.ps1` は素の `python` を
呼ぶが、このPCではそれが **ESP-IDF 同梱の Python** に解決され、torch/librosa/opencv が
入っていない。

---

## 3. 上流コードで見つけた問題

### 3.1 カメラ購読は「通話」経由でしか成立しない (仕様)

`readAppClientMessage` の `case OnCamera` は **ペイロード全体を MAC 文字列として読む**:

```go
case OnCamera:
    macAddr := string(payload)
    stackChanClient := getStackChanClient(macAddr)
```

空ペイロードで送ると `getStackChanClient("")` になり黙って捨てられる。ブラウザ側は
MAC (コロン区切り 17 文字) をペイロードに入れること。

### 3.2 `ControlMotion` は App から使えない

`ControlMotion` / `ControlAvatar` / `RequestCall` / `Opus` は `payload[:12]` を MAC として
読むが、クライアントプールのキーはトークン由来の**コロン区切り 17 文字**。12 バイト切り出しは
決して一致しない。

回避策として、首の移動は `Dance` (0x14) で行っている。`Dance` は
`getStackChanClient(client.GetMac())` で正しくルーティングされ、キーフレームに
`yawServo` / `pitchServo` (`{angle, speed}`、角度は 1/10 度) を持てるので、
**1 キーフレームのダンス = サーボ移動**として使える。

### 3.3 修正済みのバグ

`GetMac()` が `len(parts) < 2` で弾きながら `parts[2]` を読んでいた。ちょうど 2 要素の
トークンでハンドラがパニックする。`< 3` に修正。

---

## 4. ブラウザ操作ページ

`server/web/management/tachikoma.html`。スマホの公式アプリの代わり。

- WebCrypto (`RSA-OAEP` + `SHA-256`) でトークンを自分で作る。ファームと同じ構成
- WebSocket は**カスタムヘッダを送れない**ため、トークンはクエリパラメータ `token=` で渡す。
  そのため `GetMac()` にヘッダが無ければクエリを見るフォールバックを足した。URL に credential
  を載せるのは通常は悪手だが、このトークンは 10 秒で失効する
- **`http://<IP>` は secure context ではないので `crypto.subtle` が使えない。**
  PC の `localhost` では動くが、**スマホから IP で開くと動かない。**
  → Tailscale の HTTPS で解決する予定 (未完)

**注意**: ページには会社の鍵ペアの公開鍵が埋まっている。家では家の鍵に差し替えること。

---

## 5. 実機ファームの変更

| ファイル | 内容 |
|---|---|
| `secret_logic.cpp` | 認証トークンの本実装 (上記) |
| `camera_guard.h/.cpp` | `std::mutex` → `std::timed_mutex`、`kCameraLockTimeoutMs = 300` |
| `face_tracker.cpp` | カメラロックを `try_lock_for` に。**`kFaceTrackingMovesHead = false`** |
| `hal_ws_avatar.cpp` | カメラロックを `try_lock_for` に |
| `gateway/server.py` | `TACHIKOMA_SAVE_VISION_FRAMES=1` で最新フレームを保存 (既定 OFF) |

### 顔追従が首を動かさなくなっている理由 (重要)

タッチセンサは動く部分に付いているため、首を振ると誤って「押された」と判定される。
`kExpressiveMotionGuardMs = 600` のガードは動作を**要求した**時点から数えるが、
`kLookSpeed = 400` の動きはその後も揺れており、ring-down がガードを抜けて指として読まれる。
結果が自己増殖する: 誤検知が会話を開始 → 会話中だから顔追従が首を振る → 次の誤検知。
**電話中に勝手に話しかけてくる**という実害が出た。

サーボ動作を止めて物理的な原因を断った。フレームのキャプチャと `/v1/vision` への送信は
そのままなので、顔認識と話者識別は影響を受けない。

**再有効化の条件**: タッチセンサで「押下」と「振動」を区別できるようにすること
(押され続けた時間のしきい値が素直な解)。それまでは、人が触っていないのに喋ってはいけない。

---

## 6. 積み残し

| 項目 | 状態 |
|---|---|
| Tailscale | インストール済み (1.98.9)。**ログイン未完了**。`tailscale up` で出る URL をブラウザで開く必要がある |
| スマホからの閲覧 | Tailscale + HTTPS 化が前提 (secure context 問題) |
| ダンス・モーション | ページから送信できるが、**実機が動いたかの目視確認が未了** |
| CRM 連携 | 設計のみ。`C:\Users\user\ダイボ石材_CRMプロトタイプ` (Python + SQLite, ポート 8765)。ゲートウェイが SQLite を直接読めば顧客データを Gemini に渡さずに済む |
| ハンズフリー継続 | `kFollowUpWindowMs = 30000` は要求どおり。ただし `kFollowUpSpeechRms = 1400` の実測が未了 |
| USB 接続 | 今日 3 回落ちた。データ通信対応のケーブルに替えて再確認したい |
| 資格情報のローテーション | Gemini API キーがチャットとスクリーンショットに露出。CRM の `config.json` のパスコードとセッションシークレットも表示してしまった。**両方作り直すこと** |

---

## 7. 家で再開するときに必要なもの

リポジトリに入っていないので、家では新規に用意する:

1. `firmware/gateway/.env` (`office-setup.md` 3 章の手順)
2. RSA 鍵ペア (`openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048`)
3. `config.yaml` をリポジトリ外に作り `GF_GCFG_PATH` で参照
4. **`secret_logic.cpp` の `kServerPublicKeyPem` を家の公開鍵に差し替える**
5. **`tachikoma.html` の `SERVER_PUBLIC_KEY_PEM` と `MAC` を家のものに差し替える**
6. `sdkconfig` の `CONFIG_STACKCHAN_SERVER_URL` を家のサーバへ (gitignore 済みなのでコミットされない)
7. Go / MariaDB / VOICEVOX
