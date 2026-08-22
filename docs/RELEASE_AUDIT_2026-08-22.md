# タチコマ リリース可否監査 — 2026-08-22

ソフトウェア開発会社（フロント/バック/SRE/セキュリティ/保守）として、
5名のエージェントで多角監査した結果の統合判定。詳細な個票は各担当の
指摘を本書に集約。**「使えなくなったりは論外」を最上位制約として、
移行リスクの低い順に並べた改修計画つき。**

## 総合判定: 現状は「動くプロトタイプ」。リリース不可。

理由を一言で: **壊れたときに誰も気づけず、決済系に金銭事故の穴があり、
再構築の手順が人の記憶に依存している。** 機能は十分に豊かで、設計思想
（秘密を渡さない・推測しない・記憶に残さない）は一貫して優秀。落ちて
いるのは「機能」ではなく「運用の堅牢性・安全性・再現性」の3点。

| 領域 | 判定 | 一言 |
|---|---|---|
| 機能の豊かさ | 🟢 | 会話・記憶・声紋・CRM・注文まで実装済み |
| 設計思想の一貫性 | 🟢 | privacy hard lines がコード構造で強制されている |
| 決済安全 | 🔴 | 二重承認レース・再設計エンジン未接続（下記B) |
| 運用堅牢性 | 🔴 | 再起動ポリシーゼロ・監視ゼロ・全障害を「無言」で発見 |
| セキュリティ | 🟡 | 無認証トークン配布・URL無検証・レート制限なし |
| 再現性/リリース規律 | 🔴 | バージョン無し・CI無し・requirements.txt無し |

---

## A. 即修正すべき致命傷（金銭・データ事故に直結、裏取り済み）

- **A1 [決済/裏取り済] 二重承認レース** — `orderagent/server.py:303-306`。
  `approve_job` が status を check→set する間にロックが無い
  (`_jobs_lock` は `submit_job` だけが使用)。ThreadingHTTPServer は
  リクエスト毎スレッド。`payment.execute` はブラウザ操作+sleep(8) で
  15〜20秒ブロックするため、HTTPクライアントのタイムアウト→リトライで
  同一ジョブへ approve が2本入り、両方が決済ボタンを叩きうる。
  payment.py の docstring が「絶対起きてはならない」と書いた事象そのもの。
- **A2 [バックエンド/裏取り済] crm_bridge._pending にロックが無い** —
  `firmware/gateway/crm_bridge.py:56`。server.py は他の共有dictを全て
  ロックしているのに、この1ファイルだけ破っている。同一
  (device_id, asked_by) の同時リクエストで更新が失われ、CRM画面が
  別の顧客を出す危険。
- **A3 [セキュリティ/裏取り済] 家側の画面表示がリレー返却URLを無検証で開く** —
  `crm_bridge.py:401-404` の `webbrowser.open(url)`。会社側
  (`crm_relay.py:189-207`) は host 検証しているのに家側は素通し。
  リレー/CRM が誤設定・侵害された場合、任意URLを家のPCで開く。

## B. リリース前に必須（決済を本番化する前の絶対条件）

- **B1 [決済/裏取り済] 再設計エンジンが本番未接続** — `states.py`/
  `reconcile.py` は自テストからしか呼ばれず、`server.py` は status を
  生文字列で操作。`payment_uncertain` に落ちても reconcile が走らない。
  加えて `StarbucksAdapter.recent_orders()` は常に
  `history_available=False` を返すため、結果不明時の自動確認は原理的に不可能。
  「人が注文履歴を確認」が唯一の回収経路。決済ON前に要配線。
- **B2 [決済] 決済後の状態判定が sleep(8)+1回読み** —
  `starbucks/__init__.py:495-518`。遅い成功を NOT_PLACED と誤判定し、
  ユーザーが再注文→二重課金の芽。締切までポーリングに変更。
- **B3 [決済] クラッシュでジョブが消える** — `_jobs` は純メモリ。起動時に
  sqlite から復元せず、決済中クラッシュしたジョブは 404 になり
  「未解決の決済がある」と誰も気づけない。起動時スイープが必要。
- **B4 [セキュリティ] /g2/config が無認証で DEVICE_TOKEN を配布** —
  `server.py:2045-2053`。トークンは /v1/speak・/v1/settings書込・
  /v1/people書込 まで開ける。R7ペアリングまでの既知の穴。広く配る前に要対処。
