# MacBook Pro でのセットアップ

対象: MacBook Pro (M4 Pro, 16GB)
役割: **iOS ネイティブアプリの開発のみ**

---

## 1. なぜ Mac に全部を移さないのか

ゲートウェイは Windows 機に置いたままにする。理由は3つ。

**デバイスの接続先が焼き込まれている。** タチコマの NVS には
`http://192.168.2.120:8080/...` が書かれている。ゲートウェイを Mac に移すと
再フラッシュが必要になり、Mac の IP が変わるたびに焼き直すことになる。

**Mac はノート。** 閉じる、寝る、持ち出す、IP が変わる。ロボットが
「ノートを開いている間だけ動く」のは据え置き機に繋ぐより不便。Windows 機は
有線・常時稼働で、VOICEVOX の自動起動、モデル 54MB、API キー、記憶データが
すでに揃っている。

**Mac にしかできないのは iOS ビルドだけ。** そこに集中させる。

将来 Mac mini に移す(Phase 8)ときは話が別。そのときは据え置きになるので
ゲートウェイごと移してよい。

---

## 2. 構成

```
iPhone アプリ ──┐
                ├─→ Windows機 192.168.2.120:8080 ──→ タチコマ実機
ブラウザ ───────┘        (ゲートウェイ)
```

Mac は開発機であって、実行時の経路には入らない。アプリは LAN 越しに
Windows 機のゲートウェイを直接叩く。

---

## 3. 入れるもの / 入れないもの

| 必要 | 不要 |
|---|---|
| Xcode (App Store) | ESP-IDF |
| Flutter SDK | Gemini API キー |
| CocoaPods | VOICEVOX |
| リポジトリ | models/ (54MB) |
| デバイストークン | 記憶データ |

ファームウェアのビルドと書き込みは Windows 機で続ける。ESP-IDF は
入れなくてよい。

---

## 4. 手順

```bash
# リポジトリ
git clone -b phase4-ai-gateway https://github.com/Daisuke-0409/StackChan.git
cd StackChan

# Flutter (Homebrew が入っていない場合はまず https://brew.sh)
brew install --cask flutter
flutter doctor          # 不足しているものを指示してくれる

# Xcode を App Store から入れたあと
sudo xcodebuild -license accept
sudo gem install cocoapods

cd app
flutter pub get
open ios/Runner.xcworkspace   # 署名は Xcode 上で Apple ID を設定
```

`flutter doctor` が緑になれば準備完了。

---

## 5. 疎通確認

Mac と Windows 機が同じルータ配下にいれば、USB 接続の有無は関係ない
(充電用の USB-C はネットワークを作らない)。

```bash
curl http://192.168.2.120:8080/health
# {"ok": true, "provider": "gemini"}
```

これが返れば、アプリから叩ける。返らなければ Windows 側の
`run_gateway.ps1` が動いているか、IP が変わっていないかを確認する
(起動時に現在の LAN IP を表示する)。

---

## 6. アプリが使う API

すべて `Authorization: Bearer <DEVICE_TOKEN>` が必要。
詳細は `firmware/gateway/README.md`。

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/v1/settings` | 設定のスキーマと現在値 |
| PUT | `/v1/settings` | 設定の部分更新 |
| GET | `/v1/people` | 覚えている人の一覧 |
| POST | `/v1/people` | 役割の変更 / 削除 |
| GET | `/health` | 疎通確認 |

**画面はスキーマから組み立てること。** `/v1/settings` が返す
`schema.groups[].settings[]` には type/label/help/default/options が入って
いる。ここから UI を生成しておけば、サーバ側に設定を1行足すだけで
アプリを更新せずに項目が増える。項目をアプリ側にハードコードすると
その利点が消える。

既存の Web 版 (`firmware/gateway/webui.py`) が同じことをしているので、
実装の参考になる。

---

## 7. その前に

ネイティブ版に着手する前に、**Web 版 (PWA) を iPhone のホーム画面に追加して
実際に使ってみること。**

```
Safari で http://192.168.2.120:8080/ → 共有 → ホーム画面に追加
```

設定画面としてはこれで足りる可能性が高い。ネイティブに移る価値が出るのは、
通知、バックグラウンド動作、BLE 直結など PWA にできないことが要るとき。
先に使ってみてから決めるほうが早い。
