#!/bin/bash
# 話者分離（スピーカー1/2…の自動判別）のセットアップ。
# venv を作って sherpa-onnx を入れ、モデル 2 つ（発話区間検出＋声の指紋）を DL する。
# 再実行しても安全（済んでいる手順はスキップ）。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x venv/bin/python ]; then
  echo "[1/3] venv を作成..."
  python3 -m venv venv
fi
echo "[2/3] sherpa-onnx / numpy をインストール..."
venv/bin/pip install --quiet sherpa-onnx numpy

echo "[3/3] モデルをダウンロード..."
mkdir -p models/diar
if [ ! -f models/diar/sherpa-onnx-pyannote-segmentation-3-0/model.onnx ]; then
  curl -sL -o models/diar/seg.tar.bz2 \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
  tar xjf models/diar/seg.tar.bz2 -C models/diar/
  rm -f models/diar/seg.tar.bz2
fi
if [ ! -f models/diar/embedding.onnx ]; then
  curl -sfL -o models/diar/embedding.onnx \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx" \
  || curl -sfL -o models/diar/embedding.onnx \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recognition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
fi
echo "done. 動作確認: venv/bin/python diarize.py <16kHz mono wav>"
