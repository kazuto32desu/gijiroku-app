"""用語辞書の読み書き — UI から確認・編集できるようにするためのモジュール。

書式は voice-input/dictionary.yaml と同じ（PyYAML には依存しない簡易パーサ）:
    - { yomi: わんせく, text: "1sec.", note: 誤変換筆頭 }
    - { yomi: じょん, text: "ジョン", kind: 人名 }

kind（種別）は 用語 / 人名 / 社名 の 3 つ。省略時は見出しコメントと敬称から自動判定する
（「# --- 人物 ---」の下や「〜さん」「〜先生」は人名、「# --- 取引先 ---」の下は社名）。
人名・社名は文字起こし（whisper）のヒントには入れない — 無音や聞き取れない箇所に
その名前が差し込まれるため（2026-10-05 の 4 時間会議で、辞書にある講師の名前が 170 回以上出た）。
議事録の表記合わせ（summarizer）には全種別を使う。

保存時は先頭のコメント・見出しコメント（# --- 会社・サービス --- 等）を保持する。
"""
import os
import re
import shutil
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
_LOCK = threading.Lock()
_CACHE = None

# 同フォルダの dictionary.yaml を使う（無ければ ../voice-input/ の共通辞書）
CANDIDATES = [
    os.path.join(BASE, "dictionary.yaml"),
    os.path.join(BASE, "..", "voice-input", "dictionary.yaml"),
]

KINDS = ("用語", "人名", "社名")
_ENTRY = re.compile(r"^\s*-\s*\{(.*)\}\s*$")
_HONORIFIC = re.compile(r"(さん|先生|様|さま|氏|くん|君|ちゃん|殿)$")


def path() -> str:
    for c in CANDIDATES:
        if os.path.exists(c):
            return os.path.realpath(c)
    return os.path.realpath(CANDIDATES[0])


def _unquote(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _field(inner: str, key: str) -> str:
    m = re.search(rf'{key}:\s*("[^"]*"|\'[^\']*\'|[^,}}]*)', inner)
    return _unquote(m.group(1)) if m else ""


def auto_kind(text: str, section: str = "") -> str:
    """kind を書いていない語の種別を推定する（見出しコメント → 敬称の順）。"""
    if re.search(r"人物|人名|メンバー|講師", section or ""):
        return "人名"
    if re.search(r"取引先|社名|社外|会場", section or ""):
        return "社名"
    if _HONORIFIC.search((text or "").strip()):
        return "人名"
    return "用語"


def load():
    """[{yomi, text, note, kind, kind_set, comments:[...]}, ...] を返す。

    comments は直前の見出しコメント。kind は明示値（kind_set=True）か自動判定の結果。"""
    p = path()
    entries, pending = [], []
    section = ""
    started = False   # entries: 行より前（ファイル冒頭の説明）は見出しに含めない
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                s = line.rstrip("\n")
                if not started:
                    started = s.strip() == "entries:"
                    continue
                m = _ENTRY.match(s)
                if m:
                    inner = m.group(1)
                    note_m = re.search(r"note:\s*(.*?)\s*$", inner)
                    note = _unquote(note_m.group(1)) if note_m else ""
                    text = _field(inner, "text")
                    # note は行末まで取るので、kind は note より前の部分から読む
                    kind = _field(inner.split("note:", 1)[0], "kind")
                    kind_set = kind in KINDS
                    entries.append({
                        "yomi": _field(inner, "yomi"),
                        "text": text,
                        "note": note,
                        "kind": kind if kind_set else auto_kind(text, section),
                        "kind_set": kind_set,
                        "comments": pending,
                    })
                    pending = []
                elif s.strip().startswith("#") and "entries:" not in s:
                    c = s.strip().lstrip("#").strip(" -")
                    pending.append(c)
                    section = c
    except OSError:
        return []
    return entries


def _header(p: str):
    """entries: 行までをそのまま返す（無ければ既定のヘッダを作る）。"""
    try:
        out = []
        with open(p, encoding="utf-8") as f:
            for line in f:
                out.append(line.rstrip("\n"))
                if line.strip() == "entries:":
                    return out
    except OSError:
        pass
    return [
        "# 音声入力ユーザー辞書 — 読み → 正式表記",
        "# 議事録レコーダーの「用語辞書」画面から編集できます。",
        "",
        "version: 1",
        "entries:",
    ]


def _clean(v: str, limit: int = 200) -> str:
    return re.sub(r"[\r\n{}]", " ", (v or "")).strip()[:limit]


def save(entries, today: str = "") -> str:
    """UI から来た [{yomi,text,note,kind,comments}] を書き戻す。書き込んだパスを返す。"""
    p = path()
    lines = _header(p)
    if today:
        lines = [re.sub(r"^updated:\s*\S+", f"updated: {today}", ln) for ln in lines]
    section = ""
    for e in entries:
        text = _clean(e.get("text"))
        if not text:
            continue  # 正式表記が空の行は捨てる
        for c in e.get("comments") or []:
            c = _clean(c, 80)
            if c:
                lines.append(f"  # --- {c} ---")
                section = c
        yomi = _clean(e.get("yomi"), 60)
        note = _clean(e.get("note"))
        kind = e.get("kind") if e.get("kind") in KINDS else ""
        parts = [f"yomi: {yomi}" if yomi else "yomi:", f'text: "{text}"']
        # 種別は「自動判定と違うとき」か「明示されていたとき」だけ書く（共通辞書の差分を最小に）
        if kind and (e.get("kind_set") or kind != auto_kind(text, section)):
            parts.append(f"kind: {kind}")
        if note:
            parts.append(f"note: {note}")
        lines.append("  - { " + ", ".join(parts) + " }")
    body = "\n".join(lines) + "\n"
    with _LOCK:
        if os.path.exists(p):
            shutil.copy2(p, p + ".bak")          # 直前の版を必ず残す
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, p)                        # 書き込み中に壊れないよう入れ替えで保存
    reload()
    return p


def reload():
    global _CACHE
    _CACHE = [{"text": e["text"], "kind": e["kind"]} for e in load() if e.get("text")]
    return _CACHE


def _cached():
    return _CACHE if _CACHE is not None else reload()


def terms(kinds=None):
    """正式表記のリスト（議事録生成の表記合わせ用）。kinds を渡すとその種別だけ返す。"""
    return [e["text"] for e in _cached() if kinds is None or e["kind"] in kinds]


def prompt_terms():
    """文字起こし（whisper）のヒントに入れてよい語 = 用語だけ。人名・社名は入れない。"""
    return terms(("用語",))
