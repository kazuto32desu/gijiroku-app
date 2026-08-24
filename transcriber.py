"""whisper.cpp ラッパー — PCM(int16 / 16kHz / mono) を日本語テキストにする。

完全ローカル処理（オフラインで動く）。モデルは models/ 配下の ggml を使う。
辞書は voice-input/dictionary.yaml の正式表記を initial prompt に注入して表記を安定させる。
"""
import os
import re
import shutil
import subprocess
import tempfile
import wave

import dictionary

BASE = os.path.dirname(os.path.abspath(__file__))
MODEL_CANDIDATES = [
    "ggml-large-v3-turbo-q5_0.bin",
    "ggml-medium-q5_0.bin",
    "ggml-small.bin",
]
# 文字起こしエンジンは (1)環境変数 (2)同梱の bin/ (3)Homebrew の順に探す。
# 同梱版を使えば配布先に Homebrew が無くても動く（bin/ は scripts/bundle_whisper.sh が作る）。
BUNDLED_BIN = os.path.join(BASE, "bin", "whisper-cli")
WHISPER_BIN = (
    os.environ.get("WHISPER_BIN")
    or (BUNDLED_BIN if os.path.exists(BUNDLED_BIN) else None)
    or shutil.which("whisper-cli")
    or "/opt/homebrew/bin/whisper-cli"
)
SR = 16000
SILENCE_RMS = 90  # int16 RMS がこれ未満なら無音チャンク扱い（≈ -51dBFS）

# whisper が無音・雑音時に出しがちな幻聴フレーズ（YouTube 学習データ由来）
HALLUCINATION_PATTERNS = [
    "ご視聴ありがとうございました",
    "ご清聴ありがとうございました",
    "チャンネル登録",
    "最後までご覧いただき",
    "字幕は自動生成",
    "次の動画でお会いしましょう",
    "MBCニュース",
]


def model_path():
    for name in MODEL_CANDIDATES:
        p = os.path.join(BASE, "models", name)
        if os.path.exists(p):
            return p
    return None


# 用語辞書は dictionary モジュールが管理する（UI から編集でき、保存後は即反映される）


def rms_int16(pcm: bytes) -> float:
    """間引きサンプリングで int16 RMS を概算（依存ゼロ・十分高速）。"""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    mv = memoryview(pcm)
    step = max(1, n // 4000)
    total = 0
    cnt = 0
    for i in range(0, n, step):
        v = int.from_bytes(mv[2 * i : 2 * i + 2], "little", signed=True)
        total += v * v
        cnt += 1
    return (total / cnt) ** 0.5


def _clean_line(text: str) -> str:
    text = re.sub(r"\(.*?\)|（.*?）|♪+", "", text)  # (笑) [音楽] 等の非発話タグ
    text = text.strip()
    if any(h in text for h in HALLUCINATION_PATTERNS):
        return ""
    if re.fullmatch(r"[んうあおおーぁ、。．…\s]+", text):
        return ""  # 「んんんん」「うーん」等、環境音の幻聴・意味のない相槌だけの行
    text = re.sub(r"(.{2,12})\1{3,}", r"\1\1", text)  # デコーダのループ癖（同語繰り返し）を圧縮
    return text.strip()


# 例: [00:00:03.240 --> 00:00:07.800]   こんにちは
_TS_LINE = re.compile(
    r"\[(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\]\s*(.*)"
)


def _parse_segments(stdout: str):
    """whisper-cli のタイムスタンプ付き出力を [(rel_t0, rel_t1, text), ...] に変換する。"""
    segs = []
    for ln in stdout.splitlines():
        m = _TS_LINE.match(ln.strip())
        if not m:
            continue
        g = [int(x) for x in m.groups()[:8]]
        t0 = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        t1 = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        text = _clean_line(re.sub(r"\[.*?\]", "", m.group(9)))
        if text:
            segs.append((t0, t1, text))
    return segs


def transcribe_segments(pcm: bytes, context_tail: str = "", timeout: int = 180):
    """PCM チャンクを文字起こしし、チャンク内相対時刻付きセグメント [(t0, t1, text), ...] を返す。
    無音・失敗時は []（呼び出し側は続行）。"""
    model = model_path()
    if model is None:
        raise RuntimeError("whisper モデルが models/ にありません")
    if rms_int16(pcm) < SILENCE_RMS:
        return []
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm)
        prompt = "日本語のビジネス会議。用語: " + "、".join(dictionary.terms()[:40])
        if context_tail:
            prompt += "。直前の発言: " + context_tail[-120:]
        cmd = [
            WHISPER_BIN,
            "-m", model,
            "-l", "ja",
            "-t", "4",
            "-bs", "3",
            "--no-prints",
            "--prompt", prompt,
            "-f", path,
        ]
        env = dict(os.environ)
        if WHISPER_BIN == BUNDLED_BIN:
            env["GGML_BACKEND_PATH"] = os.path.dirname(BUNDLED_BIN)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode != 0:
            return []
        return _parse_segments(r.stdout or "")
    except subprocess.TimeoutExpired:
        return []
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def health() -> dict:
    return {
        "whisper_bin": WHISPER_BIN if os.path.exists(WHISPER_BIN) else None,
        "model": model_path(),
        "dict_terms": len(dictionary.terms()),
    }
