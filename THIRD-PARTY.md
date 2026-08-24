# 第三者ソフトウェアとモデル

本アプリは以下を利用します。配布パッケージにはバイナリを同梱しています。

| 名称 | 用途 | ライセンス |
|---|---|---|
| [whisper.cpp](https://github.com/ggerganov/whisper.cpp) | 音声認識エンジン | MIT |
| [ggml](https://github.com/ggerganov/ggml) | whisper.cpp の演算基盤 | MIT |
| [LLVM OpenMP Runtime](https://openmp.llvm.org/) (`libomp`) | 並列演算 | Apache-2.0 with LLVM Exception |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 話者分離 | Apache-2.0 |
| [Whisper large-v3-turbo](https://huggingface.co/ggerganov/whisper.cpp) (ggml 量子化版) | 音声認識モデル | MIT |
| [pyannote segmentation 3.0](https://huggingface.co/pyannote/segmentation-3.0)（sherpa-onnx 変換版） | 発話区間の検出 | MIT |
| [3D-Speaker ERes2Net](https://github.com/modelscope/3D-Speaker)（sherpa-onnx 変換版） | 話者埋め込み | Apache-2.0 |
