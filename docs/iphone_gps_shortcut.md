# iPhoneショートカット: 現在地をタチコマに送る（R9 Phase 0）

ゲートウェイの `/v1/location` に現在地を送ると、モバイルオーダーの店舗検索が
「今いる場所の最寄り」で動く。送っていない場合は既定店舗
（`ORDER_DEFAULT_STORE_KEY`、初期値 45520 = マクドナルド１０号高鍋店）に
フォールバックする。

## ショートカットの作り方（1回だけ）

1. ショートカットApp → 新規ショートカット
2. アクション「現在の位置情報を取得」を追加
3. アクション「URLの内容を取得」を追加し、以下を設定:
   - URL: `https://oo.tail20a9df.ts.net/v1/location`
   - 方法: POST
   - ヘッダ: `Authorization` = `Bearer <DEVICE_TOKEN>`
     （`firmware/gateway/.env` の DEVICE_TOKEN の値）
   - 本文の要求: JSON
     - `lat` = 位置情報の「緯度」（変数として挿入）
     - `lng` = 位置情報の「経度」（変数として挿入）
4. 名前を「タチコマに現在地」にして保存

iPhoneはTailscaleに入っているので、外出先からでも届く。

## 使い方

- 注文の前に一度実行するだけ（Siriに「タチコマに現在地」でも可）
- オートメーション（時刻・場所トリガ）で自動送信にしてもよい
- 位置は**メモリ上に最新1件だけ**保持され、1時間で失効・再起動で消える。
  ディスクには書かない

## 動作確認

```bash
curl -s -H "Authorization: Bearer $DEVICE_TOKEN" https://oo.tail20a9df.ts.net/v1/location
```

`{"location": {"lat": ..., "lng": ..., "ts": ...}}` が返れば届いている。