- **B5 [セキュリティ] トークン照合にレート制限なし** — `crm_relay.py`/
  `server.py _authorized`。compare_digest は使用済(良)だがtailnet越しの
  総当たりを絞る仕組みが無い。

## C. 運用堅牢性（壊れやすさの本体・SRE指摘）

- **C1 再起動ポリシーがどこにも無い。** 全プロセスが onlogon 一発起動。
  クラッシュ・0xC000013A・タスク無効化のいずれも人が気づくまで永久停止。
- **C2 ヘルス監視ゼロ。** /health・/healthz は7サービス全部に実装済み
  なのに誰も叩いていない。全障害が「ロボットが無言」で発見された根本原因。
- **C3 文字化けバグが5つの起動scriptに残存。** 8/21に
  `run_local_stt.ps1` だけ `-Encoding UTF8` で直したが、gateway/
  orderagent/crm_relay/forwarder/approval_daemon の launcher は素の
  Get-Content のまま。既知の地雷が未撤去。
- **C4 python選択ヒューリスティックが2 launcherで欠落**
  (crm_relay/forwarder は bare `python`)。過去2回踏んだ罠。
- **C5 タスク自動無効化(8/21に3回・原因不明)** — 監視基盤自体が不安定。
  recovery runbook も5タスクしか列挙しておらず不完全。

## D. リリース規律（再現性・PM指摘）

- **D1 バージョン識別・CHANGELOG・タグ・ロールバック手段が皆無。**
- **D2 requirements.txt が存在しない。** pip install が README/CLAUDE.md に散在。
- **D3 静的IP固定(192.168.2.120)が README の再構築手順に無い。**
  DHCP変化で全機体無言になった実績あり。新PCで必ず再発する。
- **D4 Geminiモデル名が4ファイルに直書き。** 1つは本番404の前科。
- **D5 テスト450件が全て手動実行。** CI/フック/自己点検タスクのいずれも無し。
- **D6 総覧ドキュメントが重複** (TACHIKOMA.md と docs/TACHIKOMA_FEATURES.md、
  テスト数が268/313/154と食い違う)。REQUIREMENTS.md の受入チェックも実態と乖離。

---

## 推奨アーキテクチャ（SRE案・採用）

**NSSM で各Pythonプロセスを真のWindowsサービス化。** 生の Task Scheduler
でも自作watchdogでもなく NSSM を選ぶ理由:
- headless サービス化で「窓を閉じて殺す(0xC000013A)」が構造的に消える
- 再起動ポリシーが標準装備（現状ゼロ）
- 8/21の「タスク勝手に無効化」は Task Scheduler 固有の不具合なので、
  依存先を SCM に移すこと自体が回避になる
- 既存の .ps1/`python -m` をそのまま exec するだけ＝書き直し不要＝低リスク
- 追加で**ヘルスポーラー1個**（50行）を常駐させ、7つの既存エンドポイントを
  15〜30秒毎に叩き、劣化を gateway の /v1/announce で音声通知する。
  「無言で発見」を「即座に通知」へ変える最小の一手。

---

## 最優先の1週間（PM案・採用）

`scripts/self_check.ps1` + `docs/RUNBOOK.md` を作り、日次タスクに載せる。
- self_check: 全ポート疎通・全タスクの Enabled 状態(Get-ScheduledTask)・
  PCのIPが焼き込みURLと一致するか・全テスト実行 → 結果をログ+音声で通知
- RUNBOOK: pip一式・静的IP・tailscale serve・.env と memory のバックアップ手順を1枚に
- これだけで運用系の D2/D3/D5/C2 と「無言障害」がまとめて閉じる

## 改修の順序（低リスク→高リスク）

1. **A1〜A3 の即修正**（ロック2箇所・URL検証1箇所。数行・テスト追加）← 本日着手
2. **C3/C4 launcher修正**（機械的・低リスク）
3. **requirements.txt + バージョン文字列 + self_check + RUNBOOK**（足すだけ）
4. **NSSM移行**（1プロセスずつ・既存script温存）
5. **B1〜B3 決済堅牢化**（決済ONの前提。エンジン配線・ポーリング・起動時復元）
6. **B4/B5 + R7ペアリング**（セキュリティの本丸）
7. server.py のモジュール分割（config.py先行→speech/speaker→provider群）
