# 議事録レコーダー（gijiroku-app）

会議を録音し、**文字起こし → 話者分離 → 議事録の自動生成** までを Mac の中だけで行うローカルアプリです。
音声はこの Mac の外に出ません（議事録の文章化のみ Claude にテキストを送ります）。

- 文字起こし: [whisper.cpp](https://github.com/ggerganov/whisper.cpp)（ローカル・時間無制限・オフライン可）
- 話者分離: [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)（ローカル・「スピーカー1／2」を自動判別）
- 議事録の構造化: [Claude Code](https://claude.com/claude-code) CLI

出力は 3 種類です。Notion にそのまま貼れる Markdown、相手に送れる自己完結 HTML、時刻と話者つきの文字起こし全文。

## 動作環境

- Apple シリコンの Mac（macOS 12 以降）
- Python は macOS 標準のものを使います（追加インストール不要）
- 議事録の自動生成には Claude Code（Max または Pro）が必要です。無くても録音・文字起こし・話者分離は動きます

## セットアップ

```bash
./setup.sh
```

Python の検出、話者分離ライブラリ（venv）の構築、音声モデル（約630MB）のダウンロード、ランチャーアプリの生成までを行います。
文字起こしエンジン（whisper.cpp）は配布パッケージに同梱していますが、このリポジトリには含めていません。
自分でビルドする場合は `brew install whisper-cpp` のうえ `scripts/bundle_whisper.sh` を実行してください。

## 更新

アプリの画面に新しい版が出たら、ボタンひとつで更新できます。
録音データ・音声モデル・用語辞書はそのまま残り、プログラムだけが入れ替わります。

## 主なファイル

| ファイル | 役割 |
|---|---|
| `server.py` | ローカルサーバー（Python 標準ライブラリのみ） |
| `transcriber.py` | whisper.cpp の呼び出し・整形 |
| `diarize.py` | 話者分離（venv 側で実行） |
| `summarizer.py` | Claude CLI で議事録に構造化 |
| `html_export.py` | 共有用 HTML の生成 |
| `dictionary.py` | 用語辞書の読み書き |
| `static/` | 画面（HTML / CSS / JS） |

## ライセンス

このリポジトリのコードは MIT ライセンスです。同梱・利用する第三者ソフトウェアは [THIRD-PARTY.md](THIRD-PARTY.md) を参照してください。
