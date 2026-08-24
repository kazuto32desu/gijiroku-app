#!/usr/bin/env python3
"""話者分離（speaker diarization）— venv 側で実行するサブプロセス。

usage: venv/bin/python diarize.py <audio.wav>
stdout に JSON: {"ok": true, "num_speakers": 2, "turns": [{"start": 0.0, "end": 4.2, "speaker": 1}, ...]}

モデル（models/diar/）:
- pyannote segmentation-3.0（発話区間と重なりの検出）
- 3D-Speaker ERes2Net（声の指紋 = 話者埋め込み。言語非依存）
すべてローカル処理（オフラインで動く）。
"""
import json
import os
import sys
import wave

BASE = os.path.dirname(os.path.abspath(__file__))
SEG_MODEL = os.path.join(BASE, "models", "diar", "sherpa-onnx-pyannote-segmentation-3-0", "model.onnx")
EMB_MODEL = os.path.join(BASE, "models", "diar", "embedding.onnx")
# クラスタ分割のしきい値（小=分かれやすい / 大=まとまりやすい）。0.5 前後が定石
THRESHOLD = float(os.environ.get("DIAR_THRESHOLD", "0.5"))


def fail(msg):
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    sys.exit(0)


def main():
    if len(sys.argv) < 2:
        fail("usage: diarize.py <audio.wav>")
    wav_path = sys.argv[1]
    if not (os.path.exists(SEG_MODEL) and os.path.exists(EMB_MODEL)):
        fail("diarization モデル未配置（scripts/setup_diarization.sh を実行）")
    try:
        import numpy as np
        import sherpa_onnx
    except ImportError as e:
        fail(f"venv に sherpa-onnx がありません: {e}")

    with wave.open(wav_path, "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            fail("16kHz mono のみ対応")
        pcm = w.readframes(w.getnframes())
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if len(samples) < 16000:
        fail("音声が短すぎます")

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=SEG_MODEL),
            num_threads=4,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMB_MODEL, num_threads=4),
        clustering=sherpa_onnx.FastClusteringConfig(threshold=THRESHOLD),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    sd = sherpa_onnx.OfflineSpeakerDiarization(config)
    if not sd.sample_rate == 16000:
        fail(f"unexpected sample rate: {sd.sample_rate}")
    result = sd.process(samples).sort_by_start_time()
    turns = [
        {"start": round(r.start, 2), "end": round(r.end, 2), "speaker": int(r.speaker) + 1}
        for r in result
    ]
    n = len({t["speaker"] for t in turns})
    print(json.dumps({"ok": True, "num_speakers": n, "turns": turns}, ensure_ascii=False))


if __name__ == "__main__":
    main()
