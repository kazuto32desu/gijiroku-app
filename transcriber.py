"""whisper.cpp ラッパー — PCM(int16 / 16kHz / mono) を日本語テキストにする。

完全ローカル処理（オフラインで動く）。モデルは models/ 配下の ggml を使う。
initial prompt（文字起こしのヒント）には辞書の「用語」と会議の参加者名だけを入れる。
辞書の人名・社名は入れない — whisper は声が弱い・無い区間でヒントの語を出力しやすく、
2026-10-05 の会議では、辞書の講師名・社外の担当者名・取引先名が休憩中などに繰り返し差し込まれた。

出力の後処理（2026-10-06 追加）:
- 声の無い区間（200ms ごとの音量の最大値が小さい）から出た文字は捨てる
- 同じ文の繰り返し（デコーダのループ）は 1 回に畳み、「要確認」の印を付ける
- whisper が勝手に付ける話者名（「スタッフ:」「田中:」等）を外す
- 要確認の行は次のチャンクのヒント（直前の発言）に使わない（差し込みの連鎖を断つ）
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from array import array

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
# 200ms ごとの音量（int16 RMS）の最大値がこれ未満の区間には声が無いとみなす（≈ -35dBFS）。
# 10/5 の会議（ブラウザの自動音量調整あり）で、話し声の山はほぼ 2000 以上・無音の幻聴は 1200 以下だった
SPEECH_PEAK_MIN = int(os.environ.get("GIJIROKU_SPEECH_PEAK", "600"))
REPEAT_WINDOW_SEC = 90   # この秒数の中で
REPEAT_MIN_CHARS = 6     # この文字数以上の同じ文が
REPEAT_LIMIT = 2         # この回数を超えて出たら、3 回目以降を捨てる
TAIL_CHARS = 80          # 次のチャンクに渡す「直前の発言」の長さ
TAIL_MAX_GAP = 20        # 直前の発言からこの秒数以上あいたら、引き継がない
FLAG = "要確認"

# whisper が無音・雑音時に出しがちな幻聴フレーズ（YouTube 学習データ由来）
HALLUCINATION_PATTERNS = [
    "ご視聴ありがとうございました",
    "ご清聴ありがとうございました",
    "ご覧いただきありがとうございます",
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


def peak_rms(pcm: bytes, t0: float = 0.0, t1: float = None) -> float:
    """t0〜t1 秒の範囲で、200ms ごとの RMS の最大値を返す（その区間に声の大きさの音があったか）。"""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    n = len(samples)
    a = max(0, int(t0 * SR))
    b = n if t1 is None else min(n, int(t1 * SR))
    win = SR // 5
    best = 0.0
    for s in range(a, max(a + 1, b - win + 1), win // 2):
        part = samples[s : min(s + win, b)][::4]   # 4 サンプルに 1 つで十分
        if part:
            r = (sum(v * v for v in part) / len(part)) ** 0.5
            if r > best:
                best = r
    return best


# 行頭や文の切れ目に whisper が付ける偽の話者名（「スタッフ:」「〇〇先生:」等）。
# 話者分離はアプリ側（diarize.py）で別に付けるので、whisper 由来のラベルは全部外す（「12:15」の数字は対象外）
_FAKE_LABEL = re.compile(r"(?:^|(?<=[\s。、！？!?」』）)…:：]))[^\s。、！？!?「」『』（）()：:0-9０-９…]{1,12}[:：]\s*")


def _collapse_repeats(text: str):
    """デコーダのループ癖（同じフレーズの連続）を畳む。3 回以上の繰り返しがあれば looped=True。"""
    looped = False
    # 4〜40 字のフレーズが（空白・句読点をはさんで）3 回以上続く → 1 回に
    new = re.sub(r"(.{4,40}?)(?:[\s、。]*\1){2,}", r"\1", text)
    if new != text:
        looped, text = True, new
    # 8 字以上のフレーズの 2 連続 → 1 回に（人は同じ長い文を 2 度続けてまず言わない）
    text = re.sub(r"(.{8,40}?)[\s、。]*\1", r"\1", text)
    # 2〜12 字の 4 回以上 → 2 回（「はいはいはいはい」→「はいはい」）
    text = re.sub(r"(.{2,12})\1{3,}", r"\1\1", text)
    return text, looped


def clean_text(text: str):
    """1 セグメントの文字を整える。(整えた文字, 印 or "") を返す。捨てる行は文字が ""。"""
    text = re.sub(r"\[.*?\]|\(.*?\)|（.*?）|♪+", "", text)  # [音楽] (笑) 等の非発話タグ
    if any(h in text for h in HALLUCINATION_PATTERNS):
        return "", ""
    text = _FAKE_LABEL.sub("", text).strip()
    text = re.sub(r"(?:拍手[、。！!\s]*){2,}", "", text)  # 「拍手、拍手、拍手」は拍手の音の書き起こし
    text, looped = _collapse_repeats(text)
    text = text.strip()
    if re.fullmatch(r"[んうあおおーぁ、。．…\s]*", text):
        return "", ""  # 空・「んんんん」「うーん」等、環境音の幻聴・意味のない相槌だけの行
    return text, (FLAG if looped else "")


# 例: [00:00:03.240 --> 00:00:07.800]   こんにちは
_TS_LINE = re.compile(
    r"\[(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\]\s*(.*)"
)


def _parse_segments(stdout: str):
    """whisper-cli のタイムスタンプ付き出力を [(rel_t0, rel_t1, 生の文字), ...] に変換する。"""
    segs = []
    for ln in stdout.splitlines():
        m = _TS_LINE.match(ln.strip())
        if not m:
            continue
        g = [int(x) for x in m.groups()[:8]]
        t0 = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        t1 = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        segs.append((t0, t1, m.group(9)))
    return segs


def normalize_participants(value):
    """「山田、佐藤さん, 鈴木」や配列を ["山田", "佐藤さん", "鈴木"] にする（重複・空は除く）。
    読点・カンマが無いときだけ空白でも区切る（「山田 太郎、佐藤」の姓名の空白は区切らない）。"""
    if isinstance(value, (list, tuple)):
        value = "、".join(str(v) for v in value)
    value = str(value or "")
    sep = r"[、,，/／;；\n\t]+" if re.search(r"[、,，/／;；\n\t]", value) else r"\s+"
    out = []
    for name in re.split(sep, value):
        name = re.sub(r"[\r{}<>]", "", name).strip()[:20]
        if name and name not in out:
            out.append(name)
    return out[:20]


def build_prompt(context_tail: str = "", participants=None) -> str:
    """文字起こしのヒント。用語と参加者名と直前の発言だけ（人名・社名の辞書は入れない）。

    「用語: 」「直前の発言: 」のようなコロン付きの見出しは使わない — whisper が真似して
    「スタッフ: 」「田中: 」のような偽の話者名を出力していたため（10/5 の会議で 400 件超）。"""
    prompt = "日本語のビジネス会議の文字起こし。"
    if participants:
        prompt += "参加者は" + "、".join(participants[:12]) + "。"
    terms = dictionary.prompt_terms()[:40]
    if terms:
        prompt += "、".join(terms) + "。"
    if context_tail:
        prompt += context_tail
    return prompt


def context_tail(segments, t_next: float, max_chars: int = TAIL_CHARS) -> str:
    """次のチャンクに渡す直前の発言。要確認の行が来たら打ち切る（差し込みの連鎖を断つ）。"""
    tail = ""
    for seg in reversed(segments):
        if seg.get("flag") or t_next - seg["t1"] > TAIL_MAX_GAP:
            break
        tail = seg["text"] + tail
        if len(tail) >= max_chars:
            break
    return tail[-max_chars:]


def transcribe_segments(pcm: bytes, context_tail: str = "", timeout: int = 180,
                        participants=None, removed=None):
    """PCM チャンクを文字起こしし、チャンク内相対時刻付きセグメント
    [{"t0", "t1", "text", ("flag")}, ...] を返す。無音・失敗時は []（呼び出し側は続行）。
    removed にリストを渡すと、捨てたセグメントを理由（reason）付きで追記する。"""
    model = model_path()
    if model is None:
        raise RuntimeError("whisper モデルが models/ にありません")
    if rms_int16(pcm) < SILENCE_RMS or peak_rms(pcm) < SPEECH_PEAK_MIN:
        return []
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm)
        cmd = [
            WHISPER_BIN,
            "-m", model,
            "-l", "ja",
            "-t", "4",
            "-bs", "3",
            "--no-prints",
            "--prompt", build_prompt(context_tail, participants),
            "-f", path,
        ]
        env = dict(os.environ)
        if WHISPER_BIN == BUNDLED_BIN:
            env["GGML_BACKEND_PATH"] = os.path.dirname(BUNDLED_BIN)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode != 0:
            return []
        return screen_segments(_parse_segments(r.stdout or ""), pcm, removed)
    except subprocess.TimeoutExpired:
        return []
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def screen_segments(raw_segs, pcm: bytes = None, removed=None):
    """[(t0, t1, 生の文字)] を整え、無音区間から出た行を捨てる。pcm が無ければ音量は見ない。"""
    out = []
    for t0, t1, raw in raw_segs:
        text, flag = clean_text(raw)
        reason = "" if text else "幻聴・雑音"
        if text and pcm is not None and peak_rms(pcm, t0 - 0.3, t1 + 0.3) < SPEECH_PEAK_MIN:
            reason = "無音"
        if reason:
            if removed is not None and raw.strip():
                removed.append({"t0": t0, "t1": t1, "text": raw.strip(), "reason": reason})
            continue
        seg = {"t0": t0, "t1": t1, "text": text}
        if flag:
            seg["flag"] = flag
        out.append(seg)
    return out


def _norm(text: str) -> str:
    return re.sub(r"[\s、。，．,.!?！？…・「」『』（）()〜~]", "", text)


def _similar(a: str, b: str) -> bool:
    """ほぼ同じ文か。片方がもう片方に含まれ、増えた部分が数文字だけのとき（「こそ、」＋同じ文 等）も同じとみなす。
    「今日最も受け取ったもの」→「今日最も受け取ったものは、自分で議事録とか…」のように続きを話している行は別物。"""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 8 and short in long_ and len(long_) - len(short) <= max(4, len(short) // 3)


def filter_repeats(new_segs, history, removed=None, seen=()):
    """直近 REPEAT_WINDOW_SEC 秒に同じ文が既に REPEAT_LIMIT 回出ていたら捨て、前の出現に要確認を付ける。

    new_segs / history / seen は絶対時刻（会議の頭からの秒）の [{"t0","t1","text"}]。
    seen は既に捨てた行（数えるだけ）— ループが 90 秒より長く続いても、2 行ずつ残り続けないように。
    history の要素は書き換える（flag を付ける）。残すセグメントのリストを返す。"""
    kept, dropped = [], []
    for seg in new_segs:
        n = _norm(seg["text"])
        if len(n) >= REPEAT_MIN_CHARS:
            def near(pool):
                return [h for h in pool
                        if 0 <= seg["t0"] - h["t0"] <= REPEAT_WINDOW_SEC and _similar(n, _norm(h["text"]))]
            same = near(list(history) + kept)
            if len(same) + len(near(list(seen) + dropped)) >= REPEAT_LIMIT:
                for h in same:
                    h["flag"] = FLAG
                dropped.append(seg)
                if removed is not None:
                    removed.append(dict(seg, reason="繰り返し"))
                continue
        kept.append(seg)
    return kept


def clean_transcript(segments, removed=None):
    """保存済みの文字起こし（[{"t0","t1","text"}]）を後から掃除する（音声は見ない）。"""
    out = []
    if removed is None:
        removed = []
    for seg in segments:
        screened = screen_segments([(seg["t0"], seg["t1"], seg["text"])], None, removed)
        for s in screened:
            for k in ("speaker", "flag"):
                if seg.get(k):
                    s[k] = seg[k]
        out.extend(filter_repeats(screened, out[-80:], removed,
                                  seen=[r for r in removed[-80:] if r.get("reason") == "繰り返し"]))
    return out


def health() -> dict:
    return {
        "whisper_bin": WHISPER_BIN if os.path.exists(WHISPER_BIN) else None,
        "model": model_path(),
        "dict_terms": len(dictionary.terms()),
    }
