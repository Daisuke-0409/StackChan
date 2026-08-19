# R9 再設計 — STEP 1 現状調査

調査日: 2026-08-19。**コード変更なし。**再設計仕様書に対する現物の対応表。
同じ調査を二度しないための記録 (§37-5)。根拠は行番号で示す。

---

## 1. 現在の R9 は「1発話 = 1注文」の一撃型

```
発話1つ → intent.detect() → OrderIntent(chain, item_text, quantity, pickup)
       → POST /jobs → _build() が一気通貫:
          店舗選択 → メニュー取得 → 商品マッチ → Playwrightでカート構築
          → カートをスクレイプ → 承認候補を作る → 読み上げ → awaiting_approval
       → 「注文して」 → payment.verify_cart() → execute()
```

`intent.detect()` (`orderagent/intent.py:63`) は**発話1つを完全な注文として解釈するか
None を返すか**の二択。会話の途中経過という概念が無い。

`order_bridge.intercept()` (`firmware/gateway/order_bridge.py:60`) は
`awaiting_approval` 中の発話を承認/拒否/素通しの3つに振り分けるだけで、
**注文内容を変える発話の行き先が存在しない**。

## 2. 仕様書の各層に対する充足度

| 仕様書の層 | 現物 | 充足 |
|---|---|---|
| 5. Location Resolver | `server.py:676-695` に `_last_location` + `ts`、`get_last_location(max_age_seconds=3600)` | **ほぼ済**。時刻は持っている。閾値が 3600秒で §5-1 の 300秒 と乖離、env 化もされていない (`server.py:1620` が既定値で呼ぶ) |
| 6. 店舗検索 | `stores.nearest()` (直線距離)、`store_detail().mopEnabled` で注文可否、`congestion.py` で時間帯推定 | **部分**。距離のみ。移動時間なし。Route Provider の差し替え口なし |
| 7. Capabilities | `mopEnabled` と `cart.pickup_options` のみ | **なし**。FULL_AUTO/ORDER_ONLY/MENU_ONLY の区別なし |
| 8. Restaurant Adapter | `mcd_adapter.py` (Playwright)、ただし `stores.py` もマック専用 | **部分**。下記 競合A |
| 10. Order Draft | — | **なし。今回の本体** |
| 11. 操作の正規化 | — | **なし** |
| 12. 訂正表現 | — | **なし** |
| 13. active_item | — | **なし** |
| 14. 代名詞 | — | **なし** |
| 17. 条件付き注文 | — | **なし** |
| 18. 商品名解釈 | `intent.match_menu()` + `ai_match.py` (Gemini が実メニューから推測、読み上げに解釈を前置) | **済**。仕様書の要求どおり実メニュー内から選んでいる |
| 20. Resolved Order | `_build()` が実カートをスクレイプして確定 | **実質済**。名前が違うだけ |
| 21. Quote | `_build()` の `summary` (`server.py:209-228`)。**カート実物から生成**、推測値ではない | **済** |
| 22. Approval Snapshot | `approved_candidate` = expected_total_yen / item_count / items[] (`server.py:187`) | **ほぼ済**。ハッシュは無いが等価の突合はしている |
| 23. 状態機械 | created/building/awaiting_approval/verifying/dry_run_done/paid/needs_info/failed/escalated/denied/expired | **済**。名前が違う。下記 競合C |
| 24. 承認語 | 「注文」必須 (`order_bridge.py:31`)。単独の「はい」は通らない | **済**。仕様書 §24 も当面維持を指示 |
| 25. Snapshot 再照合 | `payment.verify_cart()` が合計・点数・明細を突合し、不一致で PaymentRefused | **済** |
| 26. 決済3値 | `payment_uncertain` の概念はドキュメントにあるが、状態としては `escalated` 止まり | **部分**。UNKNOWN の明示と `reconcile_order()` が無い |
| 28. 既存安全ルール | ドライラン既定 / 金額上限 / 突合 / リトライ禁止 / CAPTCHA停止 | **済** |
| 29. CAPTCHA | `_check_for_captcha()` (`mcd_adapter.py:37`) | **済** |

**結論**: 仕様書の後半 (20〜29、店舗〜決済) は**ほぼ出来ている**。
欠けているのは前半 — **会話から注文を1つに収束させる部分 (10〜19)** がまるごと無い。

## 3. 流用できるもの (新規実装しない)

| ファイル | 役割 | 新設計での位置 |
|---|---|---|
| `payment.py` | 承認内容と実カートの突合、ドライラン強制 | §25/§28 をすでに満たす。**触らない** |
| `db.py` | 全遷移の監査ログ | §26/§27 の reconcile の土台。**触らない** |
| `ai_match.py` | 通称→実メニューの候補解決 (Gemini) | §18。Order Interpreter から呼ぶ |
| `intent.match_menu()` | メニュー候補のランキング | §18 の前段。そのまま |
| `stores.py` | 店舗検索・メニュー取得 | §6。当面 mcd_adapter の内部として扱う (競合A) |
| `congestion.py` | 時間帯からの混雑推定 | §6 の順位付け材料 |
| `mcd_adapter.py` | Playwright カート構築・スクレイプ | §8 の1社目 |
| `order_bridge.py` | 落ちていても通常会話に影響しない橋渡し | §1。思想ごと維持 |

