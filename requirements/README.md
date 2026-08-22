# Python 依存関係

タチコマは機能ごとに依存を分けている。最小構成なら gateway は標準ライブラリ
だけでも起動する（会話・CRM照会・音声出力キューは動く）。以下は各機能を
有効にするための追加。python 3.11 で検証（2026-08-22）。

| ファイル | 何が有効になるか |
|---|---|
| speaker.txt | 声紋・顔の話者識別（torch/librosa/opencv/numpy/scipy） |
| localstt.txt | ローカル音声認識（faster-whisper。Geminiより固有名詞に強い） |
| order.txt | モバイルオーダー（playwright + 実Chrome操作） |
| pcear.txt | 机のUSBマイク入口（sounddevice） |
| tools.txt | QRコード生成など補助 |

インストール例:
    pip install -r requirements/speaker.txt -r requirements/localstt.txt
