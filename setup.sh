#!/bin/bash
# 議事録レコーダー セットアップ（配布先で1回だけ実行する）
# やること: Python確認 → 話者分離ライブラリ導入 → 音声モデルDL → アプリ作成
set -uo pipefail
cd "$(dirname "$0")"
echo "=============================================="
echo " 議事録レコーダー セットアップ"
echo "=============================================="
echo

fail() { echo; echo "❌ $1"; echo; exit 1; }

# --- 1. Mac の確認 ---
[ "$(uname -s)" = "Darwin" ] || fail "このアプリは Mac 専用です。"
if [ "$(uname -m)" != "arm64" ]; then
  echo "⚠️  Apple シリコン（M1以降）向けに作られています。このMacでは動かない可能性があります。"
  printf "    続けますか？ [y/N]: "; read -r a; [ "$a" = "y" ] || exit 1
fi

# --- 1.5 ダウンロード由来の「隔離属性」を外す（これが残ると実行がブロックされる） ---
xattr -dr com.apple.quarantine . 2>/dev/null || true

# --- 2. Python を探す ---
echo "[1/4] Python を確認しています..."
PY=""
for c in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3 "$(command -v python3 2>/dev/null)"; do
  [ -n "$c" ] && [ -x "$c" ] && "$c" -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" 2>/dev/null && { PY="$c"; break; }
done
[ -n "$PY" ] || fail "Python 3.9 以上が見つかりません。
   https://www.python.org/downloads/macos/ から最新版をダウンロードし、
   インストーラを実行してから、もう一度このセットアップを実行してください。"
echo "      使用する Python: $PY ($("$PY" --version 2>&1))"

# --- 3. 話者分離（誰が話したかの判別）の準備 ---
echo "[2/4] 話者分離ライブラリを準備しています（1〜3分）..."
if [ ! -x venv/bin/python ]; then "$PY" -m venv venv || fail "venv の作成に失敗しました。"; fi
venv/bin/pip install --quiet --disable-pip-version-check sherpa-onnx numpy 2>&1 | grep -vE "^\s*$|WARNING: You are using pip" || true
if venv/bin/python -c "import sherpa_onnx" 2>/dev/null; then
  echo "      OK"
else
  echo "      ⚠️ 失敗しました。話者分離（スピーカー1/2の判別）だけ使えませんが、他の機能は動きます。"
fi

# --- 4. 音声モデルのダウンロード ---
echo "[3/4] 音声認識モデルをダウンロードしています（約630MB・回線により5〜20分）..."
mkdir -p models/diar
if [ ! -f models/ggml-large-v3-turbo-q5_0.bin ]; then
  curl -L --progress-bar -o models/ggml-large-v3-turbo-q5_0.bin \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin" \
    || fail "モデルのダウンロードに失敗しました。ネット接続を確認して再実行してください。"
else echo "      文字起こしモデル: 取得済み"; fi
if [ ! -f models/diar/sherpa-onnx-pyannote-segmentation-3-0/model.onnx ]; then
  curl -sL -o models/diar/seg.tar.bz2 "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2" \
    && tar xjf models/diar/seg.tar.bz2 -C models/diar/ && rm -f models/diar/seg.tar.bz2
fi
if [ ! -f models/diar/embedding.onnx ]; then
  curl -sfL -o models/diar/embedding.onnx "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx" \
   || curl -sfL -o models/diar/embedding.onnx "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recognition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx" || true
fi
[ -f models/ggml-large-v3-turbo-q5_0.bin ] || fail "文字起こしモデルがありません。"
echo "      OK"

# --- 5. アプリを作る（このMac上で作るので警告なしに起動できる） ---
echo "[4/4] アプリを作成しています..."
PYTHON_BIN="$PY" ./scripts/make_launcher.sh > /dev/null 2>&1 || fail "アプリの作成に失敗しました。"
echo "      OK"

# --- 6. 議事録AI（claude）の確認 ---
echo
if command -v claude > /dev/null 2>&1; then
  echo "✅ 議事録の自動生成: 使えます（claude を検出）"
else
  echo "⚠️  議事録の自動生成には Claude Code が必要です（未検出）"
  echo "    録音・文字起こし・話者分離はこのままでも使えます。"
fi
echo
echo "=============================================="
echo " 完了しました"
echo "=============================================="
echo "「議事録レコーダー」アプリをダブルクリックで起動できます。"
echo "Dock に入れておくと便利です（アイコンを Dock にドラッグ）。"
echo
open -R "議事録レコーダー.app" 2>/dev/null || true
