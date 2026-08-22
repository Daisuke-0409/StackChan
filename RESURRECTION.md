# 復活の書 — このPCが消えても、ここから同じタチコマを建てる

データが丸ごと飛んだ前提で、ゼロから再建する手順。所要 半日〜1日。
**秘密（APIキー・トークン）だけは別途バックアップから。在り処は各所に明記。**

前提知識は PHILOSOPHY.md（設計思想）と CLAUDE.md（実装の現在地）。
この3枚 + git リポジトリ があれば作り直せる。

---

## 0. 秘密のバックアップ在り処（最重要・これだけは事前に守る）

git には入っていない、失うと再取得が要るもの:

| 秘密 | 再取得 / バックアップ |
|---|---|
| `firmware/gateway/.env` | 手動バックアップから。無ければ各値を再発行(下記) |
| `firmware/gateway/memory/*.json` | 会話履歴・声紋・人物。**Z:\パーソナルフォルダ\タチコマ\記憶** にも回収済み |
| Gemini APIキー | Google AI Studio で再発行 |
| DEVICE_TOKEN | ESP32の `build/CMakeCache.txt` の TACHIKOMA_DEVICE_TOKEN。焼き直すなら新規でよい |
| CRM_RELAY_TOKEN | 会社PCの `.env.crm_relay`。setup_crm_relay_token.ps1 で運ぶ |
| ブラウザプロファイル(スタバ/モスのログイン) | 再ログインすればよい。orderagent/data/ は再生成される |

**推奨: `.env` と `memory/` を定期的に NAS か暗号化USBへ。** 監査(2026-08-22)で
「バックアップが informal」と指摘された唯一残る穴。

---

## 1. OS・前提ソフト

- Windows 11。Python 3.11 (`C:\Users\<user>\AppData\Local\Programs\Python\Python311`)
- Git、Tailscale、Google Chrome
- ESP-IDF v5.5.x (実機を焼く場合のみ。焼かないなら不要)

## 2. リポジトリ

    git clone <repo> StackChan
    cd StackChan

## 3. Python 依存

    pip install -r requirements/speaker.txt    # 声紋・顔
    pip install -r requirements/localstt.txt   # ローカル音声認識
    pip install -r requirements/order.txt      # モバイルオーダー
    python -m playwright install chromium       # order の初回のみ
    pip install -r requirements/pcear.txt requirements/tools.txt

## 4. 設定

    copy firmware\gateway\.env.example firmware\gateway\.env
    # .env を編集。各キーの意味と在り処は .env.example の注釈にある
    copy firmware\gateway\.env.local_stt.example firmware\gateway\.env.local_stt

## 5. VOICEVOX

VOICEVOX Engine を入れて 50021 で起動する状態にする（音声合成）。

## 6. ★静的IP（これを忘れると機体が無言になる）★

実機は焼き込まれた `192.168.2.120:8080` を叩く。**このPCのLAN IPを
192.168.2.120 に固定する**（管理者権限）。DHCP任せにするとIPが変わり、
機体が誰もいない住所をノックし続ける（2026-08-21に丸1日無言になった実例）。

    # イーサネットアダプタ名は環境依存。管理者PowerShellで:
    New-NetIPAddress -InterfaceAlias '<アダプタ>' -IPAddress 192.168.2.120 `
        -PrefixLength 24 -DefaultGateway 192.168.2.1
    Set-DnsClientServerAddress -InterfaceAlias '<アダプタ>' -ServerAddresses 192.168.2.1

## 7. 自動起動タスク（onlogon）

以下を登録（Enable-ScheduledTask で有効化。schtasks /enable は効かない事例あり）:

- Tachikoma Gateway → firmware\gatewayun_gateway.ps1
- VOICEVOX Engine (Tachikoma)
- Tachikoma Order Agent → orderagentun_orderagent.ps1
- Tachikoma Local STT → firmware\gatewayun_local_stt.ps1
- Even Terminal (Tachikoma) / Even Terminal Codex (Tachikoma)
- **Tachikoma Health Check** → self_check.ps1 -Repair を5分毎
  （onlogonでなく繰り返しトリガ。他タスクの死活監視と自動再起動。
  これだけは最初に登録すると、残りの起動忘れも拾ってくれる）
- (会社PCのみ) Tachikoma Forwarder / Tachikoma CRM Relay

## 8. Tailscale serve（メガネ・スマホ・外出先アクセス）

    tailscale serve --bg https 443 8080

証明書ドメインは oo.tail20a9df.ts.net。再起動後も生きるか要確認。

## 9. 実機（焼く場合のみ）

- 16MBフルバックアップを先に取る（16,777,216バイトちょうど検証）
- managed_components の codec クラッシュ修正: `firmware/patches/apply_codec_dev_fix.py`
- ゲートウェイURL・DEVICE_TOKEN を CMakeCache に設定して焼く

## 10. 動作確認

    powershell -File NEXT.ps1          # 状態点検表
    # 「田中さんの墓所どこ?」で CRM、実機に話しかけて会話が返るか
