"""用語辞書の読み書き — UI から確認・編集できるようにするためのモジュール。

書式は voice-input/dictionary.yaml と同じ（PyYAML には依存しない簡易パーサ）:
    - { yomi: わんせく, text: "1sec.", note: 誤変換筆頭 }

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

_ENTRY = re.compile(r"^\s*-\s*\{(.*)\}\s*$")


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


def load():
    """[{yomi, text, note, comments:[...]}, ...] を返す。comments は直前の見出しコメント。"""
    p = path()
    entries, pending = [], []
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
                    entries.append({
                        "yomi": _field(inner, "yomi"),
                        "text": _field(inner, "text"),
                        "note": note,
                        "comments": pending,
                    })
                    pending = []
                elif s.strip().startswith("#") and "entries:" not in s:
                    pending.append(s.strip().lstrip("#").strip(" -"))
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
    """UI から来た [{yomi,text,note,comments}] を書き戻す。書き込んだパスを返す。"""
    p = path()
    lines = _header(p)
    if today:
        lines = [re.sub(r"^updated:\s*\S+", f"updated: {today}", ln) for ln in lines]
    for e in entries:
        text = _clean(e.get("text"))
        if not text:
            continue  # 正式表記が空の行は捨てる
        for c in e.get("comments") or []:
            c = _clean(c, 80)
            if c:
                lines.append(f"  # --- {c} ---")
        yomi = _clean(e.get("yomi"), 60)
        note = _clean(e.get("note"))
        parts = [f"yomi: {yomi}" if yomi else "yomi:", f'text: "{text}"']
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
    _CACHE = [e["text"] for e in load() if e.get("text")]
    return _CACHE


def terms():
    """文字起こし・議事録生成のプロンプトに渡す正式表記のリスト。"""
    return _CACHE if _CACHE is not None else reload()