## 4. 競合 (§43 の形式で)

### 競合A: `stores.py` がチェーン非依存の名前でマック専用

- **何と何が**: §8「チェーン固有処理は Adapter へ閉じ込める」/ §9「特定URLは Adapter 内部だけ」 と、
  `stores.py:25-26` の `POI_URL = "https://map.mcdonalds.co.jp/api/poi"` /
  `ORDER_PAGE = "https://www.mcdonalds.co.jp/order/{key}"`
- **なぜ**: 汎用名のモジュールがマックの内部APIを直接持っているため、
  共通エンジンが `stores.nearest()` を呼ぶと、それはマックを呼んだことになる
- **既存を変更せず回避**: 新しい共通エンジンは `stores.py` を直接呼ばない。
  Adapter 経由でのみ触る。`stores.py` は当面「mcd_adapter の一部」と読み替える
- **最小変更案**: 改名・移動は **STEP 12 (2社目追加) まで遅らせる**。
  2社目を入れる時に初めて、何が本当に共通かが分かる。今動かすのは損

### 競合B: `DEFAULT_CHAIN = "mcd"` と §41 の理想UX

- **何と何が**: §41「腹減った → マック6分・KFC9分・スタバ11分、どれにする？」 と
  `intent.py:57` の `DEFAULT_CHAIN = "mcd"`、および `_IMPERATIVE_ORDER_RE`
- **なぜ**: 「腹減った」は現在**そもそも注文意図として検出されない** (命令形でもチェーン名でもない)。
  検出されたとしても既定でマックに倒れる
- **回避**: 変更しない。**2社目の Adapter が無い状態で複数チェーンを提案すると嘘になる**
- **最小変更案**: STEP 12 で 2社目が入ってから、「腹減った」系の検出と複数提案を同時に入れる

### 競合C: 状態名が仕様書と違う

- **何と何が**: §23 の `IDLE/DRAFTING/RESOLVING/QUOTE_READY/AWAITING_FINAL_APPROVAL/...` と
  現行の `created/building/awaiting_approval/verifying/...`
- **なぜ**: 名前が違うだけで意味はほぼ1対1。ただし `db.orders.status` 列、
  `order_bridge` の判定、既存テストがこの文字列に依存している
- **提案: 改名しない。** `awaiting_approval` は §23 の `AWAITING_FINAL_APPROVAL` と同義で、
  「この状態でのみ承認語を解釈する」という §23 の要件を**すでに満たしている**。
  改名は監査ログの過去行と現在行を分断するだけで、安全性は1ミリも上がらない
- **最小変更案**: `drafting` を1つ足す。それ以外はそのまま

### 競合D: 1ジョブ占有ロックと会話ドラフト

- **何と何が**: `submit_job` (`server.py:56-62`) が
  `created/building/awaiting_approval/verifying` のジョブがあれば新規を拒否する仕様と、
  数ターン続く会話ドラフト
- **なぜ**: ドラフトを「ジョブ」として作ると、会話している数分間ずっとエージェントが占有される。
  ドラフト中は Playwright を使わないのに、使うジョブと同じロックを取ることになる
- **最小変更案**: **ドラフトは job を作らない。**別の器 (draft) として持ち、
  `build_cart` に進む時点で初めて従来の job を作る。既存のロック意味論は無傷

### 競合E: `order_bridge` が注文内容の発話を運べない ← §1 に触れる唯一の箇所

- **何と何が**: §1「Gateway へ注文ロジックを大量に追加しない」 と、
  会話ドラフトには**追加発話をエージェントへ届ける経路**が要るという要求
- **なぜ**: 現在 `intercept()` は `awaiting_approval` 中の承認/拒否以外を `None` で通常会話へ流す
  (`order_bridge.py:99`)。ドラフト中の「あ、やっぱセット」は LLM の雑談に吸われる
- **最小変更案**: `intercept()` に「ドラフト進行中なら発話をエージェントへ転送する」分岐を**1つ**足す。
  判断はエージェント側 (`/draft/utter` が解釈して返答文を返す)。
  **Gateway 側にロジックは置かない** — 転送と返答の受け渡しだけ。§1 の思想は保たれる
- **これは STEP 4 以降に必要になる。STEP 2/3 では不要**

### 競合F (軽微): 位置情報の鮮度しきい値

- `get_last_location()` の既定 3600秒 に対し §5-1 は 300秒 程度を要求
- 機構は既にあるので、**呼び出し側で env 化する数行**で済む。独立した小コミットで足りる

---

## 5. 次の STEP

**STEP 2: Order Draft のデータモデル追加。Playwright には触らない。**

- 触るファイル: **`orderagent/draft.py` (新規) と `orderagent/tests/test_draft.py` (新規) のみ**
- 触らないファイル: `server.py` / `mcd_adapter.py` / `payment.py` / `order_bridge.py` / `intent.py`
- 受け入れ条件:
  - `OrderDraft` が §10 の形 (restaurant / fulfillment / items[] / 各 item の id・product・variant・size・quantity・options・status) を表現できる
  - 純粋なデータ構造とその操作のみ。ネットワーク・Playwright・LLM を呼ばない
  - 既存テスト (`orderagent/tests/`) が全て通ったまま
  - 新規ユニットテストが通る
